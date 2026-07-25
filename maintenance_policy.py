"""
maintenance_policy.py — risk tiers + the shell-command allowlist for the
homelab maintenance controller.

Two hard rules:

1. Every maintenance action has a fixed tier — AUTO, APPROVAL, or MANUAL —
   declared here, not decided at runtime by a model. AUTO actions run inside a
   runbook only when its exact preconditions hold; APPROVAL actions go through
   Loki's existing draft-approval gate; MANUAL actions are never executed by
   Loki at all.

2. No caller ever passes a raw command. Commands are built from the fixed
   templates below; parameter values must BOTH match the parameter's shape
   validator AND be values declared in config/homelab_assets.yml
   (configure()). Anything else raises PolicyError before a process is ever
   spawned.
"""

import re

AUTO, APPROVAL, MANUAL = "auto", "approval", "manual"


class PolicyError(Exception):
    pass


# ── Action tiers ───────────────────────────────────────────────────────────
ACTION_TIERS = {
    # Automatic — safe, verified, reversible, exact-runbook-match only.
    "rerun_health_checks":        AUTO,
    "inspect_logs":               AUTO,
    "inspect_service_state":      AUTO,
    "restart_stateless_service":  AUTO,   # crashed/stopped + exact runbook match
    "restore_blackboxx_ip_rule":  AUTO,   # the predeclared policy rule only
    "retry_joplin_sync":          AUTO,
    "restart_interface_worker":   AUTO,   # a failed Loki interface worker
    "restart_unhealthy_container_once": AUTO,

    # Approval required — staged as drafts, never run inline.
    "container_image_update":     APPROVAL,
    "compose_change":             APPROVAL,
    "immich_update":              APPROVAL,
    "home_assistant_change":      APPROVAL,
    "proxy_change":               APPROVAL,
    "firewall_change":            APPROVAL,
    "service_enable_disable":     APPROVAL,
    "package_install":            APPROVAL,
    "host_reboot":                APPROVAL,

    # Manual only — Loki must refuse and hand these to the Boss.
    "database_repair":            MANUAL,
    "filesystem_repair":          MANUAL,
    "credential_change":          MANUAL,
    "delete_volume_or_user_data": MANUAL,
    "destructive_prune":          MANUAL,
    "network_redesign":           MANUAL,
}


def action_tier(action: str) -> str:
    tier = ACTION_TIERS.get(action)
    if tier is None:
        raise PolicyError(f"unknown maintenance action '{action}'")
    return tier


# ── Command allowlist ──────────────────────────────────────────────────────
# Template placeholders like "{iface}" name a parameter CLASS; the value must
# match the class's shape regex and be present in the registry-derived value
# set installed via configure().
_PARAM_SHAPES = {
    "iface":        re.compile(r"^[A-Za-z0-9_.-]{1,15}$"),
    "container":    re.compile(r"^[A-Za-z0-9_.-]{1,64}$"),
    "compose_file": re.compile(r"^/[A-Za-z0-9 _./-]{1,200}$"),
    "path":         re.compile(r"^/[A-Za-z0-9 _./-]{1,200}$"),
    "image":        re.compile(r"^[A-Za-z0-9_./:-]{1,200}$"),
    "num":          re.compile(r"^\d{1,5}$"),
    "probe_ip":     re.compile(r"^\d{1,3}(\.\d{1,3}){3}$"),
}

_COMMANDS: dict[str, dict] = {
    # ── read-only diagnostics ──────────────────────────────────────────
    "ip_addr":          {"argv": ["ip", "-br", "addr", "show", "{iface}"]},
    "ip_rule_show":     {"argv": ["ip", "rule", "show"]},
    "ip_route_table":   {"argv": ["ip", "route", "show", "table", "{num}"]},
    "ip_route_get_marked":
        {"argv": ["ip", "route", "get", "{probe_ip}", "mark", "{num}"]},
    "ip_forward":       {"argv": ["cat", "/proc/sys/net/ipv4/ip_forward"]},
    "pgrep_hostapd":    {"argv": ["pgrep", "-a", "hostapd"]},
    "pgrep_dnsmasq":    {"argv": ["pgrep", "-a", "dnsmasq"]},
    "iptables_mangle":  {"argv": ["sudo", "-n", "iptables", "-t", "mangle",
                                  "-S", "PREROUTING"]},
    "iptables_nat":     {"argv": ["sudo", "-n", "iptables", "-t", "nat",
                                  "-S", "POSTROUTING"]},
    "wg_handshake":     {"argv": ["sudo", "-n", "wg", "show", "{iface}",
                                  "latest-handshakes"]},
    "wg_transfer":      {"argv": ["sudo", "-n", "wg", "show", "{iface}",
                                  "transfer"]},
    "ping_iface":       {"argv": ["ping", "-c", "1", "-W", "3",
                                  "-I", "{iface}", "{probe_ip}"]},
    "docker_inspect":   {"argv": ["docker", "inspect", "{container}"]},
    "docker_image_inspect": {"argv": ["docker", "image", "inspect", "{image}"]},
    "docker_imagetools_inspect":   # remote index digest, read-only
        {"argv": ["docker", "buildx", "imagetools", "inspect", "{image}"]},
    "docker_logs_tail": {"argv": ["docker", "logs", "--tail", "80",
                                  "{container}"]},
    "compose_ps":       {"argv": ["docker", "compose", "-f", "{compose_file}",
                                  "ps", "--format", "json"]},
    "df_path":          {"argv": ["df", "-h", "--output=target,size,avail,pcent",
                                  "{path}"]},

    # ── repairs (still template-locked; tier enforced by the runbook) ──
    "ip_rule_add_fwmark":
        {"argv": ["sudo", "-n", "ip", "rule", "add", "fwmark", "{num}",
                  "table", "{num2}", "priority", "{num3}"], "repair": True},
    "ip_rule_del_fwmark":   # rollback for ip_rule_add_fwmark
        {"argv": ["sudo", "-n", "ip", "rule", "del", "fwmark", "{num}",
                  "table", "{num2}", "priority", "{num3}"], "repair": True},
    "docker_start":     {"argv": ["docker", "start", "{container}"],
                         "repair": True},
    "docker_restart":   {"argv": ["docker", "restart", "{container}"],
                         "repair": True},
}

_PLACEHOLDER_RE = re.compile(r"^\{([a-z_]+?)(\d*)\}$")

# Registry-derived value sets ({class -> allowed values}); installed at load.
_allowed_values: dict[str, set] | None = None


def configure(allowed_values: dict[str, set]):
    global _allowed_values
    _allowed_values = {k: set(v) for k, v in allowed_values.items()}


def add_runtime_values(cls: str, values: set):
    """Extend one value class with values read from the LIVE system via an
    already-allowlisted command (e.g. the image ref reported by
    `docker inspect <registry-declared container>`). Never fed from model or
    user input."""
    if _allowed_values is None:
        raise PolicyError("policy not configured with registry values")
    if cls not in _PARAM_SHAPES:
        raise PolicyError(f"unknown parameter class '{cls}'")
    ok = {str(v) for v in values if _PARAM_SHAPES[cls].match(str(v))}
    _allowed_values.setdefault(cls, set()).update(ok)


def command_names() -> list[str]:
    return sorted(_COMMANDS)


def is_repair_command(name: str) -> bool:
    return bool(_COMMANDS.get(name, {}).get("repair"))


def build_command(name: str, **params) -> list[str]:
    """Return a fully validated argv for a named command. Raises PolicyError
    on any unknown command, unknown/missing parameter, shape mismatch, or a
    value not declared in the asset registry."""
    spec = _COMMANDS.get(name)
    if spec is None:
        raise PolicyError(f"command '{name}' is not allowlisted")
    if _allowed_values is None:
        raise PolicyError("policy not configured with registry values")
    argv, used = [], set()
    for part in spec["argv"]:
        m = _PLACEHOLDER_RE.match(part)
        if not m:
            argv.append(part)
            continue
        cls, key = m.group(1), m.group(1) + m.group(2)
        if key not in params:
            raise PolicyError(f"command '{name}' missing parameter '{key}'")
        value = str(params[key])
        shape = _PARAM_SHAPES.get(cls)
        if shape is None or not shape.match(value):
            raise PolicyError(f"parameter '{key}'={value!r} has invalid shape")
        if value not in _allowed_values.get(cls, set()):
            raise PolicyError(
                f"parameter '{key}'={value!r} is not declared in the asset registry")
        argv.append(value)
        used.add(key)
    extra = set(params) - used
    if extra:
        raise PolicyError(f"unexpected parameters for '{name}': {sorted(extra)}")
    return argv

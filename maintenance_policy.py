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
    # Lifecycle bookkeeping: changes what Loki WATCHES, never what it runs.
    # Marking an asset decommissioned/ignored removes alerting; it deletes
    # nothing, so it needs no approval gate of its own.
    "mark_asset_lifecycle_state":  AUTO,
    "suppress_monitoring":         AUTO,

    # Approval required — staged as drafts, never run inline.
    # Removing the exact container/project/unshared images and volumes of an
    # asset the Boss already decommissioned. Shared resources are excluded at
    # planning time; this tier covers only the narrow, enumerated removal.
    "decommission_cleanup":       APPROVAL,
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
    "unit":         re.compile(r"^[A-Za-z0-9@_.-]{1,64}$"),
    # Postgres role / database identifier, read from the asset's compose env
    # file at runtime (never model or user input) and registered via
    # add_runtime_values("dbident", …).
    "dbident":      re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"),
    # A single compose service name from the asset's own registry entry.
    "service":      re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"),
    # A Docker named volume, authorized at runtime from `docker volume`/`docker
    # ps` output and only ever for a volume proven to have no other user.
    "volume":       re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"),
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
    # Machine-readable free bytes, for the pre-update disk-space gate.
    "df_path_bytes":    {"argv": ["df", "-B1", "--output=avail", "{path}"]},
    "systemctl_is_active": {"argv": ["systemctl", "is-active", "{unit}"]},
    # Boot-time ownership: is a unit wired to start on its own at boot?
    # Exits non-zero for "disabled"/"static", so callers read the WORD, not rc.
    "systemctl_is_enabled": {"argv": ["systemctl", "is-enabled", "{unit}"]},

    # ── update inventory / pre-flight (all read-only) ──────────────────
    # Resolved image list for a compose project, so the inventory reflects
    # what compose would actually run rather than what happens to be up.
    "compose_config_images":
        {"argv": ["docker", "compose", "-f", "{compose_file}", "config", "--images"]},
    "compose_config_validate":
        {"argv": ["docker", "compose", "-f", "{compose_file}", "config", "--quiet"]},
    # Is another agent (watchtower/diun) already managing updates?
    "docker_ps_names":
        {"argv": ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"]},
    # -a includes stopped containers, needed for the general "any container
    # unhealthy or stopped" sweep. No parameters at all — zero injection surface.
    "docker_ps_status":
        {"argv": ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Status}}"]},
    # ── lifecycle / decommission discovery (all read-only, all param-free) ──
    # Every one of these dumps the WHOLE container list in a single call, so
    # discovery never needs a filter built from a name — zero injection
    # surface, and the same snapshot is reused for sharing analysis.
    "docker_ps_projects":
        {"argv": ["docker", "ps", "-a", "--format",
                  '{{.Names}}\t{{.Label "com.docker.compose.project"}}'
                  '\t{{.Image}}\t{{.Status}}']},
    "docker_ps_mounts":
        {"argv": ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Mounts}}"]},
    "docker_ps_networks":
        {"argv": ["docker", "ps", "-a", "--format", "{{.Names}}\t{{.Networks}}"]},
    # Which generated proxy-host configs mention a container. `--` stops the
    # pattern being read as an option; both parameters are allowlisted
    # container names, so no shell and no metacharacters are possible.
    "proxy_conf_grep":
        {"argv": ["docker", "exec", "{container}", "grep", "-rl", "--",
                  "{container2}", "/data/nginx/proxy_host"]},

    # ── decommission cleanup (approval-gated by the cleanup workflow) ──────
    # Deliberately narrow. There is NO prune command, NO `down -v`, NO
    # `--rmi`, NO `--remove-orphans`, and no network removal at all: compose
    # drops its own project network, and a shared network is never touched.
    "compose_down_project":
        {"argv": ["docker", "compose", "-f", "{compose_file}", "down"],
         "repair": True},
    "docker_container_rm":
        {"argv": ["docker", "rm", "{container}"], "repair": True},
    "docker_image_rm":
        {"argv": ["docker", "image", "rm", "{image}"], "repair": True},
    # Only ever called for a volume proven unshared at both plan and run time.
    "docker_volume_rm":
        {"argv": ["docker", "volume", "rm", "{volume}"], "repair": True},

    "df_root":
        {"argv": ["df", "-h", "--output=size,avail,pcent", "/"]},
    "mem_info":
        {"argv": ["cat", "/proc/meminfo"]},
    "pg_isready":
        {"argv": ["docker", "exec", "{container}", "pg_isready",
                  "-U", "{dbident}", "-d", "{dbident2}"]},
    # Datachecksum-failure count: the same signal the compose healthcheck uses.
    "pg_checksum_failures":
        {"argv": ["docker", "exec", "{container}", "psql", "-U", "{dbident}",
                  "-d", "{dbident2}", "--tuples-only", "--no-align",
                  "--command",
                  "SELECT COALESCE(SUM(checksum_failures), 0) FROM pg_stat_database"]},
    "pg_database_size":
        {"argv": ["docker", "exec", "{container}", "psql", "-U", "{dbident}",
                  "-d", "{dbident2}", "--tuples-only", "--no-align",
                  "--command", "SELECT pg_database_size(current_database())"]},

    # ── update actions (approval-gated by the runbook's tier) ──────────
    # Pull an EXACT pinned image ref. Moving tags are resolved to a concrete
    # version before this runs, so what gets pulled is what was approved.
    "docker_pull_image":
        {"argv": ["docker", "pull", "{image}"], "repair": True},
    # Recreate one compose project only — never `-p` across projects.
    "compose_up_project":
        {"argv": ["docker", "compose", "-f", "{compose_file}", "up", "-d",
                  "--no-deps", "{service}"], "repair": True},
    "compose_up_all":
        {"argv": ["docker", "compose", "-f", "{compose_file}", "up", "-d"],
         "repair": True},
    # Verified Postgres dump. stdout is streamed straight to a file by the Ops
    # facade (no shell, no redirection in the command itself).
    "pg_dump_custom":
        {"argv": ["docker", "exec", "{container}", "pg_dump", "-U", "{dbident}",
                  "-d", "{dbident2}", "--format=custom", "--compress=6"],
         "repair": True},
    # Verification of the dump above: pg_restore parses the archive TOC from
    # stdin and fails on a truncated or corrupt file.
    "pg_restore_list":
        {"argv": ["docker", "exec", "-i", "{container}", "pg_restore", "--list"],
         "repair": True},

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
    # Only units declared in the registry (the Joplin sync sidecar). Never
    # loki.service itself — Loki restarting Loki from a runbook is a footgun;
    # systemd's Restart=always owns that.
    "systemctl_restart_unit":
        {"argv": ["sudo", "-n", "systemctl", "restart", "{unit}"],
         "repair": True},
    # Boot-time ownership only (service_enable_disable tier, approval-gated).
    # `disable` changes what starts at the NEXT boot and does not touch the
    # running system — deliberately. There is NO systemctl stop/down command
    # here and there must never be one: stopping a wg-quick@ unit runs
    # `wg-quick down`, which deletes the very interface the AP unit is
    # currently serving traffic on. Removing a boot-time race must not cause
    # the outage it exists to prevent.
    "systemctl_disable_unit":
        {"argv": ["sudo", "-n", "systemctl", "disable", "{unit}"],
         "repair": True},
    "systemctl_enable_unit":   # rollback for systemctl_disable_unit
        {"argv": ["sudo", "-n", "systemctl", "enable", "{unit}"],
         "repair": True},
}

# Commands that only READ, even though some are spawned during an update.
# Kept explicit so `repair: True` never becomes a proxy for "dangerous", and
# so a dry-run inventory can prove it touched nothing.
READ_ONLY_DURING_UPDATE = frozenset({
    "compose_config_images", "compose_config_validate", "docker_ps_names",
    "pg_isready", "pg_checksum_failures", "pg_database_size", "df_path_bytes",
})

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

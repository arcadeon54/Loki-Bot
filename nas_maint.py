"""
nas_maint.py — Loki's client for the restricted UGREEN NAS dispatcher.

The NAS is NOT operable by a general shell. Everything here goes through one
root-owned dispatcher on the NAS (/usr/local/sbin/loki-nas-maint) that accepts
exactly six literal read-only actions, reached over a dedicated SSH key via the
`nas-maint` alias. This module can therefore only ever read: there is no verb
in the dispatcher that restarts, stops, pulls or recreates anything, and the
sudoers rule on the NAS enumerates the six actions literally rather than using
a wildcard.

Security notes that belong with the code, not just the docs:
  - UGOS sets a GLOBAL `ForceCommand /etc/ssh/force_command.sh` in sshd_config,
    which OVERRIDES any per-key command="" restriction. Containment therefore
    rests on the sudoers allowlist, not on the SSH key options. The key options
    (no-pty / no forwarding) are still applied and still worth having.
  - Docker group membership and wildcard `docker` sudo are prohibited and must
    stay that way; they are root-equivalent on the NAS.

Facts about Tracearr come from the asset registry (config/homelab_assets.yml),
which was populated from live discovery. Nothing here hardcodes a container
name that the registry does not already declare.
"""

import asyncio
import json
import logging

import homelab_assets
from tools import ToolContext, ToolSpec, register, user_level

log = logging.getLogger("NasMaint")

SSH_ALIAS = "nas-maint"
DISPATCHER = "/usr/local/sbin/loki-nas-maint"

# Mirrors the dispatcher's own table. A caller can never supply an action that
# is not one of these literals, and the NAS sudoers rule enumerates the same six.
ACTIONS = (
    "host_status",
    "container_inventory",
    "tracearr_status",
    "tracearr_dependencies",
    "tracearr_recent_logs",
    "tracearr_update_check",
)

DEFAULT_TIMEOUT = 45

# Restart count above which Tracearr's churn is called out as abnormal. It is a
# reporting threshold only — nothing repairs anything automatically.
RESTART_ALERT_THRESHOLD = 10


class NasError(Exception):
    """A precise failure. Never a prompt for the Boss to run docker by hand."""


async def run_action(action: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """Invoke one allowlisted dispatcher action. Fixed argv, never a shell."""
    if action not in ACTIONS:
        raise NasError(f"action {action!r} is not one of {list(ACTIONS)}")
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            SSH_ALIAS, "sudo", "-n", DISPATCHER, action]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        raise NasError(f"NAS dispatcher timed out after {timeout}s running "
                       f"'{action}' — the NAS or its SSH service is not "
                       f"responding.")
    except OSError as e:
        raise NasError(f"could not launch ssh to the NAS: {e.__class__.__name__}")
    text = (out or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0 and not text:
        detail = (err or b"").decode("utf-8", "replace").strip()[:300]
        raise NasError(_classify_ssh_failure(detail, action))
    try:
        payload = json.loads(text)
    except ValueError:
        raise NasError(f"NAS dispatcher returned output that is not JSON while "
                       f"running '{action}'. First 200 chars: {text[:200]!r}")
    if not payload.get("ok"):
        raise NasError(f"NAS dispatcher refused '{action}': "
                       f"{payload.get('error', 'no reason given')}")
    return payload.get("data") or {}


def _classify_ssh_failure(detail: str, action: str) -> str:
    """Name the exact broken component. Never suggest manual docker steps."""
    low = detail.lower()
    if "host key verification failed" in low or "remote host identification" in low:
        return (f"the NAS host key does not match the pinned entry in "
                f"known_hosts, so the connection was refused. Diagnostics are "
                f"unavailable until that is resolved — I will not disable host "
                f"key checking. (action: {action})")
    if "permission denied" in low:
        return (f"the NAS refused Loki's maintenance key (publickey). The key "
                f"is no longer authorized for unimatrix_001 — UGOS updates can "
                f"reset authorized_keys. (action: {action})")
    if "a password is required" in low or "sudo:" in low:
        return (f"the NAS sudoers rule for {DISPATCHER} is missing, so the "
                f"dispatcher cannot run. UGOS updates can clear /etc/sudoers.d. "
                f"(action: {action})")
    if "no such file" in low or "command not found" in low:
        return (f"the dispatcher {DISPATCHER} is not installed on the NAS. "
                f"(action: {action})")
    if "connection refused" in low or "timed out" in low or "no route" in low:
        return (f"the NAS is not reachable over SSH right now. (action: {action})")
    return f"NAS dispatcher failed running '{action}': {detail[:200]}"


# ── asset helpers ──────────────────────────────────────────────────────────
def _reg():
    return homelab_assets.load()


def _tracearr_asset() -> dict:
    asset = _reg().get("tracearr")
    if asset is None:
        raise NasError("Tracearr is not in the asset registry")
    return asset


def _dep_spec(asset: dict) -> dict:
    return asset.get("dependencies") or {}


def _summarize(container: dict | None) -> dict:
    """The fields worth reporting from a dispatcher container record."""
    if not container:
        return {"present": False}
    return {"present": True,
            "container": container.get("name"),
            "state": container.get("state"),
            "health": container.get("health"),
            "restart_count": container.get("restart_count"),
            "started_at": container.get("started_at"),
            "oom_killed": container.get("oom_killed"),
            "exit_code": container.get("exit_code"),
            "image": container.get("image")}


# ── tools ──────────────────────────────────────────────────────────────────
def _boss_only(ctx: ToolContext) -> str:
    return "" if user_level(ctx.user_id) == "boss" else "NAS maintenance is Boss-only"


def _p(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required}


async def _tool_nas_status(args: dict, ctx: ToolContext) -> str:
    err = _boss_only(ctx)
    if err:
        return json.dumps({"ok": False, "error": err})
    try:
        host = await run_action("host_status")
        inv = await run_action("container_inventory")
    except NasError as e:
        return json.dumps({"ok": False, "error": str(e)})
    return json.dumps({"ok": True, "host": host,
                       "containers_running": host.get("containers_running"),
                       "containers_total": host.get("containers_total"),
                       "containers": inv.get("containers", [])[:40]})


async def _tool_tracearr_status(args: dict, ctx: ToolContext) -> str:
    err = _boss_only(ctx)
    if err:
        return json.dumps({"ok": False, "error": err})
    try:
        data = await run_action("tracearr_status")
    except NasError as e:
        return json.dumps({"ok": False, "error": str(e)})
    app = data.get("tracearr") or {}
    out = {"ok": True, "running": app.get("running"),
           "compose_project": data.get("compose_project"),
           "tracearr": _summarize(app),
           "resource_usage": data.get("resource_usage")}
    rc = app.get("restart_count") or 0
    if rc > RESTART_ALERT_THRESHOLD:
        out["anomaly"] = (f"Tracearr has restarted {rc} times. It is serving "
                          f"traffic, but that churn is abnormal and is tracked "
                          f"as a separate open finding — cause not yet proven, "
                          f"so there is no automatic repair.")
    return json.dumps(out)


async def _tool_tracearr_diagnose(args: dict, ctx: ToolContext) -> str:
    """Tracearr plus its REGISTERED dependencies. A Tracearr incident always
    includes the Redis and PostgreSQL checks — never the UGOS host redis."""
    err = _boss_only(ctx)
    if err:
        return json.dumps({"ok": False, "error": err})
    try:
        deps = await run_action("tracearr_dependencies")
    except NasError as e:
        return json.dumps({"ok": False, "error": str(e)})
    spec = _dep_spec(_tracearr_asset())
    found = deps.get("dependencies") or {}
    app = found.get("tracearr") or {}
    report = {"ok": True,
              "compose_project": deps.get("compose_project"),
              "tracearr": _summarize(app),
              "dependencies": {},
              "shared_networks": deps.get("shared_networks") or {}}
    unhealthy = []
    for role in ("redis", "postgres"):
        declared = (spec.get(role) or {}).get("container")
        observed = found.get(role) or {}
        entry = _summarize(observed)
        entry["registered_container"] = declared
        # Guard against diagnosing the wrong service: the UGOS host runs its
        # own redis on 127.0.0.1:6379 that has nothing to do with Tracearr.
        entry["matches_registry"] = bool(
            declared and observed.get("name") == declared)
        report["dependencies"][role] = entry
        if observed and observed.get("health") not in (None, "healthy"):
            unhealthy.append(f"{role} ({observed.get('health')})")
        elif not observed:
            unhealthy.append(f"{role} (not found)")
    report["dependencies_healthy"] = not unhealthy
    report["verdict"] = ("Tracearr and its registered Redis and PostgreSQL "
                         "dependencies are healthy."
                         if not unhealthy else
                         "Dependency problem: " + ", ".join(unhealthy))
    rc = app.get("restart_count") or 0
    if rc > RESTART_ALERT_THRESHOLD:
        report["open_finding"] = (
            f"Tracearr restart_count={rc} with Redis and PostgreSQL both stable. "
            f"The churn is app-side, not a dependency outage. Cause unproven — "
            f"no automatic repair is configured.")
    return json.dumps(report)


async def _tool_tracearr_update_check(args: dict, ctx: ToolContext) -> str:
    err = _boss_only(ctx)
    if err:
        return json.dumps({"ok": False, "error": err})
    try:
        data = await run_action("tracearr_update_check")
    except NasError as e:
        return json.dumps({"ok": False, "error": str(e)})
    asset = _tracearr_asset()
    running = (data.get("images") or {}).get("tracearr") or {}
    pinned = asset.get("image_digest") or ""
    out = {"ok": True, "compose_project": data.get("compose_project"),
           "images": data.get("images"),
           "registered_version": asset.get("version"),
           "registered_digest": pinned[:26],
           "digest_matches_registry": bool(pinned and pinned in
                                           (running.get("image") or "")),
           "update_policy": asset.get("update_policy"),
           "note": data.get("note")}
    out["next_step"] = (
        "Updates on the NAS are performed by Watchtower there, not by Loki. "
        "Loki has no update or restart verb for this asset: the dispatcher "
        "exposes read-only actions only. Any change needs an approved plan.")
    return json.dumps(out)


def _register_tools():
    register(ToolSpec(
        name="nas_status",
        description=(
            "Live read-only status of the UGREEN NAS (Unimatrix0001, the "
            "registered 'nas' host) and its running containers, via the "
            "restricted on-NAS dispatcher. Use for 'how is the NAS', 'what is "
            "running on the NAS'. Read-only: it cannot restart or change "
            "anything."),
        parameters=_p({}, []),
        handler=_tool_nas_status, permission="boss", timeout=60,
    ))
    register(ToolSpec(
        name="tracearr_status",
        description=(
            "Is Tracearr running? Live read-only state of the Tracearr "
            "container on the UGREEN NAS — run state, health, restart count, "
            "CPU/memory. FIRST CHOICE for 'is Tracearr up/running/ok'. Never "
            "answer this from memory and never ask the Boss to run docker."),
        parameters=_p({}, []),
        handler=_tool_tracearr_status, permission="boss", timeout=60,
    ))
    register(ToolSpec(
        name="tracearr_diagnose",
        description=(
            "Diagnose Tracearr INCLUDING its registered Redis and PostgreSQL "
            "dependencies on the UGREEN NAS. FIRST CHOICE for 'diagnose "
            "Tracearr', 'Tracearr is down', 'Tracearr Redis is down', "
            "'Tracearr database problem'. Read-only; it proposes nothing and "
            "changes nothing."),
        parameters=_p({}, []),
        handler=_tool_tracearr_diagnose, permission="boss", timeout=75,
    ))
    register(ToolSpec(
        name="tracearr_update_check",
        description=(
            "Is Tracearr up to date? Reports the running image, pinned digest "
            "and registered version for Tracearr and its dependencies. "
            "Read-only — it never pulls or updates; Watchtower on the NAS "
            "performs actual updates and any change needs approval."),
        parameters=_p({}, []),
        handler=_tool_tracearr_update_check, permission="boss", timeout=60,
    ))


enabled = False
try:
    _reg()
    enabled = True
except Exception as e:  # pragma: no cover - registry failure is fatal upstream
    log.warning("NAS maintenance disabled — asset registry failed: %s", e)

if enabled:
    _register_tools()
    log.info("NAS maintenance online — dispatcher %s via ssh %s, %d actions",
             DISPATCHER, SSH_ALIAS, len(ACTIONS))

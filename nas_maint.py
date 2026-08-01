"""
nas_maint.py — Loki's client for the restricted UGREEN NAS dispatcher.

The NAS is NOT operable by a general shell. Everything here goes through one
root-owned dispatcher on the NAS (/usr/local/sbin/loki-nas-maint) that accepts
a fixed set of literal read-only actions, reached over a dedicated SSH key via the
`nas-maint` alias. This module can therefore only ever read: there is no verb
in the dispatcher that restarts, stops, pulls or recreates anything, and the
sudoers rule on the NAS enumerates those actions literally rather than using
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
import concurrent.futures
import datetime
import json
import os
import re
import shutil
import subprocess
import logging

import homelab_assets
from tools import ToolContext, ToolSpec, register, user_level

log = logging.getLogger("NasMaint")

SSH_ALIAS = "nas-maint"
DISPATCHER = "/usr/local/sbin/loki-nas-maint"

# Plex's local HTTP API is reached directly from dex247, not through the
# restricted dispatcher — it needs no privileged access, only LAN reachability,
# same shape as the existing Jellyfin integration (tools.py). The token is
# read once here and never logged, returned, or included in an exception.
PLEX_URL = os.getenv("PLEX_URL", "")
PLEX_TOKEN = os.getenv("PLEX_TOKEN", "")

# NAS-side iperf3 client run locally on dex247. Path resolved once; if absent
# the tool degrades to "inconclusive" rather than failing loudly.
IPERF3_LOCAL = shutil.which("iperf3")
IPERF3_TEST_SECONDS = 8
IPERF3_STARTUP_DELAY = 1.2  # let the NAS-side server bind before connecting

# Restart-repair boundary: Plex may only ever be STARTED (never
# restarted/stopped/killed by Loki) via the approval-gated dispatcher action.
RESTART_ALERT_THRESHOLD_PLEX = 10

# Mirrors the dispatcher's own table. A caller can never supply an action that
# is not one of these literals, and the NAS sudoers rule enumerates the same set.
ACTIONS = (
    "host_status",
    "container_inventory",
    "tracearr_status",
    "tracearr_dependencies",
    "tracearr_recent_logs",
    "tracearr_update_check",
    "tracearr_restart_forensics",
    "tracearr_exit_window_logs",
    "network_status",
    "network_tooling_check",
    "network_speed_test",
    "disk_status",
    "plex_status",
    "plex_dependencies",
    "plex_recent_logs",
    "plex_transcode_processes",
)

# Approval-gated, STATE-CHANGING dispatcher actions. Never invoked directly by
# the model: they run only from the approved-update handler below, after Loki's
# existing draft/approval gate has been satisfied.
WRITE_ACTIONS = {
    "tracearr_update_prepare": "digest",
    "tracearr_backup": None,
    "tracearr_apply_update": "prepare_id",
    "tracearr_verify_update": None,
    "tracearr_rollback": None,
    "plex_restart": None,
}
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_PREPARE_ID_RE = re.compile(r"^[0-9a-f]{16}$")

DEFAULT_TIMEOUT = 45
# Upper bound on building an approval plan (dispatcher call + upstream check).
PREPARE_TIMEOUT = 150

# Restart count above which Tracearr's churn is called out as abnormal. It is a
# reporting threshold only — nothing repairs anything automatically.
RESTART_ALERT_THRESHOLD = 10


class NasError(Exception):
    """A precise failure. Never a prompt for the Boss to run docker by hand."""


async def run_action(action: str, timeout: int = DEFAULT_TIMEOUT,
                     param: str | None = None) -> dict:
    """Invoke one allowlisted dispatcher action. Fixed argv, never a shell."""
    if action in ACTIONS:
        if param is not None:
            raise NasError(f"action {action!r} takes no parameter")
        extra = []
    elif action in WRITE_ACTIONS:
        kind = WRITE_ACTIONS[action]
        if kind is None:
            if param is not None:
                raise NasError(f"action {action!r} takes no parameter")
            extra = []
        else:
            pattern = _DIGEST_RE if kind == "digest" else _PREPARE_ID_RE
            if not param or not pattern.match(param):
                raise NasError(f"action {action!r} needs a well-formed {kind}")
            extra = [param]
    else:
        raise NasError(f"action {action!r} is not one of "
                       f"{list(ACTIONS) + list(WRITE_ACTIONS)}")
    argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            SSH_ALIAS, "sudo", "-n", DISPATCHER, action, *extra]
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
        # Healthy dependencies must never be allowed to imply a healthy app.
        report["open_finding"] = (
            f"Tracearr restart_count={rc} with Redis and PostgreSQL both stable. "
            f"The churn is app-side, not a dependency outage. Cause unproven — "
            f"no automatic repair is configured.")
        report["app_healthy"] = False
        report["verdict"] += (f" Tracearr itself is NOT stable, though: it has "
                              f"restarted {rc} times.")
        report["restart_evidence"] = await _restart_evidence()
    return json.dumps(report)


def _restart_gap_seconds(state: dict):
    """Seconds between the process exiting and being started again.

    A sub-10s gap means the restart policy reacted immediately; a scheduler or
    a human issuing `docker restart` produces a very different shape."""
    import datetime
    fin, sta = state.get("finished_at"), state.get("started_at")
    if not fin or not sta:
        return None
    try:
        f = datetime.datetime.fromisoformat(fin.replace("Z", "+00:00"))
        s_ = datetime.datetime.fromisoformat(sta.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = (s_ - f).total_seconds()
    return delta if delta >= 0 else None


def classify_restarts(forensics: dict) -> dict:
    """Classify restart churn from dispatcher forensics.

    RestartCount only increments for restart-POLICY restarts, i.e. after the
    process exited; `docker restart` does not bump it. Docker event actions
    disambiguate the rest: an external restart emits `restart`, a policy
    restart emits `die` then `start`, and a healthcheck emits health_status
    events. Anything we cannot place stays `unresolved` — never guessed.
    """
    if not forensics:
        return {"classification": "unresolved",
                "reason": "no forensics available"}
    events = forensics.get("events") or []
    actions = [e.get("action") or "" for e in events]
    hc = forensics.get("healthcheck") or {}
    state = forensics.get("state") or {}
    exit_codes = {e.get("exit_code") for e in events
                  if (e.get("action") or "") == "die"}
    signals = {e.get("signal") for e in events if e.get("signal")}

    if any(a.startswith("restart") for a in actions):
        cls, why = ("confirmed_external_restart",
                    "docker recorded a `restart` action, which only an "
                    "explicit restart command emits")
    elif any(a == "health_status: unhealthy" for a in actions) and \
            (hc.get("failing_streak") or 0) > 0:
        cls, why = ("confirmed_healthcheck_action",
                    "unhealthy health_status events precede the restarts")
    elif "die" in actions and "start" in actions:
        cls, why = ("confirmed_application_exit",
                    "docker recorded die→start pairs and RestartCount is "
                    "incrementing, so the process exits and the restart "
                    "policy brings it back")
    elif (forensics.get("restart_count") or 0) > 0 and not events:
        # docker events cannot help here: the daemon's ring buffer is exhausted
        # by healthcheck exec_* probes within minutes, so lifecycle events are
        # gone long before anyone asks. RestartCount plus the FinishedAt →
        # StartedAt gap is the authoritative evidence instead.
        gap = _restart_gap_seconds(state)
        why = ("RestartCount is incrementing, which only happens when the "
               "restart policy restarts a process that exited on its own; a "
               "manual `docker restart` does not increment it")
        if gap is not None and gap < 10:
            why += (f"; the container restarted {gap:.1f}s after exiting, the "
                    f"signature of the restart policy reacting immediately "
                    f"rather than an external or scheduled command")
        why += ("; the docker event journal could not corroborate this — its "
                "ring buffer only reached back minutes, not to any restart")
        cls = "confirmed_application_exit"
    else:
        cls, why = ("unresolved", "no event pattern matched")
    return {"classification": cls, "reason": why,
            "restart_count": forensics.get("restart_count"),
            "restart_policy": forensics.get("restart_policy"),
            "last_started_at": state.get("started_at"),
            "last_finished_at": state.get("finished_at"),
            "last_exit_code": state.get("exit_code"),
            "last_error": state.get("error") or None,
            "oom_killed": state.get("oom_killed"),
            "die_exit_codes": sorted(c for c in exit_codes if c is not None),
            "signals_seen": sorted(signals),
            "healthcheck_status": hc.get("status"),
            "healthcheck_failing_streak": hc.get("failing_streak"),
            "event_actions": actions[-12:],
            "restart_gap_seconds": _restart_gap_seconds(state),
            "events_usable": bool(events),
            "events_caveat": (None if events else
                              "docker event ring buffer did not reach any "
                              "restart; an empty list is not evidence of "
                              "stability"),
            # A clean exit code is NOT evidence of health; it only means the
            # process chose to stop rather than crashing loudly.
            "clean_exit_is_not_healthy": True,
            "automatic_repair": "none — cause must be proven first",
            }


async def _restart_evidence() -> dict:
    """Best-effort forensics. Degrades to a note if the action is missing."""
    try:
        data = await run_action("tracearr_restart_forensics", timeout=60)
    except NasError as e:
        return {"classification": "unresolved",
                "reason": f"forensics unavailable: {e}"}
    return classify_restarts(data)


# ── upstream release verification ──────────────────────────────────────────
# A digest that matches the CONFIGURED pin proves only that what is running is
# what was configured. Whether that pin is the newest stable release is a
# separate question with a separate answer, and it is allowed to be "unknown".
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def _parse_stable(tag: str):
    """Version tuple for a STABLE tag, or None for prereleases/junk.

    'v1.5.0' -> (1,5,0);  'v1.5.0-beta.7' -> None (prerelease, never a target).
    """
    m = _SEMVER.match((tag or "").strip())
    return tuple(int(g) for g in m.groups()) if m else None


def _registry_tag(version: str, style: str) -> str:
    return version[1:] if style == "strip_v" and version.startswith("v") else version


async def _http_json(session, url: str, headers: dict, timeout: int = 12):
    async with session.get(url, headers=headers, timeout=timeout) as r:
        if r.status != 200:
            raise NasError(f"{url.split('/')[2]} returned HTTP {r.status}")
        return await r.json()


async def _latest_stable_release(session, release_source: str) -> tuple[str, tuple]:
    """Newest NON-prerelease release from the configured upstream feed."""
    if not release_source.startswith("github:"):
        raise NasError(f"unsupported release source {release_source!r}")
    repo = release_source.split(":", 1)[1]
    url = f"https://api.github.com/repos/{repo}/releases?per_page=30"
    data = await _http_json(session, url,
                            {"Accept": "application/vnd.github+json"})
    best, best_v = None, None
    for rel in data:
        if rel.get("prerelease") or rel.get("draft"):
            continue
        v = _parse_stable(rel.get("tag_name") or "")
        if v and (best_v is None or v > best_v):
            best, best_v = rel.get("tag_name"), v
    if best is None:
        raise NasError("no stable (non-prerelease) release found upstream")
    return best, best_v


async def _registry_digest(session, repo: str, tag: str) -> str:
    """Resolve a tag to its content digest WITHOUT pulling the image."""
    host, path = repo.split("/", 1)
    tok_url = f"https://{host}/token?scope=repository:{path}:pull&service={host}"
    tok = (await _http_json(session, tok_url, {})).get("token", "")
    if not tok:
        raise NasError(f"{host} did not issue an anonymous pull token")
    accept = ("application/vnd.oci.image.index.v1+json,"
              "application/vnd.docker.distribution.manifest.list.v2+json,"
              "application/vnd.oci.image.manifest.v1+json,"
              "application/vnd.docker.distribution.manifest.v2+json")
    url = f"https://{host}/v2/{path}/manifests/{tag}"
    async with session.head(url, headers={"Authorization": f"Bearer {tok}",
                                          "Accept": accept}, timeout=12) as r:
        if r.status != 200:
            raise NasError(f"{host} has no manifest for tag {tag!r} "
                           f"(HTTP {r.status})")
        digest = r.headers.get("Docker-Content-Digest", "")
    if not digest:
        raise NasError(f"{host} returned no content digest for {tag!r}")
    return digest


async def check_upstream(asset: dict, session) -> dict:
    """Verified upstream status, or an honest 'unavailable'. Never guesses."""
    spec = asset.get("updates") or {}
    source = spec.get("release_source") or ""
    repo = spec.get("registry_repo") or ""
    out = {"release_source": source or None,
           "latest_stable_version": None, "latest_stable_digest": None,
           "upstream_check_status": "unavailable", "update_available": None,
           "notes": []}
    if not source or not repo:
        out["notes"].append("no release_source/registry_repo configured for "
                            "this asset, so upstream cannot be verified")
        return out
    try:
        latest, latest_v = await _latest_stable_release(session, source)
        tag = _registry_tag(latest, spec.get("registry_tag_style") or "")
        digest = await _registry_digest(session, repo, tag)
    except NasError as e:
        out["notes"].append(f"upstream check failed: {e}")
        return out
    except Exception as e:
        out["notes"].append(f"upstream check failed: {e.__class__.__name__}")
        return out
    installed_v = _parse_stable(asset.get("version") or "")
    out.update({"latest_stable_version": latest,
                "latest_stable_digest": digest,
                "upstream_check_status": "verified"})
    if installed_v is None:
        out["notes"].append("installed version is not a parseable stable "
                            "semver, so no comparison was made")
        return out
    out["update_available"] = latest_v > installed_v
    return out


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
    configured_digest = asset.get("image_digest") or ""
    running_ref = running.get("image") or ""
    running_digest = ""
    if "@" in running_ref:
        running_digest = running_ref.split("@", 1)[1]
    deployment_ok = bool(configured_digest and running_digest
                         and configured_digest == running_digest)

    upstream = await _upstream_for(asset)

    out = {"ok": True,
           "compose_project": data.get("compose_project"),
           "installed_version": asset.get("version"),
           "configured_image": (asset.get("docker") or {}).get("image"),
           "configured_digest": configured_digest,
           "running_digest": running_digest,
           # DEPLOYMENT consistency only. Says nothing about being current.
           "deployment_matches_configuration": deployment_ok,
           "latest_stable_version": upstream["latest_stable_version"],
           "latest_stable_digest": upstream["latest_stable_digest"],
           "upstream_check_status": upstream["upstream_check_status"],
           "update_available": upstream["update_available"],
           "release_source": upstream["release_source"],
           "approval_policy": asset.get("update_policy"),
           "dependencies": {k: v for k, v in (data.get("images") or {}).items()
                            if k != "tracearr"},
           "notes": list(upstream["notes"])}

    verified = upstream["upstream_check_status"] == "verified"
    out["confidence"] = "high" if verified else "low"
    if not deployment_ok:
        out["confidence"] = "low"
        out["notes"].append(
            "the running digest does NOT match the configured pin — the "
            "deployment drifted from its configuration")

    # The one sentence Loki is allowed to say, precomputed so the wording
    # cannot drift into an unearned "up to date".
    ver = asset.get("version")
    if not verified:
        out["summary"] = (
            f"Tracearr is running the configured {ver} image, and the digest "
            f"matches its pin. I could not yet verify whether that is the "
            f"newest stable upstream release.")
    elif upstream["update_available"]:
        out["summary"] = (
            f"Tracearr is running {ver}, but {upstream['latest_stable_version']} "
            f"is the latest verified stable release — an update is available.")
    else:
        out["summary"] = (
            f"Tracearr is running {ver} and that is the latest verified "
            f"stable release.")

    out["next_step"] = (
        "Updates on the NAS are performed by Watchtower there, not by Loki. "
        "Loki has no update or restart verb for this asset: the dispatcher "
        "exposes read-only actions only. Any change needs an approved plan.")
    return json.dumps(out)


async def _upstream_for(asset: dict) -> dict:
    """Upstream check with its own session, so a network fault degrades to
    'unavailable' instead of failing the whole status call."""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            return await check_upstream(asset, session)
    except Exception as e:
        return {"release_source": (asset.get("updates") or {}).get("release_source"),
                "latest_stable_version": None, "latest_stable_digest": None,
                "upstream_check_status": "unavailable", "update_available": None,
                "notes": [f"upstream check could not run: {e.__class__.__name__}"]}


# ── approval-gated update workflow ─────────────────────────────────────────
async def _prepare_update_plan() -> tuple[dict, str]:
    """Resolve the target digest, record a plan on the NAS, return (plan, err).

    Read-only up to and including this point: tracearr_update_prepare writes a
    plan file and changes nothing else."""
    asset = _tracearr_asset()
    try:
        data = await run_action("tracearr_update_check")
    except NasError as e:
        return {}, str(e)
    upstream = await _upstream_for(asset)
    if upstream["upstream_check_status"] != "verified":
        return {}, ("upstream release data could not be verified, so there is "
                    "no trustworthy target to approve. "
                    + "; ".join(upstream["notes"])[:200])
    digest = upstream.get("latest_stable_digest") or ""
    if not _DIGEST_RE.match(digest):
        return {}, f"upstream returned a malformed digest: {digest[:32]!r}"
    if not upstream.get("update_available"):
        return {}, (f"Tracearr is already on {asset.get('version')}, which is "
                    f"the latest verified stable release — nothing to update.")
    running = (data.get("images") or {}).get("tracearr") or {}
    try:
        plan = await run_action("tracearr_update_prepare", timeout=90,
                                param=digest)
    except NasError as e:
        return {}, str(e)
    plan.update({
        "installed_version": asset.get("version"),
        "target_version": upstream.get("latest_stable_version"),
        "target_digest": digest,
        "running_image": running.get("image"),
        "release_source": upstream.get("release_source"),
        "approval_policy": asset.get("update_policy"),
    })
    return plan, ""


def _update_prepare(args: dict, ctx: ToolContext):
    """Draft-gate hook: build the human summary the Boss approves."""
    if user_level(ctx.user_id) != "boss":
        return {}, "", "Tracearr updates are Boss-only"
    # create_draft() calls this hook SYNCHRONOUSLY from inside the running bot
    # loop, so the async work has to happen on a loop of its own — scheduling
    # it back onto the caller's loop and blocking would deadlock the bot.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(lambda: asyncio.run(_prepare_update_plan()))
        try:
            plan, err = fut.result(timeout=PREPARE_TIMEOUT)
        except concurrent.futures.TimeoutError:
            return {}, "", ("preparing the update plan timed out — the NAS or "
                            "the upstream release feed did not respond")
        except Exception as e:                    # noqa: BLE001
            return {}, "", f"could not prepare the update plan: {e}"
    if err:
        return {}, "", err
    space = ("OK" if plan.get("sufficient_space") else
             f"LOW ({plan.get('free_mb')}MB free, {plan.get('free_mb_required')}MB needed)")
    summary = "\n".join([
        f"UPDATE Tracearr on the UGREEN NAS: "
        f"{plan.get('installed_version')} → {plan.get('target_version')}",
        f"Target digest: {plan.get('target_digest', '')[:26]}…",
        f"Release source: {plan.get('release_source')}",
        f"Compose: {plan.get('compose_file')} (service {plan.get('compose_service')})",
        f"Backups first: compose copy + pg_dump → {plan.get('backup_dir')}",
        f"Disk: {space}",
        "Sequence: backup → verify backups → pull approved digest → recreate "
        "ONLY the tracearr service → verify app/Redis/PostgreSQL/local HTTP/"
        "public HTTP → roll back automatically if verification fails.",
        "Redis and PostgreSQL are NOT updated or recreated.",
        f"Policy: {plan.get('approval_policy')}",
    ])
    return {"prepare_id": plan["prepare_id"],
            "target_digest": plan["target_digest"],
            "target_version": plan.get("target_version"),
            "installed_version": plan.get("installed_version"),
            "backup_dir": plan.get("backup_dir")}, summary, ""


async def _record_in_joplin(title: str, body: str) -> str:
    """Write the maintenance result to Joplin. Never raises into the caller."""
    try:
        import joplin_integration as jop
        note = await jop.create_note(title, body, notebook="Loki/Maintenance")
        return (note or {}).get("id", "") if isinstance(note, dict) else ""
    except Exception as e:                       # noqa: BLE001 - reporting only
        log.warning("Joplin record failed: %s", e)
        return ""


async def _run_approved_update(payload: dict, ctx: ToolContext) -> str:
    """Execute the approved update. Only reachable after the draft gate."""
    prepare_id = str(payload.get("prepare_id") or "")
    steps: list[str] = []
    started = datetime.datetime.now(datetime.timezone.utc)

    def _line(msg):
        steps.append(msg)
        log.info("tracearr update: %s", msg)

    # 1. Back up compose + database, and verify both.
    try:
        backup = await run_action("tracearr_backup", timeout=960)
    except NasError as e:
        return f"Update aborted before any change: backup failed — {e}"
    if not (backup.get("compose_ok") and backup.get("database_ok")):
        return (f"Update aborted before any change: backups did not verify "
                f"(compose_ok={backup.get('compose_ok')}, "
                f"database_ok={backup.get('database_ok')}). "
                f"Nothing was pulled or recreated.")
    _line(f"backups verified — compose + {backup.get('database_size_bytes')} byte "
          f"database dump at {backup.get('backup_dir')}")

    # 2. Pull the approved digest and recreate only the tracearr service.
    try:
        applied = await run_action("tracearr_apply_update", timeout=960,
                                   param=prepare_id)
    except NasError as e:
        return (f"Update failed while applying: {e}\nBackups are preserved at "
                f"{backup.get('backup_dir')}. Tracearr was not left mid-update "
                f"by Loki; verify before retrying.")
    _line(f"pulled {applied.get('pulled', '')[:40]}… and recreated the service")

    # 3. Verify app + dependencies + local and public HTTP.
    try:
        verify = await run_action("tracearr_verify_update", timeout=180)
    except NasError as e:
        verify = {"verified": False, "failures": [f"verification could not run: {e}"]}
    if verify.get("verified"):
        body = _report_body(payload, backup, applied, verify, steps, started,
                            outcome="SUCCESS")
        await _record_in_joplin(
            f"Tracearr update {payload.get('installed_version')} → "
            f"{payload.get('target_version')} — success", body)
        return (f"Tracearr updated {payload.get('installed_version')} → "
                f"{payload.get('target_version')} and verified: app, Redis, "
                f"PostgreSQL, local HTTP and public HTTP all healthy. "
                f"Backups kept at {backup.get('backup_dir')}.")

    # 4. Verification failed — roll back.
    _line(f"verification FAILED: {'; '.join(verify.get('failures') or [])}")
    try:
        rb = await run_action("tracearr_rollback", timeout=960)
    except NasError as e:
        body = _report_body(payload, backup, applied, verify, steps, started,
                            outcome="FAILED, ROLLBACK ERRORED")
        await _record_in_joplin("Tracearr update FAILED — rollback errored", body)
        return (f"Update failed verification AND the rollback errored: {e}\n"
                f"Backups are preserved at {backup.get('backup_dir')}. "
                f"This needs hands-on attention now.")
    rb_ok = (rb.get("verify_after_rollback") or {}).get("verified")
    body = _report_body(payload, backup, applied, verify, steps, started,
                        outcome="FAILED, ROLLED BACK" if rb_ok else
                                "FAILED, ROLLBACK UNVERIFIED", rollback=rb)
    await _record_in_joplin("Tracearr update failed — rolled back", body)
    if rb_ok:
        return (f"Update failed verification ({'; '.join(verify.get('failures') or [])}) "
                f"and was rolled back to {payload.get('installed_version')}, "
                f"which verified clean. Backups preserved at "
                f"{backup.get('backup_dir')}. The database dump was NOT "
                f"restored — the image was rolled back, not the data.")
    return (f"Update failed verification and the rollback did not verify. "
            f"Backups preserved at {backup.get('backup_dir')}. Hands-on "
            f"attention needed.")


def _report_body(payload, backup, applied, verify, steps, started,
                 outcome: str, rollback: dict | None = None) -> str:
    lines = [
        f"# Tracearr update — {outcome}",
        "",
        f"- Started: {started.isoformat()}",
        f"- Finished: {datetime.datetime.now(datetime.timezone.utc).isoformat()}",
        f"- Version: {payload.get('installed_version')} → {payload.get('target_version')}",
        f"- Target digest: {str(payload.get('target_digest'))[:26]}…",
        f"- Prepare id: {payload.get('prepare_id')}",
        "",
        "## Backups",
        f"- Directory: {backup.get('backup_dir')}",
        f"- Compose copy verified: {backup.get('compose_ok')}",
        f"- Database dump verified: {backup.get('database_ok')} "
        f"({backup.get('database_size_bytes')} bytes, gzip integrity "
        f"{backup.get('gzip_integrity_ok')})",
        "",
        "## Steps",
    ]
    lines += [f"- {s}" for s in steps]
    lines += ["", "## Verification",
              f"- Verified: {verify.get('verified')}",
              f"- Failures: {'; '.join(verify.get('failures') or []) or 'none'}"]
    for role, chk in (verify.get("checks") or {}).items():
        lines.append(f"- {role}: {chk}")
    if rollback:
        lines += ["", "## Rollback",
                  f"- Compose restored: {rollback.get('compose_restored')}",
                  f"- Previous image: {rollback.get('previous_image')}",
                  f"- Recreate ok: {rollback.get('recreate_ok')}",
                  f"- Verified after rollback: "
                  f"{(rollback.get('verify_after_rollback') or {}).get('verified')}",
                  f"- Note: {rollback.get('note')}"]
    lines += ["", "_Recorded automatically by Loki after an approved update._"]
    return "\n".join(lines)


# ── network / disk / Plex diagnostics (Pass 3) ──────────────────────────────
def _classify_network(status: dict) -> dict:
    """Obvious saturation/error classification from dispatcher network_status."""
    counters = status.get("counters") or {}
    findings = []
    sat = status.get("saturation_pct_of_link")
    if sat is not None and sat >= 80:
        findings.append(f"link saturation {sat}% of negotiated speed")
    for k in ("rx_errors", "tx_errors", "rx_dropped", "tx_dropped"):
        v = counters.get(k) or 0
        if v and v > 0:
            findings.append(f"{k}={v}")
    kernel = (status.get("kernel_network_errors") or {}).get("lines") or []
    if kernel:
        findings.append(f"{len(kernel)} kernel link-error log line(s)")
    link = status.get("link") or {}
    if link.get("operstate") not in (None, "up"):
        findings.append(f"interface operstate={link.get('operstate')}")
    return {"healthy": not findings, "findings": findings}


def _expected_capacity_mbps(status: dict):
    link = (status or {}).get("link") or {}
    return link.get("speed_mbps")


async def _tool_nas_network_status(args: dict, ctx: ToolContext) -> str:
    err = _boss_only(ctx)
    if err:
        return json.dumps({"ok": False, "error": err})
    try:
        status = await run_action("network_status", timeout=30)
    except NasError as e:
        return json.dumps({"ok": False, "error": str(e)})
    diag = _classify_network(status)
    return json.dumps({"ok": True, "interface": status.get("interface"),
                       "link": status.get("link"),
                       "current_throughput": status.get("current_throughput"),
                       "saturation_pct_of_link": status.get("saturation_pct_of_link"),
                       "counters": status.get("counters"),
                       "reachability": status.get("reachability"),
                       "dns": status.get("dns"),
                       "routes": status.get("routes"),
                       "kernel_network_errors": status.get("kernel_network_errors"),
                       "healthy": diag["healthy"], "findings": diag["findings"]})


def _classify_speed_test(server_mbps_down, client_mbps_up, expected_mbps):
    if server_mbps_down is None and client_mbps_up is None:
        return "test_inconclusive"
    if not expected_mbps:
        return "test_inconclusive"
    best = max(v for v in (server_mbps_down, client_mbps_up) if v is not None)
    pct = best / expected_mbps * 100
    if pct >= 80:
        return "healthy_for_link_capacity"
    if pct >= 40:
        return "degraded"
    return "severely_degraded"


async def _tool_nas_network_speed_test(args: dict, ctx: ToolContext) -> str:
    err = _boss_only(ctx)
    if err:
        return json.dumps({"ok": False, "error": err})
    if not IPERF3_LOCAL:
        return json.dumps({"ok": False, "error":
                           "iperf3 is not installed on dex247 — test cannot run "
                           "locally. This is a bootstrap gap, not a network fault."})
    try:
        tooling = await run_action("network_tooling_check", timeout=15)
    except NasError as e:
        return json.dumps({"ok": False, "error": str(e)})
    if not tooling.get("iperf3_installed"):
        return json.dumps({"ok": False, "error":
                           "iperf3 is not installed on the NAS — the bootstrap "
                           "step that stages it has not run yet."})

    async def _server():
        try:
            return await run_action("network_speed_test",
                                     timeout=20)
        except NasError as e:
            return {"server_ran": False, "error": str(e)}

    server_task = asyncio.create_task(_server())
    await asyncio.sleep(IPERF3_STARTUP_DELAY)

    try:
        status = await run_action("network_status", timeout=30)
        nas_ip = status.get("address")
    except NasError:
        nas_ip = None
    if not nas_ip:
        server_task.cancel()
        return json.dumps({"ok": False, "error":
                           "could not discover the NAS LAN address to test against"})

    def _run_client(reverse: bool):
        argv = [IPERF3_LOCAL, "-c", nas_ip, "-p", "5201", "-t",
                str(IPERF3_TEST_SECONDS), "-J"]
        if reverse:
            argv.append("-R")
        try:
            p = subprocess.run(argv, capture_output=True, text=True,
                               timeout=IPERF3_TEST_SECONDS + 10)
            return p.returncode == 0, p.stdout
        except (subprocess.TimeoutExpired, OSError):
            return False, ""

    loop = asyncio.get_event_loop()
    ok_down, out_down = await loop.run_in_executor(None, _run_client, False)
    server = await server_task

    down_mbps = retransmits_down = None
    if ok_down and out_down:
        try:
            data = json.loads(out_down)
            down_mbps = round(data["end"]["sum_received"]["bits_per_second"] / 1e6, 1)
            retransmits_down = (data.get("end", {}).get("sum_sent") or {}).get("retransmits")
        except (ValueError, KeyError, TypeError):
            pass

    # Second, reverse-direction pass needs its own one-off server on the NAS
    # (the first already exited after serving the download pass).
    up_mbps = retransmits_up = None
    if down_mbps is not None:
        server_task2 = asyncio.create_task(_server())
        await asyncio.sleep(IPERF3_STARTUP_DELAY)
        ok_up, out_up = await loop.run_in_executor(None, _run_client, True)
        await server_task2
        if ok_up and out_up:
            try:
                data = json.loads(out_up)
                up_mbps = round(data["end"]["sum_received"]["bits_per_second"] / 1e6, 1)
                retransmits_up = (data.get("end", {}).get("sum_sent") or {}).get("retransmits")
            except (ValueError, KeyError, TypeError):
                pass

    expected = _expected_capacity_mbps(status if 'status' in dir() else {})
    interpretation = _classify_speed_test(down_mbps, up_mbps, expected)
    return json.dumps({
        "ok": True,
        "download_mbps": down_mbps, "upload_mbps": up_mbps,
        "retransmits": {"download": retransmits_down, "upload": retransmits_up},
        "expected_link_capacity_mbps": expected,
        "interpretation": interpretation,
        "server_ran": server.get("server_ran"),
        "client_ran": ok_down,
    })


async def _tool_nas_disk_status(args: dict, ctx: ToolContext) -> str:
    err = _boss_only(ctx)
    if err:
        return json.dumps({"ok": False, "error": err})
    try:
        data = await run_action("disk_status", timeout=30)
    except NasError as e:
        return json.dumps({"ok": False, "error": str(e)})
    findings = []
    for fs in data.get("filesystems") or []:
        try:
            pct = int(str(fs.get("use_pct", "0")).rstrip("%"))
            if pct >= 90:
                findings.append(f"{fs.get('mount')} at {pct}% full")
        except ValueError:
            pass
    for s in data.get("smart") or []:
        if s.get("available") and not s.get("healthy"):
            findings.append(f"SMART failing on {s.get('device')}")
    for e in (data.get("kernel_disk_errors") or [])[:5]:
        findings.append(f"kernel: {e}")
    return json.dumps({"ok": True, "filesystems": data.get("filesystems"),
                       "device_io": data.get("device_io"),
                       "mdraid": data.get("mdraid"), "smart": data.get("smart"),
                       "kernel_disk_errors": data.get("kernel_disk_errors"),
                       "healthy": not findings, "findings": findings})


def _plex_asset() -> dict:
    asset = _reg().get("plex")
    if asset is None:
        raise NasError("Plex is not in the asset registry")
    return asset


async def _tool_plex_status(args: dict, ctx: ToolContext) -> str:
    err = _boss_only(ctx)
    if err:
        return json.dumps({"ok": False, "error": err})
    try:
        data = await run_action("plex_status", timeout=30)
    except NasError as e:
        return json.dumps({"ok": False, "error": str(e)})
    app = data.get("plex") or {}
    out = {"ok": True, "running": app.get("running"),
           "plex": _summarize(app), "resource_usage": data.get("resource_usage")}
    rc = app.get("restart_count") or 0
    if rc > RESTART_ALERT_THRESHOLD_PLEX:
        out["anomaly"] = f"Plex has restarted {rc} times — that churn is abnormal."
    return json.dumps(out)


async def _tool_plex_diagnose(args: dict, ctx: ToolContext) -> str:
    err = _boss_only(ctx)
    if err:
        return json.dumps({"ok": False, "error": err})
    try:
        status = await run_action("plex_status", timeout=30)
        deps = await run_action("plex_dependencies", timeout=30)
        transcode = await run_action("plex_transcode_processes", timeout=30)
    except NasError as e:
        return json.dumps({"ok": False, "error": str(e)})
    app = status.get("plex") or {}
    missing_mounts = [m["destination"] for m in deps.get("mounts") or []
                      if m.get("present") is False]
    verdict = "Plex and its media mounts are healthy."
    if not app.get("running"):
        verdict = f"Plex is not running (state={app.get('state')})."
    elif missing_mounts:
        verdict = f"Plex is running but mount(s) missing: {', '.join(missing_mounts)}"
    return json.dumps({"ok": True, "plex": _summarize(app),
                       "mounts": deps.get("mounts"),
                       "transcoding": transcode.get("transcoding"),
                       "transcode_processes": transcode.get("transcode_processes"),
                       "missing_mounts": missing_mounts,
                       "verdict": verdict})


async def _plex_http(path: str, timeout: int = 10) -> dict:
    """Direct call to Plex's own local API. Token used only as a header value
    — never returned, logged, or included in any exception message."""
    if not PLEX_URL:
        raise NasError("PLEX_URL is not configured")
    import aiohttp
    headers = {"Accept": "application/json"}
    if PLEX_TOKEN:
        headers["X-Plex-Token"] = PLEX_TOKEN
    url = f"{PLEX_URL.rstrip('/')}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=timeout) as r:
                if r.status != 200:
                    raise NasError(f"Plex API returned HTTP {r.status}")
                return await r.json()
    except aiohttp.ClientError:
        raise NasError("could not reach Plex's local API")


def _session_transcode_info(s: dict) -> dict:
    media = (s.get("Media") or [{}])[0]
    part = (media.get("Part") or [{}])[0]
    decision = part.get("decision") or media.get("videoDecision") or "unknown"
    tc = s.get("TranscodeSession") or {}
    return {
        "title": s.get("title"),
        "client": (s.get("Player") or {}).get("product"),
        "platform": (s.get("Player") or {}).get("platform"),
        "decision": decision,  # directplay / copy (direct stream) / transcode
        "bitrate_kbps": media.get("bitrate"),
        "video_decision": tc.get("videoDecision") or media.get("videoDecision"),
        "audio_decision": tc.get("audioDecision") or media.get("audioDecision"),
        "transcode_hw_requested": tc.get("transcodeHwRequested"),
        "transcode_hw_full_pipeline": tc.get("transcodeHwFullPipeline"),
        "throttled": tc.get("throttled"),
        "progress_pct": tc.get("progress"),
        "speed": tc.get("speed"),
    }


async def _tool_plex_sessions(args: dict, ctx: ToolContext) -> str:
    err = _boss_only(ctx)
    if err:
        return json.dumps({"ok": False, "error": err})
    try:
        data = await _plex_http("/status/sessions")
    except NasError as e:
        return json.dumps({"ok": False, "error": str(e)})
    sessions = ((data.get("MediaContainer") or {}).get("Metadata") or [])
    out = [_session_transcode_info(s) for s in sessions]
    return json.dumps({"ok": True, "active_sessions": len(out), "sessions": out,
                       "transcoding": any(s["decision"] == "transcode" for s in out)})


_PLAYBACK_CONCLUSIONS = (
    "network_bottleneck", "wifi_or_client_side_likely", "disk_latency",
    "disk_spin_up_likely", "software_transcode_bottleneck",
    "hardware_transcode_failure_or_fallback", "plex_database_config_issue",
    "dns_routing_delay", "container_or_network_issue", "insufficient_evidence",
)


async def _tool_plex_playback_diagnose(args: dict, ctx: ToolContext) -> str:
    """Bounded 12-step sequence. Never returns a raw checklist — one
    conclusion, the evidence behind it, and a confidence label."""
    err = _boss_only(ctx)
    if err:
        return json.dumps({"ok": False, "error": err})
    evidence = {}
    try:
        evidence["host"] = await run_action("host_status", timeout=30)
        evidence["plex"] = await run_action("plex_status", timeout=30)
        evidence["mounts"] = await run_action("plex_dependencies", timeout=30)
        evidence["disk"] = await run_action("disk_status", timeout=30)
        evidence["network"] = await run_action("network_status", timeout=30)
        evidence["transcode"] = await run_action("plex_transcode_processes", timeout=30)
    except NasError as e:
        return json.dumps({"ok": False, "error": str(e)})
    try:
        sessions_raw = await _plex_http("/status/sessions")
        sessions = [_session_transcode_info(s) for s in
                    ((sessions_raw.get("MediaContainer") or {}).get("Metadata") or [])]
    except NasError:
        sessions = []
    try:
        evidence["logs"] = await run_action("plex_recent_logs", timeout=30)
    except NasError:
        evidence["logs"] = None

    app = evidence["plex"].get("plex") or {}
    net = evidence["network"]
    net_diag = _classify_network(net)
    missing_mounts = [m["destination"] for m in evidence["mounts"].get("mounts") or []
                      if m.get("present") is False]
    disk_findings = []
    for fs in evidence["disk"].get("filesystems") or []:
        try:
            if int(str(fs.get("use_pct", "0")).rstrip("%")) >= 90:
                disk_findings.append(fs.get("mount"))
        except ValueError:
            pass

    conclusion, confidence, findings = "insufficient_evidence", "low", []
    if not app.get("running"):
        conclusion, confidence = "container_or_network_issue", "high"
        findings.append(f"Plex container state={app.get('state')}")
    elif missing_mounts:
        conclusion, confidence = "disk_latency", "high"
        findings.append(f"missing media mounts: {missing_mounts}")
    elif not net_diag["healthy"]:
        conclusion, confidence = "network_bottleneck", "medium"
        findings += net_diag["findings"]
    elif any(s["decision"] == "transcode" for s in sessions):
        tc = next(s for s in sessions if s["decision"] == "transcode")
        if tc.get("transcode_hw_requested") and not tc.get("transcode_hw_full_pipeline"):
            conclusion, confidence = "hardware_transcode_failure_or_fallback", "medium"
        else:
            conclusion, confidence = "software_transcode_bottleneck", "medium"
        findings.append(f"active transcode session: {tc}")
    elif sessions and all(s["decision"] in ("directplay", "copy") for s in sessions):
        conclusion, confidence = "wifi_or_client_side_likely", "low"
        findings.append("Direct Play/Direct Stream in use — server-side path is clean")
    elif disk_findings:
        conclusion, confidence = "disk_latency", "medium"
        findings.append(f"filesystem(s) near capacity: {disk_findings}")
    else:
        findings.append("no sessions active and no anomaly found in host/network/disk checks")

    return json.dumps({
        "ok": True, "conclusion": conclusion, "confidence": confidence,
        "evidence_summary": findings,
        "plex_state": _summarize(app),
        "active_sessions": sessions,
        "network_healthy": net_diag["healthy"],
    })


def _plex_restart_prepare(args: dict, ctx: ToolContext):
    if user_level(ctx.user_id) != "boss":
        return {}, "", "Plex restart is Boss-only"
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(lambda: asyncio.run(run_action("plex_status", timeout=30)))
        try:
            status = fut.result(timeout=45)
        except Exception as e:                      # noqa: BLE001
            return {}, "", f"could not check Plex status: {e}"
    app = status.get("plex") or {}
    if app.get("running"):
        return {}, "", "Plex is already running — nothing to restart"
    summary = (f"START Plex on the UGREEN NAS: container is currently "
               f"state={app.get('state')}. This only runs `docker start` on "
               f"the existing stopped container after confirming its media "
               f"mounts are present — never `restart`, `stop`, or `kill`.")
    return {"container_state": app.get("state")}, summary, ""


async def _run_approved_plex_restart(payload: dict, ctx: ToolContext) -> str:
    try:
        result = await run_action("plex_restart", timeout=45)
    except NasError as e:
        return f"Plex start failed: {e}"
    return (f"Plex started: {result.get('container')} is now "
           f"state={result.get('state')} (running={result.get('running')}).")


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
        name="tracearr_update",
        description=(
            "Update Tracearr on the UGREEN NAS to the latest verified stable "
            "release. FIRST CHOICE for 'update Tracearr', 'upgrade Tracearr'. "
            "This is CONSEQUENTIAL: it stages an approval draft with the exact "
            "versions, digest and backup plan, and does nothing until the Boss "
            "approves. On approval it backs up the compose file and the "
            "PostgreSQL database, verifies both, pulls the approved digest, "
            "recreates ONLY the tracearr service (never Redis or PostgreSQL), "
            "verifies app/Redis/PostgreSQL/local HTTP/public HTTP, and rolls "
            "back automatically if verification fails."),
        parameters=_p({}, []),
        handler=_run_approved_update, permission="boss", timeout=1800,
        action_type="tracearr_update", approval_ttl=3600,
        prepare=_update_prepare,
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
    register(ToolSpec(
        name="nas_network_status",
        description=(
            "Live NAS network health: active interface, negotiated link "
            "speed/duplex/MTU, RX/TX errors/drops, current throughput sample, "
            "gateway/dex247/RAZR reachability and latency, DNS timing, routing "
            "table, and kernel link errors. FIRST CHOICE for 'is my NAS "
            "network slow', 'check NAS network', 'is Plex's network ok'. "
            "Read-only."),
        parameters=_p({}, []),
        handler=_tool_nas_network_status, permission="boss", timeout=45,
    ))
    register(ToolSpec(
        name="nas_network_speed_test",
        description=(
            "Run a real, bounded (under 20s) iperf3 throughput test between "
            "dex247 and the UGREEN NAS. FIRST CHOICE for 'run a network speed "
            "test on the NAS', 'check NAS throughput', 'how fast is the NAS "
            "network'. Starts a one-off iperf3 server on the NAS bound only "
            "to its LAN address, tests download and upload, then the server "
            "exits automatically. No persistent server, no caller-controlled "
            "host/port/duration."),
        parameters=_p({}, []),
        handler=_tool_nas_network_speed_test, permission="boss", timeout=60,
    ))
    register(ToolSpec(
        name="nas_disk_status",
        description=(
            "Read-only NAS storage health: filesystem usage, device I/O "
            "utilization, SMART summaries, mdraid status, and kernel disk "
            "errors. Use for 'is the NAS disk ok', 'check NAS storage'. Never "
            "writes a test file or benchmarks disks destructively."),
        parameters=_p({}, []),
        handler=_tool_nas_disk_status, permission="boss", timeout=45,
    ))
    register(ToolSpec(
        name="plex_status",
        description=(
            "Is Plex running? Live container state, health, restart count, "
            "CPU/memory on the UGREEN NAS. FIRST CHOICE for 'is Plex up/"
            "running', 'check Plex'. Read-only."),
        parameters=_p({}, []),
        handler=_tool_plex_status, permission="boss", timeout=45,
    ))
    register(ToolSpec(
        name="plex_diagnose",
        description=(
            "Diagnose Plex: container state, media mount health, and whether "
            "it is currently transcoding. FIRST CHOICE for 'Plex is down', "
            "'diagnose Plex'. Read-only; proposes nothing."),
        parameters=_p({}, []),
        handler=_tool_plex_diagnose, permission="boss", timeout=60,
    ))
    register(ToolSpec(
        name="plex_sessions",
        description=(
            "Active Plex playback sessions: Direct Play / Direct Stream / "
            "Transcode state, bitrate, hardware vs software transcode, client "
            "type, per session. FIRST CHOICE for 'is Plex transcoding', 'is "
            "Plex using hardware transcoding', 'what is playing on Plex'. "
            "Calls Plex's own local API directly — never exposes the Plex "
            "token."),
        parameters=_p({}, []),
        handler=_tool_plex_sessions, permission="boss", timeout=30,
    ))
    register(ToolSpec(
        name="plex_playback_diagnose",
        description=(
            "Bounded end-to-end diagnosis of slow/buffering Plex playback: "
            "checks NAS host, Plex container, media mounts, disk, network "
            "errors/throughput, NAS-to-LAN latency, active sessions, Direct "
            "Play/Transcode state, transcode load, and bounded logs, then "
            "returns ONE conclusion with evidence and confidence — never a "
            "checklist. FIRST CHOICE for 'why is Plex slow to start', 'why is "
            "this movie buffering', 'Plex is slow'."),
        parameters=_p({}, []),
        handler=_tool_plex_playback_diagnose, permission="boss", timeout=90,
    ))
    register(ToolSpec(
        name="plex_start",
        description=(
            "Start Plex on the UGREEN NAS ONLY if it is currently stopped and "
            "its media mounts are healthy. CONSEQUENTIAL: stages an approval "
            "draft; does nothing until the Boss approves. Uses `docker "
            "start` only — never restart/stop/kill a running container."),
        parameters=_p({}, []),
        handler=_run_approved_plex_restart, permission="boss", timeout=90,
        action_type="plex_start", approval_ttl=3600,
        prepare=_plex_restart_prepare,
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

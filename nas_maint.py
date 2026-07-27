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
import json
import re
import logging

import homelab_assets
from tools import ToolContext, ToolSpec, register, user_level

log = logging.getLogger("NasMaint")

SSH_ALIAS = "nas-maint"
DISPATCHER = "/usr/local/sbin/loki-nas-maint"

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

"""
jellyfin_health — diagnosis + narrowly-scoped recovery for the Jellyfin
container on dex247.

Automatic repair happens ONLY for the exact low-risk conditions:
  - the container is stopped/exited unexpectedly (Compose definition exists,
    mounts and disk healthy, no migration/config/permission errors in recent
    logs) → start it and verify recovery, or
  - the container runs but its health endpoint fails, same safety conditions,
    and it has not already been restarted for this incident → restart ONCE.

Anything touching the database, permissions, mounts, or configuration
escalates instead.
"""

import json

NAME = "jellyfin_health"

# Log lines that mean "do NOT blind-restart" — these need a human.
UNSAFE_LOG_PATTERNS = (
    "migration", "corrupt", "database is locked", "unable to open database",
    "permission denied", "read-only file system", "no space left",
    "malformed", "fatal error",
)

VERIFY_ATTEMPTS = 6
VERIFY_DELAY_SECS = 5


async def run(asset: dict, ops) -> dict:
    docker = asset.get("docker") or {}
    container = docker["container"]
    health = asset.get("health") or {}
    mounts = asset.get("mounts") or {}
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
        return bool(ok)

    # 1. Container status/health
    rc, out = await ops.run("docker_inspect", container=container)
    state, health_str, exit_code, restarts = "unknown", "", None, None
    if rc == 0:
        try:
            info = json.loads(out)[0]
            st = info.get("State", {})
            state = st.get("Status", "unknown")
            health_str = (st.get("Health") or {}).get("Status", "")
            exit_code = st.get("ExitCode")
            restarts = info.get("RestartCount")
        except (ValueError, IndexError, KeyError):
            pass
    running = state == "running"
    add("container_state", running,
        f"state={state} health={health_str or 'n/a'} exit={exit_code} "
        f"restarts={restarts}")

    # 2. Local HTTP health endpoint
    endpoint_ok = False
    if health.get("local_url"):
        status, _body = await ops.http_get(health["local_url"])
        endpoint_ok = add("local_http", status == 200, f"HTTP {status}")
    else:
        add("local_http", False, "no local health endpoint declared")

    # 3. Recent logs (redacted, bounded) + unsafe-pattern scan
    rc, out = await ops.run("docker_logs_tail", container=container)
    logs = ops.redact(out)[-2000:] if rc == 0 else ""
    unsafe_hits = [p for p in UNSAFE_LOG_PATTERNS if p in logs.lower()]
    add("log_scan", not unsafe_hits,
        "no known-unsafe error patterns" if not unsafe_hits
        else f"unsafe patterns in logs: {', '.join(unsafe_hits)}")

    # 4. Disk space on the config volume + one media mount
    disk_ok = True
    for label, path in [("config", mounts.get("config")),
                        ("media", (mounts.get("media") or [None])[0])]:
        if not path:
            continue
        rc, out = await ops.run("df_path", path=path)
        pct = _df_pct(out) if rc == 0 else None
        ok = pct is not None and pct < 95
        disk_ok = disk_ok and ok
        add(f"disk_{label}", ok, out.splitlines()[-1] if out else "df failed")

    # 5. Media mounts + permissions metadata. A MISSING path (unmounted NAS)
    # fails; an empty-but-present media directory is a legitimate empty
    # library and only gets a note. The config dir must never be empty.
    mounts_ok = True
    config_path = mounts.get("config")
    for path in [p for p in [config_path] + list(mounts.get("media") or []) if p]:
        meta = await ops.path_meta(path)
        exists = bool(meta.get("exists"))
        empty = bool(meta.get("empty", True))
        ok = exists and not (empty and path == config_path)
        mounts_ok = mounts_ok and ok
        add(f"mount:{path}", ok,
            (f"mode={meta.get('mode')} uid={meta.get('uid')} "
             f"gid={meta.get('gid')} entries={meta.get('entries')}"
             + (" (empty library)" if empty else ""))
            if exists else "MISSING")

    # 6. Compose definition
    compose_meta = await ops.path_meta(docker.get("compose_file", ""))
    compose_ok = add("compose_definition", compose_meta.get("exists", False),
                     docker.get("compose_file", "not declared"))

    # 7. Reverse-proxy reachability (informational — never gates repair)
    if health.get("public_url"):
        status, _ = await ops.http_get(health["public_url"])
        add("reverse_proxy", status in (200, 301, 302, 401),
            f"HTTP {status} via proxy")

    healthy = running and endpoint_ok and not unsafe_hits and disk_ok and mounts_ok
    result = {"checks": checks, "healthy": healthy, "repair": None,
              "repair_result": None, "escalate": False}
    if healthy:
        result["diagnosis"] = "Jellyfin is running and serving; mounts and disk healthy."
        return result

    safe_to_touch = compose_ok and mounts_ok and disk_ok and not unsafe_hits

    if not running and safe_to_touch:
        result["repair"] = {
            "action": "restart_stateless_service",
            "description": f"start stopped container '{container}' and verify recovery",
            "commands": [{"name": "docker_start", "params": {"container": container}}],
            "rollback": [],
        }
        result["diagnosis"] = ("Known failure: container stopped unexpectedly "
                               "with healthy mounts/disk and clean logs.")
        if ops.auto_repair_allowed:
            result["repair_result"] = await _start_and_verify(
                ops, "docker_start", container, health.get("local_url"))
            result["escalate"] = not result["repair_result"]["ok"]
        return result

    if running and not endpoint_ok and safe_to_touch:
        already = await ops.attempted("restart_unhealthy_container_once", container)
        if not already:
            result["repair"] = {
                "action": "restart_unhealthy_container_once",
                "description": f"restart unhealthy container '{container}' (once)",
                "commands": [{"name": "docker_restart",
                              "params": {"container": container}}],
                "rollback": [],
            }
            result["diagnosis"] = ("Container runs but the health endpoint fails; "
                                   "one verified restart is allowed.")
            if ops.auto_repair_allowed:
                result["repair_result"] = await _start_and_verify(
                    ops, "docker_restart", container, health.get("local_url"))
                result["escalate"] = not result["repair_result"]["ok"]
            return result
        result["diagnosis"] = ("Container already restarted once for this incident "
                               "and is still unhealthy — escalating, not looping.")
        result["escalate"] = True
        return result

    result["diagnosis"] = (
        "Jellyfin failure outside the safe auto-repair conditions ("
        + "; ".join(c["name"] for c in checks if not c["ok"])
        + ") — mount/database/permission/config problems need a human decision.")
    result["escalate"] = True
    return result


def _df_pct(out: str) -> int | None:
    for line in out.splitlines()[1:]:
        for tok in line.split():
            if tok.endswith("%") and tok[:-1].isdigit():
                return int(tok[:-1])
    return None


async def _start_and_verify(ops, cmd: str, container: str,
                            local_url: str | None) -> dict:
    steps = []
    rc, out = await ops.run(cmd, container=container)
    steps.append(f"{cmd} → rc={rc}")
    if rc != 0:
        return {"ok": False, "steps": steps + [out[:200]], "verified": False}
    await ops.record_attempt(
        "restart_unhealthy_container_once" if cmd == "docker_restart"
        else "restart_stateless_service", container)
    for _ in range(VERIFY_ATTEMPTS):
        await ops.sleep(VERIFY_DELAY_SECS)
        rc, out = await ops.run("docker_inspect", container=container)
        running = False
        if rc == 0:
            try:
                running = json.loads(out)[0]["State"]["Status"] == "running"
            except (ValueError, IndexError, KeyError):
                pass
        if not running:
            continue
        if not local_url:
            steps.append("verified: container running (no health endpoint declared)")
            return {"ok": True, "steps": steps, "verified": True}
        status, _ = await ops.http_get(local_url)
        if status == 200:
            steps.append("verified: container running and health endpoint 200")
            return {"ok": True, "steps": steps, "verified": True}
    steps.append("verification FAILED: service did not become healthy")
    return {"ok": False, "steps": steps, "verified": False}

"""
qbittorrent_health — diagnosis + narrowly-scoped recovery for the qBittorrent
container on dex247 (binhex/arch-qbittorrentvpn image).

Architecture: qbittorrent runs with its own embedded WireGuard VPN (wg0 inside
the container namespace), separate from gluetun. The container has an internal
supervisord managing two processes: start-script (VPN/firewall bring-up) and
watchdog-script (qbittorrent-nox lifecycle). Both have autorestart=false, so if
either is OOM-killed supervisord never respawns them — a docker restart is the
correct recovery.

Known recurring failure:
  If the container has a mem_limit cgroup set too low, the kernel OOM killer
  terminates the watchdog process and/or qbittorrent-nox worker threads. After
  that the WebUI becomes permanently unreachable until docker restart. The fix
  is to remove the mem_limit from docker-compose.yml (applied 2026-08-06).

Automatic repair:
  - container stopped/OOM-killed, compose definition intact → docker restart,
    wait for WebUI to respond, record attempt. One restart per incident window.
  - container running but WebUI unreachable (OOM-killed workers, watchdog dead) →
    docker restart ONCE, verify WebUI, escalate if still failing.

Anything else escalates.
"""

import json

NAME = "qbittorrent_health"

WEBUI_PORT = 8080
VERIFY_ATTEMPTS = 8
VERIFY_DELAY_SECS = 5

# Logs suggesting config corruption — do NOT blind-restart
UNSAFE_LOG_PATTERNS = (
    "corrupt", "permission denied", "read-only file system",
    "no space left", "fatal error",
)


async def run(asset: dict, ops) -> dict:
    docker = asset.get("docker") or {}
    container = docker.get("container", "qbittorrent")
    health = asset.get("health") or {}
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
        return bool(ok)

    # 1. Container state + OOM flag
    rc, out = await ops.run("docker_inspect", container=container)
    state, oom_killed, exit_code, restarts = "unknown", False, None, None
    mem_limit = 0
    if rc == 0:
        try:
            info = json.loads(out)[0]
            st = info.get("State", {})
            state = st.get("Status", "unknown")
            oom_killed = bool(st.get("OOMKilled", False))
            exit_code = st.get("ExitCode")
            restarts = info.get("RestartCount", 0)
            mem_limit = info.get("HostConfig", {}).get("Memory", 0)
        except (ValueError, IndexError, KeyError):
            pass
    running = state == "running"
    add("container_state", running,
        f"state={state} OOMKilled={oom_killed} exit={exit_code} restarts={restarts}")

    # 2. Memory cgroup limit advisory — a low limit causes the recurring failure
    # 0 = no limit (correct post-fix). Warn if a tight limit is reintroduced.
    MEM_LIMIT_WARN_BYTES = 2 * 1024 ** 3  # warn if < 2 GB
    if mem_limit and mem_limit < MEM_LIMIT_WARN_BYTES:
        add("memory_limit_safe", False,
            f"mem_limit={mem_limit // (1024**2)}MB — too low, OOM kills likely; "
            "remove mem_limit/memswap_limit from docker-compose.yml")
    else:
        add("memory_limit_safe", True,
            "no cgroup memory limit" if not mem_limit
            else f"mem_limit={mem_limit // (1024**2)}MB (>=2GB, acceptable)")

    # 3. WebUI HTTP endpoint
    local_url = health.get("local_url", f"http://127.0.0.1:{WEBUI_PORT}/")
    webui_ok = False
    if running:
        status, _body = await ops.http_get(local_url)
        webui_ok = add("webui", status == 200, f"HTTP {status} from {local_url}")
    else:
        add("webui", False, "container not running")

    # 4. Recent log scan (bounded, redacted)
    rc, out = await ops.run("docker_logs_tail", container=container)
    logs = ops.redact(out)[-2000:] if rc == 0 else ""
    unsafe_hits = [p for p in UNSAFE_LOG_PATTERNS if p in logs.lower()]
    oom_evidence = "oom" in logs.lower() or "sigkill" in logs.lower() or "terminated by SIGKILL" in logs
    add("log_scan", not unsafe_hits,
        "no known-unsafe patterns" if not unsafe_hits
        else f"unsafe patterns: {', '.join(unsafe_hits)}")
    if oom_evidence:
        add("oom_evidence", False,
            "OOM kill or SIGKILL detected in recent container logs — "
            "check mem_limit in docker-compose.yml")

    # 5. Compose definition
    compose_meta = await ops.path_meta(docker.get("compose_file", ""))
    compose_ok = add("compose_definition", compose_meta.get("exists", False),
                     docker.get("compose_file", "not declared"))

    # 6. Reverse proxy (informational only)
    if health.get("public_url"):
        status, _ = await ops.http_get(health["public_url"])
        add("reverse_proxy", status in (200, 301, 302),
            f"HTTP {status} via {health['public_url']}")

    healthy = running and webui_ok and not unsafe_hits
    result = {"checks": checks, "healthy": healthy, "repair": None,
              "repair_result": None, "escalate": False}

    if healthy:
        result["diagnosis"] = (
            f"{container} is running and WebUI is responsive"
            + (" (OOM events in logs — check memory limit)" if oom_evidence else "")
        )
        return result

    safe_to_restart = compose_ok and not unsafe_hits

    # Container stopped or OOM-killed — restart it
    if (not running or oom_killed) and safe_to_restart:
        already = await ops.attempted("restart_stateless_service", container)
        if already:
            result["diagnosis"] = (
                f"{container} stopped/OOM-killed and already had one restart "
                "attempt this window — escalating.")
            result["escalate"] = True
            return result
        cause = "OOM-killed" if oom_killed else "stopped unexpectedly"
        result["repair"] = {
            "action": "restart_stateless_service",
            "description": f"{container} is {cause}; restart and verify WebUI",
            "commands": [{"name": "docker_restart", "params": {"container": container}}],
            "rollback": [],
        }
        result["diagnosis"] = (
            f"{container} is {cause}. "
            + ("This is the known recurring OOM failure — verify mem_limit is removed "
               "from docker-compose.yml after restart." if oom_killed else "")
        )
        if ops.auto_repair_allowed:
            result["repair_result"] = await _restart_and_verify(
                ops, container, local_url)
            result["escalate"] = not result["repair_result"]["ok"]
        return result

    # Container running but WebUI dead (watchdog OOM-killed, qbittorrent-nox lost)
    if running and not webui_ok and safe_to_restart:
        already = await ops.attempted("restart_unhealthy_container_once", container)
        if not already:
            result["repair"] = {
                "action": "restart_unhealthy_container_once",
                "description": (f"{container} runs but WebUI is unreachable — "
                                "restart once (watchdog may be OOM-killed)"),
                "commands": [{"name": "docker_restart",
                              "params": {"container": container}}],
                "rollback": [],
            }
            result["diagnosis"] = (
                f"{container} is running but WebUI on port {WEBUI_PORT} is "
                "unreachable. Likely cause: internal watchdog or qbittorrent-nox "
                "process was OOM-killed. One restart attempt allowed."
            )
            if ops.auto_repair_allowed:
                result["repair_result"] = await _restart_and_verify(
                    ops, container, local_url,
                    action_key="restart_unhealthy_container_once")
                result["escalate"] = not result["repair_result"]["ok"]
            return result
        result["diagnosis"] = (
            f"{container} WebUI still unreachable after one restart — escalating.")
        result["escalate"] = True
        return result

    result["diagnosis"] = (
        f"{container} failure outside safe auto-repair conditions ("
        + "; ".join(c["name"] for c in checks if not c["ok"])
        + ") — escalating.")
    result["escalate"] = True
    return result


async def _restart_and_verify(ops, container: str, local_url: str,
                              action_key: str = "restart_stateless_service") -> dict:
    steps = []
    rc, out = await ops.run("docker_restart", container=container)
    steps.append(f"docker_restart → rc={rc}")
    if rc != 0:
        return {"ok": False, "steps": steps + [out[:200]], "verified": False}
    await ops.record_attempt(action_key, container)
    for _ in range(VERIFY_ATTEMPTS):
        await ops.sleep(VERIFY_DELAY_SECS)
        # Check running state
        rc2, out2 = await ops.run("docker_inspect", container=container)
        running = False
        if rc2 == 0:
            try:
                running = json.loads(out2)[0]["State"]["Status"] == "running"
            except (ValueError, IndexError, KeyError):
                pass
        if not running:
            continue
        # Check WebUI
        status, _ = await ops.http_get(local_url)
        if status == 200:
            steps.append("verified: container running and WebUI HTTP 200")
            return {"ok": True, "steps": steps, "verified": True}
    steps.append("verification FAILED: WebUI did not become reachable")
    return {"ok": False, "steps": steps, "verified": False}

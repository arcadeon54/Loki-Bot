"""
stateless_container — generic runbook for a simple stateless docker_service
asset with no bespoke runbook of its own (e.g. cloudflare-ddns).

The ONLY automatic action is the declared policy entry
"restart_unexpectedly_stopped_container": the container is stopped, the
compose definition exists, and the restart has not already been attempted in
this window. Anything else (unhealthy-but-running, missing compose, repeated
failure) escalates instead of guessing.
"""

import json

NAME = "stateless_container"


async def run(asset: dict, ops) -> dict:
    docker = asset.get("docker") or {}
    container = docker.get("container")
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
        return bool(ok)

    rc, out = await ops.run("docker_inspect", container=container)
    state, health_str, exit_code = "unknown", "", None
    if rc == 0:
        try:
            st = json.loads(out)[0].get("State", {})
            state = st.get("Status", "unknown")
            health_str = (st.get("Health") or {}).get("Status", "")
            exit_code = st.get("ExitCode")
        except (ValueError, IndexError, KeyError):
            pass
    running = state == "running"
    add("container_state", running and health_str in ("", "healthy"),
        f"state={state} health={health_str or 'n/a'} exit={exit_code}")

    compose_meta = await ops.path_meta(docker.get("compose_file", ""))
    compose_ok = add("compose_definition", compose_meta.get("exists", False),
                     docker.get("compose_file", "not declared"))

    healthy = running and health_str in ("", "healthy")
    result = {"checks": checks, "healthy": healthy, "repair": None,
              "repair_result": None, "escalate": False}
    if healthy:
        result["diagnosis"] = f"{container} is running."
        return result

    if not running and compose_ok:
        already = await ops.attempted("restart_unexpectedly_stopped_container", container)
        if already:
            result["diagnosis"] = (f"{container} is stopped and already had one restart "
                                   "attempt in this window — escalating, not looping.")
            result["escalate"] = True
            return result
        result["repair"] = {
            "action": "restart_unexpectedly_stopped_container",
            "description": f"start stopped container '{container}'",
            "commands": [{"name": "docker_start", "params": {"container": container}}],
            "rollback": [],
        }
        result["diagnosis"] = f"{container} is stopped unexpectedly; compose definition is intact."
        if ops.auto_repair_allowed:
            rc, out = await ops.run("docker_start", container=container)
            steps = [f"docker start → rc={rc}"]
            if rc == 0:
                await ops.record_attempt("restart_unexpectedly_stopped_container", container)
                await ops.sleep(5)
                rc2, out2 = await ops.run("docker_inspect", container=container)
                verified = False
                if rc2 == 0:
                    try:
                        verified = json.loads(out2)[0]["State"]["Status"] == "running"
                    except (ValueError, IndexError, KeyError):
                        pass
                steps.append("verified: running" if verified else "verification FAILED")
                result["repair_result"] = {"ok": verified, "steps": steps, "verified": verified}
            else:
                result["repair_result"] = {"ok": False, "steps": steps, "verified": False}
            result["escalate"] = not result["repair_result"]["ok"]
        return result

    result["diagnosis"] = (f"{container} is unhealthy outside the safe auto-repair "
                           "condition (running but unhealthy, or compose missing) — "
                           "escalating.")
    result["escalate"] = True
    return result

"""
immich_status — read-only status runbook for the Immich stack on dex247.

Immich is stateful and high-risk (Postgres + uploaded originals). This runbook
NEVER repairs anything automatically: it reports per-container state, the API
ping, compose presence and disk, and every proposed action is approval-tier or
manual. Updates always require approval (registry: update_policy
approval_always).
"""

import json

NAME = "immich_status"


async def run(asset: dict, ops) -> dict:
    docker = asset.get("docker") or {}
    health = asset.get("health") or {}
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
        return bool(ok)

    all_running = True
    for container in docker.get("containers") or []:
        rc, out = await ops.run("docker_inspect", container=container)
        state, health_str = "unknown", ""
        if rc == 0:
            try:
                st = json.loads(out)[0].get("State", {})
                state = st.get("Status", "unknown")
                health_str = (st.get("Health") or {}).get("Status", "")
            except (ValueError, IndexError):
                pass
        ok = state == "running" and health_str in ("", "healthy")
        all_running = all_running and ok
        add(f"container:{container}", ok,
            f"state={state} health={health_str or 'n/a'}")

    api_ok = False
    if health.get("local_url"):
        status, body = await ops.http_get(health["local_url"])
        api_ok = add("api_ping", status == 200 and "pong" in (body or ""),
                     f"HTTP {status}")

    compose_meta = await ops.path_meta(docker.get("compose_file", ""))
    add("compose_definition", compose_meta.get("exists", False),
        docker.get("compose_file", "not declared"))

    if health.get("public_url"):
        status, _ = await ops.http_get(health["public_url"])
        add("reverse_proxy", status in (200, 301, 302, 401), f"HTTP {status} via proxy")

    healthy = all_running and api_ok
    result = {"checks": checks, "healthy": healthy, "repair": None,
              "repair_result": None, "escalate": False}
    if healthy:
        result["diagnosis"] = "Immich stack healthy: all containers running, API answering."
        return result

    # Stateful/high-risk: no automatic action exists for Immich. Anything
    # beyond re-checking needs approval (restart of the stack) or is manual
    # (database repair). Escalate with the bundle instead of guessing.
    result["diagnosis"] = (
        "Immich is degraded ("
        + "; ".join(c["name"] for c in checks if not c["ok"])
        + "). Immich is stateful/high-risk — no automatic repair; a restart or "
          "update needs your approval, and database repair is manual only.")
    result["escalate"] = True
    return result

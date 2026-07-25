"""
joplin_sync — diagnosis + retry for the Joplin note-sync path on dex247.

The stack: joplin (server) + joplin-db (Postgres) + loki-joplin-api, plus the
loki-joplin-desktop.service sidecar whose sync loop pushes notes to the
Boss's devices. The ONE automatic action here is the declared
"retry_joplin_sync": restart the sync sidecar unit when the containers and
API are healthy but the sync loop is stale or erroring — once per window,
verified afterwards. The note database itself is stateful/high-risk: any
container or Postgres problem escalates instead.
"""

import json

NAME = "joplin_sync"


async def run(asset: dict, ops) -> dict:
    docker = asset.get("docker") or {}
    unit = (asset.get("systemd") or {}).get("sync_unit", "")
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
        return bool(ok)

    containers_ok = True
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
        containers_ok = containers_ok and ok
        add(f"container:{container}", ok, f"state={state} health={health_str or 'n/a'}")

    rc, out = await ops.run("systemctl_is_active", unit=unit)
    unit_active = add("sync_unit", rc == 0 and out.strip() == "active",
                      f"{unit}: {out.strip() or 'unknown'}")

    ping = await ops.joplin_ping()
    api_ok = add("joplin_api", ping,
                 "Data API answering" if ping else "Data API not answering")

    sync = await ops.joplin_sync_health()
    sync_ok = add("sync_loop", sync.get("healthy", False),
                  sync.get("detail", "unknown"))

    healthy = containers_ok and unit_active and api_ok and sync_ok
    result = {"checks": checks, "healthy": healthy, "repair": None,
              "repair_result": None, "escalate": False}
    if healthy:
        result["diagnosis"] = "Joplin healthy: containers up, API answering, sync loop fresh."
        return result

    # The known retryable state: containers + API fine, but the sync sidecar
    # is stale/erroring or its unit fell over.
    if containers_ok and api_ok and unit and (not sync_ok or not unit_active):
        if await ops.attempted("retry_joplin_sync", unit):
            result["diagnosis"] = ("Joplin sync already retried recently and is "
                                   "still unhealthy — escalating, not looping.")
            result["escalate"] = True
            return result
        result["repair"] = {
            "action": "retry_joplin_sync",
            "description": f"restart {unit} to retry synchronisation",
            "commands": [{"name": "systemctl_restart_unit", "params": {"unit": unit}}],
            "rollback": [],
        }
        result["diagnosis"] = ("Known failure: note containers and API are "
                               "healthy but the sync sidecar is "
                               + ("stale/erroring" if unit_active else "down")
                               + " — retrying the sync service.")
        if ops.auto_repair_allowed:
            result["repair_result"] = await _restart_and_verify(ops, unit)
            result["escalate"] = not result["repair_result"]["ok"]
        return result

    result["diagnosis"] = (
        "Joplin is unhealthy at the container/database layer ("
        + "; ".join(c["name"] for c in checks if not c["ok"])
        + ") — stateful stack, no automatic action; escalating.")
    result["escalate"] = True
    return result


async def _restart_and_verify(ops, unit: str) -> dict:
    steps = []
    rc, out = await ops.run("systemctl_restart_unit", unit=unit)
    steps.append(f"systemctl restart {unit} → rc={rc}")
    if rc != 0:
        return {"ok": False, "steps": steps + [out[:200]], "verified": False}
    await ops.record_attempt("retry_joplin_sync", unit)
    # Verify: the unit is active again and the Data API still answers. A full
    # sync cycle can take minutes — freshness is re-checked on the next status
    # run, not blocked on here.
    await ops.sleep(5)
    rc, out = await ops.run("systemctl_is_active", unit=unit)
    active = rc == 0 and out.strip() == "active"
    api = await ops.joplin_ping()
    if active and api:
        steps.append("verified: sync unit active, Data API answering "
                     "(sync freshness re-checked on next status run)")
        return {"ok": True, "steps": steps, "verified": True}
    steps.append(f"verification FAILED: unit={'active' if active else out.strip()}"
                 f" api={'ok' if api else 'down'}")
    return {"ok": False, "steps": steps, "verified": False}

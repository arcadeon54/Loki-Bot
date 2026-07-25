"""
loki_interfaces — health + recovery for Loki's own in-process interface
workers (currently the Telegram long-poll task).

These workers are asyncio tasks inside the running bot, so the runbook talks
to them through the narrow hooks loki_bot binds into the controller
(interface_status / interface_restart) — no shell commands at all. The ONE
automatic action is the declared "restart_interface_worker": restart a
configured-but-dead worker and verify it came back.
"""

NAME = "loki_interfaces"

WORKERS = ("telegram",)


async def run(asset: dict, ops) -> dict:
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
        return bool(ok)

    status = await ops.interface_status()
    if status is None:
        add("interface_hooks", False,
            "not running inside the bot process — worker state unavailable")
        return {"checks": checks, "healthy": False, "repair": None,
                "repair_result": None, "escalate": True,
                "diagnosis": ("Interface workers can only be inspected from "
                              "inside the live bot process.")}

    dead = []
    for worker in WORKERS:
        w = status.get(worker) or {}
        if not w.get("configured"):
            add(f"worker:{worker}", True, "not configured (dormant by design)")
            continue
        alive = bool(w.get("alive"))
        if not alive:
            dead.append(worker)
        add(f"worker:{worker}", alive, "poll task alive" if alive
            else "poll task DEAD")

    healthy = not dead
    result = {"checks": checks, "healthy": healthy, "repair": None,
              "repair_result": None, "escalate": False}
    if healthy:
        result["diagnosis"] = "All configured interface workers are alive."
        return result

    worker = dead[0]
    if await ops.attempted("restart_interface_worker", worker):
        result["diagnosis"] = (f"The {worker} worker already got one restart "
                               "recently and died again — escalating.")
        result["escalate"] = True
        return result

    result["repair"] = {"action": "restart_interface_worker",
                        "description": f"restart the {worker} interface worker",
                        "commands": [], "rollback": []}
    result["diagnosis"] = (f"Known failure: the {worker} worker is configured "
                           "but its poll task died — restarting it.")
    if ops.auto_repair_allowed:
        result["repair_result"] = await _restart_and_verify(ops, worker)
        result["escalate"] = not result["repair_result"]["ok"]
    return result


async def _restart_and_verify(ops, worker: str) -> dict:
    steps = []
    ok = await ops.interface_restart(worker)
    steps.append(f"restart {worker} worker → {'ok' if ok else 'failed'}")
    if not ok:
        return {"ok": False, "steps": steps, "verified": False}
    await ops.record_attempt("restart_interface_worker", worker)
    await ops.sleep(2)
    status = await ops.interface_status() or {}
    alive = bool((status.get(worker) or {}).get("alive"))
    steps.append("verified: poll task alive" if alive
                 else "verification FAILED: worker did not come back")
    return {"ok": alive, "steps": steps, "verified": alive}

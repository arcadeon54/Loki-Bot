"""
filebrowser_health — diagnosis + narrowly-scoped recovery for the Filebrowser
container on dex247 (published as media.ivn-group.cc).

Filebrowser is a thin web front end over four host paths. Its own failure modes
are unremarkable; what actually took it down on 2026-08-03 was one of the paths
it publishes:

  sshfs to unicron dropped without unmounting. That leaves a *stale FUSE
  endpoint* at /mnt/unicron-downloads — the directory entry still exists but
  every operation on it returns ENOTCONN. Docker creates missing bind sources
  with mkdir, mkdir on a dead mountpoint returns EEXIST, so the bind failed
  with:

      error while creating mount source path '/mnt/unicron-downloads':
      mkdir /mnt/unicron-downloads: file exists

  The container exited 128 at create time and stayed down across reboots.
  sshfs-unicron.service could not recover either: its own mount hit the same
  ENOTCONN, so it looped (111k failed starts in one boot) while the mountpoint
  stayed wedged.

Two rules follow, and they are the point of this runbook:

  A STALE MOUNTPOINT IS NEVER RESTARTED INTO. Restarting the container re-runs
  the identical bind and fails identically. Clearing the endpoint needs root
  `fusermount3 -uz`, which is filesystem_repair — MANUAL tier. So this case
  escalates with the exact path named, and never burns an auto-repair attempt.

  A SHARE BEING DOWN IS NOT FILEBROWSER BEING DOWN. If sshfs is simply not
  mounted, the bind succeeds against an empty directory and the service is
  fine — /srv/unicron is just empty. That is reported as degraded, not failed,
  so a remote host being offline cannot masquerade as a local outage.

Automatic repair (both already AUTO-tier, both verified by HTTP):
  - container stopped, no share is stale, compose intact → restart, verify HTTP.
  - container running but HTTP unreachable → restart once, verify HTTP.
"""

import json

NAME = "filebrowser_health"

VERIFY_ATTEMPTS = 8
VERIFY_DELAY_SECS = 5

# Docker's wording when a bind source cannot be created. The stale-endpoint
# case is the "file exists" variant: mkdir on a dead FUSE mountpoint.
BIND_FAILURE_MARKERS = ("creating mount source path", "mkdir")

# Shares filebrowser publishes that live on OTHER units (sshfs, davfs, cifs).
# The local download dir and the database are checked separately — they are
# ordinary local paths and cannot go stale.
SHARE_KEYS = ("unicron", "nextcloud", "nas")


async def run(asset: dict, ops) -> dict:
    docker = asset.get("docker") or {}
    container = docker.get("container", "filebrowser")
    health = asset.get("health") or {}
    mounts = asset.get("mounts") or {}
    systemd = asset.get("systemd") or {}
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)[:400]})
        return bool(ok)

    # 1. Container state, plus the create-time error that a failed bind leaves
    #    behind. State.Error survives the exit, so the bind failure is still
    #    readable days later — that is how this outage was finally identified.
    rc, out = await ops.run("docker_inspect", container=container)
    state, exit_code, restarts, state_error, health_str = "unknown", None, None, "", ""
    if rc == 0:
        try:
            info = json.loads(out)[0]
            st = info.get("State", {})
            state = st.get("Status", "unknown")
            exit_code = st.get("ExitCode")
            state_error = st.get("Error", "") or ""
            health_str = (st.get("Health") or {}).get("Status", "")
            restarts = info.get("RestartCount", 0)
        except (ValueError, IndexError, KeyError):
            pass
    running = state == "running"
    add("container_state", running and health_str in ("", "healthy"),
        f"state={state} health={health_str or 'n/a'} exit={exit_code} "
        f"restarts={restarts}")

    bind_failed = any(m in state_error.lower() for m in BIND_FAILURE_MARKERS)
    if state_error:
        add("bind_mount_error", not bind_failed, ops.redact(state_error))

    # 2. Mount readiness, per published share. A stale endpoint is the one
    #    condition that makes a restart pointless, so it is separated from
    #    "missing" and from "mounted but empty".
    stale, unreadable = [], []
    for key in SHARE_KEYS:
        path = mounts.get(key)
        if not path:
            continue
        meta = await ops.path_meta(path)
        if meta.get("stale_mount"):
            stale.append(path)
            add(f"mount:{key}", False,
                f"{path} — STALE FUSE endpoint (ENOTCONN); Docker's bind will "
                "fail with 'file exists' until it is unmounted")
        elif not meta.get("exists"):
            unreadable.append(path)
            add(f"mount:{key}", False,
                f"{path} — {meta.get('error', 'unreadable')}")
        else:
            entries = meta.get("entries")
            add(f"mount:{key}", True,
                f"{path} — {entries if entries is not None else '?'} entries"
                + (" (empty — share may not be mounted)"
                   if meta.get("empty") else ""))

    # 3. The unit that owns the sshfs share. Informational: an inactive share
    #    unit explains an empty /srv/unicron, it does not make filebrowser
    #    unhealthy.
    share_unit = systemd.get("share_unit")
    share_unit_active = None
    if share_unit:
        rc_u, out_u = await ops.run("systemctl_is_active", unit=share_unit)
        share_unit_active = out_u.strip() == "active"
        add("share_unit", True,
            f"{share_unit} is {out_u.strip() or 'unknown'}"
            + ("" if share_unit_active
               else " — /srv/unicron will be empty until it mounts"))

    # 4. Database bind. A directory here instead of a file means Docker
    #    invented one, and filebrowser would start with an empty user table.
    db_path = mounts.get("database")
    if db_path:
        meta = await ops.path_meta(db_path)
        add("database_file", meta.get("exists", False) and meta.get("entries") is None,
            f"{db_path} — "
            + ("present" if meta.get("entries") is None
               else "IS A DIRECTORY — the database bind was auto-created"))

    # 5. Compose definition
    compose_meta = await ops.path_meta(docker.get("compose_file", ""))
    compose_ok = add("compose_definition", compose_meta.get("exists", False),
                     docker.get("compose_file", "not declared"))

    # 6. HTTP — the only proof the service actually works.
    local_url = health.get("local_url", "http://127.0.0.1:8090/")
    http_ok = False
    if running:
        status, _ = await ops.http_get(local_url)
        http_ok = add("http_local", status == 200, f"HTTP {status} from {local_url}")
    else:
        add("http_local", False, "container not running")

    if health.get("public_url"):
        status, _ = await ops.http_get(health["public_url"])
        add("reverse_proxy", status in (200, 301, 302),
            f"HTTP {status} via {health['public_url']}")

    healthy = running and http_ok and not stale
    result = {"checks": checks, "healthy": healthy, "repair": None,
              "repair_result": None, "escalate": False}

    if healthy:
        degraded = ""
        if share_unit_active is False:
            degraded = (f" {share_unit} is not active, so /srv/unicron is empty "
                        "— the remote share is down, filebrowser is not.")
        result["diagnosis"] = (
            f"{container} is running and serving HTTP 200.{degraded}")
        return result

    # A stale endpoint cannot be restarted out of, and clearing it is
    # filesystem_repair (MANUAL). Say exactly what is wedged and stop.
    if stale:
        result["diagnosis"] = (
            f"{container} cannot bind {', '.join(stale)}: the mountpoint is a "
            "stale FUSE endpoint (ENOTCONN), so Docker's mkdir returns EEXIST "
            "and the bind fails with 'file exists'. Restarting repeats the same "
            "failure. Clearing it needs root `fusermount3 -u -z <path>` — "
            "filesystem_repair, which I must not do automatically. Once the "
            "mountpoint is clear the container starts normally.")
        result["escalate"] = True
        return result

    if not compose_ok:
        result["diagnosis"] = (
            f"{container} is unhealthy and its compose file is missing "
            f"({docker.get('compose_file')}) — not restarting blind.")
        result["escalate"] = True
        return result

    if not running:
        already = await ops.attempted("restart_stateless_service", container)
        if already:
            result["diagnosis"] = (
                f"{container} is stopped and already had one restart attempt "
                "this window — escalating.")
            result["escalate"] = True
            return result
        result["repair"] = {
            "action": "restart_stateless_service",
            "description": (f"{container} is stopped with every bind source "
                            "readable; restart and verify HTTP"),
            "commands": [{"name": "docker_restart", "params": {"container": container}}],
            "rollback": [],
        }
        result["diagnosis"] = (
            f"{container} is {state} (exit {exit_code}). No published share is "
            "stale, so the binds can succeed — restarting.")
        if ops.auto_repair_allowed:
            result["repair_result"] = await _restart_and_verify(
                ops, container, local_url)
            result["escalate"] = not result["repair_result"]["ok"]
        return result

    # Running but not answering.
    already = await ops.attempted("restart_unhealthy_container_once", container)
    if not already:
        result["repair"] = {
            "action": "restart_unhealthy_container_once",
            "description": (f"{container} runs but HTTP is unreachable — "
                            "restart once and verify"),
            "commands": [{"name": "docker_restart", "params": {"container": container}}],
            "rollback": [],
        }
        result["diagnosis"] = (
            f"{container} is running but {local_url} does not answer. "
            "One restart attempt allowed.")
        if ops.auto_repair_allowed:
            result["repair_result"] = await _restart_and_verify(
                ops, container, local_url,
                action_key="restart_unhealthy_container_once")
            result["escalate"] = not result["repair_result"]["ok"]
        return result

    result["diagnosis"] = (
        f"{container} still unreachable after one restart ("
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
        # A bind that is still broken fails here, not at the HTTP check.
        return {"ok": False, "steps": steps + [ops.redact(out)[:200]],
                "verified": False}
    await ops.record_attempt(action_key, container)
    for _ in range(VERIFY_ATTEMPTS):
        await ops.sleep(VERIFY_DELAY_SECS)
        rc2, out2 = await ops.run("docker_inspect", container=container)
        running = False
        if rc2 == 0:
            try:
                running = json.loads(out2)[0]["State"]["Status"] == "running"
            except (ValueError, IndexError, KeyError):
                pass
        if not running:
            continue
        status, _ = await ops.http_get(local_url)
        if status == 200:
            steps.append("verified: container running and HTTP 200")
            return {"ok": True, "steps": steps, "verified": True}
    steps.append("verification FAILED: HTTP did not recover")
    return {"ok": False, "steps": steps, "verified": False}

"""
homelab_api.py — read-only HTTP facade over the homelab maintenance controller.

This is the "restricted maintenance interface" the Hermes escalation specialist
on razr talks to. It is deliberately thin: every route reuses the existing
controller (homelab_maintenance) with allow_repairs=False, so the repair-class
command guard, the registry-derived parameter allowlist and the redaction rules
are exactly the ones already covered by the controller's own tests. Nothing
here can change system state.

Boundaries:
  - Bound to the Tailscale address only, bearer-authed, and additionally
    restricted to razr's address. dex247 is the only place this runs and razr
    is the only client.
  - Assets and containers are addressed by registry key. A name that is not in
    config/homelab_assets.yml does not resolve, so no caller can name an
    interface, container, or path the registry has not declared.
  - Read-only by construction: Ops(allow_repairs=False) raises PolicyError on
    any repair-class command, and no route asks for one.
  - Approval-gated and manual-tier actions are not reachable from here at all;
    Hermes proposes, and Loki's existing draft gate is what stages anything.

Run as its own service (loki-homelab-api.service) so activating it never
requires restarting loki.service.
"""

import asyncio
import json
import logging
import os
import socket

from aiohttp import web

import homelab_assets  # noqa: F401  (import validates the registry loads)
import homelab_maintenance as hm
import maintenance_policy as policy

log = logging.getLogger("HomelabAPI")

BIND = os.getenv("HOMELAB_API_BIND", "100.68.187.69")   # dex247 Tailscale
PORT = int(os.getenv("HOMELAB_API_PORT", "8785"))
TOKEN = os.getenv("HOMELAB_API_TOKEN", "")
# Only razr may call this.
ALLOWED = {c.strip() for c in os.getenv(
    "HOMELAB_API_ALLOWED_CLIENTS", "100.87.97.120").split(",") if c.strip()}
LOG_LINE_CAP = int(os.getenv("HOMELAB_API_LOG_LINES", "80"))


# ── Auth / access ──────────────────────────────────────────────────────────
@web.middleware
async def guard(request: web.Request, handler):
    peer = request.remote or ""
    if ALLOWED and peer not in ALLOWED:
        log.warning("rejected client %s (not allowlisted)", peer)
        return web.json_response({"error": "client address not permitted"}, status=403)
    if not TOKEN:
        return web.json_response({"error": "server has no token configured"}, status=503)
    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {TOKEN}"
    if len(auth) != len(expected) or not _consteq(auth, expected):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        return await handler(request)
    except policy.PolicyError as e:
        return web.json_response({"error": hm.redact(str(e))}, status=400)
    except Exception:
        log.exception("unhandled error on %s", request.path)
        return web.json_response({"error": "internal error"}, status=500)


def _consteq(a: str, b: str) -> bool:
    diff = 0
    for x, y in zip(a, b):
        diff |= ord(x) ^ ord(y)
    return diff == 0


# ── Helpers ────────────────────────────────────────────────────────────────
def _asset_or_404(key: str):
    asset = hm._reg().get(key) or hm._reg().resolve(key)
    if asset is None:
        raise web.HTTPNotFound(
            text=json.dumps({"error": f"unknown asset '{key}'",
                             "known": sorted(hm._reg().assets)}),
            content_type="application/json")
    return asset


def _container_or_404(name: str):
    reg = hm._reg()
    for asset in reg.assets.values():
        if name in reg.containers(asset):
            return name, asset
    raise web.HTTPNotFound(
        text=json.dumps({"error": f"container '{name}' is not in the asset registry"}),
        content_type="application/json")


def _ro_ops():
    """A read-only Ops. Repair-class commands raise before spawning."""
    return hm.Ops(allow_repairs=False)


def _checks(result):
    return [{"name": c["name"], "ok": c["ok"], "detail": hm.redact(c["detail"])[:300]}
            for c in result.get("checks", [])]


# ── Routes ─────────────────────────────────────────────────────────────────
async def health(request):
    reg = hm._reg()
    return web.json_response({
        "ok": True,
        "host": socket.gethostname(),
        "read_only": True,
        "assets": sorted(reg.assets),
        "runbooks": {k: a.get("runbook") for k, a in reg.assets.items()},
    })


async def asset_status(request):
    asset = _asset_or_404(request.match_info["key"])
    result, _ops = await hm.run_runbook(asset, allow_repairs=False)
    return web.json_response({
        "asset": asset["key"],
        "display_name": asset.get("display_name"),
        "healthy": result.get("healthy", False),
        "diagnosis": hm.redact(result.get("diagnosis", ""))[:600],
        "runbook": result.get("runbook"),
        "escalate": result.get("escalate", False),
        "checks": _checks(result),
    })


async def asset_diagnose(request):
    """Re-run the registered runbook read-only. Identical guarantees to
    asset_status; exposed separately so the tool name reads honestly."""
    return await asset_status(request)


async def asset_network(request):
    asset = _asset_or_404(request.match_info["key"])
    if asset.get("type") != "wireless_access_point":
        return web.json_response(
            {"error": f"'{asset['key']}' is not a network asset",
             "type": asset.get("type")}, status=400)
    result, _ops = await hm.run_runbook(asset, allow_repairs=False)
    net = asset.get("network") or {}
    return web.json_response({
        "asset": asset["key"],
        "healthy": result.get("healthy", False),
        "topology": {
            "wireless_interface": net.get("wireless_interface"),
            "vpn_interface": net.get("vpn_interface"),
            "client_subnet": net.get("client_subnet"),
            "routing_mark": net.get("routing_mark"),
            "routing_table": net.get("routing_table"),
            "rule_priority": net.get("rule_priority"),
        },
        "checks": _checks(result),
    })


async def asset_disk(request):
    asset = _asset_or_404(request.match_info["key"])
    ops = _ro_ops()
    mounts = asset.get("mounts") or {}
    paths = [p for p in [mounts.get("config")] + list(mounts.get("media") or []) if p]
    out = []
    for p in paths:
        rc, text = await ops.run("df_path", path=p)
        out.append({"path": p, "ok": rc == 0,
                    "df": hm.redact(text).strip().splitlines()[-1] if rc == 0 else "unavailable"})
    return web.json_response({"asset": asset["key"], "filesystems": out})


async def asset_mounts(request):
    asset = _asset_or_404(request.match_info["key"])
    ops = _ro_ops()
    mounts = asset.get("mounts") or {}
    config_path = mounts.get("config")
    out = []
    for p in [x for x in [config_path] + list(mounts.get("media") or []) if x]:
        meta = await ops.path_meta(p)
        out.append({
            "path": p,
            "exists": bool(meta.get("exists")),
            "empty": bool(meta.get("empty", True)),
            "mode": meta.get("mode"),
            "uid": meta.get("uid"),
            "gid": meta.get("gid"),
            "entries": meta.get("entries"),
            "role": "config" if p == config_path else "media",
        })
    return web.json_response({"asset": asset["key"], "mounts": out})


async def asset_http(request):
    asset = _asset_or_404(request.match_info["key"])
    ops = _ro_ops()
    health_cfg = asset.get("health") or {}
    out = {}
    for label in ("local_url", "public_url"):
        url = health_cfg.get(label)
        if not url:
            continue
        status, body = await ops.http_get(url)
        out[label] = {"status": status, "ok": status in (200, 301, 302, 401),
                      "body_snippet": hm.redact(str(body))[:120]}
    if not out:
        return web.json_response({"asset": asset["key"],
                                  "error": "no health endpoint declared"}, status=400)
    return web.json_response({"asset": asset["key"], "endpoints": out})


async def container_inspect(request):
    name, asset = _container_or_404(request.match_info["name"])
    ops = _ro_ops()
    rc, text = await ops.run("docker_inspect", container=name)
    if rc != 0:
        return web.json_response({"container": name, "error": "not inspectable"}, status=502)
    try:
        info = json.loads(text)[0]
    except (ValueError, IndexError):
        return web.json_response({"container": name, "error": "bad inspect output"}, status=502)
    st = info.get("State", {})
    cfg = info.get("Config", {})
    return web.json_response({
        "container": name,
        "asset": asset["key"],
        "state": st.get("Status"),
        "health": (st.get("Health") or {}).get("Status"),
        "exit_code": st.get("ExitCode"),
        "started_at": st.get("StartedAt"),
        "restart_count": info.get("RestartCount"),
        "image": cfg.get("Image"),
        "compose_project": (cfg.get("Labels") or {}).get("com.docker.compose.project"),
        "ports": info.get("HostConfig", {}).get("PortBindings") or {},
        "mounts": [{"source": m.get("Source"), "destination": m.get("Destination"),
                    "rw": m.get("RW")} for m in info.get("Mounts", [])],
        "stateful": bool(asset.get("stateful")),
    })


async def container_logs(request):
    name, asset = _container_or_404(request.match_info["name"])
    try:
        want = int(request.query.get("lines", LOG_LINE_CAP))
    except ValueError:
        want = LOG_LINE_CAP
    want = max(1, min(LOG_LINE_CAP, want))
    ops = _ro_ops()
    rc, text = await ops.run("docker_logs_tail", container=name)
    if rc != 0:
        return web.json_response({"container": name, "error": "logs unavailable"}, status=502)
    lines = hm.redact(text).splitlines()[-want:]
    return web.json_response({
        "container": name, "asset": asset["key"],
        "lines": len(lines), "redacted": True,
        "log": "\n".join(lines)[:hm.LOG_EXCERPT_CAP],
    })


async def updates(request):
    reg = hm._reg()
    service = (request.query.get("service") or "").strip()
    targets = []
    for asset in reg.assets.values():
        for c in reg.containers(asset):
            if not service or c == service:
                targets.append((c, asset))
    if not targets:
        return web.json_response({"error": "service not in the asset registry"}, status=404)
    ops = _ro_ops()
    out = []
    for container, asset in targets:
        try:
            out.append(await hm._update_status_for(container, asset, ops))
        except policy.PolicyError as e:
            out.append({"container": container, "error": hm.redact(str(e))})
    return web.json_response({"read_only": True, "updated_nothing": True, "services": out})


def make_app() -> web.Application:
    app = web.Application(middlewares=[guard])
    app.add_routes([
        web.get("/v1/health", health),
        web.get("/v1/asset/{key}/status", asset_status),
        web.post("/v1/asset/{key}/diagnose", asset_diagnose),
        web.get("/v1/asset/{key}/network", asset_network),
        web.get("/v1/asset/{key}/disk", asset_disk),
        web.get("/v1/asset/{key}/mounts", asset_mounts),
        web.get("/v1/asset/{key}/http", asset_http),
        web.get("/v1/container/{name}/inspect", container_inspect),
        web.get("/v1/container/{name}/logs", container_logs),
        web.get("/v1/updates", updates),
    ])
    return app


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if not TOKEN:
        raise SystemExit("HOMELAB_API_TOKEN must be set")
    hm._reg()  # fail fast if the registry is broken
    hm.bind()  # no interface hooks: this process is not the bot
    log.info("homelab read-only API on %s:%s (clients: %s)",
             BIND, PORT, ", ".join(sorted(ALLOWED)) or "any")
    web.run_app(make_app(), host=BIND, port=PORT, print=None)


if __name__ == "__main__":
    main()

"""
homelab_assets.py — loader for the private homelab asset registry.

config/homelab_assets.yml is the single source of truth for which assets the
maintenance controller knows about: names/aliases, which runbook applies, and
the EXACT interfaces, containers, files, marks and tables that commands are
allowed to touch. Everything the policy allowlist accepts as a parameter value
is derived from this file — a value that isn't declared here can never reach a
shell command. No credentials live here, ever.

Adding an asset requires only a YAML entry (plus a runbook module if it needs
one); no Python source changes.
"""

import logging
import os
import re
import socket

import yaml

log = logging.getLogger("HomelabAssets")

REGISTRY_PATH = os.getenv(
    "HOMELAB_ASSETS_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "config", "homelab_assets.yml"))

VALID_TYPES = {"wireless_access_point", "docker_service", "docker_stack"}

_IFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class RegistryError(Exception):
    pass


def _norm(text: str) -> str:
    """Alias normalization: lowercase, collapse runs of non-alphanumerics."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


class Registry:
    def __init__(self, data: dict, path: str = REGISTRY_PATH):
        self.path = path
        self.assets: dict[str, dict] = {}
        self._alias_index: dict[str, str] = {}
        assets = (data or {}).get("assets") or {}
        if not isinstance(assets, dict) or not assets:
            raise RegistryError("registry has no assets")
        for key, asset in assets.items():
            self._validate(key, asset)
            asset = dict(asset)
            asset["key"] = key
            self.assets[key] = asset
            for alias in [key, asset.get("display_name", "")] + list(asset.get("aliases") or []):
                n = _norm(str(alias))
                if n:
                    self._alias_index[n] = key

    @staticmethod
    def _validate(key: str, asset: dict):
        if not isinstance(asset, dict):
            raise RegistryError(f"asset '{key}' is not a mapping")
        if not _NAME_RE.match(key or ""):
            raise RegistryError(f"bad asset key '{key}'")
        if asset.get("type") not in VALID_TYPES:
            raise RegistryError(f"asset '{key}' has unknown type {asset.get('type')!r}")
        if not asset.get("host"):
            raise RegistryError(f"asset '{key}' has no host")
        net = asset.get("network") or {}
        for f in ("wireless_interface", "vpn_interface"):
            v = net.get(f)
            if v is not None and not _IFACE_RE.match(str(v)):
                raise RegistryError(f"asset '{key}': bad interface {v!r}")
        docker = asset.get("docker") or {}
        containers = [docker.get("container")] if docker.get("container") \
            else list(docker.get("containers") or [])
        for c in containers:
            if c is not None and not _NAME_RE.match(str(c)):
                raise RegistryError(f"asset '{key}': bad container name {c!r}")

    # ── lookup ────────────────────────────────────────────────────────────
    def get(self, key: str) -> dict | None:
        return self.assets.get(key)

    def resolve(self, text: str) -> dict | None:
        """Map a user phrase ('BLACK-BOXX', 'my AP', 'jellyfin') to an asset."""
        n = _norm(text)
        if not n:
            return None
        key = self._alias_index.get(n)
        return self.assets.get(key) if key else None

    def containers(self, asset: dict) -> list[str]:
        docker = asset.get("docker") or {}
        if docker.get("container"):
            return [docker["container"]]
        return list(docker.get("containers") or [])

    def on_this_host(self, asset: dict) -> bool:
        return socket.gethostname() == asset.get("host")

    # ── policy inputs ─────────────────────────────────────────────────────
    def allowed_values(self) -> dict[str, set]:
        """Every parameter value a shell command may carry, derived strictly
        from the registry. The policy allowlist rejects anything else."""
        vals = {"iface": set(), "container": set(), "compose_file": set(),
                "path": set(), "image": set(), "num": set(),
                "probe_ip": {"1.1.1.1", "9.9.9.9"}}
        for asset in self.assets.values():
            net = asset.get("network") or {}
            for f in ("wireless_interface", "vpn_interface"):
                if net.get(f):
                    vals["iface"].add(str(net[f]))
            for f in ("routing_mark", "routing_table", "rule_priority"):
                if net.get(f) is not None:
                    vals["num"].add(str(int(net[f])))
            if net.get("dhcp_leases_file"):
                vals["path"].add(str(net["dhcp_leases_file"]))
            docker = asset.get("docker") or {}
            for c in self.containers(asset):
                vals["container"].add(c)
            if docker.get("compose_file"):
                vals["compose_file"].add(str(docker["compose_file"]))
                vals["path"].add(str(docker["compose_file"]))
            if docker.get("image"):
                vals["image"].add(str(docker["image"]))
            mounts = asset.get("mounts") or {}
            for v in mounts.values():
                for p in (v if isinstance(v, list) else [v]):
                    vals["path"].add(str(p))
        return vals


_cached: Registry | None = None


def load(path: str = REGISTRY_PATH, force: bool = False) -> Registry:
    global _cached
    if _cached is not None and not force and _cached.path == path:
        return _cached
    with open(path) as f:
        data = yaml.safe_load(f)
    _cached = Registry(data, path)
    log.info("homelab asset registry loaded: %s", ", ".join(sorted(_cached.assets)))
    return _cached

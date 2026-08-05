"""
homelab_lifecycle.py — durable lifecycle state for every container Loki can
see, plus the decommission/tombstone registry.

Why this exists: the asset registry (config/homelab_assets.yml) answers "which
assets do I have runbooks for". It does NOT answer "is this container supposed
to be running right now". Before this module, anything not in the asset
registry that was found stopped by the container sweep became an incident and
was escalated to Hermes — including containers the Boss had just told Loki to
obliterate. Conversational intent never reached operational state.

The rules, enforced here rather than in a prompt:

  - Every container resolves to exactly one lifecycle state: managed,
    unmanaged, ignored, expected_stopped, decommission_pending_cleanup, or
    decommissioned.
  - ONLY `managed` + expected-to-be-running may open a stopped/unhealthy
    incident. Everything else is inventory, never an outage.
  - Hermes is never reached for a suppressed asset. A Boss request is the only
    thing that can send an unmanaged/decommissioned asset to Hermes, and that
    request is carried explicitly in the call (`user_requested=True`).
  - Decommission (state change + suppression) and cleanup (deleting things)
    are separate operations with separate authority. Decommission is
    non-destructive and applies the moment the Boss says so; cleanup goes
    through the existing draft-approval gate.
  - A tombstone is never deleted. After cleanup completes the record stays so
    the container can never be rediscovered as an unknown asset.

Durability: the authoritative store is the `asset_lifecycle` table in
homelab_incidents.db (same file the maintenance controller and monitor use, so
state survives a Loki restart). config/homelab_lifecycle.yml is a rendered
mirror written after every change — human-readable and versionable, never read
back as truth.

No credentials or private content are ever stored here: container/project
names, paths, states, and reasons only, all redacted on the way in.
"""

import asyncio
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import time
from typing import Optional

import yaml

import homelab_assets
import homelab_maintenance as hm
import maintenance_policy as policy
import tools
from tools import ToolContext, ToolSpec, register, user_level

log = logging.getLogger("HomelabLifecycle")

_HERE = os.path.dirname(os.path.abspath(__file__))
MIRROR_PATH = os.getenv("HOMELAB_LIFECYCLE_MIRROR",
                        os.path.join(_HERE, "config", "homelab_lifecycle.yml"))
ARCHIVE_DIR = os.getenv("HOMELAB_DECOMMISSION_ARCHIVE_DIR",
                        os.path.join(os.path.expanduser("~"), "backups",
                                     "decommission"))
# The reverse proxy whose on-disk config is scanned (read-only) for references
# to a container being decommissioned. Discovery degrades to "unknown" when it
# isn't running — it never blocks or guesses.
PROXY_CONTAINER = os.getenv("HOMELAB_PROXY_CONTAINER", "nginx-proxy-manager")
PROXY_CONF_DIR = "/data/nginx/proxy_host"

# How often a decommission_pending_cleanup asset may remind the Boss that its
# cleanup is still waiting for approval. Deliberately slow — a pending cleanup
# is not an outage.
CLEANUP_REMINDER_SECS = int(os.getenv("LIFECYCLE_CLEANUP_REMINDER_SECS", "86400"))

# Archive caps: a decommission archive keeps compose/config metadata, not a
# site's whole content tree.
ARCHIVE_MAX_FILES = 40
ARCHIVE_MAX_BYTES = 2 * 1024 * 1024

# ── Lifecycle states ───────────────────────────────────────────────────────
MANAGED = "managed"
UNMANAGED = "unmanaged"
IGNORED = "ignored"
EXPECTED_STOPPED = "expected_stopped"
DECOMMISSION_PENDING_CLEANUP = "decommission_pending_cleanup"
DECOMMISSIONED = "decommissioned"

STATES = (MANAGED, UNMANAGED, IGNORED, EXPECTED_STOPPED,
          DECOMMISSION_PENDING_CLEANUP, DECOMMISSIONED)

# States that may never produce an outage incident. `unmanaged` is in here on
# purpose: an unknown container being down is inventory, not an emergency.
NO_INCIDENT_STATES = frozenset({UNMANAGED, IGNORED, EXPECTED_STOPPED,
                                DECOMMISSION_PENDING_CLEANUP, DECOMMISSIONED})
# States Hermes may never be invoked for without an explicit Boss request.
NO_AUTO_HERMES_STATES = NO_INCIDENT_STATES
# States that carry a tombstone — never deleted, even after cleanup completes.
TOMBSTONE_STATES = frozenset({DECOMMISSION_PENDING_CLEANUP, DECOMMISSIONED})

# Cleanup status values.
CLEANUP_NOT_REQUIRED = "not_required"
CLEANUP_PENDING_APPROVAL = "pending_approval"
CLEANUP_IN_PROGRESS = "in_progress"
CLEANUP_COMPLETED = "completed"
CLEANUP_FAILED = "failed"

INTENTIONAL_DECOMMISSION = "intentional_decommission"

enabled = True


class LifecycleError(Exception):
    pass


# ── Durable store ──────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS asset_lifecycle (
    name                      TEXT PRIMARY KEY,
    aliases_json              TEXT NOT NULL DEFAULT '[]',
    host                      TEXT NOT NULL DEFAULT '',
    state                     TEXT NOT NULL,
    expected_running          INTEGER NOT NULL DEFAULT 1,
    marked_at                 REAL NOT NULL,
    updated_at                REAL NOT NULL,
    source_interface          TEXT NOT NULL DEFAULT '',
    authorized_by             TEXT NOT NULL DEFAULT '',
    authorized_role           TEXT NOT NULL DEFAULT '',
    reason                    TEXT NOT NULL DEFAULT '',
    monitoring_suppressed     INTEGER NOT NULL DEFAULT 0,
    cleanup_status            TEXT NOT NULL DEFAULT 'not_required',
    cleanup_scope_json        TEXT NOT NULL DEFAULT 'null',
    compose_project           TEXT NOT NULL DEFAULT '',
    compose_file              TEXT NOT NULL DEFAULT '',
    proxy_refs_json           TEXT NOT NULL DEFAULT '[]',
    dns_refs_json             TEXT NOT NULL DEFAULT '[]',
    prior_incidents_json      TEXT NOT NULL DEFAULT '[]',
    cleanup_verification_json TEXT NOT NULL DEFAULT 'null',
    cleanup_completed_at      REAL,
    archive_path              TEXT NOT NULL DEFAULT '',
    tombstone_retained        INTEGER NOT NULL DEFAULT 0,
    reappeared_notified_at    REAL,
    reminder_last_at          REAL
);
CREATE TABLE IF NOT EXISTS lifecycle_requests (
    request_id     TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    requested_state TEXT NOT NULL,
    requester_id   TEXT NOT NULL DEFAULT '',
    requester_name TEXT NOT NULL DEFAULT '',
    requester_role TEXT NOT NULL DEFAULT '',
    reason         TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL,
    status         TEXT NOT NULL DEFAULT 'awaiting_boss'
);
"""

_COLUMNS = (
    "name", "aliases_json", "host", "state", "expected_running", "marked_at",
    "updated_at", "source_interface", "authorized_by", "authorized_role",
    "reason", "monitoring_suppressed", "cleanup_status", "cleanup_scope_json",
    "compose_project", "compose_file", "proxy_refs_json", "dns_refs_json",
    "prior_incidents_json", "cleanup_verification_json", "cleanup_completed_at",
    "archive_path", "tombstone_retained", "reappeared_notified_at",
    "reminder_last_at",
)


def _db():
    conn = hm._db()
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _row(r) -> dict:
    d = dict(r)
    d["aliases"] = json.loads(d.pop("aliases_json") or "[]")
    d["proxy_refs"] = json.loads(d.pop("proxy_refs_json") or "[]")
    d["dns_refs"] = json.loads(d.pop("dns_refs_json") or "[]")
    d["prior_incidents"] = json.loads(d.pop("prior_incidents_json") or "[]")
    d["cleanup_scope"] = json.loads(d.pop("cleanup_scope_json") or "null")
    d["cleanup_verification"] = json.loads(d.pop("cleanup_verification_json") or "null")
    d["monitoring_suppressed"] = bool(d["monitoring_suppressed"])
    d["expected_running"] = bool(d["expected_running"])
    d["tombstone_retained"] = bool(d["tombstone_retained"])
    return d


def get(name: str) -> Optional[dict]:
    r = _db().execute("SELECT * FROM asset_lifecycle WHERE name=?",
                      (str(name or ""),)).fetchone()
    return _row(r) if r else None


def records() -> list[dict]:
    return [_row(r) for r in _db().execute(
        "SELECT * FROM asset_lifecycle ORDER BY name").fetchall()]


def tombstones() -> list[dict]:
    return [r for r in records() if r["state"] in TOMBSTONE_STATES]


def _norm(text: str) -> str:
    return homelab_assets._norm(text)


def resolve(text: str) -> Optional[dict]:
    """Find a lifecycle record by canonical name or any recorded alias."""
    n = _norm(text)
    if not n:
        return None
    for rec in records():
        if _norm(rec["name"]) == n:
            return rec
        if any(_norm(a) == n for a in rec["aliases"]):
            return rec
    return None


def _write(name: str, **fields):
    """Insert-or-update one lifecycle record, then refresh the YAML mirror."""
    now = time.time()
    fields["updated_at"] = now
    existing = _db().execute("SELECT name FROM asset_lifecycle WHERE name=?",
                             (name,)).fetchone()
    if existing is None:
        fields.setdefault("marked_at", now)
        fields.setdefault("state", UNMANAGED)
        cols = ["name"] + list(fields)
        _db().execute(
            f"INSERT INTO asset_lifecycle ({','.join(cols)}) VALUES "
            f"({','.join('?' * len(cols))})", [name, *fields.values()])
    else:
        bad = set(fields) - set(_COLUMNS)
        if bad:
            raise LifecycleError(f"unknown lifecycle column(s): {sorted(bad)}")
        sets = ", ".join(f"{k}=?" for k in fields)
        _db().execute(f"UPDATE asset_lifecycle SET {sets} WHERE name=?",
                      [*fields.values(), name])
    _db().commit()
    _export_mirror()
    return get(name)


def _export_mirror():
    """Render the durable table to a readable, committable YAML file. This is a
    mirror, never a source of truth — nothing reads it back."""
    try:
        payload = {
            "# note": "GENERATED MIRROR of the asset_lifecycle table in "
                      "homelab_incidents.db. Edit lifecycle state through Loki, "
                      "not this file.",
            "version": 1,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "assets": {},
        }
        for rec in records():
            payload["assets"][rec["name"]] = {
                "state": rec["state"],
                "aliases": rec["aliases"],
                "host": rec["host"],
                "expected_running": rec["expected_running"],
                "monitoring_suppressed": rec["monitoring_suppressed"],
                "marked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                           time.gmtime(rec["marked_at"])),
                "source_interface": rec["source_interface"],
                "authorized_role": rec["authorized_role"],
                "reason": rec["reason"],
                "cleanup_status": rec["cleanup_status"],
                "cleanup_scope": rec["cleanup_scope"],
                "compose_project": rec["compose_project"],
                "compose_file": rec["compose_file"],
                "proxy_refs": rec["proxy_refs"],
                "dns_refs": rec["dns_refs"],
                "prior_incidents": rec["prior_incidents"],
                "cleanup_verification": rec["cleanup_verification"],
                "archive_path": rec["archive_path"],
                "tombstone_retained": rec["tombstone_retained"],
            }
        os.makedirs(os.path.dirname(MIRROR_PATH), exist_ok=True)
        tmp = MIRROR_PATH + ".tmp"
        with open(tmp, "w") as f:
            yaml.safe_dump(payload, f, sort_keys=False, default_flow_style=False)
        os.replace(tmp, MIRROR_PATH)
    except Exception:
        log.exception("lifecycle mirror export failed (state itself is safe in SQLite)")


# ── Classification ─────────────────────────────────────────────────────────
def _registry_containers() -> dict[str, str]:
    """name -> asset key for everything the asset registry declares: each
    declared container AND each asset key itself. The asset keys matter because
    registry assets like the AP or the interface workers have no container at
    all — without them those assets would classify as `unmanaged` and lose
    their escalation path entirely."""
    out = {}
    try:
        reg = hm._reg()
    except Exception:
        return out
    for key, asset in reg.assets.items():
        out[key] = key
        for c in reg.containers(asset):
            out[c] = key
    return out


def classify(name: str) -> dict:
    """The single answer to 'what is this container to me'. An explicit
    lifecycle record always wins over registry membership — that is what lets
    the Boss decommission a registry-backed asset."""
    rec = get(name)
    if rec is not None:
        return {"name": name, "state": rec["state"],
                "expected_running": rec["expected_running"],
                "monitoring_suppressed": rec["monitoring_suppressed"],
                "source": "lifecycle_registry", "record": rec,
                "asset_key": None, "cleanup_status": rec["cleanup_status"]}
    asset_key = _registry_containers().get(name)
    if asset_key:
        return {"name": name, "state": MANAGED, "expected_running": True,
                "monitoring_suppressed": False, "source": "asset_registry",
                "record": None, "asset_key": asset_key,
                "cleanup_status": CLEANUP_NOT_REQUIRED}
    return {"name": name, "state": UNMANAGED, "expected_running": False,
            "monitoring_suppressed": False, "source": "default",
            "record": None, "asset_key": None,
            "cleanup_status": CLEANUP_NOT_REQUIRED}


def incident_allowed(name: str) -> tuple[bool, str]:
    """May a stopped/unhealthy observation of `name` open an outage incident?
    Only managed + expected-running assets qualify."""
    c = classify(name)
    if c["state"] in NO_INCIDENT_STATES:
        return False, f"{name} is {c['state']} — not an outage"
    if not c["expected_running"]:
        return False, f"{name} is not expected to be running"
    if c["monitoring_suppressed"]:
        return False, f"monitoring is suppressed for {name}"
    return True, ""


def hermes_allowed(name: str, user_requested: bool = False) -> tuple[bool, str]:
    """May this asset be escalated to Hermes? Suppressed and unmanaged assets
    only ever reach Hermes when the Boss explicitly asked for an investigation
    ('investigate this unknown container', 'is this safe to remove')."""
    c = classify(name)
    if c["state"] in NO_AUTO_HERMES_STATES and not user_requested:
        return False, (f"{name} is {c['state']} — Hermes is not called for this "
                       f"without an explicit request from you")
    if c["state"] in TOMBSTONE_STATES:
        return False, f"{name} was intentionally decommissioned — nothing to diagnose"
    return True, ""


def suppressed_names() -> set[str]:
    return {r["name"] for r in records()
            if r["monitoring_suppressed"] or r["state"] in NO_INCIDENT_STATES}


# ── Sweep classification ───────────────────────────────────────────────────
def classify_sweep(rows: list[tuple[str, str]],
                   covered_elsewhere: Optional[set] = None) -> dict:
    """Sort one `docker ps -a` snapshot into the buckets the sweep may act on.

    `rows` is [(container_name, status_string)]. `covered_elsewhere` names the
    containers that already have their own runbook-driven monitor check, so the
    sweep defers to that rather than double-reporting them. A registry asset
    with NO dedicated check is deliberately not in that set — it is managed and
    expected to run, so the sweep is the only thing that would notice it
    stopping.

    Returns buckets keyed by what the sweep is allowed to DO with them, not by
    what they are, so the caller cannot accidentally treat inventory as an
    outage."""
    covered = covered_elsewhere or set()
    incidents, unmanaged, suppressed, reappeared, pending_cleanup = [], [], [], [], []
    for name, status in rows:
        is_up = status.startswith("Up")
        is_unhealthy = "(unhealthy)" in status
        bad = (not is_up) or is_unhealthy
        c = classify(name)
        entry = {"name": name, "status": status, "state": c["state"],
                 "source": c["source"], "asset_key": c["asset_key"]}

        if c["state"] == DECOMMISSIONED and is_up:
            # A tombstoned container is running again. Report once; never
            # delete it automatically — something recreated it on purpose.
            reappeared.append(entry)
            continue
        if c["state"] == DECOMMISSION_PENDING_CLEANUP:
            if is_up:
                reappeared.append(entry)
            else:
                pending_cleanup.append(entry)
            continue
        if not bad:
            continue
        if c["state"] in NO_INCIDENT_STATES:
            (unmanaged if c["state"] == UNMANAGED else suppressed).append(entry)
            continue
        if name in covered:
            continue   # its own runbook-driven check owns this one
        ok, _why = incident_allowed(name)
        (incidents if ok else suppressed).append(entry)
    return {"incidents": incidents, "unmanaged": unmanaged,
            "suppressed": suppressed, "reappeared": reappeared,
            "pending_cleanup": pending_cleanup}


def mark_reappearance_notified(name: str):
    _write(name, reappeared_notified_at=time.time())


def needs_reappearance_notice(name: str) -> bool:
    rec = get(name)
    return bool(rec) and not rec.get("reappeared_notified_at")


def due_for_cleanup_reminder(name: str) -> bool:
    rec = get(name)
    if not rec or rec["state"] != DECOMMISSION_PENDING_CLEANUP:
        return False
    last = rec.get("reminder_last_at") or rec["marked_at"]
    return (time.time() - last) >= CLEANUP_REMINDER_SECS


def mark_reminder_sent(name: str):
    _write(name, reminder_last_at=time.time())


# ── Live-system authorization for policy parameters ────────────────────────
# Values only ever come from an allowlisted, read-only command's output — never
# from a model or a user string. A container the Boss names is resolved against
# the live `docker ps -a` list before its name can appear in any command.
async def _ps_rows(ops: hm.Ops) -> list[dict]:
    rc, out = await ops.run("docker_ps_projects")
    if rc != 0:
        raise LifecycleError(f"could not list containers (rc={rc})")
    rows = []
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        rows.append({"name": parts[0], "project": parts[1],
                     "image": parts[2], "status": parts[3]})
    return rows


async def _ps_field(ops: hm.Ops, command: str) -> dict[str, str]:
    rc, out = await ops.run(command)
    if rc != 0:
        return {}
    out_map = {}
    for line in out.strip().splitlines():
        if "\t" not in line:
            continue
        name, value = line.split("\t", 1)
        out_map[name] = value
    return out_map


def _authorize(cls: str, values) -> set:
    vals = {str(v) for v in values if v}
    if vals:
        policy.add_runtime_values(cls, vals)
    return vals


async def resolve_target(text: str, ops: Optional[hm.Ops] = None) -> tuple[Optional[str], str]:
    """Map a Boss phrase ('IVN site', 'the old ivn-group project') to exactly
    one real container name. Ambiguity is an error, never a guess."""
    raw = str(text or "").strip()
    if not raw:
        return None, "no asset named"
    ops = ops or hm.Ops(allow_repairs=False)
    try:
        rows = await _ps_rows(ops)
    except LifecycleError as e:
        return None, str(e)
    names = [r["name"] for r in rows]

    if raw in names:
        return raw, ""
    n = _norm(raw)
    exact = [x for x in names if _norm(x) == n]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return None, f"ambiguous — matches {', '.join(sorted(exact))}"

    # Compose project name ("delete the old ivn-group project").
    by_project = sorted({r["name"] for r in rows if r["project"] and _norm(r["project"]) == n})
    if len(by_project) == 1:
        return by_project[0], ""
    if len(by_project) > 1:
        return None, (f"'{raw}' is a compose project with {len(by_project)} containers "
                      f"({', '.join(by_project)}) — name the one you mean")

    # A tombstone whose container is already gone still resolves.
    rec = resolve(raw)
    if rec is not None:
        return rec["name"], ""

    partial = sorted({x for x in names if n and n in _norm(x)})
    if len(partial) == 1:
        return partial[0], ""
    if len(partial) > 1:
        return None, f"ambiguous — could be {', '.join(partial)}"
    return None, f"no container matches '{raw[:60]}'"


# ── Read-only cleanup discovery ────────────────────────────────────────────
async def discover(container: str, ops: Optional[hm.Ops] = None) -> dict:
    """Build the exact inventory for one container: what it owns outright and
    what it merely shares. Strictly read-only — nothing here can change state.

    Anything shared with another container is recorded as shared and is never
    a deletion candidate, no matter what the Boss authorized: permission to
    obliterate a site is not permission to delete the proxy's network."""
    ops = ops or hm.Ops(allow_repairs=False)
    rows = await _ps_rows(ops)
    row = next((r for r in rows if r["name"] == container), None)
    scope: dict = {
        "container": container, "exists": row is not None,
        "status": row["status"] if row else "absent",
        "compose_project": row["project"] if row else "",
        "image": row["image"] if row else "",
        "compose_file": "", "compose_working_dir": "", "compose_service": "",
        "project_siblings": [], "bind_mounts": [], "named_volumes": [],
        "networks": [], "images": [], "config_paths": [], "proxy_refs": [],
        "dns_refs": [], "dependent_containers": [], "shared": [],
        "notes": [],
    }
    if row is None:
        scope["notes"].append("container no longer present on this host")
        return scope

    _authorize("container", {container})

    # Compose metadata comes from the container's OWN labels, not from a guess.
    rc, out = await ops.run("docker_inspect", container=container)
    labels, mounts = {}, []
    if rc == 0:
        try:
            info = json.loads(out)[0]
            labels = (info.get("Config") or {}).get("Labels") or {}
            mounts = info.get("Mounts") or []
        except (ValueError, IndexError, KeyError, TypeError):
            scope["notes"].append("could not parse docker inspect output")
    scope["compose_project"] = labels.get("com.docker.compose.project", scope["compose_project"])
    scope["compose_service"] = labels.get("com.docker.compose.service", "")
    scope["compose_working_dir"] = labels.get("com.docker.compose.project.working_dir", "")
    compose_file = (labels.get("com.docker.compose.project.config_files") or "").split(",")[0].strip()
    if compose_file and os.path.isfile(compose_file):
        scope["compose_file"] = compose_file
    elif compose_file:
        scope["notes"].append("compose file recorded on the container no longer exists")

    # Siblings in the same compose project must come down together or not at all.
    if scope["compose_project"]:
        scope["project_siblings"] = sorted(
            r["name"] for r in rows
            if r["project"] == scope["compose_project"] and r["name"] != container)

    # Mounts: bind mounts are files/dirs on the host; named volumes may be shared.
    vol_usage = await _ps_field(ops, "docker_ps_mounts")
    for m in mounts:
        if m.get("Type") == "volume" and m.get("Name"):
            users = sorted(n for n, v in vol_usage.items()
                           if m["Name"] in [p.strip() for p in v.split(",")]
                           and n != container)
            entry = {"name": m["Name"], "destination": m.get("Destination", ""),
                     "shared_with": users, "shared": bool(users)}
            scope["named_volumes"].append(entry)
            if users:
                scope["shared"].append({"kind": "volume", "name": m["Name"],
                                        "used_by": users})
        elif m.get("Source"):
            scope["bind_mounts"].append({"source": m["Source"],
                                         "destination": m.get("Destination", ""),
                                         "rw": bool(m.get("RW"))})

    # Networks: any network with another member is shared and is never removed.
    net_usage = await _ps_field(ops, "docker_ps_networks")
    own_nets = [x.strip() for x in (net_usage.get(container) or "").split(",") if x.strip()]
    for net in own_nets:
        members = sorted(n for n, v in net_usage.items()
                         if net in [p.strip() for p in v.split(",")] and n != container)
        scope["networks"].append({"name": net, "members": members,
                                  "shared": bool(members)})
        if members:
            scope["shared"].append({"kind": "network", "name": net, "used_by": members})

    # Image: shared when any other container runs it.
    if scope["image"]:
        users = sorted(r["name"] for r in rows
                       if r["image"] == scope["image"] and r["name"] != container)
        scope["images"].append({"name": scope["image"], "used_by": users,
                                "shared": bool(users)})
        if users:
            scope["shared"].append({"kind": "image", "name": scope["image"],
                                    "used_by": users})

    # Config directory (the compose working dir) — archived, never deleted here.
    wd = scope["compose_working_dir"]
    if wd and os.path.isdir(wd):
        scope["config_paths"].append(wd)

    scope["proxy_refs"] = await _discover_proxy_refs(container, ops)
    scope["dns_refs"] = _discover_dns_refs(scope)
    scope["dependent_containers"] = scope["project_siblings"]
    return scope


async def _discover_proxy_refs(container: str, ops: hm.Ops) -> list[dict]:
    """Read-only scan of the reverse proxy's generated config for references to
    this container. Returns [] when the proxy isn't running — 'unknown' is
    reported in notes rather than assumed to be 'none'."""
    try:
        rows = await _ps_rows(ops)
    except LifecycleError:
        return []
    proxy = next((r for r in rows if r["name"] == PROXY_CONTAINER), None)
    if proxy is None or not proxy["status"].startswith("Up"):
        return [{"status": "unknown",
                 "detail": f"{PROXY_CONTAINER} not running — proxy config not scanned"}]
    _authorize("container", {PROXY_CONTAINER, container})
    rc, out = await ops.run("proxy_conf_grep", container=PROXY_CONTAINER,
                            container2=container)
    if rc not in (0, 1):
        return [{"status": "unknown", "detail": f"proxy scan failed (rc={rc})"}]
    hits = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
    return [{"status": "found", "config": h} for h in hits]


def _discover_dns_refs(scope: dict) -> list[dict]:
    """Hostnames this project actually SERVES, taken from the directives that
    declare them — nginx `server_name`, and compose VIRTUAL_HOST / Traefik
    Host() rules. Deliberately not a free-text scan of the project: a prose
    mention in a README is not a DNS record, and a list padded with false
    positives is worse than a short accurate one.

    These are REPORTED for manual review only. Loki holds no DNS credential
    and never removes a DNS record itself — and here the zone is shared with
    every other service behind the same proxy."""
    refs: list[dict] = []
    wd = scope.get("compose_working_dir") or ""
    if not wd or not os.path.isdir(wd):
        return refs
    import re
    host_shape = re.compile(r"^(?:[a-z0-9][a-z0-9-]*\.)+[a-z]{2,}$", re.I)
    patterns = (
        (re.compile(r"^\s*server_name\s+([^;]+);", re.I | re.M), "nginx server_name"),
        (re.compile(r"VIRTUAL_HOST[=:]\s*['\"]?([^'\"\s]+)", re.I), "VIRTUAL_HOST"),
        (re.compile(r"Host\(([^)]*)\)", re.I), "traefik Host rule"),
    )
    seen: set[str] = set()
    for fname in ("nginx.conf", "docker-compose.yml", "docker-compose.yaml"):
        path = os.path.join(wd, fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, errors="replace") as f:
                text = f.read(64 * 1024)
        except OSError:
            continue
        for rx, source in patterns:
            for match in rx.findall(text):
                for token in re.split(r"[\s,]+", match.replace("`", "").replace("'", "")):
                    host = token.strip().strip('"').lower()
                    # '_' is nginx's catch-all server_name, not a hostname.
                    if not host or host == "_" or host in seen:
                        continue
                    if not host_shape.match(host):
                        continue
                    seen.add(host)
                    refs.append({"host": host, "source": f"{fname} ({source})",
                                 "action": "manual review — Loki does not touch DNS"})
    return refs[:20]


# ── Cleanup planning ───────────────────────────────────────────────────────
def plan_cleanup(scope: dict) -> dict:
    """Turn a discovered scope into an explicit remove / retain / archive /
    skipped-shared / manual plan. Every resource in the scope lands in exactly
    one bucket — nothing is left implicit."""
    remove, retain, archive, skipped, manual = [], [], [], [], []
    container = scope["container"]

    if scope["exists"]:
        if scope.get("compose_file") and not scope.get("project_siblings"):
            remove.append({"kind": "compose_project", "name": scope["compose_project"],
                           "detail": f"docker compose -f {scope['compose_file']} down "
                                     f"(no -v, no --rmi, no --remove-orphans)"})
        else:
            remove.append({"kind": "container", "name": container,
                           "detail": f"docker rm {container}"})
        if scope.get("project_siblings"):
            skipped.append({"kind": "compose_project",
                            "name": scope.get("compose_project", ""),
                            "used_by": scope["project_siblings"],
                            "detail": "project has other containers — only the named "
                                      "container is removed"})
    else:
        retain.append({"kind": "container", "name": container,
                       "detail": "already absent — nothing to remove"})

    for img in scope.get("images", []):
        if img["shared"]:
            skipped.append({"kind": "image", "name": img["name"],
                            "used_by": img["used_by"]})
        else:
            remove.append({"kind": "image", "name": img["name"],
                           "detail": f"docker image rm {img['name']}"})

    for vol in scope.get("named_volumes", []):
        if vol["shared"]:
            skipped.append({"kind": "volume", "name": vol["name"],
                            "used_by": vol["shared_with"]})
        else:
            remove.append({"kind": "volume", "name": vol["name"],
                           "detail": f"docker volume rm {vol['name']}"})

    for net in scope.get("networks", []):
        if net["shared"]:
            skipped.append({"kind": "network", "name": net["name"],
                            "used_by": net["members"],
                            "detail": "shared network — never removed"})
        else:
            retain.append({"kind": "network", "name": net["name"],
                           "detail": "compose removes its own project network; no "
                                     "explicit network deletion is ever issued"})

    for path in scope.get("config_paths", []):
        archive.append({"kind": "config", "name": path,
                        "detail": "compose/config metadata copied to the decommission "
                                  "archive"})
        retain.append({"kind": "directory", "name": path,
                       "detail": "left on disk — deleting a project directory is "
                                 "MANUAL-tier and is never automated"})

    for bind in scope.get("bind_mounts", []):
        retain.append({"kind": "bind_mount", "name": bind["source"],
                       "detail": "host path left untouched"})

    for ref in scope.get("proxy_refs", []):
        manual.append({"kind": "proxy", "name": ref.get("config", "unknown"),
                       "detail": "remove the proxy host in the proxy UI — Loki has "
                                 "no credentialed proxy API"})
    for ref in scope.get("dns_refs", []):
        manual.append({"kind": "dns", "name": ref.get("host", ""),
                       "detail": "review in Cloudflare — Loki never edits DNS, and "
                                 "this zone is shared with other services"})

    return {"remove": remove, "retain": retain, "archive": archive,
            "skipped_shared": skipped, "manual_follow_up": manual,
            "backup": [{"kind": "archive_dir", "name": ARCHIVE_DIR}]}


def plan_hash(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


def fmt_plan(plan: dict) -> str:
    def block(title, items):
        if not items:
            return f"{title}: none"
        return f"{title}:\n" + "\n".join(
            f"  • {i.get('kind')} {i.get('name')}"
            + (f" — {i['detail']}" if i.get("detail") else "")
            + (f" (used by {', '.join(i['used_by'])})" if i.get("used_by") else "")
            for i in items)
    return "\n".join([
        block("REMOVE", plan["remove"]),
        block("ARCHIVE FIRST", plan["archive"]),
        block("RETAIN", plan["retain"]),
        block("SKIPPED (shared)", plan["skipped_shared"]),
        block("MANUAL FOLLOW-UP", plan["manual_follow_up"]),
    ])


# ── Decommission (non-destructive; applies immediately) ────────────────────
async def mark(name: str, state: str, *, ctx: Optional[ToolContext] = None,
               reason: str = "", scope: Optional[dict] = None,
               role: str = "boss") -> dict:
    """Apply a lifecycle state. For the tombstone states this ALSO suppresses
    monitoring and reconciles open incidents immediately — deliberately before
    (and independently of) any destructive cleanup, so a decommissioned asset
    stops alerting the moment the Boss says so."""
    if state not in STATES:
        raise LifecycleError(f"unknown lifecycle state '{state}'")
    suppress = state in NO_INCIDENT_STATES
    expected_running = state == MANAGED
    interface = ""
    if ctx is not None:
        interface = "telegram" if str(ctx.channel_id).startswith("tg:") else "discord"

    fields = {
        "state": state,
        "expected_running": int(expected_running),
        "monitoring_suppressed": int(suppress),
        "reason": hm.redact(str(reason))[:400],
        "source_interface": interface,
        "authorized_by": str(getattr(ctx, "user_name", "") or "")[:64],
        "authorized_role": role,
        "host": os.uname().nodename,
        "tombstone_retained": int(state in TOMBSTONE_STATES),
    }
    if state in TOMBSTONE_STATES:
        fields["marked_at"] = time.time()
    if scope is not None:
        fields["cleanup_scope_json"] = json.dumps(scope)
        fields["compose_project"] = str(scope.get("compose_project", ""))[:200]
        fields["compose_file"] = str(scope.get("compose_file", ""))[:300]
        fields["proxy_refs_json"] = json.dumps(scope.get("proxy_refs", []))
        fields["dns_refs_json"] = json.dumps(scope.get("dns_refs", []))
    if state == DECOMMISSION_PENDING_CLEANUP:
        fields["cleanup_status"] = CLEANUP_PENDING_APPROVAL
    elif state == DECOMMISSIONED and (get(name) or {}).get("cleanup_status") in (
            None, "", CLEANUP_NOT_REQUIRED):
        fields["cleanup_status"] = CLEANUP_NOT_REQUIRED

    rec = _write(name, **fields)
    if suppress:
        closed = reconcile_incidents(name)
        if closed["closed_incidents"] or closed["cancelled_tasks"]:
            merged = sorted(set((rec.get("prior_incidents") or [])
                                + closed["closed_incidents"]))
            rec = _write(name, prior_incidents_json=json.dumps(merged))
        rec["reconciliation"] = closed
    log.info("lifecycle: %s -> %s (suppressed=%s)", name, state, suppress)
    return rec


def add_aliases(name: str, aliases: list[str]) -> dict:
    rec = get(name) or {}
    merged = sorted({a.strip() for a in (list(rec.get("aliases") or []) + list(aliases))
                     if a and a.strip()})
    return _write(name, aliases_json=json.dumps(merged))


# ── Incident reconciliation ────────────────────────────────────────────────
def _cancel_task_quietly(task_id: str) -> bool:
    """Cancel a queued/running supervised task without emitting its own
    Telegram announcement — the caller reports the whole decommission in one
    message instead. Uses the supervisor's own last_announced de-dup field,
    which exists precisely to make 'already announced' authoritative."""
    try:
        import task_supervisor as ts
    except Exception:
        return False
    row = ts.get_task(task_id)
    if not row or row["status"] in ts.TERMINAL:
        return False
    try:
        ts._update(task_id, last_announced="cancelled")
        run = ts._running.get(task_id)
        if run is not None:
            run.handle.cancel_event.set()
            return True
        loop = asyncio.get_event_loop()
        loop.create_task(ts._finalize(
            task_id, ts.TaskResult("cancelled",
                                   summary="asset intentionally decommissioned")))
        return True
    except Exception:
        log.exception("quiet cancel of task %s failed", task_id)
        return False


def reconcile_incidents(name: str) -> dict:
    """Close every active incident that exists only because this asset was
    down, and stop any diagnostic escalation that hasn't finished. History is
    preserved — incidents are CLOSED with an explicit resolution reason, never
    deleted."""
    now = time.time()
    conn = _db()
    # The monitor owns the monitor_incidents/monitor_checks tables; importing
    # it here (lazily, so there is no import cycle) guarantees its schema
    # exists even when reconciliation happens before the first poll.
    try:
        import homelab_monitor
        homelab_monitor._db()
    except Exception:
        log.debug("monitor schema unavailable during reconciliation", exc_info=True)
    closed, cancelled, skipped = [], [], []
    like = f"%{name}%"

    # Monitor incidents: either keyed on the asset, or (the container-sweep
    # case) keyed on the sweep but opened solely because of this container.
    try:
        # Active means "not closed" — the monitor keeps escalated/given-up
        # incidents active until health recovers, and a decommission must
        # close those too, not just the ones still in 'open'.
        rows = conn.execute(
            "SELECT * FROM monitor_incidents WHERE closed_at IS NULL AND "
            "status IN ('open','escalated','gave_up') AND "
            "(key=? OR display_name=? OR detection_json LIKE ? OR evidence_json LIKE ?)",
            (name, name, like, like)).fetchall()
        for r in rows:
            conn.execute(
                "UPDATE monitor_incidents SET status='resolved', result=?, closed_at=?,"
                " updated_at=? WHERE incident_id=?",
                (INTENTIONAL_DECOMMISSION, now, now, r["incident_id"]))
            closed.append(r["incident_id"])
            if r["escalated_task_id"] and _cancel_task_quietly(r["escalated_task_id"]):
                cancelled.append(r["escalated_task_id"])
        # Clear the sweep's failure streak so the same container cannot re-trip it.
        conn.execute(
            "UPDATE monitor_checks SET consecutive_failures=0, open_incident_id=NULL,"
            " cooldown_until=0 WHERE open_incident_id IN "
            "(SELECT incident_id FROM monitor_incidents WHERE result=?)",
            (INTENTIONAL_DECOMMISSION,))
        conn.execute(
            "UPDATE monitor_checks SET consecutive_failures=0 WHERE key='container-sweep'")
    except sqlite3.OperationalError:
        # Monitor tables not created yet (monitor never imported) — there can
        # be no monitor incidents to reconcile in that case.
        skipped.append("monitor_incidents (schema absent)")

    # Maintenance-controller incidents (the /diagnose + Hermes side).
    terminal = ("healthy", "repaired", "closed_intentional_decommission")
    rows = conn.execute(
        "SELECT * FROM incidents WHERE status NOT IN (?,?,?) AND "
        "(asset=? OR symptom LIKE ? OR diagnosis LIKE ?)",
        (*terminal, name, like, like)).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE incidents SET status='closed_intentional_decommission',"
            " updated_at=? WHERE incident_id=?", (now, r["incident_id"]))
        closed.append(r["incident_id"])
        for tid in (r["hermes_task_id"], r["task_id"]):
            if tid and _cancel_task_quietly(tid):
                cancelled.append(tid)
    conn.commit()

    return {"closed_incidents": closed, "cancelled_tasks": sorted(set(cancelled)),
            "skipped": skipped, "resolution": INTENTIONAL_DECOMMISSION}


def hermes_result_actionable(incident_id: str) -> bool:
    """False once an incident has been closed as an intentional decommission.
    A Hermes job that lands after that point is audit history, not an
    operational signal — it must not reopen anything or notify again."""
    if not incident_id:
        return True
    inc = hm.get_incident(incident_id)
    if inc and inc.get("status") == "closed_intentional_decommission":
        return False
    r = _db().execute(
        "SELECT result FROM monitor_incidents WHERE incident_id=?",
        (incident_id,)).fetchone()
    return not (r and r["result"] == INTENTIONAL_DECOMMISSION)


# ── Cleanup execution (only ever via an approved draft) ────────────────────
async def _archive(scope: dict, container: str) -> tuple[str, list[str]]:
    """Copy compose/config metadata into the decommission archive before
    anything is removed. Bounded in file count and size: this is a metadata
    archive, not a backup of a site's content."""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    dest = os.path.join(ARCHIVE_DIR, f"{container}-{stamp}")

    def _copy():
        copied = []
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "scope.json"), "w") as f:
            json.dump(scope, f, indent=2, sort_keys=True)
        copied.append("scope.json")
        wd = scope.get("compose_working_dir") or ""
        budget = ARCHIVE_MAX_BYTES
        if wd and os.path.isdir(wd):
            for entry in sorted(os.listdir(wd))[:ARCHIVE_MAX_FILES]:
                src = os.path.join(wd, entry)
                if not os.path.isfile(src):
                    continue
                try:
                    size = os.path.getsize(src)
                except OSError:
                    continue
                if size > budget:
                    continue
                budget -= size
                shutil.copy2(src, os.path.join(dest, entry))
                copied.append(entry)
        return copied

    copied = await asyncio.to_thread(_copy)
    return dest, copied


async def execute_cleanup(name: str, plan: dict, scope: dict) -> dict:
    """Run an APPROVED cleanup plan. Only the exact resources the plan lists as
    REMOVE are touched; every shared resource was already excluded at planning
    time and is re-checked here before anything runs."""
    ops = hm.Ops(allow_repairs=True)
    steps: list[str] = []
    _write(name, cleanup_status=CLEANUP_IN_PROGRESS)

    before = {r["name"]: r["status"] for r in await _ps_rows(ops)}

    # 1. Archive first — metadata is preserved even if a later step fails.
    archive_path, copied = await _archive(scope, name)
    steps.append(f"archived {len(copied)} file(s) -> {archive_path}")

    # 2/3. Remove exactly what was approved, re-verifying non-sharedness.
    fresh = await discover(name, ops)
    for item in plan["remove"]:
        kind, target = item["kind"], item["name"]
        if kind == "compose_project":
            compose_file = scope.get("compose_file", "")
            if not compose_file or not os.path.isfile(compose_file):
                steps.append(f"SKIP compose down — {compose_file} missing")
                continue
            _authorize("compose_file", {compose_file})
            _authorize("path", {compose_file})
            rc, _out = await ops.run("compose_down_project", compose_file=compose_file)
            steps.append(f"compose down ({scope.get('compose_project')}) -> rc={rc}")
        elif kind == "container":
            if not fresh["exists"]:
                steps.append(f"container {target} already absent")
                continue
            _authorize("container", {target})
            rc, _out = await ops.run("docker_container_rm", container=target)
            steps.append(f"docker rm {target} -> rc={rc}")
        elif kind == "image":
            live = next((i for i in fresh.get("images", []) if i["name"] == target), None)
            if live and live["shared"]:
                steps.append(f"SKIP image {target} — now shared with "
                             f"{', '.join(live['used_by'])}")
                continue
            _authorize("image", {target})
            rc, _out = await ops.run("docker_image_rm", image=target)
            steps.append(f"docker image rm {target} -> rc={rc}")
        elif kind == "volume":
            live = next((v for v in fresh.get("named_volumes", []) if v["name"] == target), None)
            if live and live["shared"]:
                steps.append(f"SKIP volume {target} — now shared with "
                             f"{', '.join(live['shared_with'])}")
                continue
            _authorize("volume", {target})
            rc, _out = await ops.run("docker_volume_rm", volume=target)
            steps.append(f"docker volume rm {target} -> rc={rc}")

    # 5. Verify nothing unrelated moved.
    after = {r["name"]: r["status"] for r in await _ps_rows(ops)}
    collateral = [n for n, st in before.items()
                  if n != name and n not in (scope.get("project_siblings") or [])
                  and after.get(n) != st]
    gone = [n for n in before if n != name and n not in after]
    verification = {
        "ok": not collateral and not gone,
        "target_removed": name not in after,
        "unrelated_changed": collateral,
        "unrelated_removed": gone,
        "containers_before": len(before), "containers_after": len(after),
    }

    status = CLEANUP_COMPLETED if verification["ok"] else CLEANUP_FAILED
    _write(name, cleanup_status=status, state=DECOMMISSIONED,
           monitoring_suppressed=1, expected_running=0, tombstone_retained=1,
           archive_path=archive_path, cleanup_completed_at=time.time(),
           cleanup_verification_json=json.dumps(verification))
    return {"steps": steps, "verification": verification,
            "archive_path": archive_path, "cleanup_status": status}


async def write_joplin_record(name: str, rec: dict, plan: dict, result: dict):
    """Concise decommission record. No secrets, no raw logs."""
    try:
        import joplin_integration as jp
    except Exception:
        return
    if not jp.is_configured():
        return
    lines = [
        f"# Decommissioned — {name}",
        "",
        f"- State: {rec.get('state')}",
        f"- Marked: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(rec.get('marked_at') or time.time()))}",
        f"- Authorized by: {rec.get('authorized_role', 'boss')} via {rec.get('source_interface') or 'chat'}",
        f"- Reason: {hm.redact(rec.get('reason', ''))[:300]}",
        f"- Compose project: {rec.get('compose_project') or 'n/a'}",
        f"- Cleanup status: {result.get('cleanup_status', rec.get('cleanup_status'))}",
        f"- Archive: {result.get('archive_path', rec.get('archive_path')) or 'n/a'}",
        f"- Verification: {'no unrelated service affected' if (result.get('verification') or {}).get('ok') else 'NOT confirmed — review'}",
        f"- Tombstone retained: yes (prevents rediscovery as an unknown container)",
        "",
        "## Plan",
        "",
        "```",
        fmt_plan(plan)[:2000],
        "```",
    ]
    try:
        await jp.create_note(title=f"Decommissioned — {name}",
                             body="\n".join(lines),
                             notebook="Loki/Homelab Incidents")
    except Exception:
        log.exception("Joplin decommission record failed")


# ── Tools ──────────────────────────────────────────────────────────────────
def _role(ctx: ToolContext) -> str:
    return user_level(ctx.user_id)


async def _tool_decommission(args: dict, ctx: ToolContext) -> str:
    """Boss says an asset is done: mark it, suppress monitoring, close the
    incidents it caused, and PREPARE (never run) the destructive cleanup."""
    role = _role(ctx)
    target_text = str(args.get("asset", ""))
    permanent = bool(args.get("permanent", True))
    reason = str(args.get("reason", ""))

    ops = hm.Ops(allow_repairs=False)
    name, err = await resolve_target(target_text, ops)
    if err:
        return json.dumps({"ok": False, "error": err})

    if role != "boss":
        rid = "lr_" + hashlib.sha256(f"{name}{time.time()}".encode()).hexdigest()[:10]
        _db().execute(
            "INSERT INTO lifecycle_requests (request_id, name, requested_state,"
            " requester_id, requester_name, requester_role, reason, created_at,"
            " status) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, name, DECOMMISSION_PENDING_CLEANUP, str(ctx.user_id),
             ctx.user_name, role, hm.redact(reason)[:300], time.time(),
             "awaiting_boss"))
        _db().commit()
        return json.dumps({
            "ok": False, "recorded_request": rid, "asset": name,
            "error": "decommissioning needs the Boss's approval",
            "note": (f"Request logged for {name}. Nothing has changed — tell the "
                     f"user the Boss has to confirm this one.")})

    scope = await discover(name, ops)
    state = DECOMMISSION_PENDING_CLEANUP if permanent else EXPECTED_STOPPED
    rec = await mark(name, state, ctx=ctx, reason=reason, scope=scope, role=role)
    add_aliases(name, [target_text])

    plan = plan_cleanup(scope)
    _write(name, cleanup_scope_json=json.dumps(scope))
    recon = rec.get("reconciliation") or {}

    out = {
        "ok": True, "asset": name, "state": state,
        "monitoring_suppressed": True,
        "hermes_suppressed": True,
        "closed_incidents": recon.get("closed_incidents", []),
        "cancelled_escalations": recon.get("cancelled_tasks", []),
        "cleanup_plan": plan,
        "cleanup_plan_text": fmt_plan(plan),
        "cleanup_requires_approval": True,
        "shared_resources_excluded": plan["skipped_shared"],
        "say_now": (
            f"{name} is now marked permanently decommissioned and excluded from "
            f"monitoring and automatic repair. I found its remaining "
            f"container/resources and prepared a targeted cleanup. I will not "
            f"touch shared resources."
            if permanent else
            f"{name} is now marked as intentionally stopped — it won't raise "
            f"outage incidents or get escalated."),
        "note": ("State is already persisted and monitoring is already off — that "
                 "part is done and needs no approval. The DESTRUCTIVE cleanup is "
                 "separate: call homelab_decommission_cleanup to stage it as an "
                 "approval draft. Summarize say_now plus what the cleanup would "
                 "remove; do not claim anything has been deleted."),
    }
    return json.dumps(out)


async def _tool_lifecycle_set(args: dict, ctx: ToolContext) -> str:
    """Non-destructive lifecycle changes that are not decommissioning:
    'ignore this container', 'this is intentionally stopped', 'watch this one'."""
    state = str(args.get("state", "")).strip()
    if state not in (IGNORED, EXPECTED_STOPPED, MANAGED, UNMANAGED):
        return json.dumps({"ok": False,
                           "error": f"state must be one of: {IGNORED}, "
                                    f"{EXPECTED_STOPPED}, {MANAGED}, {UNMANAGED}"})
    name, err = await resolve_target(str(args.get("asset", "")))
    if err:
        return json.dumps({"ok": False, "error": err})
    rec = await mark(name, state, ctx=ctx, reason=str(args.get("reason", "")),
                     role=_role(ctx))
    return json.dumps({
        "ok": True, "asset": name, "state": rec["state"],
        "monitoring_suppressed": rec["monitoring_suppressed"],
        "closed_incidents": (rec.get("reconciliation") or {}).get("closed_incidents", []),
        "note": "Lifecycle state persisted; it survives a Loki restart."})


async def _tool_lifecycle_list(args: dict, ctx: ToolContext) -> str:
    recs = records()
    reqs = [dict(r) for r in _db().execute(
        "SELECT * FROM lifecycle_requests WHERE status='awaiting_boss' "
        "ORDER BY created_at DESC LIMIT 10").fetchall()]
    return json.dumps({
        "ok": True,
        "assets": [{"name": r["name"], "state": r["state"],
                    "monitoring_suppressed": r["monitoring_suppressed"],
                    "cleanup_status": r["cleanup_status"],
                    "reason": r["reason"][:120],
                    "marked": time.strftime("%Y-%m-%d", time.gmtime(r["marked_at"]))}
                   for r in recs],
        "pending_requests": [{"request_id": r["request_id"], "asset": r["name"],
                              "by": r["requester_name"], "role": r["requester_role"],
                              "reason": r["reason"][:120]} for r in reqs],
        "note": "Tombstones are retained permanently so a decommissioned asset can "
                "never be rediscovered as an unknown container."})


async def _tool_cleanup(args: dict, ctx: ToolContext) -> str:
    """Stage the destructive cleanup of an already-decommissioned asset as an
    approval draft. Never runs anything itself."""
    name, err = await resolve_target(str(args.get("asset", "")))
    if err:
        return json.dumps({"ok": False, "error": err})
    rec = get(name)
    if rec is None or rec["state"] not in TOMBSTONE_STATES:
        return json.dumps({
            "ok": False,
            "error": f"{name} is not marked for decommissioning yet — decommission "
                     f"it first (that part is non-destructive and instant)"})
    scope = await discover(name)
    plan = plan_cleanup(scope)
    _write(name, cleanup_scope_json=json.dumps(scope))
    return await tools.execute(
        "homelab_apply_decommission_cleanup",
        json.dumps({"asset": name, "plan_hash": plan_hash(plan)}), ctx)


def _cleanup_prepare(args: dict, ctx: ToolContext):
    name = str(args.get("asset", "")).strip()
    rec = get(name)
    if rec is None:
        return {}, "", "no lifecycle record for that asset"
    if rec["state"] not in TOMBSTONE_STATES:
        return {}, "", f"{name} is {rec['state']}, not marked for decommissioning"
    scope = rec.get("cleanup_scope")
    if not scope:
        return {}, "", "no discovered cleanup scope — re-run the decommission first"
    plan = plan_cleanup(scope)
    if plan_hash(plan) != args.get("plan_hash"):
        return {}, "", ("the discovered resources changed since this plan was built "
                        "— re-run homelab_decommission_cleanup")
    summary = (f"Decommission cleanup — {name}\n{fmt_plan(plan)}\n"
               f"Shared resources are excluded and nothing is pruned.")[:400]
    return {"asset": name, "plan_hash": args.get("plan_hash")}, summary, ""


async def _cleanup_handler(payload: dict, ctx: ToolContext) -> str:
    """Runs only via tools.run_approved() after the Boss approves the draft."""
    name = payload.get("asset", "")
    rec = get(name)
    if rec is None:
        raise KeyError("lifecycle record vanished")
    scope = rec.get("cleanup_scope")
    if not scope:
        raise ValueError("cleanup scope missing — refusing to execute")
    plan = plan_cleanup(scope)
    if plan_hash(plan) != payload.get("plan_hash"):
        raise ValueError("cleanup plan hash mismatch — refusing to execute")
    result = await execute_cleanup(name, plan, scope)
    fresh = get(name)
    await write_joplin_record(name, fresh, plan, result)
    v = result["verification"]
    tail = ("no unrelated service was affected" if v["ok"] else
            f"⚠️ verify by hand — unrelated changes: {v['unrelated_changed'] or v['unrelated_removed']}")
    manual = plan["manual_follow_up"]
    extra = (f" Manual follow-up left for you: "
             f"{', '.join(sorted({m['kind'] for m in manual}))}." if manual else "")
    return (f"{name} cleaned up ({result['cleanup_status']}). "
            f"{'; '.join(result['steps'])[:400]}. {tail}. "
            f"Tombstone retained.{extra}")[:780]


def _p(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required}


def _register_tools():
    register(ToolSpec(
        name="homelab_decommission",
        description=(
            "Record that a container/service is intentionally finished, when the "
            "Boss says things like 'decommission it', 'retire it', 'remove it "
            "permanently', 'delete the old project', 'obliterate it', 'kill it "
            "off', or 'it is not coming back'. This is NON-DESTRUCTIVE and takes "
            "effect immediately: it persists the lifecycle state, stops all "
            "monitoring/auto-repair/Hermes escalation for that asset, closes any "
            "incident it already caused, and prepares (but never runs) a targeted "
            "cleanup. Use it as soon as the intent is clear — do NOT wait for the "
            "deletion to be approved first. Set permanent=false for 'ignore this "
            "container' / 'this is intentionally stopped'. Deleting anything is a "
            "separate step: homelab_decommission_cleanup."),
        parameters=_p({
            "asset": {"type": "string",
                      "description": "container, compose project, or asset name as "
                                     "the Boss said it (e.g. 'IVN site')"},
            "reason": {"type": "string",
                       "description": "why, in the Boss's words"},
            "permanent": {"type": "boolean",
                          "description": "true = decommissioned for good (default); "
                                         "false = intentionally stopped for now"},
        }, ["asset"]),
        handler=_tool_decommission, permission="crew", timeout=90,
    ))
    register(ToolSpec(
        name="homelab_lifecycle_set",
        description=(
            "Set a container's monitoring lifecycle state without decommissioning "
            "it: 'ignored' (never alert on it), 'expected_stopped' (intentionally "
            "not running), 'managed' (alert when it is down), or 'unmanaged' "
            "(quiet inventory only). Use for 'ignore this container', 'this is "
            "intentionally stopped', 'start watching this one'."),
        parameters=_p({"asset": {"type": "string"},
                       "state": {"type": "string",
                                 "enum": [IGNORED, EXPECTED_STOPPED, MANAGED, UNMANAGED]},
                       "reason": {"type": "string"}},
                      ["asset", "state"]),
        handler=_tool_lifecycle_set, permission="boss", timeout=60,
    ))
    register(ToolSpec(
        name="homelab_lifecycle_list",
        description=("List lifecycle states and decommission tombstones — which "
                     "containers are ignored, intentionally stopped, pending "
                     "cleanup, or already decommissioned."),
        parameters=_p({}, []),
        handler=_tool_lifecycle_list, permission="boss", timeout=20,
    ))
    register(ToolSpec(
        name="homelab_decommission_cleanup",
        description=(
            "Stage the DESTRUCTIVE cleanup of an asset that is already marked "
            "decommissioned: removes that container/compose project and only its "
            "unshared image and volumes, after archiving its compose/config "
            "metadata. Shared images, volumes and networks are always excluded, "
            "and no prune is ever run. This only prepares an approval draft — "
            "the Boss must approve it before anything is deleted."),
        parameters=_p({"asset": {"type": "string"}}, ["asset"]),
        handler=_tool_cleanup, permission="boss", timeout=90,
    ))
    register(ToolSpec(
        name="homelab_apply_decommission_cleanup",
        description=("INTERNAL: approval-gated execution of a decommission cleanup "
                     "plan. Prefer homelab_decommission_cleanup, which routes here."),
        parameters=_p({"asset": {"type": "string"},
                       "plan_hash": {"type": "string"}},
                      ["asset", "plan_hash"]),
        handler=_cleanup_handler, permission="boss", timeout=300,
        action_type="decommission_cleanup", approval_ttl=3600,
        prepare=_cleanup_prepare,
    ))


try:
    _db()
    _register_tools()
    log.info("homelab lifecycle registry online — %d record(s), %d tombstone(s)",
             len(records()), len(tombstones()))
except Exception as e:  # pragma: no cover - defensive, matches sibling modules
    enabled = False
    log.warning("homelab lifecycle disabled: %s", e)

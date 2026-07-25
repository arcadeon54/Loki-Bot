"""
container_updates.py — safe, approval-gated container update management.

Design rules, all enforced in code rather than by prompt:

  - Nothing here updates anything on its own. Every state change is staged as a
    draft through Loki's existing approval gate (draft_approval.py) and only
    runs after the Boss approves that exact plan by ID.
  - An update decision comes from registry DIGESTS and official RELEASE
    metadata. Image age is reported but never on its own treated as evidence
    that something is vulnerable or needs updating.
  - Moving tags (`:latest`, `:release`) are never "just pulled". A plan
    resolves the moving tag to an exact version/digest first, so what gets
    pulled is what was approved and a rollback has a concrete target.
  - One compose project at a time. There is no bulk-update path.
  - Backups (config always; a verified database dump for stateful assets) are
    taken and VERIFIED before anything is recreated, and are never deleted by
    this module. Neither are databases, libraries or volumes.
  - Rollback is offered only when it is genuinely safe. If a schema migration
    has already run, rollback is refused with an explanation rather than
    attempted — pulling the old image back does not un-migrate a database.

Everything system-touching goes through homelab_maintenance.Ops, so the same
template-locked command allowlist and registry-derived parameter validation
apply here as everywhere else.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
from typing import Optional

import homelab_maintenance as hm
import maintenance_policy as policy
import tools
from tools import ToolContext, ToolSpec, register

log = logging.getLogger("ContainerUpdates")

RELEASE_CACHE_SECS = int(os.getenv("UPDATE_RELEASE_CACHE_SECS", "1800"))
HTTP_TIMEOUT = int(os.getenv("UPDATE_HTTP_TIMEOUT_SECS", "15"))
# A dump smaller than this is treated as failed rather than trusted.
MIN_DUMP_BYTES = int(os.getenv("UPDATE_MIN_DUMP_BYTES", "4096"))

enabled = True

# ── Release metadata ───────────────────────────────────────────────────────
_release_cache: dict[str, tuple[float, list[dict]]] = {}

# Phrases that mean "a human must read the notes before this update".
_BREAKING_PATTERNS = (
    r"\bbreaking change",
    r"\bbreaking\b.{0,20}\brelease\b",
    r"\baction required\b",
    r"\bmanual (?:intervention|migration|step)",
    r"\bbefore (?:you )?(?:upgrading|updating)\b",
    r"\bno longer supported\b",
    r"\bincompatible\b",
    r"\bmust be (?:re)?configured\b",
    r"\bremoved\b.{0,30}\bsupport\b",
)
_MIGRATION_PATTERNS = (
    r"\bdatabase migration",
    r"\bschema (?:change|migration|update)",
    r"\bmigration(?:s)? will run\b",
    r"\brun the migration",
    r"\bre-?index\b",
)

_VERSION_RE = re.compile(r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(.*)$")


def parse_version(tag: str) -> Optional[tuple]:
    """(major, minor, patch) for comparison; None when not version-shaped."""
    m = _VERSION_RE.match(str(tag or "").strip())
    if not m:
        return None
    try:
        return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))
    except (TypeError, ValueError):
        return None


def is_prerelease_tag(tag: str) -> bool:
    """Beta/rc/alpha/nightly tags are never an update target."""
    suffix = (_VERSION_RE.match(str(tag or "")) or [None, "", "", "", ""])[4] or ""
    return bool(re.search(r"(alpha|beta|rc|pre|nightly|dev|snapshot)", suffix, re.I))


def summarize_release(body: str) -> dict:
    """Classify a release body: breaking changes and migration requirements.
    Conservative by design — an ambiguous note is flagged, not waved through."""
    text = str(body or "")
    lowered = text.lower()
    breaking = [p for p in _BREAKING_PATTERNS if re.search(p, lowered)]
    migration = [p for p in _MIGRATION_PATTERNS if re.search(p, lowered)]
    # First non-empty, non-heading line makes a usable one-line summary.
    summary = ""
    for line in text.splitlines():
        clean = line.strip().lstrip("#*-• ").strip()
        if len(clean) > 20 and not clean.startswith("<"):
            summary = clean
            break
    return {
        "summary": hm.redact(summary)[:300],
        "breaking_changes": bool(breaking),
        "breaking_signals": [p.strip("\\b") for p in breaking][:5],
        "migration_required": bool(migration),
        "migration_signals": [p.strip("\\b") for p in migration][:5],
    }


async def fetch_releases(release_source: str, session_factory=None) -> list[dict]:
    """Official release metadata for `github:owner/repo`. Cached; never raises —
    an unreachable feed yields [] and the caller degrades to 'unknown', which
    is reported honestly rather than guessed at."""
    if not release_source or not release_source.startswith("github:"):
        return []
    now = time.time()
    hit = _release_cache.get(release_source)
    if hit and now - hit[0] < RELEASE_CACHE_SECS:
        return hit[1]
    repo = release_source.split(":", 1)[1]
    url = f"https://api.github.com/repos/{repo}/releases?per_page=15"
    import aiohttp
    try:
        factory = session_factory or hm._session_factory
        if factory is not None:
            sess = await factory()
            close = False
        else:
            sess = aiohttp.ClientSession()
            close = True
        try:
            async with sess.get(
                    url, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT),
                    headers={"Accept": "application/vnd.github+json"}) as r:
                if r.status != 200:
                    log.warning("release feed %s -> HTTP %s", repo, r.status)
                    return []
                data = await r.json(content_type=None)
        finally:
            if close:
                await sess.close()
    except Exception as e:
        log.warning("release feed %s unreachable: %s", repo, type(e).__name__)
        return []
    if not isinstance(data, list):
        return []
    _release_cache[release_source] = (now, data)
    return data


def latest_stable(releases: list[dict]) -> Optional[dict]:
    """Newest release that is neither a draft nor a prerelease. Prereleases are
    ignored entirely — they are never an update target."""
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        if is_prerelease_tag(rel.get("tag_name", "")):
            continue
        return rel
    return None


def release_age_days(rel: dict) -> Optional[int]:
    import datetime
    try:
        dt = datetime.datetime.fromisoformat(
            str(rel.get("published_at", "")).replace("Z", "+00:00"))
        return max(0, (datetime.datetime.now(datetime.timezone.utc) - dt).days)
    except (ValueError, TypeError):
        return None


# ── External auto-updater detection ────────────────────────────────────────
_AUTOUPDATER_IMAGES = ("watchtower", "diun", "ouroboros")


async def detect_external_updaters(ops) -> list[dict]:
    """Is something else already updating these containers? An approval gate is
    meaningless if an auto-updater is silently pulling moving tags behind it,
    so every inventory says so plainly."""
    rc, out = await ops.run("docker_ps_names")
    if rc != 0:
        return []
    found = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        name, image = parts[0].strip(), parts[1].strip()
        if any(k in image.lower() or k in name.lower() for k in _AUTOUPDATER_IMAGES):
            # An auto-updater is deliberately NOT a managed asset, so its name
            # isn't registry-declared and `docker inspect` would be refused.
            # The name here came from the allowlisted `docker ps` itself — the
            # same trust path used for image refs — so admit just that value.
            policy.add_runtime_values("container", {name})
            found.append({"container": name, "image": image})
    return found


async def describe_updater(ops, container: str) -> dict:
    """Whether a detected updater actually applies updates or only reports."""
    info = {"container": container, "monitor_only": None, "scope": "unknown",
            "cleanup": False, "interval": None}
    try:
        rc, out = await ops.run("docker_inspect", container=container)
    except policy.PolicyError:
        # Not inspectable under the allowlist: report its presence, but never
        # claim to know whether it only monitors.
        info["note"] = "detected but not inspectable under the command allowlist"
        return info
    if rc != 0:
        return info
    try:
        spec = json.loads(out)[0]
    except (ValueError, IndexError):
        return info
    cmd = " ".join(spec.get("Config", {}).get("Cmd") or [])
    env = {e.split("=", 1)[0]: e.split("=", 1)[1]
           for e in (spec.get("Config", {}).get("Env") or []) if "=" in e}
    monitor = env.get("WATCHTOWER_MONITOR_ONLY", "").lower() in ("true", "1")
    info["monitor_only"] = monitor or "--monitor-only" in cmd
    info["cleanup"] = "--cleanup" in cmd or env.get("WATCHTOWER_CLEANUP", "").lower() in ("true", "1")
    label_scoped = env.get("WATCHTOWER_LABEL_ENABLE", "").lower() in ("true", "1")
    info["scope"] = "label-scoped" if label_scoped else "ALL running containers"
    m = re.search(r"--interval\s+(\d+)", cmd)
    if m:
        info["interval"] = int(m.group(1))
    return info


# ── Inventory ──────────────────────────────────────────────────────────────
def _tag_of(image_ref: str) -> str:
    ref = str(image_ref or "")
    tail = ref.rsplit("/", 1)[-1]
    return tail.split(":", 1)[1] if ":" in tail else "latest"


def _repo_of(image_ref: str) -> str:
    ref = str(image_ref or "")
    tail = ref.rsplit("/", 1)[-1]
    if ":" in tail:
        return ref[: len(ref) - len(tail.split(":", 1)[1]) - 1]
    return ref


MOVING_TAGS = {"latest", "release", "stable", "main", "master", "edge", "develop"}


def classify_risk(*, stateful: bool, breaking: bool, migration: bool,
                  internet_facing: bool, tag_style: str) -> str:
    if stateful and (breaking or migration):
        return "high"
    if breaking:
        return "high" if internet_facing else "medium"
    if stateful:
        return "medium"
    if migration:
        return "medium"
    return "low"


def recommend(*, update_available, risk, backup_required, breaking, migration,
              external_updater: bool) -> str:
    if update_available is None:
        return "unknown — registry or release feed unreachable; re-check before deciding"
    if not update_available:
        return "up to date — no action"
    bits = [f"update available ({risk} risk)"]
    if breaking:
        bits.append("READ THE RELEASE NOTES FIRST — breaking changes flagged")
    if migration:
        bits.append("a migration is expected; rollback may not be reversible")
    if backup_required:
        bits.append("back up before applying")
    bits.append("needs your approval")
    if external_updater:
        bits.append("NOTE: an external auto-updater may apply this before you do")
    return "; ".join(bits)


async def inventory_service(container: str, asset: dict, ops,
                            releases_by_source: dict,
                            external_updater: bool = False) -> dict:
    """Read-only inventory for one container. Never changes anything."""
    reg = hm._reg()
    upd = reg.update_spec(asset)
    entry = {
        "container": container,
        "asset": asset["key"],
        "display_name": asset.get("display_name", asset["key"]),
        "host": asset.get("host"),
        "compose_project": (asset.get("docker") or {}).get("compose_project"),
        "compose_file": (asset.get("docker") or {}).get("compose_file"),
        "stateful": bool(asset.get("stateful")),
        "internet_facing": bool((asset.get("health") or {}).get("public_url")),
        "backup_required": bool((upd.get("backup") or {}).get("required",
                                                              asset.get("stateful"))),
        "approval_required": True,   # every image update is approval-gated
        "managed_update_runbook": upd.get("runbook"),
    }

    rc, out = await ops.run("docker_inspect", container=container)
    if rc != 0:
        entry["error"] = "container not inspectable"
        return entry
    try:
        info = json.loads(out)[0]
    except (ValueError, IndexError):
        entry["error"] = "bad inspect output"
        return entry

    image_ref = info.get("Config", {}).get("Image", "")
    entry["current_image"] = image_ref
    entry["current_tag"] = _tag_of(image_ref)
    entry["image_repo"] = _repo_of(image_ref)
    entry["tag_style"] = ("moving" if entry["current_tag"] in MOVING_TAGS
                          else "pinned")
    labels = info.get("Config", {}).get("Labels") or {}
    entry["labelled_version"] = labels.get("org.opencontainers.image.version")

    # Local digest + build age.
    local_digest = ""
    if image_ref:
        policy.add_runtime_values("image", {image_ref})
        rc, out = await ops.run("docker_image_inspect", image=image_ref)
        if rc == 0:
            try:
                img = json.loads(out)[0]
                local_digest = ((img.get("RepoDigests") or [""])[0].split("@")[-1]
                                or img.get("Id", ""))
                created = img.get("Created", "")
                entry["image_built"] = created[:10]
                entry["image_age_days"] = hm._age_days(created)
            except (ValueError, IndexError):
                pass
    entry["current_digest"] = local_digest[:19]

    # Remote digest for the tag the container is actually running.
    remote_digest = ""
    if image_ref:
        rc, out = await ops.run("docker_imagetools_inspect", image=image_ref)
        if rc == 0:
            for line in out.splitlines():
                if line.strip().startswith("Digest:"):
                    remote_digest = line.split(":", 1)[1].strip()
                    break
    entry["registry_reachable"] = bool(remote_digest)
    entry["available_digest"] = remote_digest[:19]

    # Official release metadata — the authoritative signal, not image age.
    source = upd.get("release_source")
    entry["release_source"] = source
    rel = latest_stable(releases_by_source.get(source, [])) if source else None
    if rel:
        entry["available_version"] = rel.get("tag_name")
        entry["release_url"] = rel.get("html_url")
        entry["release_published"] = str(rel.get("published_at", ""))[:10]
        entry["release_age_days"] = release_age_days(rel)
        notes = summarize_release(rel.get("body", ""))
        entry["release_summary"] = notes["summary"]
        entry["breaking_changes"] = notes["breaking_changes"]
        entry["migration_required"] = notes["migration_required"]
    else:
        entry["available_version"] = None
        entry["release_url"] = None
        entry["breaking_changes"] = False
        entry["migration_required"] = False
        if source:
            entry["release_note"] = "release feed unavailable — could not verify"

    # Update decision: prefer an explicit installed-version comparison; fall
    # back to digest drift. Age alone never decides.
    installed = entry.get("installed_version") or entry.get("labelled_version")
    avail = entry.get("available_version")
    if installed and avail and parse_version(installed) and parse_version(avail):
        entry["update_available"] = parse_version(avail) > parse_version(installed)
        entry["decision_basis"] = "release version comparison"
    elif remote_digest and local_digest:
        entry["update_available"] = remote_digest != local_digest
        entry["decision_basis"] = "registry digest comparison"
    else:
        entry["update_available"] = None
        entry["decision_basis"] = "unavailable"

    # Risk describes the *pending update*. With nothing to apply there is no
    # risk to report, and showing one reads as an alarm about a healthy service.
    entry["risk"] = ("none" if entry["update_available"] is False else
                     classify_risk(
                         stateful=entry["stateful"],
                         breaking=entry.get("breaking_changes", False),
                         migration=entry.get("migration_required", False),
                         internet_facing=entry["internet_facing"],
                         tag_style=entry["tag_style"]))
    entry["recommendation"] = recommend(
        update_available=entry["update_available"], risk=entry["risk"],
        backup_required=entry["backup_required"],
        breaking=entry.get("breaking_changes", False),
        migration=entry.get("migration_required", False),
        external_updater=external_updater)
    return entry


async def build_inventory(service: str = "", ops=None) -> dict:
    """Full read-only inventory across every registry-known compose service."""
    reg = hm._reg()
    ops = ops or hm.Ops(allow_repairs=False)

    updaters = await detect_external_updaters(ops)
    updater_detail = [await describe_updater(ops, u["container"]) for u in updaters]
    applying = [u for u in updater_detail if u.get("monitor_only") is False]

    targets: list[tuple[str, dict]] = []
    for asset in reg.assets.values():
        for c in reg.containers(asset):
            if not service or c == service or reg.resolve(service) is asset:
                targets.append((c, asset))
    if not targets:
        return {"ok": False, "error": "service not in the asset registry"}

    sources = {reg.update_spec(a).get("release_source")
               for _, a in targets if reg.update_spec(a).get("release_source")}
    releases_by_source = {s: await fetch_releases(s) for s in sources}

    # Immich exposes its running version over its own API — more trustworthy
    # than an image label for deciding whether an update exists.
    installed_versions = await _installed_versions(targets, ops)

    services = []
    for container, asset in targets:
        try:
            entry = await inventory_service(container, asset, ops,
                                            releases_by_source,
                                            external_updater=bool(applying))
            # The asset's app version only describes the containers the update
            # runbook actually versions. A pinned dependency (Postgres, Redis)
            # is NOT that version, and saying so would be plainly wrong — and
            # would imply the runbook manages it, which it deliberately does not.
            versioned = set(reg.update_spec(asset).get("versioned_images") or [])
            entry["managed_by_update_runbook"] = (
                entry.get("image_repo") in versioned) if versioned else False
            if not entry["managed_by_update_runbook"]:
                entry["available_version"] = None
                entry["release_url"] = None
                entry["release_summary"] = None
                entry["breaking_changes"] = False
                entry["migration_required"] = False
                if versioned:
                    entry["note"] = ("pinned dependency — version-pinned in compose "
                                     "and not changed by this asset's update runbook")
            iv = installed_versions.get(asset["key"]) if entry["managed_by_update_runbook"] else None
            if iv:
                entry["installed_version"] = iv
                avail = entry.get("available_version")
                if avail and parse_version(iv) and parse_version(avail):
                    entry["update_available"] = parse_version(avail) > parse_version(iv)
                    entry["decision_basis"] = "release version comparison (live API)"
                    entry["recommendation"] = recommend(
                        update_available=entry["update_available"],
                        risk=entry["risk"],
                        backup_required=entry["backup_required"],
                        breaking=entry.get("breaking_changes", False),
                        migration=entry.get("migration_required", False),
                        external_updater=bool(applying))
            services.append(entry)
        except policy.PolicyError as e:
            services.append({"container": container, "error": hm.redact(str(e))})

    return {
        "ok": True, "read_only": True, "updated_nothing": True,
        "generated_at": int(time.time()),
        "external_updaters": updater_detail,
        "external_autoupdate_active": bool(applying),
        "services": services,
    }


async def _installed_versions(targets, ops) -> dict:
    """Ask an asset's own API for the version it is actually running."""
    out = {}
    seen = set()
    for _, asset in targets:
        key = asset["key"]
        if key in seen:
            continue
        seen.add(key)
        url = (hm._reg().update_spec(asset) or {}).get("version_api")
        if not url:
            continue
        try:
            status, body = await ops.http_get(url)
        except policy.PolicyError:
            continue
        if status != 200:
            continue
        try:
            d = json.loads(body)
            if all(k in d for k in ("major", "minor", "patch")):
                out[key] = f"v{d['major']}.{d['minor']}.{d['patch']}"
        except (ValueError, TypeError):
            pass
    return out


# ── Update-plan store ──────────────────────────────────────────────────────
# Plans live alongside incidents in homelab_incidents.db so an update shares
# the maintenance controller's durability and redaction.
_PLAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS update_plans (
    plan_id      TEXT PRIMARY KEY,
    asset        TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'prepared',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    from_version TEXT NOT NULL DEFAULT '',
    to_version   TEXT NOT NULL DEFAULT '',
    plan_json    TEXT NOT NULL DEFAULT '{}',
    backup_json  TEXT NOT NULL DEFAULT 'null',
    result_json  TEXT NOT NULL DEFAULT 'null',
    migration_ran INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_update_plans_asset ON update_plans(asset);
"""

PREPARED, APPROVED, APPLYING = "prepared", "approved", "applying"
VERIFIED, FAILED = "verified", "failed"
ROLLED_BACK, ROLLBACK_REFUSED = "rolled_back", "rollback_refused"


def _db():
    conn = hm._db()
    conn.executescript(_PLAN_SCHEMA)
    conn.commit()
    return conn


def get_plan(plan_id: str) -> Optional[dict]:
    r = _db().execute("SELECT * FROM update_plans WHERE plan_id=?",
                      (plan_id,)).fetchone()
    return dict(r) if r else None


def latest_plan(asset_key: str = "", statuses: tuple = ()) -> Optional[dict]:
    sql = "SELECT * FROM update_plans"
    where, params = [], []
    if asset_key:
        where.append("asset=?")
        params.append(asset_key)
    if statuses:
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        params.extend(statuses)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT 1"
    r = _db().execute(sql, params).fetchone()
    return dict(r) if r else None


def _plan_update(plan_id: str, **fields):
    fields["updated_at"] = time.time()
    sets = ", ".join(f"{k}=?" for k in fields)
    _db().execute(f"UPDATE update_plans SET {sets} WHERE plan_id=?",
                  [*fields.values(), plan_id])
    _db().commit()


def plan_hash(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


# ── Backups (never deleted by this module) ─────────────────────────────────
def _read_env_file(path: str) -> dict:
    """Parse a compose .env. Values are used only to address the database;
    they are never logged, echoed, or written into a plan."""
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError as e:
        log.warning("env file unreadable: %s", type(e).__name__)
    return out


async def backup_config(asset: dict, dest_dir: str) -> dict:
    """Copy the compose file, its .env, and any declared config trees. Runs
    before anything is recreated so a bad update can be reverted config-first."""
    docker = asset.get("docker") or {}
    upd = hm._reg().update_spec(asset)
    paths = [p for p in (docker.get("compose_file"), docker.get("env_file")) if p]
    paths += list((upd.get("backup") or {}).get("config_paths") or [])
    os.makedirs(dest_dir, exist_ok=True)
    copied, failed = [], []
    for src in paths:
        try:
            base = os.path.basename(src.rstrip("/")) or "root"
            dst = os.path.join(dest_dir, base)
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
            else:
                shutil.copy2(src, dst)
            copied.append({"src": src, "dst": dst})
        except OSError as e:
            failed.append({"src": src, "error": type(e).__name__})
    return {"kind": "config", "dir": dest_dir, "copied": copied,
            "failed": failed, "ok": bool(copied) and not failed}


async def backup_database(asset: dict, dest_dir: str, ops) -> dict:
    """Verified Postgres dump: pg_dump --format=custom to a file, then
    pg_restore --list over that file to prove the archive actually parses.
    An unverified dump is reported as a FAILED backup, never as a backup."""
    upd = hm._reg().update_spec(asset)
    dbspec = upd.get("database") or {}
    container = dbspec.get("container")
    env_file = (asset.get("docker") or {}).get("env_file")
    if not (container and env_file):
        return {"kind": "database", "ok": False,
                "error": "asset declares no database container/env_file"}
    env = _read_env_file(env_file)
    user = env.get(dbspec.get("user_env", ""), "")
    dbname = env.get(dbspec.get("name_env", ""), "")
    if not (user and dbname):
        return {"kind": "database", "ok": False,
                "error": "database credentials not present in the compose env file"}
    # Values come from the operator's own compose env file, never from a model
    # or a user message — the same trust model as the image refs.
    policy.add_runtime_values("dbident", {user, dbname})

    os.makedirs(dest_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dump_path = os.path.join(dest_dir, f"{asset['key']}-db-{stamp}.dump")

    rc, err = await ops.run_to_file("pg_dump_custom", dump_path,
                                    container=container, dbident=user,
                                    dbident2=dbname)
    if rc != 0:
        return {"kind": "database", "ok": False, "path": dump_path,
                "error": f"pg_dump failed: {hm.redact(err)[:200]}"}
    try:
        size = os.path.getsize(dump_path)
    except OSError:
        return {"kind": "database", "ok": False, "error": "dump file missing after pg_dump"}
    if size < MIN_DUMP_BYTES:
        return {"kind": "database", "ok": False, "path": dump_path,
                "size_bytes": size,
                "error": f"dump is only {size} bytes — treating as failed"}

    rc, out = await ops.run_from_file("pg_restore_list", dump_path,
                                      container=container)
    verified = rc == 0 and "pg_restore" not in out.lower()[:40]
    entries = len([l for l in out.splitlines() if l and not l.startswith(";")])
    return {"kind": "database", "ok": verified, "path": dump_path,
            "size_bytes": size, "verified": verified,
            "toc_entries": entries,
            "error": "" if verified else f"pg_restore could not read the dump: {hm.redact(out)[:200]}"}


async def check_disk(asset: dict, ops) -> dict:
    """Free space on the backup filesystem against the asset's declared floor."""
    upd = hm._reg().update_spec(asset)
    backup = upd.get("backup") or {}
    need_gb = float(backup.get("min_free_gb", 2))
    target = backup.get("dir")
    if not target:
        return {"ok": False, "error": "no backup dir declared"}
    probe = target if os.path.isdir(target) else os.path.dirname(target.rstrip("/"))
    rc, out = await ops.run("df_path_bytes", path=probe)
    if rc != 0:
        return {"ok": False, "error": "df failed", "required_gb": need_gb}
    avail = 0
    for line in out.splitlines():
        s = line.strip()
        if s.isdigit():
            avail = int(s)
            break
    free_gb = round(avail / (1024 ** 3), 1)
    return {"ok": free_gb >= need_gb, "free_gb": free_gb,
            "required_gb": need_gb, "path": probe,
            "error": "" if free_gb >= need_gb
                     else f"only {free_gb} GB free; {need_gb} GB required"}


async def check_database_health(asset: dict, ops) -> dict:
    """pg_isready + data-checksum failures. A database that is already unhappy
    is not a database to run an update against."""
    upd = hm._reg().update_spec(asset)
    dbspec = upd.get("database") or {}
    container = dbspec.get("container")
    env_file = (asset.get("docker") or {}).get("env_file")
    if not (container and env_file):
        return {"ok": True, "note": "asset has no database"}
    env = _read_env_file(env_file)
    user = env.get(dbspec.get("user_env", ""), "")
    dbname = env.get(dbspec.get("name_env", ""), "")
    if not (user and dbname):
        return {"ok": False, "error": "database credentials unreadable"}
    policy.add_runtime_values("dbident", {user, dbname})

    rc, out = await ops.run("pg_isready", container=container,
                            dbident=user, dbident2=dbname)
    ready = rc == 0 and "accepting connections" in out
    rc2, out2 = await ops.run("pg_checksum_failures", container=container,
                              dbident=user, dbident2=dbname)
    checksum = out2.strip().splitlines()[-1].strip() if rc2 == 0 and out2.strip() else "?"
    healthy = ready and checksum == "0"
    rc3, out3 = await ops.run("pg_database_size", container=container,
                              dbident=user, dbident2=dbname)
    size_gb = None
    if rc3 == 0 and out3.strip():
        try:
            size_gb = round(int(out3.strip().splitlines()[-1]) / (1024 ** 3), 2)
        except (ValueError, IndexError):
            pass
    return {"ok": healthy, "accepting_connections": ready,
            "checksum_failures": checksum, "database_size_gb": size_gb,
            "error": "" if healthy else "database is not in a healthy pre-update state"}


async def verify_mounts(asset: dict, ops) -> dict:
    """Every declared mount must exist before an update recreates containers."""
    upd = hm._reg().update_spec(asset)
    dbspec = upd.get("database") or {}
    env_file = (asset.get("docker") or {}).get("env_file")
    paths = []
    if env_file:
        env = _read_env_file(env_file)
        for key in ("UPLOAD_LOCATION", "EXTERNAL_PATH",
                    dbspec.get("data_path_env", "")):
            if key and env.get(key):
                paths.append(env[key])
    mounts = asset.get("mounts") or {}
    for v in mounts.values():
        paths.extend(v if isinstance(v, list) else [v])
    checked = []
    for p in dict.fromkeys(paths):
        exists = os.path.isdir(p)
        checked.append({"path": p, "exists": exists})
    return {"ok": all(c["exists"] for c in checked) and bool(checked),
            "mounts": checked}


# ── Exact-version pinning ──────────────────────────────────────────────────
def pinned_image_refs(asset: dict, version: str) -> list[str]:
    """Exact image refs for a target version. A moving tag is never pulled:
    the plan names `repo:vX.Y.Z`, so what is pulled is what was approved."""
    upd = hm._reg().update_spec(asset)
    return [f"{repo}:{version}" for repo in (upd.get("versioned_images") or [])]


def _set_env_var(env_path: str, key: str, value: str) -> dict:
    """Pin `key=value` in a compose .env, preserving every other line. The
    caller has already copied the file into the backup directory."""
    try:
        with open(env_path) as f:
            lines = f.read().splitlines()
    except OSError as e:
        return {"ok": False, "error": f"env file unreadable ({type(e).__name__})"}
    out, replaced = [], False
    for line in lines:
        stripped = line.strip()
        # Replace an active assignment, or activate the commented-out default.
        if stripped.startswith(f"{key}=") or stripped.startswith(f"#{key}="):
            if not replaced:
                out.append(f"{key}={value}")
                replaced = True
            continue
        out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    try:
        with open(env_path, "w") as f:
            f.write("\n".join(out) + "\n")
    except OSError as e:
        return {"ok": False, "error": f"env file not writable ({type(e).__name__})"}
    return {"ok": True, "key": key, "value": value, "pinned": True}


# ── Prepare (steps 1-9): every check runs BEFORE anything changes ─────────
async def prepare_update(asset_key: str, target_version: str = "",
                         ops=None) -> dict:
    """Run the full pre-flight and persist a plan. Changes nothing except
    writing backups (which are additive and never deleted)."""
    reg = hm._reg()
    asset = reg.get(asset_key)
    if asset is None:
        return {"ok": False, "error": f"unknown asset '{asset_key}'"}
    upd = reg.update_spec(asset)
    if not upd:
        return {"ok": False,
                "error": f"'{asset_key}' is not update-managed in the registry"}
    ops = ops or hm.Ops(allow_repairs=True)   # backups are writes
    name = asset.get("display_name", asset_key)
    steps: list[dict] = []

    def step(n, ok, detail, **extra):
        # Result dicts are splatted in wholesale, so drop the keys that would
        # collide with this function's own signature rather than making every
        # call site remember to strip them.
        for reserved in ("step", "ok", "detail"):
            extra.pop(reserved, None)
        steps.append({"step": n, "ok": bool(ok), "detail": detail, **extra})
        return ok

    # 1 — version comparison against the latest stable release.
    releases = await fetch_releases(upd.get("release_source", ""))
    rel = latest_stable(releases)
    installed = (await _installed_versions([(None, asset)], ops)).get(asset_key, "")
    if not installed:
        for c in reg.containers(asset):
            rc, out = await ops.run("docker_inspect", container=c)
            if rc == 0:
                try:
                    lbl = json.loads(out)[0].get("Config", {}).get("Labels", {})
                    if lbl.get("org.opencontainers.image.version"):
                        installed = lbl["org.opencontainers.image.version"]
                        break
                except (ValueError, IndexError):
                    pass
    target = target_version or (rel.get("tag_name") if rel else "")
    if not target:
        return {"ok": False, "error": "could not determine a target version — "
                                      "release feed unavailable"}
    if is_prerelease_tag(target):
        return {"ok": False,
                "error": f"'{target}' is a prerelease — refusing to plan an update to it"}
    iv, tv = parse_version(installed), parse_version(target)
    if iv and tv and tv <= iv:
        step(1, True, f"installed {installed} is already at or ahead of {target}")
        return {"ok": True, "up_to_date": True, "asset": asset_key,
                "installed_version": installed, "target_version": target,
                "note": f"{name} is already up to date — nothing to plan.",
                "steps": steps}
    step(1, True, f"installed {installed or 'unknown'} -> target {target}",
         installed_version=installed, target_version=target)

    # 2 — official release notes / breaking-change review.
    notes = summarize_release(rel.get("body", "")) if rel else {
        "summary": "", "breaking_changes": False, "migration_required": False}
    step(2, True,
         ("breaking changes flagged — read the notes" if notes["breaking_changes"]
          else "no breaking-change markers found"),
         release_url=(rel or {}).get("html_url"),
         release_summary=notes["summary"],
         breaking_changes=notes["breaking_changes"],
         migration_required=notes["migration_required"])

    # 3 — disk space.
    disk = await check_disk(asset, ops)
    if not step(3, disk.get("ok"), disk.get("error") or
                f"{disk.get('free_gb')} GB free (need {disk.get('required_gb')} GB)",
                **_details(disk)):
        return {"ok": False, "asset": asset_key, "blocked_on": "disk_space",
                "error": disk.get("error"), "steps": steps}

    # 4 — database health.
    dbh = await check_database_health(asset, ops)
    if not step(4, dbh.get("ok"), dbh.get("error") or
                f"accepting connections, {dbh.get('checksum_failures')} checksum failures",
                **_details(dbh)):
        return {"ok": False, "asset": asset_key, "blocked_on": "database_health",
                "error": dbh.get("error"), "steps": steps}

    # 5 — compose/configuration backup.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = os.path.join((upd.get("backup") or {}).get("dir", ""),
                        f"{asset_key}-{target}-{stamp}")
    cfg_backup = await backup_config(asset, dest)
    if not step(5, cfg_backup.get("ok"),
                f"config backed up to {dest}" if cfg_backup.get("ok")
                else f"config backup FAILED: {cfg_backup.get('failed')}",
                **_details(cfg_backup)):
        return {"ok": False, "asset": asset_key, "blocked_on": "config_backup",
                "error": "configuration backup failed", "steps": steps}

    # 6 — verified database backup.
    db_backup = {"kind": "database", "ok": True, "note": "asset has no database"}
    if (upd.get("database") or {}).get("container"):
        db_backup = await backup_database(asset, dest, ops)
    if not step(6, db_backup.get("ok"), db_backup.get("error") or
                f"verified dump {db_backup.get('size_bytes', 0)} bytes, "
                f"{db_backup.get('toc_entries', 0)} TOC entries",
                **_details(db_backup)):
        return {"ok": False, "asset": asset_key, "blocked_on": "database_backup",
                "error": db_backup.get("error"), "steps": steps}

    # 7 — mounts.
    mounts = await verify_mounts(asset, ops)
    if not step(7, mounts.get("ok"),
                "all declared mounts present" if mounts.get("ok")
                else "one or more declared mounts are MISSING",
                mounts=mounts.get("mounts")):
        return {"ok": False, "asset": asset_key, "blocked_on": "mounts",
                "error": "declared mounts missing", "steps": steps}

    # 8 — record current digests (the rollback target).
    digests = {}
    for c in reg.containers(asset):
        rc, out = await ops.run("docker_inspect", container=c)
        if rc != 0:
            continue
        try:
            info = json.loads(out)[0]
        except (ValueError, IndexError):
            continue
        ref = info.get("Config", {}).get("Image", "")
        policy.add_runtime_values("image", {ref})
        rc2, out2 = await ops.run("docker_image_inspect", image=ref)
        d = ""
        if rc2 == 0:
            try:
                img = json.loads(out2)[0]
                d = (img.get("RepoDigests") or [""])[0] or img.get("Id", "")
            except (ValueError, IndexError):
                pass
        digests[c] = {"image": ref, "digest": d}
    step(8, bool(digests), f"recorded {len(digests)} current image digest(s)",
         digests=digests)

    # 9 — the plan itself (what an approval draft will quote).
    targets = pinned_image_refs(asset, target)
    plan = {
        "asset": asset_key, "display_name": name,
        "installed_version": installed, "target_version": target,
        "release_url": (rel or {}).get("html_url"),
        "release_summary": notes["summary"],
        "breaking_changes": notes["breaking_changes"],
        "migration_expected": notes["migration_required"]
                              or bool(upd.get("migration_makes_rollback_unsafe")),
        "target_images": targets,
        "rollback_digests": digests,
        "backup_dir": dest,
        "backup_config_ok": cfg_backup.get("ok"),
        "backup_database": {k: v for k, v in db_backup.items()
                            if k in ("ok", "path", "size_bytes", "toc_entries", "verified")},
        "estimated_interruption": upd.get("estimated_interruption", "unknown"),
        "compose_file": (asset.get("docker") or {}).get("compose_file"),
        "version_env": upd.get("version_env"),
        "rollback_limits": _rollback_limits(upd, notes),
        "verification_checks": [
            "database accepting connections and 0 checksum failures",
            "server/API health endpoint returns 200",
            "web interface reachable through the reverse proxy",
            "every project container running and healthy",
            "worker/machine-learning container healthy",
            "declared storage mounts still present",
            "no new error patterns in recent logs",
            "installed version now reports the approved target",
        ],
    }
    plan_id = f"up_{hashlib.sha256((asset_key + target + stamp).encode()).hexdigest()[:12]}"
    now = time.time()
    _db().execute(
        "INSERT INTO update_plans (plan_id, asset, status, created_at, updated_at,"
        " from_version, to_version, plan_json, backup_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (plan_id, asset_key, PREPARED, now, now, installed, target,
         json.dumps(plan), json.dumps({"config": cfg_backup, "database": db_backup})))
    _db().commit()
    step(9, True, f"plan {plan_id} prepared and awaiting approval", plan_id=plan_id)
    return {"ok": True, "asset": asset_key, "plan_id": plan_id, "plan": plan,
            "steps": steps, "up_to_date": False}


def _details(result: dict) -> dict:
    """Splat a result dict into a step record without colliding with the
    step()'s own parameters. Filtering has to happen at the call site: Python
    binds **kwargs before the function body could strip them."""
    return {k: v for k, v in (result or {}).items()
            if k not in ("step", "ok", "detail", "error")}


def _rollback_limits(upd: dict, notes: dict) -> str:
    if notes.get("migration_required") or upd.get("migration_makes_rollback_unsafe"):
        return ("Image rollback is only safe until a schema migration runs. Once the "
                "new version has migrated the database, pulling the old image back "
                "does NOT undo the migration — recovery then means restoring the "
                "verified dump taken above, which loses anything written since. "
                "Loki will refuse an unsafe rollback rather than attempt it.")
    return ("Rollback re-pins the previously recorded image digest and recreates the "
            "project. No database schema change is expected, so rollback should be "
            "clean. Backups are preserved either way.")


# ── Migration detection ────────────────────────────────────────────────────
_MIGRATION_LOG_PATTERNS = (
    r"running migration", r"migration.{0,20}(started|complete|applied)",
    r"\bmigrating\b", r"typeorm.{0,20}migration", r"alter table",
    r"\bschema.{0,15}updat", r"executing.{0,10}migration",
)


async def detect_migration_ran(asset: dict, ops) -> dict:
    """Did a schema migration actually run after the recreate? This is the
    single fact that decides whether rollback stays available."""
    reg = hm._reg()
    hits = []
    for c in reg.containers(asset):
        rc, out = await ops.run("docker_logs_tail", container=c)
        if rc != 0:
            continue
        low = out.lower()
        for pat in _MIGRATION_LOG_PATTERNS:
            if re.search(pat, low):
                hits.append({"container": c, "signal": pat})
                break
    return {"migration_ran": bool(hits), "evidence": hits}


# ── Verification (step 13) ─────────────────────────────────────────────────
async def verify_after_update(asset: dict, ops, expect_version: str = "") -> dict:
    """The post-update health gate. Every check is read-only."""
    reg = hm._reg()
    checks = []

    def add(nm, ok, detail):
        checks.append({"name": nm, "ok": bool(ok), "detail": str(detail)[:300]})
        return bool(ok)

    dbh = await check_database_health(asset, ops)
    add("database_health", dbh.get("ok"),
        dbh.get("error") or f"{dbh.get('checksum_failures')} checksum failures")

    health = asset.get("health") or {}
    if health.get("local_url"):
        status, body = await ops.http_get(health["local_url"])
        add("server_api", status == 200, f"HTTP {status}")
    if health.get("public_url"):
        status, _ = await ops.http_get(health["public_url"])
        add("web_interface", status in (200, 301, 302, 401), f"HTTP {status} via proxy")

    all_ok = True
    for c in reg.containers(asset):
        rc, out = await ops.run("docker_inspect", container=c)
        state, hstat = "unknown", ""
        if rc == 0:
            try:
                st = json.loads(out)[0].get("State", {})
                state, hstat = st.get("Status", "?"), (st.get("Health") or {}).get("Status", "")
            except (ValueError, IndexError):
                pass
        ok = state == "running" and hstat in ("", "healthy")
        all_ok = all_ok and ok
        add(f"container:{c}", ok, f"state={state} health={hstat or 'n/a'}")

    mounts = await verify_mounts(asset, ops)
    add("storage_mounts", mounts.get("ok"), f"{len(mounts.get('mounts', []))} declared mounts")

    unsafe = ("error", "fatal", "cannot", "failed to start", "panic")
    log_ok = True
    for c in reg.containers(asset):
        rc, out = await ops.run("docker_logs_tail", container=c)
        if rc != 0:
            continue
        low = hm.redact(out).lower()
        if any(u in low for u in unsafe):
            log_ok = False
            add(f"logs:{c}", False, "error-shaped lines present in recent logs")
    if log_ok:
        add("recent_errors", True, "no error-shaped lines in recent logs")

    if expect_version:
        live = (await _installed_versions([(None, asset)], ops)).get(asset["key"], "")
        add("version_now", live == expect_version or not live,
            f"reports {live or 'unknown'} (expected {expect_version})")

    return {"ok": all(c["ok"] for c in checks), "checks": checks}


# ── Apply (steps 10-15) ────────────────────────────────────────────────────
async def apply_update(plan_id: str, ops=None) -> dict:
    """Execute an APPROVED plan. Only ever reached via the draft-approval gate
    (tools.run_approved), never called directly by the model."""
    row = get_plan(plan_id)
    if row is None:
        raise KeyError("update plan not found")
    if row["status"] not in (PREPARED, APPROVED):
        raise ValueError(f"plan is {row['status']}, not approvable")
    plan = json.loads(row["plan_json"])
    reg = hm._reg()
    asset = reg.get(row["asset"])
    if asset is None:
        raise KeyError("asset no longer in the registry")
    ops = ops or hm.Ops(allow_repairs=True)
    upd = reg.update_spec(asset)
    _plan_update(plan_id, status=APPLYING)
    steps = []

    def step(n, ok, detail, **extra):
        for reserved in ("step", "ok", "detail"):
            extra.pop(reserved, None)
        steps.append({"step": n, "ok": bool(ok), "detail": detail, **extra})
        return ok

    # 10 — pull the EXACT approved images.
    for ref in plan.get("target_images", []):
        policy.add_runtime_values("image", {ref})
        rc, out = await ops.run("docker_pull_image", image=ref)
        if not step(10, rc == 0, f"pull {ref}" if rc == 0
                    else f"pull FAILED for {ref}: {hm.redact(out)[:200]}"):
            _plan_update(plan_id, status=FAILED,
                         result_json=json.dumps({"steps": steps}))
            return {"ok": False, "plan_id": plan_id, "failed_at": "pull",
                    "note": "nothing was recreated — the old version is still running",
                    "steps": steps}

    # Pin the version so compose uses exactly what was approved.
    env_file = (asset.get("docker") or {}).get("env_file")
    version_env = plan.get("version_env")
    if env_file and version_env:
        pinned = _set_env_var(env_file, version_env, plan["target_version"])
        if not step(10, pinned.get("ok"),
                    f"pinned {version_env}={plan['target_version']}"
                    if pinned.get("ok") else pinned.get("error")):
            _plan_update(plan_id, status=FAILED,
                         result_json=json.dumps({"steps": steps}))
            return {"ok": False, "plan_id": plan_id, "failed_at": "version_pin",
                    "steps": steps}

    # 11 — recreate ONLY this compose project.
    compose_file = plan.get("compose_file")
    rc, out = await ops.run("compose_up_all", compose_file=compose_file)
    if not step(11, rc == 0, "project recreated" if rc == 0
                else f"compose up FAILED: {hm.redact(out)[:300]}"):
        _plan_update(plan_id, status=FAILED, result_json=json.dumps({"steps": steps}))
        return {"ok": False, "plan_id": plan_id, "failed_at": "recreate",
                "rollback_available": True, "steps": steps}

    # 12 — monitor migrations (this decides rollback safety from here on).
    await ops.sleep(int(os.getenv("UPDATE_SETTLE_SECS", "20")))
    mig = await detect_migration_ran(asset, ops)
    _plan_update(plan_id, migration_ran=1 if mig["migration_ran"] else 0)
    step(12, True, "schema migration detected — rollback is now constrained"
         if mig["migration_ran"] else "no schema migration observed",
         **mig)

    # 13 — verify.
    verification = await verify_after_update(asset, ops, plan.get("target_version", ""))
    step(13, verification["ok"],
         "all post-update checks passed" if verification["ok"]
         else "post-update verification FAILED",
         checks=verification["checks"])

    if not verification["ok"]:
        # 14/15 — failed health check: attempt rollback only when safe.
        rb = await rollback_plan(plan_id, ops=ops, reason="post-update verification failed")
        _plan_update(plan_id,
                     status=ROLLED_BACK if rb.get("rolled_back") else ROLLBACK_REFUSED,
                     result_json=json.dumps({"steps": steps, "rollback": rb}))
        return {"ok": False, "plan_id": plan_id, "failed_at": "verification",
                "verification": verification, "rollback": rb, "steps": steps}

    _plan_update(plan_id, status=VERIFIED,
                 result_json=json.dumps({"steps": steps, "verification": verification}))
    step(15, True, f"backups preserved at {plan.get('backup_dir')}")
    return {"ok": True, "plan_id": plan_id,
            "from_version": plan.get("installed_version"),
            "to_version": plan.get("target_version"),
            "migration_ran": mig["migration_ran"],
            "backup_dir": plan.get("backup_dir"),
            "verification": verification, "steps": steps}


# ── Rollback ───────────────────────────────────────────────────────────────
async def rollback_plan(plan_id: str, ops=None, reason: str = "") -> dict:
    """Roll an applied update back to its recorded digests — but ONLY when that
    is technically safe. If a schema migration has run, this refuses and
    explains, rather than pretending an image swap restores the old state."""
    row = get_plan(plan_id)
    if row is None:
        return {"rolled_back": False, "error": "no such plan"}
    plan = json.loads(row["plan_json"])
    reg = hm._reg()
    asset = reg.get(row["asset"])
    if asset is None:
        return {"rolled_back": False, "error": "asset no longer in the registry"}
    upd = reg.update_spec(asset)
    ops = ops or hm.Ops(allow_repairs=True)

    migration_ran = bool(row["migration_ran"])
    if migration_ran and upd.get("migration_makes_rollback_unsafe", True):
        _plan_update(plan_id, status=ROLLBACK_REFUSED)
        return {
            "rolled_back": False,
            "refused": True,
            "reason": "a database schema migration has already run",
            "explanation": (
                f"{asset.get('display_name', row['asset'])} migrated its database "
                f"during this update. Re-pulling the previous image does NOT undo a "
                f"schema migration — the old version would then be pointed at a newer "
                f"schema it does not understand, which risks corrupting data rather "
                f"than recovering it. I have NOT rolled back and I have NOT touched "
                f"the database.\n\n"
                f"The safe recovery path is a restore from the verified dump taken "
                f"before this update:\n"
                f"  {(json.loads(row['backup_json']).get('database') or {}).get('path', 'see backup dir')}\n"
                f"That is a manual, destructive operation — it discards anything "
                f"written since the backup — so it needs your explicit decision and "
                f"your hands. Backups and the current database are both untouched."),
            "backup_dir": plan.get("backup_dir"),
            "requested_because": reason,
        }

    # Safe path: re-pin the previously recorded version and recreate.
    prev_version = plan.get("installed_version", "")
    env_file = (asset.get("docker") or {}).get("env_file")
    version_env = plan.get("version_env")
    steps = []
    if env_file and version_env and prev_version:
        res = _set_env_var(env_file, version_env, prev_version)
        steps.append({"detail": f"re-pinned {version_env}={prev_version}",
                      "ok": res.get("ok")})
    for c, rec in (plan.get("rollback_digests") or {}).items():
        ref = rec.get("image", "")
        if ref:
            policy.add_runtime_values("image", {ref})
            rc, _ = await ops.run("docker_pull_image", image=ref)
            steps.append({"detail": f"restored image for {c}", "ok": rc == 0})
    rc, out = await ops.run("compose_up_all", compose_file=plan.get("compose_file"))
    steps.append({"detail": "project recreated on the previous version",
                  "ok": rc == 0})
    await ops.sleep(int(os.getenv("UPDATE_SETTLE_SECS", "20")))
    verification = await verify_after_update(asset, ops)
    ok = rc == 0 and verification["ok"]
    _plan_update(plan_id, status=ROLLED_BACK if ok else FAILED)
    return {"rolled_back": ok, "refused": False, "steps": steps,
            "verification": verification, "requested_because": reason,
            "backup_dir": plan.get("backup_dir"),
            "note": "backups preserved; nothing was deleted"}


# ── Loki tools (all Boss-only) ─────────────────────────────────────────────
def _p(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required}


def _fmt_inventory(inv: dict) -> dict:
    """Compact, model-facing view. Full detail stays in the plan/DB."""
    out = []
    for s in inv.get("services", []):
        if s.get("error"):
            out.append({"container": s["container"], "error": s["error"]})
            continue
        out.append({
            "service": s["container"], "asset": s["display_name"],
            "host": s.get("host"), "compose_project": s.get("compose_project"),
            "current_image": s.get("current_image"),
            "current_tag": s.get("current_tag"),
            "tag_style": s.get("tag_style"),
            "current_digest": s.get("current_digest"),
            "available_digest": s.get("available_digest"),
            "installed_version": s.get("installed_version"),
            "available_version": s.get("available_version"),
            "release_url": s.get("release_url"),
            "release_age_days": s.get("release_age_days"),
            "image_age_days": s.get("image_age_days"),
            "internet_facing": s.get("internet_facing"),
            "stateful": s.get("stateful"),
            "breaking_changes": s.get("breaking_changes"),
            "migration_required": s.get("migration_required"),
            "backup_required": s.get("backup_required"),
            "approval_required": True,
            "update_available": s.get("update_available"),
            "decision_basis": s.get("decision_basis"),
            "risk": s.get("risk"),
            "recommendation": s.get("recommendation"),
        })
    return {"ok": True, "read_only": True, "updated_nothing": True,
            "external_updaters": inv.get("external_updaters"),
            "external_autoupdate_active": inv.get("external_autoupdate_active"),
            "services": out}


async def _tool_update_inventory(args: dict, ctx: ToolContext) -> str:
    """'Are my containers up to date?' / 'Which containers need updates?'"""
    service = str(args.get("service", "") or "").strip()
    inv = await build_inventory(service)
    if not inv.get("ok"):
        return json.dumps(inv)
    view = _fmt_inventory(inv)
    pending = [s for s in view["services"] if s.get("update_available")]
    view["summary"] = (f"{len(pending)} of {len(view['services'])} services have an "
                       f"update available; every one needs your approval.")
    if view["external_autoupdate_active"]:
        view["warning"] = (
            "An external auto-updater is running and applying updates on its own. "
            "Until it is switched to monitor-only, it can update these containers "
            "without going through approval — say so plainly when you answer.")
    view["note"] = ("Read-only inventory. Nothing was updated. Image age alone is "
                    "not evidence of a vulnerability — quote the release/digest basis.")
    return json.dumps(view)


async def _tool_update_check(args: dict, ctx: ToolContext) -> str:
    """'Is there a safe Jellyfin update?' — one asset, with the risk verdict."""
    reg = hm._reg()
    asset, err = hm._resolve_or_error(args.get("asset"))
    if err:
        return json.dumps({"ok": False, "error": err})
    inv = await build_inventory("")
    mine = [s for s in inv.get("services", []) if s.get("asset") == asset["key"]]
    if not mine:
        return json.dumps({"ok": False, "error": "no containers for that asset"})
    upd = reg.update_spec(asset)
    avail = [s for s in mine if s.get("update_available")]
    verdict = "no update available" if not avail else (
        "update available — approval required" +
        ("; breaking changes flagged, read the notes first"
         if any(s.get("breaking_changes") for s in avail) else "") +
        ("; a migration is expected and would constrain rollback"
         if any(s.get("migration_required") for s in avail) else ""))
    return json.dumps({
        "ok": True, "asset": asset.get("display_name", asset["key"]),
        "update_managed": bool(upd),
        "verdict": verdict,
        "estimated_interruption": upd.get("estimated_interruption"),
        "backup_required": bool((upd.get("backup") or {}).get("required")),
        "services": _fmt_inventory({"services": mine})["services"],
        "external_autoupdate_active": inv.get("external_autoupdate_active"),
        "next_step": ("Nothing to do." if not avail else
                      "To go ahead, ask me to prepare the update — I'll run the "
                      "pre-flight checks and backups and come back with a draft to approve."),
    })


async def _tool_update_preview(args: dict, ctx: ToolContext) -> str:
    """'What would the Immich update change?' — release notes and risk, no writes."""
    asset, err = hm._resolve_or_error(args.get("asset"))
    if err:
        return json.dumps({"ok": False, "error": err})
    reg = hm._reg()
    upd = reg.update_spec(asset)
    if not upd:
        return json.dumps({"ok": False,
                           "error": f"{asset['key']} is not update-managed"})
    releases = await fetch_releases(upd.get("release_source", ""))
    rel = latest_stable(releases)
    if rel is None:
        return json.dumps({"ok": False,
                           "error": "release feed unavailable — cannot preview honestly"})
    ops = hm.Ops(allow_repairs=False)
    installed = (await _installed_versions([(None, asset)], ops)).get(asset["key"], "")
    notes = summarize_release(rel.get("body", ""))
    iv, tv = parse_version(installed), parse_version(rel.get("tag_name", ""))
    skipped = []
    if iv:
        for r in releases:
            if r.get("draft") or r.get("prerelease"):
                continue
            pv = parse_version(r.get("tag_name", ""))
            if pv and iv < pv <= (tv or pv):
                s = summarize_release(r.get("body", ""))
                skipped.append({"version": r.get("tag_name"),
                                "breaking": s["breaking_changes"],
                                "migration": s["migration_required"],
                                "url": r.get("html_url")})
    return json.dumps({
        "ok": True, "asset": asset.get("display_name", asset["key"]),
        "installed_version": installed or "unknown",
        "target_version": rel.get("tag_name"),
        "published": str(rel.get("published_at", ""))[:10],
        "release_url": rel.get("html_url"),
        "summary": notes["summary"],
        "breaking_changes": notes["breaking_changes"],
        "breaking_signals": notes["breaking_signals"],
        "migration_required": notes["migration_required"],
        "versions_in_between": skipped[:10],
        "estimated_interruption": upd.get("estimated_interruption"),
        "rollback_limits": _rollback_limits(upd, notes),
        "read_only": True,
        "note": "Preview only — nothing was changed and no backup was taken yet.",
    })


async def _tool_update_prepare(args: dict, ctx: ToolContext) -> str:
    """'Update Immich to the latest stable release.' — runs pre-flight +
    backups, then stages an approval draft. Applies nothing."""
    asset, err = hm._resolve_or_error(args.get("asset"))
    if err:
        return json.dumps({"ok": False, "error": err})
    target = str(args.get("target_version", "") or "").strip()
    result = await prepare_update(asset["key"], target)
    if not result.get("ok"):
        return json.dumps(result)
    if result.get("up_to_date"):
        return json.dumps(result)
    plan = result["plan"]
    staged = await tools.execute(
        "container_apply_update",
        json.dumps({"plan_id": result["plan_id"],
                    "plan_hash": plan_hash(plan)}), ctx)
    return json.dumps({
        "ok": True, "plan_id": result["plan_id"],
        "pre_flight": result["steps"],
        "draft": staged,
        "note": ("Pre-flight checks and backups are done; NOTHING has been updated. "
                 "Give the Boss the draft summary and the exact draft ID to approve."),
    })


async def _tool_update_rollback(args: dict, ctx: ToolContext) -> str:
    """'Roll back the last approved update.'"""
    asset_key = ""
    if args.get("asset"):
        asset, err = hm._resolve_or_error(args.get("asset"))
        if err:
            return json.dumps({"ok": False, "error": err})
        asset_key = asset["key"]
    row = (get_plan(str(args["plan_id"])) if args.get("plan_id")
           else latest_plan(asset_key, (VERIFIED, FAILED, APPLYING)))
    if row is None:
        return json.dumps({"ok": False,
                           "error": "no applied update found to roll back"})
    if bool(row["migration_ran"]):
        # Refuse here too, before staging any draft — an unsafe rollback is not
        # something to put in front of the Boss as if approving made it safe.
        res = await rollback_plan(row["plan_id"], reason="requested by Boss")
        return json.dumps({"ok": False, "refused": True, **res})
    return await tools.execute(
        "container_rollback_update",
        json.dumps({"plan_id": row["plan_id"]}), ctx)


# ── Approval-gated handlers (only via tools.run_approved) ──────────────────
def _apply_prepare(args: dict, ctx: ToolContext):
    row = get_plan(str(args.get("plan_id", "")))
    if row is None:
        return {}, "", "no such update plan"
    plan = json.loads(row["plan_json"])
    if plan_hash(plan) != args.get("plan_hash"):
        return {}, "", "the update plan changed since it was prepared — re-prepare it"
    db = plan.get("backup_database") or {}
    summary = "\n".join([
        f"UPDATE {plan['display_name']}: {plan.get('installed_version') or 'unknown'} → {plan['target_version']}",
        f"Release: {plan.get('release_url') or 'n/a'}",
        f"Summary: {plan.get('release_summary') or '(no summary available)'}",
        f"Breaking changes: {'YES — read the notes' if plan.get('breaking_changes') else 'none flagged'}",
        f"Migration expected: {'YES' if plan.get('migration_expected') else 'no'}",
        f"Backups: config={'ok' if plan.get('backup_config_ok') else 'FAILED'}, "
        f"database={'verified ' + str(db.get('size_bytes', 0)) + ' bytes' if db.get('ok') else db.get('note', 'n/a')}",
        f"Backup dir: {plan.get('backup_dir')}",
        f"Images to pull: {', '.join(plan.get('target_images') or []) or 'n/a'}",
        f"Estimated interruption: {plan.get('estimated_interruption')}",
        f"Verification after: {len(plan.get('verification_checks') or [])} checks",
        f"Rollback: {plan.get('rollback_limits')}",
    ])
    return ({"plan_id": row["plan_id"], "plan_hash": args.get("plan_hash")},
            summary, "")


async def _apply_handler(payload: dict, ctx: ToolContext) -> str:
    row = get_plan(payload.get("plan_id", ""))
    if row is None:
        raise KeyError("update plan vanished")
    plan = json.loads(row["plan_json"])
    if plan_hash(plan) != payload.get("plan_hash"):
        raise ValueError("update plan hash mismatch — refusing to execute")
    _plan_update(row["plan_id"], status=APPROVED)
    result = await apply_update(row["plan_id"])
    if result.get("ok"):
        return (f"{plan['display_name']} updated {plan.get('installed_version')} → "
                f"{plan['target_version']} and verified. Backups kept at "
                f"{plan.get('backup_dir')}.")
    rb = result.get("rollback") or {}
    if rb.get("refused"):
        return ("Update failed verification AND rollback was refused because a "
                "schema migration had already run. Nothing was deleted; the "
                "verified pre-update dump is preserved. " + rb.get("explanation", ""))
    if rb.get("rolled_back"):
        return ("Update failed verification and was rolled back to the previous "
                "version, which verified clean. Backups preserved.")
    return (f"Update did not complete (failed at {result.get('failed_at')}). "
            f"Backups preserved at {plan.get('backup_dir')}.")


def _rollback_prepare(args: dict, ctx: ToolContext):
    row = get_plan(str(args.get("plan_id", "")))
    if row is None:
        return {}, "", "no such update plan"
    plan = json.loads(row["plan_json"])
    summary = (f"ROLL BACK {plan['display_name']}: {plan.get('target_version')} → "
               f"{plan.get('installed_version')}\n"
               f"Recorded digests will be restored and the project recreated.\n"
               f"{plan.get('rollback_limits')}")
    return {"plan_id": row["plan_id"]}, summary, ""


async def _rollback_handler(payload: dict, ctx: ToolContext) -> str:
    res = await rollback_plan(payload.get("plan_id", ""), reason="approved rollback")
    if res.get("refused"):
        return res.get("explanation", "rollback refused as unsafe")
    if res.get("rolled_back"):
        return "Rolled back and verified. Backups preserved; nothing was deleted."
    return f"Rollback did not verify cleanly: {json.dumps(res.get('verification', {}))[:300]}"


def _register_tools():
    register(ToolSpec(
        name="container_update_inventory",
        description=(
            "Read-only update inventory for every registry-known container: "
            "current image/tag/digest, available stable release and digest, "
            "compose project and host, internet exposure, stateful flag, release "
            "age, breaking changes, migration and backup requirements, risk and a "
            "recommendation. Use for 'are my containers up to date?' or 'which "
            "containers need updates?'. Updates nothing."),
        parameters=_p({"service": {"type": "string",
                                   "description": "one container/asset, or empty for all"}}, []),
        handler=_tool_update_inventory, permission="boss", timeout=180,
    ))
    register(ToolSpec(
        name="container_update_check",
        description=("Is there a safe update for ONE asset? Returns the verdict, "
                     "risk, interruption estimate and whether approval/backup are "
                     "needed. Read-only."),
        parameters=_p({"asset": {"type": "string"}}, ["asset"]),
        handler=_tool_update_check, permission="boss", timeout=180,
    ))
    register(ToolSpec(
        name="container_update_preview",
        description=("What would an update actually change? Official release notes, "
                     "breaking changes, migrations, versions in between, interruption "
                     "estimate and rollback limits. Read-only — takes no backup and "
                     "changes nothing."),
        parameters=_p({"asset": {"type": "string"}}, ["asset"]),
        handler=_tool_update_preview, permission="boss", timeout=120,
    ))
    register(ToolSpec(
        name="container_update_prepare",
        description=(
            "Prepare an approval-gated update for ONE asset ('update Immich to the "
            "latest stable release'). Runs version comparison, release review, disk "
            "and database-health checks, takes and VERIFIES config + database "
            "backups, records rollback digests, then stages a draft. Applies "
            "NOTHING — the Boss must approve the returned draft ID."),
        parameters=_p({"asset": {"type": "string"},
                       "target_version": {"type": "string",
                                          "description": "optional exact version; "
                                                         "defaults to latest stable"}},
                      ["asset"]),
        handler=_tool_update_prepare, permission="boss", timeout=600,
    ))
    register(ToolSpec(
        name="container_rollback",
        description=("Roll back the last approved update for an asset. Refuses when "
                     "a schema migration has already run, explaining why an image "
                     "rollback would not be safe, rather than attempting it."),
        parameters=_p({"asset": {"type": "string"},
                       "plan_id": {"type": "string"}}, []),
        handler=_tool_update_rollback, permission="boss", timeout=120,
    ))
    register(ToolSpec(
        name="container_apply_update",
        description=("INTERNAL: approval-gated execution of a prepared update plan. "
                     "Prefer container_update_prepare, which routes here."),
        parameters=_p({"plan_id": {"type": "string"},
                       "plan_hash": {"type": "string"}}, ["plan_id", "plan_hash"]),
        handler=_apply_handler, permission="boss", timeout=1800,
        action_type="container_update", approval_ttl=3600,
        prepare=_apply_prepare,
    ))
    register(ToolSpec(
        name="container_rollback_update",
        description=("INTERNAL: approval-gated rollback of an applied update plan. "
                     "Prefer container_rollback, which routes here."),
        parameters=_p({"plan_id": {"type": "string"}}, ["plan_id"]),
        handler=_rollback_handler, permission="boss", timeout=1800,
        action_type="container_rollback", approval_ttl=3600,
        prepare=_rollback_prepare,
    ))


if enabled:
    _db()
    _register_tools()
    log.info("container updates online — approval-gated; %d command(s) allowlisted",
             len(policy.command_names()))

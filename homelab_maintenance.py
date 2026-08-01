"""
homelab_maintenance.py — Loki's secure homelab maintenance controller.

Known operational problems ("BLACK-BOXX has no internet", "Jellyfin is down")
are handled by DETERMINISTIC runbooks — no external LLM is consumed for the
diagnosis itself. Unknown problems produce a bounded, redacted diagnostic
bundle that a future Hermes agent can pick up; nothing is launched here.

Boundaries (all enforced in code, not by prompt):
  - Assets come from config/homelab_assets.yml (homelab_assets.py). Tools
    accept ONLY asset names/aliases resolved against that registry — never
    arbitrary commands, hosts, interfaces, service names, or paths.
  - Every shell command is built by maintenance_policy.build_command() from a
    fixed template; parameter values must be declared in the registry.
  - Action risk tiers live in maintenance_policy.ACTION_TIERS. AUTO actions
    run inside runbooks only when their exact preconditions hold; APPROVAL
    actions go through the existing draft-approval gate; MANUAL actions are
    refused.
  - All tools are Boss-only. Diagnoses run as supervised "homelab_incident"
    tasks (priority above informational tasks) tied to the originating
    message/conversation.
  - Everything persisted or escalated is redacted (secrets, MACs, WG keys)
    and bounded.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import socket
import sqlite3
import time
import uuid
from typing import Optional

import homelab_assets
import maintenance_policy as policy
import tools
from tools import ToolContext, ToolSpec, register, user_level

log = logging.getLogger("HomelabMaintenance")

DB_PATH = os.getenv("HOMELAB_DB_PATH",
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "homelab_incidents.db"))
CMD_TIMEOUT = int(os.getenv("HOMELAB_CMD_TIMEOUT_SECS", "20"))
HTTP_TIMEOUT = int(os.getenv("HOMELAB_HTTP_TIMEOUT_SECS", "8"))
OUTPUT_CAP = 16 * 1024
LOG_EXCERPT_CAP = 2000
RESTART_ONCE_WINDOW = int(os.getenv("HOMELAB_RESTART_WINDOW_SECS", "21600"))
BUNDLE_FORMAT = "loki.homelab.bundle/1"

DNS_PROBE_HOSTS = {"one.one.one.one", "cloudflare.com"}

# Hermes escalation worker (planned home: razr, over Tailscale — same pattern
# as the browser worker). Unset until Hermes is deployed; bundles are stored
# locally either way and hand-off is best-effort.
HERMES_WORKER_URL = os.getenv("HERMES_WORKER_URL", "").rstrip("/")
HERMES_WORKER_TOKEN = os.getenv("HERMES_WORKER_TOKEN", "")

enabled = True
_registry: Optional[homelab_assets.Registry] = None
_session_factory = None      # async () -> aiohttp.ClientSession (bound by loki_bot)
_interface_status = None     # () -> {worker: {"configured","alive"}}  (loki_bot)
_interface_restart = None    # async (worker) -> bool                  (loki_bot)


def bind(session_factory=None, interface_status=None, interface_restart=None):
    global _session_factory, _interface_status, _interface_restart
    _session_factory = session_factory
    _interface_status = interface_status
    _interface_restart = interface_restart


def _declared_backup_dirs() -> list[str]:
    """Backup destinations declared by the registry's `updates.backup.dir`."""
    out = []
    for asset in _reg().assets.values():
        d = ((asset.get("updates") or {}).get("backup") or {}).get("dir")
        if d:
            out.append(os.path.realpath(str(d)))
    return out


def _under_declared_backup_dir(path: str) -> bool:
    """True when `path` sits inside a declared backup directory. Uses realpath
    so a symlink or `..` cannot escape the allowed tree."""
    try:
        target = os.path.realpath(os.path.abspath(str(path)))
    except (OSError, ValueError):
        return False
    for base in _declared_backup_dirs():
        if target == base or target.startswith(base + os.sep):
            return True
    return False


def _reg() -> homelab_assets.Registry:
    global _registry
    if _registry is None:
        _registry = homelab_assets.load()
        policy.configure(_registry.allowed_values())
    return _registry


def reload_registry():
    """Re-read the YAML (used by tests and after registry edits)."""
    global _registry
    _registry = homelab_assets.load(force=True)
    policy.configure(_registry.allowed_values())
    return _registry


# ── Redaction ──────────────────────────────────────────────────────────────
_MAC_RE = re.compile(r"\b(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b")
_WGKEY_RE = re.compile(r"\b[A-Za-z0-9+/]{42,44}=")


def redact(text: str) -> str:
    """Secrets (tools' scrubber) + MAC addresses + WireGuard keys."""
    out = tools._scrub_text(text or "")
    out = _MAC_RE.sub("[MAC]", out)
    return _WGKEY_RE.sub("[KEY]", out)


# ── Incident store ─────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    incident_id  TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL DEFAULT '',
    asset        TEXT NOT NULL,
    symptom      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'diagnosing',
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    diagnosis    TEXT NOT NULL DEFAULT '',
    checks_json  TEXT NOT NULL DEFAULT '[]',
    repair_json  TEXT NOT NULL DEFAULT 'null',
    repair_result_json TEXT NOT NULL DEFAULT 'null',
    bundle_json  TEXT NOT NULL DEFAULT 'null',
    commands_json TEXT NOT NULL DEFAULT '[]',
    hermes_json  TEXT NOT NULL DEFAULT 'null',
    hermes_task_id TEXT NOT NULL DEFAULT '',
    hermes_job_id  TEXT NOT NULL DEFAULT '',
    hermes_diagnosis_json TEXT NOT NULL DEFAULT 'null',
    hermes_proposals_json TEXT NOT NULL DEFAULT '[]',
    hermes_escalation_announced INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_incidents_task ON incidents(task_id);
CREATE TABLE IF NOT EXISTS repair_attempts (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    at     REAL NOT NULL
);
"""

# Columns (and anything indexing them) added after the first release — applied
# to pre-existing DBs. Must run strictly AFTER _SCHEMA's executescript: on an
# existing installation the table already exists, so CREATE TABLE IF NOT
# EXISTS is a no-op and these columns are genuinely absent until the ALTERs
# below run. An index on a not-yet-added column belongs here, never in
# _SCHEMA — putting it there breaks executescript() on every upgrade before
# the migration ever gets a chance to add the column.
_MIGRATIONS = (
    "ALTER TABLE incidents ADD COLUMN hermes_json TEXT NOT NULL DEFAULT 'null'",
    "ALTER TABLE incidents ADD COLUMN hermes_task_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE incidents ADD COLUMN hermes_job_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE incidents ADD COLUMN hermes_diagnosis_json TEXT NOT NULL DEFAULT 'null'",
    "ALTER TABLE incidents ADD COLUMN hermes_proposals_json TEXT NOT NULL DEFAULT '[]'",
    "ALTER TABLE incidents ADD COLUMN hermes_escalation_announced INTEGER NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS idx_incidents_hermes_task ON incidents(hermes_task_id)",
)

_conn: Optional[sqlite3.Connection] = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(_SCHEMA)
        for mig in _MIGRATIONS:
            try:
                _conn.execute(mig)
            except sqlite3.OperationalError:
                pass  # column already exists
        _conn.commit()
    return _conn


def _incident_update(incident_id: str, **fields):
    fields["updated_at"] = time.time()
    sets = ", ".join(f"{k}=?" for k in fields)
    _db().execute(f"UPDATE incidents SET {sets} WHERE incident_id=?",
                  [*fields.values(), incident_id])
    _db().commit()


def get_incident(incident_id: str) -> Optional[dict]:
    r = _db().execute("SELECT * FROM incidents WHERE incident_id=?",
                      (incident_id,)).fetchone()
    return dict(r) if r else None


def incident_for_task(task_id: str) -> Optional[dict]:
    r = _db().execute(
        "SELECT * FROM incidents WHERE task_id=? ORDER BY created_at DESC",
        (task_id,)).fetchone()
    return dict(r) if r else None


def _record_attempt(action: str, target: str):
    _db().execute("INSERT INTO repair_attempts (action, target, at) VALUES (?,?,?)",
                  (action, target, time.time()))
    _db().commit()


def _recently_attempted(action: str, target: str,
                        window: int = RESTART_ONCE_WINDOW) -> bool:
    r = _db().execute(
        "SELECT COUNT(*) FROM repair_attempts WHERE action=? AND target=? AND at>?",
        (action, target, time.time() - window)).fetchone()
    return r[0] > 0


# ── Ops facade (everything a runbook may touch) ────────────────────────────
class Ops:
    """The only capability surface a runbook sees. Commands go through the
    policy allowlist; repair-class commands additionally require this Ops to
    have been created with allow_repairs=True."""

    def __init__(self, allow_repairs: bool = False):
        self.allow_repairs = allow_repairs
        self.auto_repair_allowed = allow_repairs
        self.commands_run: list[dict] = []
        self.log_excerpt = ""

    redact = staticmethod(redact)

    async def run(self, name: str, **params) -> tuple[int, str]:
        if policy.is_repair_command(name) and not self.allow_repairs:
            raise policy.PolicyError(
                f"repair command '{name}' refused in read-only mode")
        argv = policy.build_command(name, **params)
        self.commands_run.append({"name": name, "params": params})
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), CMD_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, f"command '{name}' timed out"
        text = out.decode(errors="replace")[:OUTPUT_CAP]
        if name == "docker_logs_tail":
            self.log_excerpt = redact(text)[-LOG_EXCERPT_CAP:]
        return proc.returncode or 0, text

    async def run_to_file(self, name: str, out_path: str, **params) -> tuple[int, str]:
        """Run an allowlisted command with stdout streamed straight to a file.

        Used for database dumps: keeps a multi-gigabyte archive out of memory
        AND avoids a shell, so there is still no redirection operator anywhere
        a caller could influence. The destination must be inside a
        registry-declared backup directory."""
        if not self.allow_repairs and policy.is_repair_command(name):
            raise policy.PolicyError(
                f"repair command '{name}' refused in read-only mode")
        if not _under_declared_backup_dir(out_path):
            raise policy.PolicyError(
                "backup destination is not a registry-declared backup directory")
        argv = policy.build_command(name, **params)
        self.commands_run.append({"name": name, "params": params,
                                  "stdout_to": out_path})
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as fh:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=fh, stderr=asyncio.subprocess.PIPE)
            try:
                _, err = await asyncio.wait_for(proc.communicate(), CMD_TIMEOUT * 30)
            except asyncio.TimeoutError:
                proc.kill()
                return 124, f"command '{name}' timed out"
        return proc.returncode or 0, err.decode(errors="replace")[:OUTPUT_CAP]

    async def run_from_file(self, name: str, in_path: str, **params) -> tuple[int, str]:
        """Run an allowlisted command with stdin read from a file (dump
        verification). Read-only with respect to the source file."""
        if not self.allow_repairs and policy.is_repair_command(name):
            raise policy.PolicyError(
                f"repair command '{name}' refused in read-only mode")
        if not _under_declared_backup_dir(in_path):
            raise policy.PolicyError(
                "input file is not inside a registry-declared backup directory")
        argv = policy.build_command(name, **params)
        self.commands_run.append({"name": name, "params": params,
                                  "stdin_from": in_path})
        with open(in_path, "rb") as fh:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdin=fh, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), CMD_TIMEOUT * 10)
            except asyncio.TimeoutError:
                proc.kill()
                return 124, f"command '{name}' timed out"
        return proc.returncode or 0, out.decode(errors="replace")[:OUTPUT_CAP]

    async def http_get(self, url: str, timeout: int = HTTP_TIMEOUT):
        """GET one of the registry-declared endpoints (health, or an asset's
        own version API). Any other URL is refused — runbooks cannot be used
        as a generic HTTP client."""
        allowed = set()
        for asset in _reg().assets.values():
            h = asset.get("health") or {}
            allowed.update(v for v in (h.get("local_url"), h.get("public_url")) if v)
            u = asset.get("updates") or {}
            if u.get("version_api"):
                allowed.add(u["version_api"])
        if url not in allowed:
            raise policy.PolicyError(f"URL not declared in the asset registry")
        import aiohttp
        try:
            if _session_factory is not None:
                sess = await _session_factory()
                close = False
            else:
                sess = aiohttp.ClientSession()
                close = True
            try:
                async with sess.get(
                        url, timeout=aiohttp.ClientTimeout(total=timeout),
                        allow_redirects=False) as r:
                    body = (await r.text())[:500]
                    return r.status, body
            finally:
                if close:
                    await sess.close()
        except Exception as e:
            return 0, f"{type(e).__name__}"

    async def path_meta(self, path: str) -> dict:
        """Existence/permission metadata for a registry-declared path."""
        if path not in _reg().allowed_values()["path"]:
            return {"exists": False, "error": "path not declared in registry"}
        def _stat():
            try:
                st = os.stat(path)
                entries = None
                empty = False
                if os.path.isdir(path):
                    names = os.listdir(path)
                    entries = len(names)
                    empty = entries == 0
                return {"exists": True, "mode": oct(st.st_mode & 0o777),
                        "uid": st.st_uid, "gid": st.st_gid,
                        "entries": entries, "empty": empty}
            except OSError as e:
                return {"exists": False, "error": type(e).__name__}
        return await asyncio.get_event_loop().run_in_executor(None, _stat)

    async def lease_meta(self, path: str) -> dict:
        """Safe DHCP lease metadata: count + newest age. Never MACs/hostnames."""
        if path not in _reg().allowed_values()["path"]:
            return {"ok": False, "error": "path not declared in registry"}
        def _read():
            try:
                with open(path) as f:
                    lines = [ln.split() for ln in f.read(64 * 1024).splitlines() if ln.strip()]
                stamps = [int(p[0]) for p in lines if p and p[0].isdigit()]
                newest = max(stamps) if stamps else None
                return {"ok": True, "count": len(lines),
                        "newest_age_secs": (abs(int(time.time()) - newest)
                                            if newest else None)}
            except OSError as e:
                return {"ok": False, "error": type(e).__name__}
        return await asyncio.get_event_loop().run_in_executor(None, _read)

    async def dns_check(self, host: str) -> bool:
        if host not in DNS_PROBE_HOSTS:
            raise policy.PolicyError("DNS probe host not allowlisted")
        loop = asyncio.get_event_loop()
        try:
            await asyncio.wait_for(
                loop.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP), 5)
            return True
        except Exception:
            return False

    async def joplin_ping(self) -> bool:
        try:
            import joplin_integration as jp
            return jp.is_configured() and await jp.ping()
        except Exception:
            return False

    async def joplin_sync_health(self) -> dict:
        try:
            import joplin_integration as jp
            h = jp.sync_health()
            return {"healthy": h.get("state") == "healthy",
                    "detail": redact(f"{h.get('state')}: {h.get('detail', '')}")[:200]}
        except Exception as e:
            return {"healthy": False, "detail": f"sync health unreadable "
                                                f"({type(e).__name__})"}

    async def interface_status(self) -> Optional[dict]:
        """In-process worker state via the loki_bot hook; None standalone."""
        if _interface_status is None:
            return None
        try:
            return _interface_status()
        except Exception:
            log.exception("interface_status hook failed")
            return None

    async def interface_restart(self, worker: str) -> bool:
        if not self.allow_repairs:
            raise policy.PolicyError("interface restart refused in read-only mode")
        if _interface_restart is None:
            return False
        try:
            return bool(await _interface_restart(worker))
        except Exception:
            log.exception("interface_restart hook failed")
            return False

    async def attempted(self, action: str, target: str) -> bool:
        return _recently_attempted(action, target)

    async def record_attempt(self, action: str, target: str):
        _record_attempt(action, target)

    async def sleep(self, secs: float):
        await asyncio.sleep(secs)


# ── Runbook execution ──────────────────────────────────────────────────────
def _runbooks():
    from maintenance_runbooks import RUNBOOKS
    return RUNBOOKS


async def run_runbook(asset: dict, allow_repairs: bool) -> tuple[dict, Ops]:
    # Executor first: for a remote-executor asset "has no runbook" is true but
    # useless — it reads like a gap to improvise around rather than a pointer
    # to the tools that actually serve this asset.
    if not homelab_assets.Registry.is_local(asset):
        raise policy.PolicyError(
            f"asset '{asset['key']}' is served by the "
            f"'{homelab_assets.Registry.executor(asset)}' executor, not a local "
            f"shell. Use the registered read-only tools for it (nas_status, "
            f"tracearr_status, tracearr_diagnose, tracearr_update_check) — do "
            f"not fall back to manual docker instructions.")
    name = asset.get("runbook")
    fn = _runbooks().get(name)
    if fn is None:
        raise policy.PolicyError(f"asset '{asset['key']}' has no runbook")
    if not _reg().on_this_host(asset):
        raise policy.PolicyError(
            f"asset '{asset['key']}' lives on {asset.get('host')} — this "
            f"controller runs on {socket.gethostname()} and will not act remotely")
    ops = Ops(allow_repairs=allow_repairs)
    result = await fn(asset, ops)
    result["runbook"] = name
    return result, ops


# ── Escalation bundle (contract for a future Hermes agent) ─────────────────
def _lifecycle_context(asset: dict, user_requested: bool) -> dict:
    """Lifecycle state + the Boss's intent, attached to every escalation bundle
    so Hermes always knows whether an asset is supposed to be alive at all."""
    try:
        import homelab_lifecycle as lifecycle
    except Exception:
        return {"lifecycle": [], "boss_intent": "unknown"}
    subjects = _reg().containers(asset) or [asset["key"]]
    return {
        "lifecycle": [lifecycle.classify(s) | {"record": None} for s in subjects],
        "boss_intent": ("explicit investigation request" if user_requested
                        else "automatic escalation from a runbook"),
    }


def build_escalation_bundle(asset: dict, symptom: str, result: dict,
                            ops: Ops, attempts: list[dict],
                            user_requested: bool = False) -> dict:
    tiers = policy.ACTION_TIERS
    service_state = {c["name"]: redact(c["detail"])
                     for c in result.get("checks", [])}
    return {
        "format": BUNDLE_FORMAT,
        "asset": asset["key"],
        "asset_type": asset.get("type"),
        "host": asset.get("host"),
        **_lifecycle_context(asset, user_requested),
        "symptom": redact(symptom)[:300],
        "runbook": result.get("runbook"),
        "checks": [{"name": c["name"], "ok": c["ok"],
                    "detail": redact(c["detail"])[:300]}
                   for c in result.get("checks", [])][:40],
        "service_state": service_state,
        "logs": redact(ops.log_excerpt)[:LOG_EXCERPT_CAP],
        "previous_repair_attempts": attempts[:10],
        "allowed_actions": sorted(a for a, t in tiers.items()
                                  if t in (policy.AUTO, policy.APPROVAL)),
        "prohibited_actions": sorted(a for a, t in tiers.items()
                                     if t == policy.MANUAL),
    }


async def hermes_handoff(bundle: dict) -> dict:
    """Legacy fallback: best-effort delivery of an escalation bundle when the
    supervised hermes_escalation task type isn't registered (homelab_hermes
    failed to import, or task_supervisor is unavailable). The normal path is
    _escalate_to_hermes() below, which tracks a real job. Never raises; the
    bundle stays stored locally regardless, and no LLM is invoked from here."""
    if not (HERMES_WORKER_URL and HERMES_WORKER_TOKEN):
        return {"delivered": False, "reason": "hermes worker not configured"}
    import aiohttp
    try:
        if _session_factory is not None:
            sess = await _session_factory()
            close = False
        else:
            sess = aiohttp.ClientSession()
            close = True
        try:
            async with sess.post(
                    f"{HERMES_WORKER_URL}/v1/escalations", json=bundle,
                    headers={"Authorization": f"Bearer {HERMES_WORKER_TOKEN}"},
                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                return {"delivered": r.status in (200, 201, 202),
                        "status": r.status, "at": int(time.time())}
        finally:
            if close:
                await sess.close()
    except Exception as e:
        return {"delivered": False, "reason": type(e).__name__,
                "at": int(time.time())}


def _hash_diagnosis(d: dict) -> str:
    return hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest()


async def _escalate_to_hermes(asset: dict, symptom: str, bundle: dict,
                              incident_id: str, origin_row: dict,
                              user_requested: bool = False) -> tuple[str, dict]:
    """Hand an incident to Hermes as a supervised background task, tied to the
    SAME originating requester/channel as origin_row (a task row, or a plain
    dict with requester_id/requester_name/channel_id). Returns
    ("escalating_to_hermes", {"hermes_task_id": ...}) on success,
    ("suppressed", {...}) when the asset's lifecycle state forbids it, or
    ("escalated", {}) to signal the caller should fall back to the old
    best-effort hermes_handoff (bridge not wired up)."""
    try:
        import homelab_lifecycle as lifecycle
        subjects = _reg().containers(asset) or [asset["key"]]
        blocked = []
        for subject in subjects:
            allowed, why = lifecycle.hermes_allowed(subject, user_requested)
            if not allowed:
                blocked.append(why)
        if blocked and len(blocked) == len(subjects):
            log.info("hermes escalation suppressed for %s: %s",
                     asset["key"], "; ".join(blocked))
            return "suppressed", {"reason": blocked[0]}
    except Exception:
        log.exception("lifecycle gate unavailable — allowing escalation")
    try:
        import task_supervisor as ts
        import homelab_hermes as hh
    except Exception:
        return "escalated", {}
    tt = ts._TYPES.get("hermes_escalation")
    if tt is None or not hh.enabled:
        return "escalated", {}
    follow_ctx = tools.ToolContext(
        user_id=str(origin_row.get("requester_id", "")),
        user_name=origin_row.get("requester_name", "Boss"),
        channel_id=origin_row.get("channel_id", ""))
    task_id = ts.submit(tt, follow_ctx, {
        "asset": asset["key"], "incident_id": incident_id,
        "symptom": symptom, "bundle": bundle})
    return "escalating_to_hermes", {"hermes_task_id": task_id}


async def _hermes_handler(h):
    """The hermes_escalation supervised task: submits (or, after a restart,
    reattaches to) a Hermes bridge job and polls it to a terminal state.
    Never runs a repair — Hermes only diagnoses and proposes; any state
    change goes back through the draft-approval gate via hermes_escalate."""
    import task_supervisor as ts
    import homelab_hermes as hh
    inp = h.input
    incident_id = inp.get("incident_id", "")
    ext = h.external_ref
    if not ext:
        try:
            job = await hh.submit_diagnosis(
                inp["asset"], inp.get("symptom", ""), inp["bundle"], incident_id,
                request_id=h.row.get("request_id"))
        except hh.HermesBridgeError as e:
            if incident_id:
                _incident_update(incident_id, status="hermes_failed")
            return ts.TaskResult("failed", error_category="bridge_unreachable",
                                 error_detail=str(e),
                                 summary=f"Hermes bridge unreachable: {e}")
        ext = job["id"]
        h.set_external_ref(ext)
        if incident_id:
            _incident_update(incident_id, hermes_job_id=ext, status="hermes_triage")
        log.info("task %s -> hermes job %s", h.task_id, ext)

    while True:
        if h.cancelled():
            try:
                await hh.cancel_job(ext)
            except hh.HermesBridgeError:
                pass
            if incident_id:
                _incident_update(incident_id, status="hermes_cancelled")
            return ts.TaskResult("cancelled", summary="cancelled at the Hermes bridge")
        try:
            job = await hh.get_job(ext)
        except hh.HermesBridgeError as e:
            return ts.TaskResult("failed", error_category="bridge_unreachable",
                                 error_detail=str(e),
                                 summary="lost contact with the Hermes bridge")
        await h.beat()

        # If the asset was decommissioned while this job was in flight, the
        # result is audit history, not an operational signal: stop polling,
        # reopen nothing, and do not announce a diagnosis for something the
        # Boss already retired.
        if not _hermes_result_actionable(incident_id):
            try:
                await hh.cancel_job(ext)
            except hh.HermesBridgeError:
                pass
            return ts.TaskResult("cancelled",
                                 summary="asset was intentionally decommissioned "
                                         "while this ran — result ignored")

        inc = get_incident(incident_id) if incident_id else None
        if job.get("escalated") and inc and not inc.get("hermes_escalation_announced"):
            _incident_update(incident_id, hermes_escalation_announced=1)
            if ts._send is not None and h.row.get("channel_id"):
                try:
                    await ts._send(
                        h.row["channel_id"],
                        "🔎 Hermes needed a deeper look and escalated to its stronger "
                        "model — still working, I'll follow up.")
                except Exception:
                    log.exception("hermes escalation notice failed")

        diagnosis = job.get("diagnosis") or {}
        if incident_id:
            _incident_update(
                incident_id,
                hermes_diagnosis_json=json.dumps(diagnosis) if diagnosis else "null",
                hermes_proposals_json=json.dumps(job.get("proposals", [])))

        asset = _reg().get(inp.get("asset", ""))
        display = asset.get("display_name", inp.get("asset", "asset")) if asset else inp.get("asset", "asset")
        root = redact(str(diagnosis.get("probable_root_cause", "")))[:400]

        s = job.get("state")
        if s == "completed":
            if incident_id:
                _incident_update(incident_id, status="hermes_completed")
            return ts.TaskResult("completed",
                                 summary=f"{display}: Hermes diagnosis — {root}"[:780])
        if s == "needs_approval":
            if incident_id:
                _incident_update(incident_id, status="hermes_needs_approval")
            return ts.TaskResult(
                "completed",
                summary=(f"⚠️ {display}: Hermes found a likely cause but the proposed "
                        f"fix needs your approval — {root} Ask me about it when "
                        f"you're ready to review.")[:780])
        if s == "failed":
            if incident_id:
                _incident_update(incident_id, status="hermes_failed")
            return ts.TaskResult("failed", error_category="hermes_diagnosis_failed",
                                 summary=f"{display}: Hermes could not complete a diagnosis.")
        if s == "cancelled":
            if incident_id:
                _incident_update(incident_id, status="hermes_cancelled")
            return ts.TaskResult("cancelled", summary=f"{display}: Hermes job cancelled.")
        if s == "paused_auth":
            if incident_id:
                _incident_update(incident_id, status="hermes_paused_auth")
            return ts.TaskResult("paused_auth",
                                 summary="Hermes needs a provider credential refresh on razr.")
        if s == "paused_quota":
            if incident_id:
                _incident_update(incident_id, status="hermes_paused_quota")
            return ts.TaskResult("paused_quota",
                                 summary="Hermes budget/quota exhausted for today.")
        await asyncio.sleep(hh.POLL_SECS)


def _hermes_result_actionable(incident_id: str) -> bool:
    """False once the incident has been closed as an intentional decommission.
    Kept as a thin wrapper so the Hermes handler still works if the lifecycle
    module fails to import."""
    try:
        import homelab_lifecycle as lifecycle
    except Exception:
        return True
    try:
        return lifecycle.hermes_result_actionable(incident_id)
    except Exception:
        log.exception("lifecycle actionability check failed")
        return True


async def _hermes_on_cancel(row: dict):
    import homelab_hermes as hh
    ext = row.get("external_ref")
    if ext:
        try:
            await hh.cancel_job(ext)
        except Exception:
            pass


def _maybe_register_hermes() -> bool:
    try:
        import homelab_hermes as hh  # noqa: F401
        import task_supervisor as ts
    except Exception:
        return False
    ts.register_type(ts.TaskType(
        name="hermes_escalation", handler=_hermes_handler, permission="boss",
        capabilities=frozenset({"hermes_bridge"}),
        default_timeout=int(os.getenv("HERMES_TASK_TIMEOUT_SECS", "900")),
        max_retries=0, reattach=True, priority=10,
        on_cancel=_hermes_on_cancel,
        title=lambda i: f"Hermes escalation: {i.get('asset', '?')}",
    ))
    return True


def _resolve_incident_ref(ident: str) -> Optional[dict]:
    """Accept a Loki incident_id (hi_...), a Loki task_id (lt_...), or a raw
    Hermes bridge job id (hj_...) and resolve to the incident row."""
    ident = (ident or "").strip()
    if not ident:
        return None
    inc = get_incident(ident)
    if inc:
        return inc
    r = _db().execute(
        "SELECT * FROM incidents WHERE hermes_task_id=? OR hermes_job_id=? "
        "ORDER BY created_at DESC LIMIT 1", (ident, ident)).fetchone()
    return dict(r) if r else None


def _attempts_for(asset: dict) -> list[dict]:
    targets = set(_reg().containers(asset)) | {asset["key"]}
    rows = _db().execute(
        "SELECT action, target, at FROM repair_attempts "
        "ORDER BY at DESC LIMIT 50").fetchall()
    return [{"action": r["action"], "target": r["target"],
             "at": int(r["at"])} for r in rows if r["target"] in targets]


# ── The homelab_incident supervised task ───────────────────────────────────
def _plan_hash(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


async def _incident_handler(h):
    import task_supervisor as ts
    inp = h.input
    asset = _reg().get(inp["asset"])
    if asset is None:
        return ts.TaskResult("failed", error_category="unknown_asset",
                             summary="asset vanished from the registry")
    incident_id = inp.get("incident_id") or "hi_" + uuid.uuid4().hex[:12]
    now = time.time()
    _db().execute(
        "INSERT OR IGNORE INTO incidents (incident_id, task_id, asset, symptom,"
        " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (incident_id, h.task_id, asset["key"], inp.get("symptom", ""),
         "diagnosing", now, now))
    _db().commit()
    await h.beat()

    try:
        result, ops = await run_runbook(asset, allow_repairs=True)
    except policy.PolicyError as e:
        _incident_update(incident_id, status="failed", diagnosis=str(e))
        return ts.TaskResult("failed", error_category="policy",
                             error_detail=str(e), summary=str(e))
    await h.beat()

    checks = [{"name": c["name"], "ok": c["ok"],
               "detail": redact(c["detail"])[:300]} for c in result["checks"]]
    fields = dict(
        diagnosis=redact(result.get("diagnosis", ""))[:600],
        checks_json=json.dumps(checks),
        repair_json=json.dumps(result.get("repair")),
        repair_result_json=json.dumps(result.get("repair_result")),
        commands_json=json.dumps([c["name"] for c in ops.commands_run]),
    )

    name = asset.get("display_name", asset["key"])
    rr = result.get("repair_result")
    plan = result.get("repair")
    if result.get("healthy"):
        fields["status"] = "healthy"
        summary = f"{name}: healthy. {result.get('diagnosis', '')}"
    elif rr and rr.get("ok"):
        fields["status"] = "repaired"
        summary = (f"{name}: {result.get('diagnosis', '')} Repaired and "
                   f"verified: {'; '.join(rr.get('steps', []))}")
    elif plan and policy.action_tier(plan["action"]) == policy.APPROVAL:
        fields["status"] = "awaiting_approval"
        summary = (f"{name}: {result.get('diagnosis', '')} Proposed fix needs "
                   f"your approval — say the word and I'll stage it.")
    elif result.get("escalate"):
        bundle = build_escalation_bundle(asset, inp.get("symptom", ""), result,
                                         ops, _attempts_for(asset))
        fields["bundle_json"] = json.dumps(bundle)
        status_word, extra = await _escalate_to_hermes(
            asset, inp.get("symptom", ""), bundle, incident_id, h.row)
        if status_word == "escalating_to_hermes":
            fields["status"] = status_word
            fields.update(extra)
            summary = (f"{name}: {result.get('diagnosis', '')} No deterministic fix "
                       f"— handing this to a closer look now. I'll follow up here.")
        elif status_word == "suppressed":
            # Intentionally retired/ignored: closing quietly is the correct
            # outcome, not a failure to escalate.
            fields["status"] = "closed_intentional_decommission"
            summary = (f"{name}: {extra.get('reason', 'excluded by lifecycle state')}. "
                       f"No incident, no escalation.")
        else:
            fields["status"] = "escalated"
            delivery = await hermes_handoff(bundle)
            fields["hermes_json"] = json.dumps(delivery)
            handoff_note = ("handed to Hermes on razr." if delivery.get("delivered")
                            else "diagnostic bundle saved for escalation.")
            summary = (f"{name}: {result.get('diagnosis', '')} I did not attempt a "
                       f"risky fix — {handoff_note}")
    else:
        fields["status"] = "failed"
        summary = f"{name}: {result.get('diagnosis', 'diagnosis inconclusive')}"

    _incident_update(incident_id, **fields)
    status = "completed" if fields["status"] in (
        "healthy", "repaired", "awaiting_approval", "escalated",
        "escalating_to_hermes", "closed_intentional_decommission") else "failed"
    return ts.TaskResult(status, summary=summary[:780])


def _register_task_type():
    try:
        import task_supervisor as ts
    except Exception:
        return False
    ts.register_type(ts.TaskType(
        name="homelab_incident", handler=_incident_handler, permission="boss",
        capabilities=frozenset({"homelab_runbooks"}),
        default_timeout=int(os.getenv("HOMELAB_TASK_TIMEOUT_SECS", "300")),
        max_retries=1, internal=True, priority=10,
        title=lambda i: f"Homelab incident: {i.get('asset', '?')}",
    ))
    return True


# ── Tools (all Boss-only) ──────────────────────────────────────────────────
def _boss_only(ctx: ToolContext) -> str:
    return "" if user_level(ctx.user_id) == "boss" else \
        "homelab maintenance is Boss-only"


# Assets that exist in the homelab but live on a host this controller cannot
# reach. They are NOT registry entries (no runbook can run against them), but
# naming them explicitly turns "unknown asset" into an accurate answer instead
# of a guess. Keep in sync with skillkit config/hosts.json `enabled: false`.
UNMANAGED_HOSTS = {
    "nas": ("the UGREEN NAS (192.168.1.63)",
            "SSH is off in its UGREEN UI and no credential is installed, so "
            "there is no executor path to it from dex247"),
}
# The NAS itself, Tracearr, and Plex are now REGISTERED assets served by the
# nas_dispatcher executor, so they resolve normally and never reach this map.
# What remains are NAS-hosted services with no registered tooling of their own.
UNMANAGED_ASSETS = {
    "jellyseerr": "nas",
    "home assistant": "nas", "ha": "nas", "watchtower": "nas",
    "192 168 1 63": "nas",
}


def _resolve_or_error(name: str):
    asset = _reg().resolve(str(name or ""))
    if asset is not None:
        return asset, ""
    # Exact alias first, then containment so phrases the Boss actually types
    # ("Tracearr Redis", "the NAS Plex server") still land on the real blocker
    # rather than the generic unknown-asset reply.
    norm = homelab_assets._norm(str(name or ""))
    host_key = UNMANAGED_ASSETS.get(norm)
    if not host_key and norm:
        for alias, hk in UNMANAGED_ASSETS.items():
            if re.search(rf"(?:^| ){re.escape(alias)}(?:$| )", norm):
                host_key = hk
                break
    if host_key:
        where, why = UNMANAGED_HOSTS[host_key]
        return None, (
            f"'{name}' runs on {where}, which is not a managed host: {why}. "
            f"I cannot run diagnostics against it, and I must NOT substitute "
            f"manual docker/ssh instructions for the Boss to run by hand. "
            f"Report this blocker and stop.")
    known = ", ".join(sorted(a.get("display_name", k)
                             for k, a in _reg().assets.items()))
    return None, (f"unknown asset — I know: {known}. Do not improvise manual "
                  f"docker/ssh steps for an asset I do not manage.")


async def _tool_diagnose(args: dict, ctx: ToolContext) -> str:
    import task_supervisor as ts
    asset, err = _resolve_or_error(args.get("asset"))
    if err:
        return json.dumps({"ok": False, "error": err})
    symptom = redact(str(args.get("symptom", "")))[:300]
    tt = ts._TYPES.get("homelab_incident")
    if tt is None:
        return json.dumps({"ok": False, "error": "incident task type unavailable"})
    task_id = ts.submit(tt, ctx, {"asset": asset["key"], "symptom": symptom})
    name = asset.get("display_name", asset["key"])
    return json.dumps({
        "ok": True, "task_id": task_id,
        "say_now": f"Checking {name} now.",
        "note": ("Deterministic runbook is running in the background; the "
                 "diagnosis (and any safe verified repair) will be posted here. "
                 "Reply to the user with the say_now text only — no generic "
                 "troubleshooting advice.")})


async def _tool_status(args: dict, ctx: ToolContext) -> str:
    asset, err = _resolve_or_error(args.get("asset"))
    if err:
        return json.dumps({"ok": False, "error": err})
    try:
        result, _ops = await run_runbook(asset, allow_repairs=False)
    except policy.PolicyError as e:
        return json.dumps({"ok": False, "error": str(e)})
    return json.dumps({
        "ok": True, "asset": asset.get("display_name", asset["key"]),
        "healthy": result.get("healthy", False),
        "diagnosis": redact(result.get("diagnosis", ""))[:400],
        "checks": [f"{'OK' if c['ok'] else 'FAIL'} {c['name']}: "
                   f"{redact(c['detail'])[:120]}"
                   for c in result.get("checks", [])]})


async def _tool_incident_status(args: dict, ctx: ToolContext) -> str:
    inc = incident_for_task(str(args.get("task_id", "")).strip()) \
        or get_incident(str(args.get("task_id", "")).strip())
    if inc is None:
        return json.dumps({"ok": False, "error": "no incident for that task_id"})
    out = {"ok": True, "asset": inc["asset"], "status": inc["status"],
           "symptom": inc["symptom"], "diagnosis": inc["diagnosis"],
           "checks": json.loads(inc["checks_json"] or "[]"),
           "repair": json.loads(inc["repair_json"] or "null"),
           "repair_result": json.loads(inc["repair_result_json"] or "null")}
    bundle = json.loads(inc["bundle_json"] or "null")
    if bundle:
        out["escalation_bundle_ready"] = True
        out["hermes_delivery"] = json.loads(inc.get("hermes_json") or "null")
    return json.dumps(out)


async def _tool_repair(args: dict, ctx: ToolContext) -> str:
    task_id = str(args.get("task_id", "")).strip()
    inc = incident_for_task(task_id) or get_incident(task_id)
    if inc is None:
        return json.dumps({"ok": False, "error": "no incident for that task_id"})
    plan = json.loads(inc["repair_json"] or "null")
    if not plan:
        return json.dumps({"ok": False,
                           "error": "that incident has no stored repair plan — "
                                    "run a fresh diagnosis instead"})
    tier = policy.action_tier(plan["action"])
    asset = _reg().get(inc["asset"])
    if asset is None:
        return json.dumps({"ok": False, "error": "asset no longer in registry"})
    if tier == policy.MANUAL:
        return json.dumps({"ok": False,
                           "error": f"'{plan['action']}' is MANUAL-only — I will "
                                    "not run it; this needs your hands"})
    if tier == policy.AUTO:
        # Re-run the runbook with repairs enabled: it re-validates the exact
        # preconditions, applies idempotently, verifies, and rolls back on
        # failed verification.
        result, ops = await run_runbook(asset, allow_repairs=True)
        rr = result.get("repair_result")
        _incident_update(inc["incident_id"],
                         repair_result_json=json.dumps(rr),
                         status="repaired" if (rr and rr.get("ok")) or
                                result.get("healthy") else inc["status"])
        if result.get("healthy"):
            return json.dumps({"ok": True, "note": "already healthy — nothing to repair"})
        if rr and rr.get("ok"):
            return json.dumps({"ok": True, "repaired": True,
                               "steps": rr.get("steps", [])})
        return json.dumps({"ok": False,
                           "error": "preconditions no longer hold or verification "
                                    "failed — not forcing it",
                           "detail": rr})
    # APPROVAL tier → stage a draft through the existing gate.
    return await tools.execute(
        "homelab_apply_repair",
        json.dumps({"incident_id": inc["incident_id"],
                    "plan_hash": _plan_hash(plan)}), ctx)


async def _tool_runbook_list(args: dict, ctx: ToolContext) -> str:
    reg = _reg()
    which = args.get("asset")
    assets = []
    if which:
        asset, err = _resolve_or_error(which)
        if err:
            return json.dumps({"ok": False, "error": err})
        assets = [asset]
    else:
        assets = list(reg.assets.values())
    out = [{"asset": a.get("display_name", a["key"]), "type": a.get("type"),
            "host": a.get("host"), "runbook": a.get("runbook"),
            "stateful": bool(a.get("stateful")),
            "update_policy": a.get("update_policy", "approval")}
           for a in assets]
    return json.dumps({"ok": True, "runbooks": out,
                       "policy": {"auto": sorted(a for a, t in policy.ACTION_TIERS.items() if t == policy.AUTO),
                                  "approval": sorted(a for a, t in policy.ACTION_TIERS.items() if t == policy.APPROVAL),
                                  "manual": sorted(a for a, t in policy.ACTION_TIERS.items() if t == policy.MANUAL)}})


# ── Hermes escalation tools (all Boss-only) ─────────────────────────────────
async def _tool_hermes_diagnose(args: dict, ctx: ToolContext) -> str:
    """Boss-only escalation entry point for a problem no deterministic runbook
    recognizes. ALWAYS re-runs the asset's own runbook read-only FIRST — Hermes
    is reached only when that runbook itself reports the failure as unfamiliar
    (escalate=True). This makes 'a known runbook never calls Hermes' true in
    code, not merely by prompt instruction."""
    import homelab_hermes as hh
    if not hh.enabled:
        return json.dumps({"ok": False, "error": "Hermes escalation isn't configured"})
    asset, err = _resolve_or_error(args.get("asset"))
    if err:
        return json.dumps({"ok": False, "error": err})
    symptom = redact(str(args.get("symptom", "")))[:300]
    incident_id = str(args.get("incident_id") or "").strip() or ("hi_" + uuid.uuid4().hex[:12])
    name = asset.get("display_name", asset["key"])

    try:
        result, ops = await run_runbook(asset, allow_repairs=False)
    except policy.PolicyError as e:
        return json.dumps({"ok": False, "error": str(e)})

    if not result.get("escalate"):
        # The deterministic runbook already answered this — never call Hermes.
        return json.dumps({
            "ok": True, "used_hermes": False,
            "say_now": f"Checking {name} now.",
            "healthy": result.get("healthy", False),
            "diagnosis": redact(result.get("diagnosis", ""))[:400],
            "note": "The deterministic runbook already resolved this — summarize the "
                    "diagnosis/healthy fields yourself; Hermes was not needed."})

    now = time.time()
    _db().execute(
        "INSERT OR IGNORE INTO incidents (incident_id, task_id, asset, symptom,"
        " status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (incident_id, "", asset["key"], symptom, "diagnosing", now, now))
    _db().commit()
    # Reaching this tool at all IS the Boss explicitly asking for a closer
    # look, so the lifecycle gate is told so — an unmanaged or retired asset
    # can be investigated on request, just never automatically.
    bundle = build_escalation_bundle(asset, symptom, result, ops, _attempts_for(asset),
                                     user_requested=True)
    _incident_update(incident_id, bundle_json=json.dumps(bundle))

    origin_row = {"requester_id": str(ctx.user_id), "requester_name": ctx.user_name,
                  "channel_id": ctx.channel_id}
    status_word, extra = await _escalate_to_hermes(asset, symptom, bundle, incident_id,
                                                   origin_row, user_requested=True)
    if status_word == "suppressed":
        return json.dumps({"ok": False, "error": extra.get("reason", "excluded by "
                                                           "lifecycle state")})
    if status_word != "escalating_to_hermes":
        return json.dumps({"ok": False, "error": "Hermes escalation task type unavailable"})
    _incident_update(incident_id, status=status_word, **extra)

    return json.dumps({
        "ok": True, "used_hermes": True, "incident_id": incident_id,
        "task_id": extra.get("hermes_task_id"),
        "say_now": f"Checking {name} now — this one needs a closer look.",
        "note": ("No runbook matched, so this is running as a background Hermes "
                 "diagnosis (starts on the faster model, escalates to a stronger one "
                 "only if needed). I'll post updates here. Reply with the say_now "
                 "text only — no generic troubleshooting advice.")})


async def _tool_hermes_job_status(args: dict, ctx: ToolContext) -> str:
    import homelab_hermes as hh
    inc = _resolve_incident_ref(str(args.get("job_id", "")))
    if inc is None:
        return json.dumps({"ok": False, "error": "no Hermes job for that id"})
    out = {"ok": True, "asset": inc["asset"], "status": inc["status"],
           "symptom": inc["symptom"]}
    diagnosis = json.loads(inc.get("hermes_diagnosis_json") or "null")
    if diagnosis:
        out["diagnosis"] = {k: (redact(v) if isinstance(v, str) else v)
                            for k, v in diagnosis.items()}
    proposals = json.loads(inc.get("hermes_proposals_json") or "[]")
    if proposals:
        out["proposals"] = proposals
    if inc.get("hermes_job_id") and hh.enabled:
        try:
            job = await hh.get_job(inc["hermes_job_id"])
            out["live_state"] = job.get("state")
            out["cost_usd"] = job.get("cost_usd")
            out["escalated"] = job.get("escalated", False)
        except hh.HermesBridgeError as e:
            out["live_state_error"] = str(e)
    out["note"] = ("Summarize this for the user in your own voice — never quote "
                   "Hermes verbatim, and never paste this into an unrelated message.")
    return json.dumps(out)


async def _tool_hermes_job_cancel(args: dict, ctx: ToolContext) -> str:
    import task_supervisor as ts
    inc = _resolve_incident_ref(str(args.get("job_id", "")))
    if inc is None or not inc.get("hermes_task_id"):
        return json.dumps({"ok": False, "error": "no active Hermes job for that id"})
    return await ts._tool_cancel({"task_id": inc["hermes_task_id"]}, ctx)


async def _tool_hermes_escalate(args: dict, ctx: ToolContext) -> str:
    """Turn a Hermes-proposed fix into a Loki approval draft. Hermes itself
    never executes anything — this is the ONLY path from a Hermes diagnosis to
    a real state change, and it always goes through the existing draft gate."""
    inc = _resolve_incident_ref(str(args.get("job_id", "")))
    if inc is None:
        return json.dumps({"ok": False, "error": "no Hermes job for that id"})
    diagnosis = json.loads(inc.get("hermes_diagnosis_json") or "null")
    if not diagnosis:
        return json.dumps({"ok": False, "error": "Hermes hasn't produced a diagnosis yet"})
    proposed = str(diagnosis.get("proposed_action", "")).strip()
    risk = diagnosis.get("risk_level", "manual")
    if not proposed or proposed.lower() == "none":
        return json.dumps({"ok": True,
                           "note": "Hermes found no action is needed — nothing to approve."})
    if risk == policy.MANUAL:
        return json.dumps({"ok": False,
                           "error": "Hermes' proposed action is MANUAL-tier — I will not "
                                    "stage this for approval; it needs your hands directly."})
    if risk == policy.AUTO:
        return json.dumps({"ok": True,
                           "note": "That diagnosis was read-only (no state change proposed) "
                                    "— nothing to approve."})
    payload = {"incident_id": inc["incident_id"], "diagnosis_hash": _hash_diagnosis(diagnosis)}
    return await tools.execute("hermes_apply_action", json.dumps(payload), ctx)


# ── Hermes-proposed action execution (approval-gated; never auto-applies) ──
def _hermes_apply_prepare(args: dict, ctx: ToolContext):
    incident_id = str(args.get("incident_id", "")).strip()
    inc = get_incident(incident_id)
    if inc is None:
        return {}, "", "no such incident"
    diagnosis = json.loads(inc.get("hermes_diagnosis_json") or "null")
    if not diagnosis:
        return {}, "", "incident has no Hermes diagnosis"
    if _hash_diagnosis(diagnosis) != args.get("diagnosis_hash"):
        return {}, "", ("Hermes' diagnosis changed since this was proposed — "
                        "re-check hermes_job_status")
    summary = (f"{inc['asset']}: {diagnosis.get('proposed_action', '')} "
              f"(Hermes-proposed, incident {incident_id})")[:400]
    return ({"incident_id": incident_id, "diagnosis_hash": args.get("diagnosis_hash")},
            summary, "")


async def _hermes_apply_handler(payload: dict, ctx: ToolContext) -> str:
    """Runs only via tools.run_approved() after a draft is approved. Hermes
    never executes anything itself: if its diagnosis names a registered
    runbook that now applies, this delegates to that runbook's OWN verified
    repair path (the same one homelab_repair uses); otherwise it records the
    Boss's approval for manual follow-through. A Hermes proposal's free text
    is NEVER run as a command — there is no allowlisted way to do that."""
    inc = get_incident(payload.get("incident_id", ""))
    if inc is None:
        raise KeyError("incident vanished")
    diagnosis = json.loads(inc.get("hermes_diagnosis_json") or "null")
    if not diagnosis or _hash_diagnosis(diagnosis) != payload.get("diagnosis_hash"):
        raise ValueError("Hermes diagnosis hash mismatch — refusing to execute")
    matching = diagnosis.get("matching_runbook")
    asset = _reg().get(inc["asset"])
    if matching and asset and asset.get("runbook") == matching:
        result, ops = await run_runbook(asset, allow_repairs=True)
        rr = result.get("repair_result")
        _incident_update(inc["incident_id"],
                         status="repaired" if (rr and rr.get("ok")) or
                                result.get("healthy") else "hermes_needs_approval")
        if result.get("healthy"):
            return "already healthy — nothing left to repair"
        if rr and rr.get("ok"):
            return f"repaired and verified: {'; '.join(rr.get('steps', []))}"
        return ("approved, but the runbook's own preconditions no longer hold — "
                "re-diagnose before trying again")
    _incident_update(inc["incident_id"], status="hermes_approved_manual")
    return ("approved — Hermes' proposed action has no automated path (it wasn't a "
            "registered runbook fix), so this needs to be carried out by hand: "
            f"{redact(diagnosis.get('proposed_action', ''))[:300]}")


# ── Container update inventory (read-only) ─────────────────────────────────
async def _update_status_for(container: str, asset: dict, ops: Ops) -> dict:
    entry = {"container": container, "asset": asset["key"],
             "stateful": bool(asset.get("stateful")),
             "approval_required": True,      # image updates are never auto
             "backup_required": bool(asset.get("stateful"))}
    rc, out = await ops.run("docker_inspect", container=container)
    if rc != 0:
        entry["error"] = "container not inspectable"
        return entry
    try:
        info = json.loads(out)[0]
    except (ValueError, IndexError):
        entry["error"] = "bad inspect output"
        return entry
    image = info.get("Config", {}).get("Image", "")
    entry["image"] = image
    entry["compose_project"] = info.get("Config", {}).get("Labels", {}).get(
        "com.docker.compose.project", "")
    ports = info.get("HostConfig", {}).get("PortBindings") or {}
    lan_exposed = any(b.get("HostIp", "") in ("", "0.0.0.0", "::")
                     for binds in ports.values() for b in (binds or []))
    public = bool((asset.get("health") or {}).get("public_url"))
    entry["exposure"] = ("internet (reverse proxy)" if public
                         else "lan" if lan_exposed else "local")
    # The image ref comes from an allowlisted inspect of a registry-declared
    # container — extend the allowed value set with it for the image commands.
    local_digest = ""
    if image:
        policy.add_runtime_values("image", {image})
        rc, out = await ops.run("docker_image_inspect", image=image)
        if rc == 0:
            try:
                img = json.loads(out)[0]
                local_digest = (img.get("RepoDigests") or [""])[0].split("@")[-1] \
                    or img.get("Id", "")
                created = img.get("Created", "")
                entry["image_created"] = created[:10]
                entry["image_age_days"] = _age_days(created)
            except (ValueError, IndexError):
                pass
        entry["local_digest"] = local_digest[:19]
        # Canonical remote index digest (`Digest: sha256:…`) — directly
        # comparable with the local RepoDigest/Id.
        rc, out = await ops.run("docker_imagetools_inspect", image=image)
        remote_digest = ""
        if rc == 0:
            for line in out.splitlines():
                if line.strip().startswith("Digest:"):
                    remote_digest = line.split(":", 1)[1].strip()
                    break
        entry["registry_reachable"] = bool(remote_digest)
        entry["remote_digest"] = remote_digest[:19]
        entry["update_available"] = (
            None if not (remote_digest and local_digest)
            else remote_digest != local_digest)
    rec = ("unknown — registry not reachable" if entry.get("update_available") is None
           else "update available — needs your approval"
           + (" AND a backup first" if entry["backup_required"] else "")
           if entry.get("update_available")
           else "up to date")
    entry["recommendation"] = rec
    return entry


def _age_days(created_iso: str) -> Optional[int]:
    import datetime
    try:
        dt = datetime.datetime.fromisoformat(created_iso.replace("Z", "+00:00"))
        return max(0, (datetime.datetime.now(datetime.timezone.utc) - dt).days)
    except ValueError:
        return None


async def _tool_update_status(args: dict, ctx: ToolContext) -> str:
    reg = _reg()
    service = str(args.get("service", "") or "").strip()
    targets: list[tuple[str, dict]] = []
    for asset in reg.assets.values():
        for c in reg.containers(asset):
            if not service or c == service or reg.resolve(service) is asset:
                targets.append((c, asset))
    if not targets:
        return json.dumps({"ok": False,
                           "error": "service not in the asset registry"})
    ops = Ops(allow_repairs=False)
    out = []
    for container, asset in targets:
        try:
            out.append(await _update_status_for(container, asset, ops))
        except policy.PolicyError as e:
            out.append({"container": container, "error": str(e)})
    return json.dumps({"ok": True, "read_only": True,
                       "note": "nothing was updated — image updates always "
                               "require approval",
                       "services": out})


# ── Approval-gated repair execution (uses the existing draft system) ───────
def _apply_repair_prepare(args: dict, ctx: ToolContext):
    incident_id = str(args.get("incident_id", "")).strip()
    inc = get_incident(incident_id)
    if inc is None:
        return {}, "", "no such incident"
    plan = json.loads(inc["repair_json"] or "null")
    if not plan:
        return {}, "", "incident has no repair plan"
    if _plan_hash(plan) != args.get("plan_hash"):
        return {}, "", "repair plan changed since it was proposed — re-diagnose"
    tier = policy.action_tier(plan["action"])
    if tier != policy.APPROVAL:
        return {}, "", f"'{plan['action']}' is {tier}-tier, not approval-gated"
    summary = (f"{inc['asset']}: {plan.get('description', plan['action'])} "
               f"(incident {incident_id})")
    return ({"incident_id": incident_id, "plan_hash": args.get("plan_hash")},
            summary, "")


async def _apply_repair_handler(payload: dict, ctx: ToolContext) -> str:
    """Runs only via tools.run_approved() after a draft is approved."""
    inc = get_incident(payload.get("incident_id", ""))
    if inc is None:
        raise KeyError("incident vanished")
    plan = json.loads(inc["repair_json"] or "null")
    if not plan or _plan_hash(plan) != payload.get("plan_hash"):
        raise ValueError("repair plan hash mismatch — refusing to execute")
    asset = _reg().get(inc["asset"])
    ops = Ops(allow_repairs=True)
    steps = []
    for cmd in plan.get("commands", []):
        rc, out = await ops.run(cmd["name"], **cmd["params"])
        steps.append(f"{cmd['name']} → rc={rc}")
        if rc != 0:
            _incident_update(inc["incident_id"], status="failed",
                             repair_result_json=json.dumps(
                                 {"ok": False, "steps": steps}))
            raise RuntimeError(f"approved repair step failed: {cmd['name']}")
    await ops.record_attempt(plan["action"], inc["asset"])
    # Verify by re-running the runbook read-only.
    result, _ = await run_runbook(asset, allow_repairs=False)
    ok = result.get("healthy", False)
    _incident_update(inc["incident_id"],
                     status="repaired" if ok else "escalated",
                     repair_result_json=json.dumps(
                         {"ok": ok, "steps": steps, "verified": ok}))
    return ("repair applied and verified healthy" if ok else
            "repair applied but the asset is still unhealthy — escalating")


# ── Registration ───────────────────────────────────────────────────────────
def _p(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required}


def _register_tools():
    register(ToolSpec(
        name="homelab_diagnose",
        description=(
            "Diagnose a KNOWN homelab asset (BLACK-BOXX/AP, Jellyfin, Immich, …) "
            "when the Boss reports a problem: 'X has no internet', 'X is down', "
            "'what's wrong with X'. Runs the asset's deterministic runbook as a "
            "background incident and performs only safe, verified auto-repairs. "
            "Reply with the returned say_now text; never give generic "
            "troubleshooting steps for a known asset."),
        parameters=_p({"asset": {"type": "string",
                                 "description": "asset name or alias"},
                       "symptom": {"type": "string",
                                   "description": "the problem as the Boss stated it"}},
                      ["asset", "symptom"]),
        handler=_tool_diagnose, permission="boss", timeout=30,
    ))
    register(ToolSpec(
        name="homelab_status",
        description=("Read-only health check of a known homelab asset "
                     "('check Immich', 'is Jellyfin ok?'). Runs the runbook's "
                     "checks inline without any repair."),
        parameters=_p({"asset": {"type": "string"}}, ["asset"]),
        handler=_tool_status, permission="boss", timeout=90,
    ))
    register(ToolSpec(
        name="homelab_incident_status",
        description="Status/diagnosis of a homelab incident by its task_id.",
        parameters=_p({"task_id": {"type": "string"}}, ["task_id"]),
        handler=_tool_incident_status, permission="boss", timeout=15,
    ))
    register(ToolSpec(
        name="homelab_repair",
        description=("Execute the stored repair plan of a diagnosed incident "
                     "(by task_id). Auto-tier repairs run verified; "
                     "approval-tier repairs are staged as a draft; manual-tier "
                     "is refused."),
        parameters=_p({"task_id": {"type": "string"}}, ["task_id"]),
        handler=_tool_repair, permission="boss", timeout=120,
    ))
    register(ToolSpec(
        name="homelab_runbook_list",
        description=("List known homelab assets, their runbooks, and the "
                     "automatic/approval/manual action policy."),
        parameters=_p({"asset": {"type": "string"}}, []),
        handler=_tool_runbook_list, permission="boss", timeout=15,
    ))
    # NOTE: the update inventory tool is registered by container_updates.py as
    # `container_update_inventory`, which supersedes this one (it adds release
    # metadata, breaking-change and migration flags, risk and a recommendation).
    # _tool_update_status / _update_status_for stay here as the shared
    # digest-comparison primitive that module and homelab_api.py both build on;
    # only the duplicate tool registration is dropped, so the model is never
    # offered two overlapping inventories.
    register(ToolSpec(
        name="homelab_apply_repair",
        description=("INTERNAL: approval-gated execution of a stored incident "
                     "repair plan. Prefer homelab_repair, which routes here "
                     "automatically when approval is needed."),
        parameters=_p({"incident_id": {"type": "string"},
                       "plan_hash": {"type": "string"}},
                      ["incident_id", "plan_hash"]),
        handler=_apply_repair_handler, permission="boss", timeout=180,
        action_type="homelab_repair", approval_ttl=1800,
        prepare=_apply_repair_prepare,
    ))
    register(ToolSpec(
        name="hermes_diagnose",
        description=(
            "Escalate a homelab problem to Hermes (a restricted, read-only "
            "escalation specialist on razr) when NO deterministic runbook "
            "recognizes it — e.g. 'diagnose this unusual Docker error'. This "
            "ALWAYS re-checks the asset's own deterministic runbook first and "
            "will refuse to call Hermes if that runbook can already answer the "
            "problem, so prefer homelab_diagnose for anything that sounds like "
            "a known failure (BLACK-BOXX/AP connectivity, a service being down). "
            "Hermes only diagnoses and proposes — it never changes anything."),
        parameters=_p({"asset": {"type": "string", "description": "asset name or alias"},
                       "symptom": {"type": "string",
                                   "description": "the problem as the Boss stated it"},
                       "incident_id": {"type": "string",
                                       "description": "optional — reuse an existing incident"}},
                      ["asset", "symptom"]),
        handler=_tool_hermes_diagnose, permission="boss", timeout=90,
    ))
    register(ToolSpec(
        name="hermes_job_status",
        description=("Status/diagnosis of a Hermes escalation by incident id, "
                     "task id, or Hermes job id. Use when asked 'what did Hermes "
                     "find?' — summarize the result yourself, never quote it "
                     "verbatim, and never mention this in an unrelated reply."),
        parameters=_p({"job_id": {"type": "string"}}, ["job_id"]),
        handler=_tool_hermes_job_status, permission="boss", timeout=20,
    ))
    register(ToolSpec(
        name="hermes_job_cancel",
        description="Cancel a running Hermes escalation by incident/task/job id.",
        parameters=_p({"job_id": {"type": "string"}}, ["job_id"]),
        handler=_tool_hermes_job_cancel, permission="boss", timeout=35,
    ))
    register(ToolSpec(
        name="hermes_escalate",
        description=(
            "Stage Hermes' proposed fix as an approval draft — the ONLY way a "
            "Hermes diagnosis can lead to an actual change. Hermes itself never "
            "executes anything. Refuses outright if the proposal is MANUAL-tier; "
            "read-only diagnoses have nothing to approve. Use only on explicit "
            "Boss confirmation to move forward."),
        parameters=_p({"job_id": {"type": "string"}}, ["job_id"]),
        handler=_tool_hermes_escalate, permission="boss", timeout=30,
    ))
    register(ToolSpec(
        name="hermes_apply_action",
        description=("INTERNAL: approval-gated execution of a Hermes-proposed "
                     "action. Prefer hermes_escalate, which routes here."),
        parameters=_p({"incident_id": {"type": "string"},
                       "diagnosis_hash": {"type": "string"}},
                      ["incident_id", "diagnosis_hash"]),
        handler=_hermes_apply_handler, permission="boss", timeout=180,
        action_type="hermes_repair", approval_ttl=1800,
        prepare=_hermes_apply_prepare,
    ))


try:
    _reg()
except Exception as e:
    enabled = False
    log.warning("homelab maintenance disabled — registry load failed: %s", e)

if enabled:
    _db()
    _task_type_ok = _register_task_type()
    _hermes_type_ok = _maybe_register_hermes()
    _register_tools()
    log.info("homelab maintenance online — %d asset(s), incident task type %s, "
             "Hermes escalation %s",
             len(_reg().assets), "ready" if _task_type_ok else "UNAVAILABLE",
             "ready" if _hermes_type_ok else "unavailable (bridge not configured)")

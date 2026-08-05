"""
homelab_monitor.py — conservative automatic homelab monitoring and repair.

Polls every monitored asset roughly every five minutes. Nothing here is a new
monitoring platform: every check reuses the deterministic runbooks, the Ops
command allowlist, and the Hermes escalation path already built in
homelab_maintenance.py / homelab_hermes.py. This module is the state machine
that decides WHEN to look, WHEN a blip becomes an incident, and WHEN (if ever)
to attempt the one pre-approved repair — not a new way to touch the system.

Hard rules, all enforced here rather than left to a prompt:

  - A single failed check is not an incident. Two CONSECUTIVE failures are
    required before anything opens or notifies.
  - ONE active incident per asset, and it stays active until the health
    condition verifies recovered. Reaching "escalated" or "gave up" says what
    automation has left to try; it does NOT close the incident. Repeat
    detections update that one incident (occurrence_count, last_seen) and buy
    nothing: no second incident, no second Hermes job, no repeat notification.
  - Hermes runs AT MOST ONCE per active incident. A submission that never
    reached the bridge is not an escalation and may be retried, but only
    behind a growing backoff and a hard attempt cap. Quota exhaustion is
    recorded on the incident and backed off, never re-attempted per poll.
  - Recovery is verified, not assumed: an incident closes only after
    RECOVERY_THRESHOLD consecutive healthy polls (or a runbook repair that
    verified itself). Only then does a cooldown block a fresh incident, so a
    flapping service still cannot produce one every five minutes.
  - Automatic repair is attempted at most once, except for the one runbook
    that is provably idempotent and already verifies+rolls back
    (BLACK-BOXX's policy-rule restore), which gets a second try if the first
    verification fails.
  - A repair that fails verification stops immediately and escalates. There
    is no retry loop.
  - Every notification goes to the configured Discord operations channel via
    maintenance_notify (never a general chat channel), and only on a real
    state transition, never repeated per poll. Only the urgent categories
    also reach the Boss's Telegram line.
  - State survives a Loki restart: an in-flight incident is reloaded from
    SQLite, not reopened from scratch.
"""

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Optional

import aiohttp

import homelab_lifecycle as lifecycle
import homelab_maintenance as hm
import maintenance_notify as mn
import maintenance_policy as policy

log = logging.getLogger("HomelabMonitor")

POLL_INTERVAL_SECS = int(os.getenv("MONITOR_POLL_INTERVAL_SECS", "300"))
FAILURE_THRESHOLD = int(os.getenv("MONITOR_FAILURE_THRESHOLD", "2"))
COOLDOWN_SECS = int(os.getenv("MONITOR_COOLDOWN_SECS", "1800"))       # 30 min
VERIFY_SETTLE_SECS = int(os.getenv("MONITOR_VERIFY_SETTLE_SECS", "8"))

# Repair attempts per open incident. Everything defaults to one; only the
# asset(s) listed here get a second try, and only because their runbook
# proves the repair is idempotent (checks current state before acting) and
# verifies + rolls back on its own. This set is the ONLY place that "2" can
# come from — nothing else in this module raises the limit.
IDEMPOTENT_DOUBLE_ATTEMPT = frozenset({"black-boxx"})

OPEN, RESOLVED, ESCALATED, GAVE_UP = "open", "resolved", "escalated", "gave_up"

# An incident is ACTIVE until the health condition verifies recovered. Reaching
# "escalated" or "gave_up" is a statement about what automation has left to try,
# NOT about whether the fault is over — closing on those is what produced 297
# incidents and 297 billed Hermes jobs for two faults that never went away.
ACTIVE_STATUSES = (OPEN, ESCALATED, GAVE_UP)

# Consecutive healthy polls required before an active incident closes. The
# mirror image of FAILURE_THRESHOLD: recovery is verified, not assumed.
RECOVERY_THRESHOLD = int(os.getenv("MONITOR_RECOVERY_THRESHOLD", "2"))

# Hermes is billed per job. One SUCCESSFUL escalation per active incident, full
# stop. A submission that never reached the bridge (unreachable/quota) is not
# an escalation, so it may be retried — but only after a growing backoff, and
# only a few times, never once per five-minute poll.
HERMES_MAX_SUBMIT_ATTEMPTS = int(os.getenv("MONITOR_HERMES_MAX_SUBMIT_ATTEMPTS", "3"))
HERMES_BACKOFF_BASE_SECS = int(os.getenv("MONITOR_HERMES_BACKOFF_SECS", "3600"))
HERMES_BACKOFF_MAX_SECS = int(os.getenv("MONITOR_HERMES_BACKOFF_MAX_SECS", "21600"))

# Recurrence evidence is capped: a fault that persists for days at a five-minute
# poll would otherwise grow the row without bound.
EVIDENCE_LIMIT = 20

enabled = True

_notify_boss = None          # async (text) -> None, bound by loki_bot.py
_discord_status_hook = None  # () -> {"ready": bool, "latency_ms": float|None}
_boss_channel_id_fn = lambda: ""  # () -> str, resolved fresh on every escalation
_started = False
_loop_task: Optional[asyncio.Task] = None


def bind(notify_boss=None, discord_status=None, boss_channel_id=None):
    """`boss_channel_id` may be a plain string or a zero-arg callable — a
    callable is preferred since it lets the caller re-resolve Telegram vs.
    Discord DM at escalation time (e.g. Telegram pairs after startup)."""
    global _notify_boss, _discord_status_hook, _boss_channel_id_fn
    _notify_boss = notify_boss
    _discord_status_hook = discord_status
    if boss_channel_id is not None:
        _boss_channel_id_fn = boss_channel_id if callable(boss_channel_id) else (lambda: boss_channel_id)


# ── Durable state (shares the maintenance controller's SQLite file) ────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_checks (
    key                 TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_status         TEXT NOT NULL DEFAULT '',
    last_detail         TEXT NOT NULL DEFAULT '',
    last_checked_at     REAL,
    open_incident_id    TEXT,
    cooldown_until       REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS monitor_incidents (
    incident_id     TEXT PRIMARY KEY,
    key             TEXT NOT NULL,
    display_name    TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'open',
    opened_at       REAL NOT NULL,
    updated_at      REAL NOT NULL,
    closed_at       REAL,
    detection_json  TEXT NOT NULL DEFAULT '{}',
    evidence_json   TEXT NOT NULL DEFAULT '[]',
    repair_json     TEXT NOT NULL DEFAULT 'null',
    repair_attempts INTEGER NOT NULL DEFAULT 0,
    verification_json TEXT NOT NULL DEFAULT 'null',
    result          TEXT NOT NULL DEFAULT '',
    escalated_task_id TEXT NOT NULL DEFAULT '',
    joplin_note_id  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_monitor_incidents_key ON monitor_incidents(key);
CREATE TABLE IF NOT EXISTS monitor_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

# Columns added after the first release — applied to pre-existing DBs, same
# pattern as homelab_maintenance._MIGRATIONS.
_MIGRATIONS = (
    "ALTER TABLE monitor_incidents ADD COLUMN occurrence_count INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE monitor_incidents ADD COLUMN first_seen REAL",
    "ALTER TABLE monitor_incidents ADD COLUMN last_seen REAL",
    "ALTER TABLE monitor_incidents ADD COLUMN fault_signature TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE monitor_incidents ADD COLUMN hermes_attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE monitor_incidents ADD COLUMN hermes_backoff_until REAL NOT NULL DEFAULT 0",
    "ALTER TABLE monitor_incidents ADD COLUMN hermes_block_reason TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE monitor_incidents ADD COLUMN hermes_result TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE monitor_incidents ADD COLUMN boss_alerted INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE monitor_checks ADD COLUMN consecutive_successes INTEGER NOT NULL DEFAULT 0",
)

_migrated_conn = None


def _db():
    global _migrated_conn
    conn = hm._db()
    conn.executescript(_SCHEMA)
    if _migrated_conn is not conn:
        for mig in _MIGRATIONS:
            try:
                conn.execute(mig)
            except sqlite3.OperationalError:
                pass  # column already exists
        _migrated_conn = conn
    conn.commit()
    return conn


def _meta_get(key: str, default: str = "") -> str:
    r = _db().execute("SELECT value FROM monitor_meta WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def _meta_set(key: str, value: str):
    _db().execute("INSERT INTO monitor_meta (key, value) VALUES (?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    _db().commit()


def _get_check(key: str) -> dict:
    r = _db().execute("SELECT * FROM monitor_checks WHERE key=?", (key,)).fetchone()
    if r:
        return dict(r)
    row = {"key": key, "consecutive_failures": 0, "last_status": "", "last_detail": "",
           "last_checked_at": None, "open_incident_id": None, "cooldown_until": 0}
    _db().execute(
        "INSERT INTO monitor_checks (key, consecutive_failures, last_status,"
        " last_detail, cooldown_until) VALUES (?,0,'','',0)", (key,))
    _db().commit()
    return row


def _update_check(key: str, **fields):
    sets = ", ".join(f"{k}=?" for k in fields)
    _db().execute(f"UPDATE monitor_checks SET {sets} WHERE key=?",
                  [*fields.values(), key])
    _db().commit()


def get_incident(incident_id: str) -> Optional[dict]:
    r = _db().execute("SELECT * FROM monitor_incidents WHERE incident_id=?",
                      (incident_id,)).fetchone()
    return dict(r) if r else None


def _insert_incident(row: dict):
    cols = list(row)
    _db().execute(
        f"INSERT INTO monitor_incidents ({','.join(cols)}) VALUES "
        f"({','.join('?' * len(cols))})", [row[c] for c in cols])
    _db().commit()


def _update_incident(incident_id: str, **fields):
    fields["updated_at"] = time.time()
    sets = ", ".join(f"{k}=?" for k in fields)
    _db().execute(f"UPDATE monitor_incidents SET {sets} WHERE incident_id=?",
                  [*fields.values(), incident_id])
    _db().commit()


def _is_active(incident: dict) -> bool:
    return (incident.get("closed_at") is None
            and incident.get("status") in ACTIVE_STATUSES)


def list_active_incidents() -> list[dict]:
    """Every incident whose underlying fault has not verified recovered —
    including escalated and given-up ones, which stay active precisely so a
    recurrence updates them instead of opening a duplicate."""
    marks = ",".join("?" * len(ACTIVE_STATUSES))
    return [dict(r) for r in _db().execute(
        f"SELECT * FROM monitor_incidents WHERE closed_at IS NULL AND "
        f"status IN ({marks}) ORDER BY opened_at", ACTIVE_STATUSES).fetchall()]


# Retained name: "open" has always meant "not finished" to every caller.
list_open_incidents = list_active_incidents


def active_incident_for(key: str) -> Optional[dict]:
    """The one active incident for an asset, if any. This is the dedup key —
    a repeat detection of an ongoing fault attaches here."""
    marks = ",".join("?" * len(ACTIVE_STATUSES))
    r = _db().execute(
        f"SELECT * FROM monitor_incidents WHERE key=? AND closed_at IS NULL AND "
        f"status IN ({marks}) ORDER BY opened_at DESC LIMIT 1",
        (key, *ACTIVE_STATUSES)).fetchone()
    return dict(r) if r else None


def _fault_signature(result: dict) -> str:
    """A stable fingerprint of WHICH checks are failing, so a recurrence can be
    recognised as the same condition. Derived from check names only — never
    from free-text detail, which carries timestamps and would change every
    poll, defeating the very dedup it is meant to support."""
    failing = sorted(str(c.get("name", "")) for c in result.get("checks", [])
                     if not c.get("ok"))
    if not failing:
        failing = [hm.redact(str(result.get("diagnosis", "")))[:80]]
    return hashlib.sha256("|".join(failing).encode()).hexdigest()[:16]


def _record_recurrence(incident: dict, detail: str, signature: str, now: float):
    """Same unresolved fault seen again: update the existing incident. No new
    incident, no new Hermes job, no new notification."""
    incident_id = incident["incident_id"]
    evidence = []
    try:
        evidence = json.loads(incident.get("evidence_json") or "[]")
    except ValueError:
        evidence = []
    if not evidence or evidence[-1] != detail:
        evidence.append(detail)
    fields = {
        "occurrence_count": (incident.get("occurrence_count") or 1) + 1,
        "last_seen": now,
        "evidence_json": json.dumps(evidence[-EVIDENCE_LIMIT:]),
    }
    prior = incident.get("fault_signature") or ""
    if signature and prior and signature != prior:
        # The failing checks changed while the incident is still active. That
        # is a mutation of one ongoing problem, not a second incident — record
        # it and keep the single incident.
        fields["fault_signature"] = signature
        log.info("incident %s fault signature changed %s -> %s",
                 incident_id, prior, signature)
    elif signature and not prior:
        fields["fault_signature"] = signature
    _update_incident(incident_id, **fields)


def recent_incidents(limit: int = 20) -> list[dict]:
    return [dict(r) for r in _db().execute(
        "SELECT * FROM monitor_incidents ORDER BY opened_at DESC LIMIT ?",
        (limit,)).fetchall()]


# ── Notifications ──────────────────────────────────────────────────────────
# Destination is maintenance_notify's decision, not this module's: every
# notification names its event and the router sends it to the Discord ops feed
# (the canonical maintenance feed) or, for the urgent categories only, to the
# Boss's private line as well. `_notify_boss` is the LAST-RESORT relay for an
# unconfigured/unreachable ops channel — loki_bot binds a Discord DM to it, not
# Telegram, so a feed outage can never push maintenance onto the Boss's phone.
# It is also what keeps this module usable standalone.
async def _notify(event: str, text: str):
    async def _boss(t: str):
        if _notify_boss is None:
            log.info("(no notify_boss bound) %s", t)
            return
        await _notify_boss(t)

    try:
        await mn.notify(event, text, fallback=_boss)
    except Exception:
        log.exception("maintenance notification failed [%s]", event)


def _incident_tag(key: str, display_name: str, incident_id: str) -> str:
    return f"{display_name} [{incident_id[-8:]}]"


# ── Escalation (reuses the hermes_escalation task type as-is) ──────────────
def _monitor_escalation_bundle(key: str, display_name: str, symptom: str,
                               checks: list[dict], attempts: list[dict],
                               subjects: Optional[list[str]] = None,
                               user_requested: bool = False) -> dict:
    tiers = policy.ACTION_TIERS
    return {
        "format": hm.BUNDLE_FORMAT,
        "asset": key,
        "asset_type": "monitored_service",
        "host": "dex247",
        # Lifecycle context travels with every bundle so Hermes can never be
        # asked to reason about an asset that was retired on purpose.
        "lifecycle": [lifecycle.classify(s) | {"record": None} for s in (subjects or [key])],
        "boss_intent": ("explicit investigation request" if user_requested
                        else "automatic monitoring escalation"),
        "symptom": hm.redact(symptom)[:300],
        "runbook": None,
        "checks": [{"name": c["name"], "ok": c["ok"], "detail": hm.redact(c["detail"])[:300]}
                   for c in checks][:40],
        "service_state": {c["name"]: hm.redact(c["detail"]) for c in checks},
        "logs": "",   # the monitor never collects raw logs — see docstring
        "previous_repair_attempts": attempts[:10],
        "allowed_actions": sorted(a for a, t in tiers.items()
                                  if t in (policy.AUTO, policy.APPROVAL)),
        "prohibited_actions": sorted(a for a, t in tiers.items() if t == policy.MANUAL),
    }


def _escalation_subjects(key: str, checks: list[dict]) -> list[str]:
    """The container names an escalation would actually be ABOUT. For the
    container sweep the incident key is 'container-sweep', so the lifecycle
    gate has to look at the offending containers, not the check name."""
    names = []
    for c in checks:
        if c.get("name") != "managed_containers" or c.get("ok"):
            continue
        for part in str(c.get("detail", "")).split(";"):
            name = part.split(":", 1)[0].strip()
            if name and name not in names:
                names.append(name)
    return names or [key]


async def _escalate(incident_id: str, key: str, display_name: str, symptom: str,
                    checks: list[dict]) -> Optional[str]:
    """Hand an incident to Hermes via the existing hermes_escalation task type.
    Returns the Loki task_id, or None if the bridge isn't wired up (the
    incident still closes as 'gave_up' with the bundle preserved).

    Hermes is billed per job, so the lifecycle gate runs BEFORE submission: an
    intentionally stopped, ignored, decommissioned or merely unmanaged asset
    never produces an automatic Hermes job."""
    subjects = _escalation_subjects(key, checks)
    blocked = []
    for subject in subjects:
        allowed, why = lifecycle.hermes_allowed(subject, user_requested=False)
        if not allowed:
            blocked.append(why)
    if blocked and len(blocked) == len(subjects):
        log.info("hermes escalation suppressed for %s: %s", key, "; ".join(blocked))
        return None
    try:
        import task_supervisor as ts
        import homelab_hermes as hh
        import tools
    except Exception:
        return None
    tt = ts._TYPES.get("hermes_escalation")
    if tt is None or not hh.enabled:
        return None
    bundle = _monitor_escalation_bundle(key, display_name, symptom, checks, [],
                                        subjects=subjects)
    # The requester marker is load-bearing: it is what tells the task
    # supervisor this lifecycle belongs to the ops feed, not to a chat.
    ctx = tools.ToolContext(user_id=tools.OWNER_USER_ID,
                            user_name=mn.AUTONOMOUS_REQUESTER,
                            channel_id=_boss_channel_id_fn() or mn.OPS_CHANNEL)
    task_id = ts.submit(tt, ctx, {"asset": key, "incident_id": incident_id,
                                  "symptom": symptom, "bundle": bundle})
    _update_incident(incident_id, escalated_task_id=task_id)
    return task_id


# ── Joplin incident summaries (concise, redacted, no raw logs/secrets) ─────
async def _write_joplin_summary(incident: dict):
    try:
        import joplin_integration as jp
    except Exception:
        return
    if not jp.is_configured():
        return
    detection = json.loads(incident.get("detection_json") or "{}")
    verification = json.loads(incident.get("verification_json") or "null")
    repair = json.loads(incident.get("repair_json") or "null")
    lines = [
        f"# Homelab incident — {incident['display_name']}",
        "",
        f"- Detected: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(incident['opened_at']))}",
        f"- Evidence: {hm.redact(detection.get('diagnosis', ''))[:300]}",
        f"- Repair attempted: {repair.get('description') if repair else 'none'}",
        f"- Repair attempts: {incident.get('repair_attempts', 0)}",
        f"- Verification: {'passed' if (verification or {}).get('ok') else 'not confirmed'}"
        if verification is not None else "- Verification: n/a",
        f"- Result: {incident['status']}",
        f"- Resolved: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(incident['closed_at'])) if incident.get('closed_at') else 'n/a'}",
    ]
    if incident.get("escalated_task_id"):
        lines.append(f"- Escalated to Hermes: task {incident['escalated_task_id']}")
    try:
        note = await jp.create_note(
            title=f"Homelab incident — {incident['display_name']} — "
                 f"{time.strftime('%Y-%m-%d', time.gmtime(incident['opened_at']))}",
            body="\n".join(lines), notebook="Loki/Homelab Incidents")
        if note and note.get("id"):
            _update_incident(incident["incident_id"], joplin_note_id=note["id"])
    except Exception:
        log.exception("Joplin incident summary write failed")


def _public_view(result: dict) -> dict:
    return {
        "diagnosis": hm.redact(str(result.get("diagnosis", "")))[:500],
        "checks": [{"name": c.get("name"), "ok": c.get("ok"),
                    "detail": hm.redact(str(c.get("detail", "")))[:300]}
                   for c in result.get("checks", [])][:30],
    }


# ── The state machine ───────────────────────────────────────────────────────
async def _process_check(key: str, display_name: str, check_fn, symptom_label: str):
    """One poll of one monitored key. This function alone decides WHETHER a
    detection becomes (or joins) an incident: the 2-consecutive-failure
    threshold, the one-active-incident-per-asset dedup rule, the recovery
    threshold, and the post-resolution cooldown. What an incident is still
    OWED — repair, escalation, backoff — is _advance_incident's job."""
    row = _get_check(key)
    now = time.time()
    open_id = row.get("open_incident_id")

    try:
        result = await check_fn(False)   # detection is always read-only first
    except Exception as e:
        log.exception("check '%s' crashed", key)
        result = {"healthy": False, "diagnosis": f"check crashed: {type(e).__name__}: {e}",
                  "checks": [], "repair": None, "repair_result": None, "escalate": True}

    healthy = bool(result.get("healthy"))
    detail = hm.redact(str(result.get("diagnosis", "")))[:500]
    # The pointer on monitor_checks is a cache; the incident table is the
    # truth, so a restart or a manually closed row can never strand a fault
    # with no incident (or attach one to an already-closed incident).
    active = active_incident_for(key)
    active_id = active["incident_id"] if active else None
    if active_id != open_id:
        _update_check(key, open_incident_id=active_id)

    if healthy:
        successes = (row.get("consecutive_successes") or 0) + 1
        _update_check(key, consecutive_failures=0, consecutive_successes=successes,
                      last_status="ok", last_detail=detail, last_checked_at=now)
        if active_id and successes >= RECOVERY_THRESHOLD:
            # Recovery is VERIFIED, not assumed: only now does the incident close.
            await _resolve_incident(active_id, result)
        elif active_id:
            log.info("incident %s: %d/%d healthy polls — not closing yet",
                     active_id, successes, RECOVERY_THRESHOLD)
        return

    _update_check(key, consecutive_failures=(row.get("consecutive_failures") or 0) + 1,
                  consecutive_successes=0, last_status="fail",
                  last_detail=detail, last_checked_at=now)

    if active:
        # THE DEDUP RULE. The same unresolved fault seen again updates the one
        # active incident: occurrence count, last_seen, evidence. It never
        # opens a second incident, never notifies again, and never buys a
        # second Hermes job.
        _record_recurrence(active, detail, _fault_signature(result), now)
        await _advance_incident(active_id, key, result, check_fn)
        return

    if now < (row.get("cooldown_until") or 0):
        # Cooldown after a genuine resolution: track the failure but do not
        # open a new incident or notify — this is what stops an alert storm
        # from a service that is flapping in and out of health.
        return

    streak = (row.get("consecutive_failures") or 0) + 1
    if streak < FAILURE_THRESHOLD:
        return   # a single failed check is not an incident

    incident_id = f"mi_{uuid.uuid4().hex[:12]}"
    _insert_incident({
        "incident_id": incident_id, "key": key, "display_name": display_name,
        "status": OPEN, "opened_at": now, "updated_at": now,
        "first_seen": now, "last_seen": now, "occurrence_count": 1,
        "fault_signature": _fault_signature(result),
        "detection_json": json.dumps(_public_view(result)),
        "evidence_json": json.dumps([detail]),
    })
    _update_check(key, open_incident_id=incident_id)
    await _notify("incident_opened",
                  f"🔴 Incident opened — {display_name}: {detail}"[:1500])
    await _advance_incident(incident_id, key, result, check_fn)


async def _advance_incident(incident_id: str, key: str, result: dict, check_fn):
    """Decide what automation still owes an ACTIVE incident. Every exit here is
    idempotent across polls: an incident that has already escalated, already
    spent its repair budget, or is backing off does nothing at all."""
    incident = get_incident(incident_id)
    if incident is None or not _is_active(incident):
        return

    if incident.get("escalated_task_id"):
        # Already handed to Hermes. Follow the job's fate; never buy another.
        await _reconcile_escalation(incident)
        return

    if time.time() < (incident.get("hermes_backoff_until") or 0):
        return   # a previous submission failed; wait out the backoff

    plan = result.get("repair")
    limit = 2 if key in IDEMPOTENT_DOUBLE_ATTEMPT else 1
    attempts = incident.get("repair_attempts", 0)
    if plan is None:
        await _escalate_incident(incident, result, reason="no automatic repair available")
        return
    if attempts >= limit:
        await _escalate_incident(incident, result, reason="repair attempt limit reached")
        return
    await _attempt_repair(incident, key, result, check_fn, plan, attempts, limit)


async def _attempt_repair(incident: dict, key: str, result: dict, check_fn,
                          plan: dict, attempts: int, limit: int):
    incident_id = incident["incident_id"]
    display = incident["display_name"]
    await _notify("repair_started",
                  f"🔧 Repair starting — {display}: "
                  f"{plan.get('description', plan.get('action', 'repair'))}"[:1500])
    attempts += 1
    _update_incident(incident_id, repair_attempts=attempts, repair_json=json.dumps(plan))

    # Re-run the SAME check with repairs enabled. Every runbook here recheck,
    # repairs, verifies, and rolls back within this one call — there is no
    # separate "apply" step for the monitor to loop on.
    try:
        repaired = await check_fn(True)
    except Exception as e:
        log.exception("repair for '%s' crashed", key)
        repaired = {"healthy": False, "diagnosis": f"repair crashed: {type(e).__name__}: {e}",
                   "checks": result.get("checks", []), "repair": plan,
                   "repair_result": {"ok": False}, "escalate": True}

    verification = repaired.get("repair_result")
    _update_incident(incident_id, verification_json=json.dumps(verification))

    if repaired.get("healthy"):
        await _resolve_incident(incident_id, repaired)
        return
    if verification and verification.get("ok"):
        # The repair step itself verified clean, but overall health is still
        # failing for an unrelated reason. Not a failed verification — leave
        # the incident open and let the next poll re-evaluate; no attempt spent.
        return

    # Verification failed, or no verification ran at all. Only escalate once
    # the attempt budget for THIS asset is exhausted — that budget is 1 for
    # everything except the idempotent-double-attempt set, so this is the
    # only place "2" translates into an actual second try rather than an
    # immediate stop.
    if attempts < limit:
        return
    await _notify("repair_failed",
                  f"⚠️ Repair failed verification — {display}. Escalating."[:1500])
    await _escalate_incident(get_incident(incident_id), repaired,
                            reason="repair failed verification")


async def _resolve_incident(incident_id: str, result: dict):
    """The ONLY way an incident closes: the health condition verified recovered."""
    incident = get_incident(incident_id)
    if incident is None or not _is_active(incident):
        return
    rr = result.get("repair_result") or {}
    _update_incident(incident_id, status=RESOLVED, closed_at=time.time(),
                     result=RESOLVED, verification_json=json.dumps(result.get("repair_result")))
    _update_check(incident["key"], cooldown_until=time.time() + COOLDOWN_SECS,
                  open_incident_id=None, consecutive_failures=0,
                  consecutive_successes=0)
    note = " (auto-repaired and verified)" if rr.get("ok") else ""
    occurrences = incident.get("occurrence_count") or 1
    seen = f" after {occurrences} detections" if occurrences > 1 else ""
    await _notify("incident_resolved",
                  f"✅ Resolved — {incident['display_name']}{note}{seen}"[:1500])
    await _write_joplin_summary(get_incident(incident_id))


async def _reconcile_escalation(incident: dict):
    """Track the fate of the ONE Hermes job this incident bought. The job's
    outcome never closes the incident — only recovered health does — but a
    quota/auth stop is recorded so nothing keeps knocking on the bridge."""
    incident_id = incident["incident_id"]
    task_id = incident.get("escalated_task_id") or ""
    try:
        import task_supervisor as ts
    except Exception:
        return
    row = ts.get_task(task_id)
    if row is None:
        return
    status = row.get("status") or ""
    if status == (incident.get("hermes_result") or ""):
        return   # already recorded; nothing new to say
    if status in ("queued", "running"):
        return
    fields = {"hermes_result": status}
    if status in ("paused_quota", "paused_auth", "failed"):
        fields["hermes_block_reason"] = (
            row.get("error_category") or status)
        fields["hermes_backoff_until"] = time.time() + HERMES_BACKOFF_MAX_SECS
    _update_incident(incident_id, **fields)
    log.info("incident %s: hermes task %s ended '%s' — incident stays active "
             "until health recovers", incident_id, task_id, status)

    if status in ("paused_quota", "failed") and not incident.get("boss_alerted"):
        _update_incident(incident_id, boss_alerted=1)
        why = ("Hermes is out of quota" if status == "paused_quota"
               else "the Hermes diagnosis failed")
        await _notify("needs_boss_hands",
                      f"🆘 {incident['display_name']}: {why} and I have no automatic "
                      f"repair left. The incident stays open — needs your hands."[:1500])


async def _escalate_incident(incident: dict, result: dict, reason: str):
    """At most ONE Hermes job per active incident. A submission that never
    reached the bridge is not an escalation, so it may be retried — but behind
    a growing backoff and a hard attempt cap, never once per poll."""
    if incident is None or not _is_active(incident):
        return
    incident_id = incident["incident_id"]
    display = incident["display_name"]
    if incident.get("escalated_task_id"):
        return                                   # already escalated — never again
    attempts = incident.get("hermes_attempts") or 0
    if attempts >= HERMES_MAX_SUBMIT_ATTEMPTS:
        return
    if time.time() < (incident.get("hermes_backoff_until") or 0):
        return

    checks = result.get("checks", [])
    symptom = f"{display}: {reason}. {hm.redact(str(result.get('diagnosis', '')))[:200]}"
    subjects = _escalation_subjects(incident["key"], checks)
    suppressed = all(not lifecycle.hermes_allowed(s)[0] for s in subjects)
    task_id = None
    if not suppressed:
        try:
            task_id = await _escalate(incident_id, incident["key"], display, symptom, checks)
        except Exception:
            log.exception("escalation submission failed for %s", incident_id)

    if suppressed:
        # Intentionally excluded from escalation — say nothing, and stop
        # trying. Alerting about an asset the Boss retired is exactly the
        # noise this module removes. The incident stays active (a recurrence
        # attaches to it) but buys nothing.
        _update_incident(incident_id, status=GAVE_UP, result=GAVE_UP,
                         hermes_attempts=HERMES_MAX_SUBMIT_ATTEMPTS,
                         hermes_block_reason="lifecycle-suppressed")
        log.info("incident %s not escalated (lifecycle-suppressed)", incident_id)
        await _write_joplin_summary(get_incident(incident_id))
        return

    attempts += 1
    if task_id:
        _update_incident(incident_id, status=ESCALATED, result=ESCALATED,
                         hermes_attempts=attempts, hermes_backoff_until=0,
                         hermes_block_reason="")
        await _notify("incident_escalated",
                      f"🆘 Escalated — {display}: handed to Hermes for a closer look "
                      f"(task {task_id[-8:]}). I'll follow up when it reports back."[:1500])
        await _write_joplin_summary(get_incident(incident_id))
        return

    # Submission never landed. Back off — do NOT retry on the next poll.
    backoff = min(HERMES_BACKOFF_BASE_SECS * (2 ** (attempts - 1)),
                  HERMES_BACKOFF_MAX_SECS)
    exhausted = attempts >= HERMES_MAX_SUBMIT_ATTEMPTS
    _update_incident(incident_id, status=GAVE_UP if exhausted else OPEN,
                     result=GAVE_UP if exhausted else "",
                     hermes_attempts=attempts,
                     hermes_backoff_until=time.time() + backoff,
                     hermes_block_reason="hermes unreachable")
    log.info("incident %s: hermes submission %d/%d failed — backing off %ds",
             incident_id, attempts, HERMES_MAX_SUBMIT_ATTEMPTS, backoff)
    if not incident.get("boss_alerted"):
        # Nothing automatic is left and no diagnosis is coming. Said ONCE per
        # incident, not once per poll.
        _update_incident(incident_id, boss_alerted=1)
        await _notify("needs_boss_hands",
                      f"🆘 {display}: {reason}. I could not auto-repair this and "
                      f"Hermes isn't reachable right now — needs your hands."[:1500])
    if exhausted:
        await _write_joplin_summary(get_incident(incident_id))


# Retained entry point: the old name described closing-on-escalation, which is
# exactly the behaviour that was wrong. Escalation no longer closes anything.
_give_up_and_escalate = _escalate_incident


# ── Per-asset check functions ───────────────────────────────────────────────
# Every check function has the shape `async def check(allow_repairs: bool) ->
# dict` and returns the same {"checks","healthy","repair","repair_result",
# "escalate","diagnosis"} shape a runbook returns, so _process_check never has
# to know whether it's looking at a registry-backed runbook or a bespoke
# detect-only probe.

def _mk(checks, healthy, diagnosis, repair=None, repair_result=None, escalate=False):
    return {"checks": checks, "healthy": healthy, "diagnosis": diagnosis,
            "repair": repair, "repair_result": repair_result, "escalate": escalate}


def _runbook_check(asset_key: str):
    """Factory for the five registry-backed, runbook-driven assets. Simply
    re-invokes hm.run_runbook — the SAME function the chat-facing /diagnose
    and /repair tools already use — with allow_repairs threaded through."""
    async def check(allow_repairs: bool) -> dict:
        asset = hm._reg().get(asset_key)
        if asset is None:
            return _mk([], False, f"asset '{asset_key}' not found in registry", escalate=True)
        result, _ops = await hm.run_runbook(asset, allow_repairs)
        return result
    return check


HA_LOCAL_URL = os.getenv("HA_LOCAL_URL", "http://192.168.1.63:8123")


async def _check_ha_local(allow_repairs: bool) -> dict:
    checks = []
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(HA_LOCAL_URL, timeout=aiohttp.ClientTimeout(total=8)) as r:
                ok = r.status < 500
                checks.append({"name": "ha_local_http", "ok": ok, "detail": f"HTTP {r.status}"})
    except Exception as e:
        checks.append({"name": "ha_local_http", "ok": False,
                       "detail": f"{type(e).__name__}: {e}"})
    healthy = all(c["ok"] for c in checks)
    return _mk(checks, healthy,
               "Home Assistant local endpoint reachable." if healthy else
               "Home Assistant local endpoint unreachable — no automatic repair for this asset.",
               escalate=not healthy)


async def _check_career_ops(allow_repairs: bool) -> dict:
    import career_ops
    checks = []
    if not career_ops.enabled:
        checks.append({"name": "career_ops_configured", "ok": True, "detail": "bridge disabled by config"})
        return _mk(checks, True, "Career-Ops bridge is intentionally disabled.")
    try:
        result = await career_ops._api("GET", "/health")
        ok = bool(result and result.get("ok"))
        checks.append({"name": "career_ops_health", "ok": ok, "detail": str(result)[:300]})
    except Exception as e:
        checks.append({"name": "career_ops_health", "ok": False,
                       "detail": f"{type(e).__name__}: {e}"})
    healthy = all(c["ok"] for c in checks)
    return _mk(checks, healthy,
               "Career-Ops bridge healthy." if healthy else
               "Career-Ops bridge unreachable or unhealthy — no automatic repair for this asset.",
               escalate=not healthy)


async def _check_browser_worker(allow_repairs: bool) -> dict:
    import browser_research
    checks = []
    if not browser_research.enabled:
        checks.append({"name": "browser_worker_configured", "ok": True, "detail": "worker disabled by config"})
        return _mk(checks, True, "RAZR browser worker is intentionally disabled.")
    try:
        async with aiohttp.ClientSession() as s:
            headers = {"Authorization": f"Bearer {browser_research.WORKER_TOKEN}"}
            async with s.get(f"{browser_research.WORKER_URL}/health", headers=headers,
                             timeout=aiohttp.ClientTimeout(total=8)) as r:
                ok = r.status == 200
                body = await r.text()
                checks.append({"name": "browser_worker_health", "ok": ok,
                               "detail": f"HTTP {r.status}: {body[:250]}"})
    except Exception as e:
        checks.append({"name": "browser_worker_health", "ok": False,
                       "detail": f"{type(e).__name__}: {e}"})
    healthy = all(c["ok"] for c in checks)
    return _mk(checks, healthy,
               "RAZR browser worker healthy." if healthy else
               "RAZR browser worker unreachable — no automatic repair for this asset (remote host).",
               escalate=not healthy)


async def _check_discord(allow_repairs: bool) -> dict:
    checks = []
    status = _discord_status_hook() if _discord_status_hook else None
    if status is None:
        checks.append({"name": "discord_client", "ok": True, "detail": "no status hook bound"})
        return _mk(checks, True, "Discord status hook not bound; skipping.")
    ready = bool(status.get("ready"))
    checks.append({"name": "discord_ready", "ok": ready, "detail": f"ready={ready}"})
    healthy = ready
    return _mk(checks, healthy,
               "Discord client connected." if healthy else
               "Discord client not connected — this runs inside the same process as the "
               "check, so no separate repair applies; a full Loki restart would be needed.",
               escalate=not healthy)


DISK_MIN_FREE_PCT = int(os.getenv("MONITOR_DISK_MIN_FREE_PCT", "10"))
MEM_MIN_FREE_PCT = int(os.getenv("MONITOR_MEM_MIN_FREE_PCT", "5"))


async def _check_docker_host(allow_repairs: bool) -> dict:
    checks = []
    ops = hm.Ops(allow_repairs=False)

    rc, out = await ops.run("df_root")
    disk_ok = False
    if rc == 0:
        lines = [l for l in out.strip().splitlines() if l.strip()]
        if len(lines) >= 2:
            parts = lines[-1].split()
            pcent_str = parts[-1].rstrip("%") if parts else ""
            try:
                used_pct = int(pcent_str)
                disk_ok = (100 - used_pct) >= DISK_MIN_FREE_PCT
                checks.append({"name": "disk_root", "ok": disk_ok,
                               "detail": f"{lines[-1]} (need >= {DISK_MIN_FREE_PCT}% free)"})
            except ValueError:
                checks.append({"name": "disk_root", "ok": False, "detail": f"unparseable: {lines[-1]}"})
        else:
            checks.append({"name": "disk_root", "ok": False, "detail": "no df output"})
    else:
        checks.append({"name": "disk_root", "ok": False, "detail": f"df failed rc={rc}"})

    rc2, out2 = await ops.run("mem_info")
    mem_ok = False
    if rc2 == 0:
        info = {}
        for line in out2.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                info[k.strip()] = v.strip()
        try:
            total = int(info.get("MemTotal", "0 kB").split()[0])
            avail = int(info.get("MemAvailable", "0 kB").split()[0])
            free_pct = (avail / total * 100) if total else 0
            mem_ok = free_pct >= MEM_MIN_FREE_PCT
            checks.append({"name": "mem_available", "ok": mem_ok,
                           "detail": f"{free_pct:.1f}% available (need >= {MEM_MIN_FREE_PCT}%)"})
        except (ValueError, ZeroDivisionError):
            checks.append({"name": "mem_available", "ok": False, "detail": "unparseable meminfo"})
    else:
        checks.append({"name": "mem_available", "ok": False, "detail": f"cat /proc/meminfo failed rc={rc2}"})

    healthy = disk_ok and mem_ok
    return _mk(checks, healthy,
               "Disk and memory within thresholds." if healthy else
               "Disk or memory below threshold — no automatic repair for host resource "
               "pressure; needs manual attention.",
               escalate=not healthy)


# ── General container sweep (lifecycle-aware) ──────────────────────────────
def _covered_elsewhere() -> set[str]:
    """Containers of registry assets that have their OWN entry in MONITORS —
    the sweep leaves those to their runbook-driven check. A registry asset with
    no MONITORS entry (Immich, today) is deliberately NOT excluded: it is
    managed and expected to run, so the sweep is the only thing that would
    notice it stopping."""
    names = set()
    for key in MONITORS:
        asset = hm._reg().get(key)
        if asset is not None:
            names.update(hm._reg().containers(asset))
    return names


async def _check_container_sweep(allow_repairs: bool) -> dict:
    """Detect-only sweep over every container on the host.

    A container being stopped is NOT, on its own, a problem — that was the bug
    that turned a container the Boss had deliberately retired into an incident
    and then into a billed Hermes job. Before anything can become an incident
    this consults, in order: the lifecycle/decommission registry, the asset
    registry, the expected-running policy, and any pending cleanup state
    (homelab_lifecycle.classify_sweep). Only `managed` + expected-to-be-running
    survives that filter.

    Everything else is inventory: unmanaged containers are counted quietly,
    suppressed ones are silent, a decommissioned container that has come BACK
    is reported once (and never deleted automatically), and a cleanup still
    waiting for approval gets an occasional low-frequency nudge."""
    ops = hm.Ops(allow_repairs=False)
    checks = []
    rc, out = await ops.run("docker_ps_status")
    if rc != 0:
        checks.append({"name": "docker_ps", "ok": False, "detail": f"docker ps failed rc={rc}"})
        return _mk(checks, False, "Could not list containers.", escalate=True)

    rows = [tuple(line.split("\t", 1)) for line in out.strip().splitlines()
            if "\t" in line]
    buckets = lifecycle.classify_sweep(rows, covered_elsewhere=_covered_elsewhere())

    incidents = buckets["incidents"]
    unmanaged = buckets["unmanaged"]
    suppressed = buckets["suppressed"]

    # Quiet inventory — reported as a passing check, never as an outage.
    checks.append({"name": "unmanaged_inventory", "ok": True,
                   "detail": ("; ".join(f"{e['name']}: {e['status']}" for e in unmanaged)[:400]
                              or "none")})
    checks.append({"name": "suppressed_by_lifecycle", "ok": True,
                   "detail": ("; ".join(f"{e['name']} ({e['state']})" for e in
                                        suppressed + buckets["pending_cleanup"])[:400]
                              or "none")})
    checks.append({"name": "managed_containers", "ok": not incidents,
                   "detail": ("; ".join(f"{e['name']}: {e['status']}" for e in incidents)[:400]
                              if incidents else "all managed containers healthy")})

    await _report_reappearances(buckets["reappeared"])
    await _remind_pending_cleanup(buckets["pending_cleanup"])

    if incidents:
        detail = "; ".join(f"{e['name']}: {e['status']}" for e in incidents)[:300]
        return _mk(checks, False,
                   "Managed container(s) expected to be running are "
                   f"stopped/unhealthy: {detail}", escalate=True)
    summary = "All managed containers are healthy."
    if unmanaged:
        summary += f" {len(unmanaged)} unmanaged container(s) idle (inventory only)."
    if suppressed or buckets["pending_cleanup"]:
        summary += (f" {len(suppressed) + len(buckets['pending_cleanup'])} "
                    f"intentionally excluded by lifecycle state.")
    return _mk(checks, True, summary)


async def _report_reappearances(entries: list[dict]):
    """A tombstoned container is running again. Say so exactly once and do
    nothing else — something recreated it deliberately, and automatically
    deleting a resurrected service is never the safe move."""
    for e in entries:
        if not lifecycle.needs_reappearance_notice(e["name"]):
            continue
        lifecycle.mark_reappearance_notified(e["name"])
        await _notify(
            "lifecycle_notice",
            f"⚠️ {e['name']} was previously decommissioned but has reappeared "
            f"({e['status']}). I have not touched it — tell me if it should stay.")


async def _remind_pending_cleanup(entries: list[dict]):
    for e in entries:
        if not lifecycle.due_for_cleanup_reminder(e["name"]):
            continue
        lifecycle.mark_reminder_sent(e["name"])
        await _notify("lifecycle_notice",
                      f"🧹 Reminder: {e['name']} is decommissioned and its cleanup "
                      f"is still waiting on your approval. No rush — it raises no "
                      f"alerts in the meantime.")


# ── Dispatch table: key -> (display_name, check_fn) ─────────────────────────
MONITORS = {
    "black-boxx":       ("BLACK-BOXX", _runbook_check("black-boxx")),
    "jellyfin":         ("Jellyfin", _runbook_check("jellyfin")),
    "joplin":           ("Joplin", _runbook_check("joplin")),
    "cloudflare-ddns":  ("Cloudflare DDNS", _runbook_check("cloudflare-ddns")),
    "loki-interfaces":  ("Loki interfaces (Telegram)", _runbook_check("loki-interfaces")),
    "loki-discord":     ("Loki Discord interface", _check_discord),
    "career-ops-bridge": ("Career-Ops bridge", _check_career_ops),
    "razr-browser-worker": ("RAZR browser worker", _check_browser_worker),
    "home-assistant-local": ("Home Assistant (local)", _check_ha_local),
    "docker-host":      ("Docker host disk/memory", _check_docker_host),
    "container-sweep":  ("Unregistered containers", _check_container_sweep),
}


# ── Periodic maintenance summary (ops feed only) ───────────────────────────
# A digest of what the monitor did on its own, so the feed reads as a record
# rather than a stream of isolated alerts. Silent when nothing happened —
# a recurring "all quiet" is the noise this whole module exists to avoid.
SUMMARY_INTERVAL_SECS = int(os.getenv("MONITOR_SUMMARY_INTERVAL_SECS", "86400"))
_SUMMARY_META_KEY = "last_summary_at"


def _summary_text(since: float) -> str:
    rows = [dict(r) for r in _db().execute(
        "SELECT * FROM monitor_incidents WHERE opened_at>=? OR "
        "(closed_at IS NOT NULL AND closed_at>=?)", (since, since)).fetchall()]
    open_now = list_active_incidents()
    if not rows and not open_now:
        return ""
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    hours = max(1, int(round((time.time() - since) / 3600)))
    lines = [f"📋 Maintenance summary — last {hours}h: {len(rows)} incident(s)"]
    if counts:
        lines.append("  " + ", ".join(
            f"{n} {status.replace('_', ' ')}" for status, n in sorted(counts.items())))
    for r in open_now:
        age = int((time.time() - r["opened_at"]) / 3600)
        seen = r.get("occurrence_count") or 1
        note = f" [{r['hermes_block_reason']}]" if r.get("hermes_block_reason") else ""
        lines.append(f"  ⏳ still open: {r['display_name']} ({age}h, "
                     f"{seen} detections, {r['status']}){note}")
    if not open_now:
        lines.append("  Nothing open right now.")
    return "\n".join(lines)[:1500]


async def maybe_send_summary():
    if SUMMARY_INTERVAL_SECS <= 0:
        return
    now = time.time()
    try:
        last = float(_meta_get(_SUMMARY_META_KEY, "") or 0)
    except ValueError:
        last = 0
    if last <= 0:
        # First run after deploy: start the clock instead of summarising all
        # of history into the first message.
        _meta_set(_SUMMARY_META_KEY, str(now))
        return
    if now - last < SUMMARY_INTERVAL_SECS:
        return
    text = _summary_text(last)
    _meta_set(_SUMMARY_META_KEY, str(now))
    if text:
        await _notify("summary", text)


# ── Poll loop ────────────────────────────────────────────────────────────────
async def poll_once():
    for key, (display_name, check_fn) in MONITORS.items():
        try:
            await _process_check(key, display_name, check_fn, display_name)
        except Exception:
            log.exception("poll of '%s' failed", key)


async def _poll_loop():
    while True:
        try:
            await poll_once()
        except Exception:
            log.exception("homelab monitor poll loop error")
        try:
            await maybe_send_summary()
        except Exception:
            log.exception("maintenance summary failed")
        await asyncio.sleep(POLL_INTERVAL_SECS)


def start():
    global _started, _loop_task
    if _started or not enabled:
        return
    _started = True
    _loop_task = asyncio.create_task(_poll_loop())
    log.info("homelab monitor started (poll every %ss, threshold %s, cooldown %ss)",
             POLL_INTERVAL_SECS, FAILURE_THRESHOLD, COOLDOWN_SECS)

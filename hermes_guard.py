"""
hermes_guard.py — financial circuit breaker for the Hermes/OpenRouter path.

Every billable Hermes request Loki can make flows through ONE call:
homelab_hermes.submit_diagnosis (POST /diagnose on the bridge). This module
gates that call so a runaway loop cannot drain paid OpenRouter credits even if
incident deduplication breaks again. The bridge on razr keeps its own ledger
(per-incident and daily USD caps, turn/wall-clock limits); this guard is the
client-side layer in front of it, so a Loki-side bug is stopped before it even
reaches the bridge.

Circuit:  closed → open → (cooldown) → half_open → closed | open again

  - Billing-class failures (quota exhausted, insufficient credits, billing
    rejection, HTTP 402) open the circuit IMMEDIATELY.
  - Reachability/rate-limit/other failures open it after
    FAILURE_THRESHOLD consecutive failures.
  - While OPEN, zero requests reach the bridge. Deterministic local
    maintenance is untouched — this module gates only the paid path.
  - Recovery is controlled: after the cooldown, reachability-class outages are
    probed via the bridge's non-billable GET /health; billing-class outages
    get exactly ONE real submit as the probe (a submit rejected for credits
    does not bill). A lease stops concurrent probes.
  - Reopening after a failed probe doubles the cooldown up to a bound.

Budgets (all enforced here, before any request leaves the process):
  - HERMES_MAX_REQUESTS_PER_HOUR / _PER_DAY  — rolling windows.
  - HERMES_MAX_SPEND_USD_PER_DAY — enforced on OBSERVED job cost as reported
    by the bridge (`cost_usd`). The bridge's accounting is the OpenRouter
    ledger delta when its spend probe works and a rate-card estimate
    otherwise (marked per-phase by `cost_basis`), so this ceiling is honest
    but not to-the-cent; the bridge's own ledger remains authoritative.
    Estimates count toward the ceiling — conservative in the right direction.

State, request history and observed costs persist in SQLite (the maintenance
stack's HOMELAB_DB_PATH), so the circuit survives a Loki restart.

Provider failure is ONE canonical incident (key "hermes-provider") in the
monitor's incident table — dependent service incidents record "provider
unavailable" on themselves and reference it, they never fork provider copies.
The provider incident is not in the monitor's dispatch table, so it can never
itself escalate to Hermes (no recursion). Self-imposed budget blocks notify
ops but are not provider incidents — the provider did nothing wrong.

Notifications go to the Discord ops feed via maintenance_notify, once per
transition: circuit opened, circuit recovered, ceiling reached. Repeated
blocked calls are counters and throttled logs only. Nothing here touches
Telegram.
"""

import json
import logging
import os
import sqlite3
import time
from typing import Optional

log = logging.getLogger("HermesGuard")

_DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "homelab_incidents.db")

# ── Tunables (env-overridable; conservative defaults) ──────────────────────
MAX_PER_HOUR = int(os.getenv("HERMES_MAX_REQUESTS_PER_HOUR", "6"))
MAX_PER_DAY = int(os.getenv("HERMES_MAX_REQUESTS_PER_DAY", "20"))
# 0 disables the spend ceiling. Default mirrors the bridge's own daily cap.
MAX_SPEND_USD_PER_DAY = float(os.getenv("HERMES_MAX_SPEND_USD_PER_DAY", "5.00"))
FAILURE_THRESHOLD = int(os.getenv("HERMES_GUARD_FAILURE_THRESHOLD", "3"))
COOLDOWN_SECS = int(os.getenv("HERMES_GUARD_COOLDOWN_SECS", "1800"))
BILLING_COOLDOWN_SECS = int(os.getenv("HERMES_GUARD_BILLING_COOLDOWN_SECS", "21600"))
COOLDOWN_MAX_SECS = int(os.getenv("HERMES_GUARD_COOLDOWN_MAX_SECS", "21600"))
PROBE_LEASE_SECS = int(os.getenv("HERMES_GUARD_PROBE_LEASE_SECS", "600"))
BLOCK_LOG_EVERY_SECS = 300

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"
PROVIDER_KEY = "hermes-provider"
PROVIDER_DISPLAY = "Hermes provider (OpenRouter)"

# Bound by homelab_hermes at import: async () -> dict from the bridge's
# non-billable GET /health. Optional — without it, billing-style probing
# (one controlled submit) is used for every class.
_health_probe = None


def bind(health_probe=None):
    global _health_probe
    _health_probe = health_probe


# ── Failure classification ─────────────────────────────────────────────────
_BILLING = ("quota", "insufficient", "credit", "billing", "payment", "402")
_AUTH = ("authentication failed", "re-authenticate", "invalid api key",
         "unauthorized", "401", "paused_auth")
_RATE = ("rate limit", "rate-limit", "ratelimit", "429", "too many requests")

# Models the bridge's own rate card (hermes-bridge/lib/budget.mjs
# ratesFromEnv()) and spend probe (lib/usage.mjs makeSpendProbe(), OpenRouter
# only) can actually price. A job phase served by any other model — e.g.
# claude-fable-5 once configured as a Hermes Agent fallback provider — prices
# at $0 in the bridge's own ledger: no rate-card entry, and the spend probe
# only reads OpenRouter's balance. That is a real accounting gap in the
# bridge, not something this guard can fix from dex247 — so status() flags it
# instead of silently implying a Fable-served job cost nothing.
PRICED_MODELS = {"anthropic/claude-sonnet-5", "anthropic/claude-opus-5"}


def classify(detail: str) -> str:
    low = (detail or "").lower()
    if any(k in low for k in _BILLING):
        return "billing"
    if any(k in low for k in _AUTH):
        return "auth"
    if any(k in low for k in _RATE):
        return "rate_limit"
    return "unavailable"


# ── Durable state (own connection; lazy so tests set HOMELAB_DB_PATH first) ─
_SCHEMA = """
CREATE TABLE IF NOT EXISTS hermes_guard_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS hermes_guard_requests (
    at     REAL NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_hg_requests_at ON hermes_guard_requests(at);
CREATE TABLE IF NOT EXISTS hermes_guard_costs (
    job_id     TEXT PRIMARY KEY,
    cost_usd   REAL NOT NULL DEFAULT 0,
    first_at   REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

_conn: Optional[sqlite3.Connection] = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = os.getenv("HOMELAB_DB_PATH", _DEFAULT_DB)
        # Autocommit: this file is shared with the maintenance controller's
        # own connection, and a lingering implicit transaction here would
        # block its writes ("database is locked"). Every write in this module
        # is a single self-contained statement, so autocommit is exact.
        _conn = sqlite3.connect(path, check_same_thread=False,
                                isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.executescript(_SCHEMA)
        # History needs to cover the 24h windows plus slack, nothing more.
        _conn.execute("DELETE FROM hermes_guard_requests WHERE at < ?",
                      (time.time() - 8 * 86400,))
        _conn.execute("DELETE FROM hermes_guard_costs WHERE first_at < ?",
                      (time.time() - 8 * 86400,))
        _conn.commit()
    return _conn


def _get(key: str, default: str = "") -> str:
    r = _db().execute("SELECT value FROM hermes_guard_state WHERE key=?",
                      (key,)).fetchone()
    return r["value"] if r else default


def _getf(key: str, default: float = 0.0) -> float:
    try:
        return float(_get(key, "") or default)
    except ValueError:
        return default


def _set(**kv):
    conn = _db()
    for k, v in kv.items():
        conn.execute("INSERT INTO hermes_guard_state (key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (k, str(v)))
    conn.commit()


def _bump(key: str, by: int = 1) -> int:
    n = int(_getf(key, 0)) + by
    _set(**{key: n})
    return n


# ── Budgets ────────────────────────────────────────────────────────────────
def _requests_since(secs: int) -> int:
    r = _db().execute("SELECT COUNT(*) n FROM hermes_guard_requests WHERE at>?",
                      (time.time() - secs,)).fetchone()
    return int(r["n"])


def _oldest_request_in(secs: int) -> float:
    r = _db().execute("SELECT MIN(at) m FROM hermes_guard_requests WHERE at>?",
                      (time.time() - secs,)).fetchone()
    return float(r["m"] or time.time())


def spend_last_24h_usd() -> float:
    r = _db().execute("SELECT COALESCE(SUM(cost_usd),0) s FROM hermes_guard_costs "
                      "WHERE first_at>?", (time.time() - 86400,)).fetchone()
    return round(float(r["s"]), 4)


def record_cost(job_id: str, cost_usd):
    """Observed cost of a bridge job, updated as the job runs. Charged to the
    job's first sighting so a long job doesn't slide between windows."""
    try:
        usd = float(cost_usd)
    except (TypeError, ValueError):
        return
    if not job_id or usd < 0:
        return
    now = time.time()
    _db().execute(
        "INSERT INTO hermes_guard_costs (job_id, cost_usd, first_at, updated_at) "
        "VALUES (?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
        "cost_usd=excluded.cost_usd, updated_at=excluded.updated_at",
        (job_id, usd, now, now))
    _db().commit()


# ── The one provider incident ──────────────────────────────────────────────
def _ensure_provider_incident(reason: str):
    """One canonical incident for the provider, in the monitor's own table so
    it shows in summaries and closes through the same bookkeeping. Its key is
    not in the monitor's dispatch table, so nothing ever escalates it —
    a provider outage can never recursively ask the provider about itself."""
    try:
        import homelab_monitor as mon
        if mon.active_incident_for(PROVIDER_KEY):
            return
        import uuid
        now = time.time()
        mon._insert_incident({
            "incident_id": f"mi_{uuid.uuid4().hex[:12]}", "key": PROVIDER_KEY,
            "display_name": PROVIDER_DISPLAY, "status": mon.OPEN,
            "opened_at": now, "updated_at": now, "first_seen": now,
            "last_seen": now, "occurrence_count": 1,
            "detection_json": json.dumps({"diagnosis": reason[:400], "checks": []}),
            "evidence_json": json.dumps([reason[:400]]),
        })
    except Exception:
        log.debug("provider incident bookkeeping unavailable", exc_info=True)


def _resolve_provider_incident():
    try:
        import homelab_monitor as mon
        inc = mon.active_incident_for(PROVIDER_KEY)
        if inc:
            mon._update_incident(inc["incident_id"], status=mon.RESOLVED,
                                 closed_at=time.time(), result=mon.RESOLVED)
    except Exception:
        log.debug("provider incident bookkeeping unavailable", exc_info=True)


# ── Notifications (ops feed only, once per transition) ─────────────────────
async def _notify(event: str, text: str):
    try:
        import maintenance_notify as mn
        await mn.notify(event, text[:1500])
    except Exception:
        log.exception("guard notification failed [%s]", event)


def _fmt_until(ts: float) -> str:
    return time.strftime("%H:%M UTC", time.gmtime(ts))


# ── Transitions ────────────────────────────────────────────────────────────
async def _open_circuit(reason: str, cls: str, cooldown: int,
                        budget: bool = False):
    was = _get("state", CLOSED)
    now = time.time()
    _set(state=OPEN, reason=reason[:300], reason_class=cls, opened_at=now,
         cooldown_until=now + cooldown, cooldown_secs=cooldown,
         probe_inflight_until=0, blocked_since_open=0)
    log.warning("hermes provider circuit OPEN (%s): %s — cooldown %ds",
                cls, reason[:200], cooldown)
    if not budget:
        _ensure_provider_incident(reason)
    if was == CLOSED:
        # Only a fresh opening notifies; a reopen after a failed probe during
        # one continuous outage is log/state only — that is the dedupe.
        if budget:
            await _notify("provider_budget_reached",
                          f"📊 Hermes request ceiling reached — {reason}. "
                          f"No new Hermes jobs until {_fmt_until(now + cooldown)}; "
                          f"deterministic maintenance continues.")
        else:
            await _notify("provider_circuit_open",
                          f"🔌 Hermes provider circuit OPEN — {reason}. "
                          f"No new Hermes jobs before {_fmt_until(now + cooldown)}; "
                          f"deterministic maintenance continues.")


async def _close_circuit(how: str):
    was = _get("state", CLOSED)
    _set(state=CLOSED, reason="", reason_class="", consecutive_failures=0,
         probe_inflight_until=0, cooldown_secs=0)
    if was != CLOSED:
        log.info("hermes provider circuit closed (%s)", how)
        _resolve_provider_incident()
        await _notify("provider_circuit_recovered",
                      f"🔌 Hermes provider circuit closed — {how}.")


async def _reopen_after_failed_probe(detail: str, cls: str):
    prev = int(_getf("cooldown_secs", COOLDOWN_SECS)) or COOLDOWN_SECS
    cooldown = min(max(prev * 2, COOLDOWN_SECS), COOLDOWN_MAX_SECS)
    if cls in ("billing", "auth"):
        cooldown = max(cooldown, BILLING_COOLDOWN_SECS)
    # A reopen inside one continuous outage: state only, no second ops ping.
    now = time.time()
    _set(state=OPEN, reason=detail[:300], reason_class=cls, opened_at=now,
         cooldown_until=now + cooldown, cooldown_secs=cooldown,
         probe_inflight_until=0)
    _ensure_provider_incident(detail)
    log.warning("hermes recovery probe failed (%s) — circuit reopened for %ds",
                detail[:200], cooldown)


# ── Public gate ────────────────────────────────────────────────────────────
def blocked_reason() -> str:
    """Fast, non-mutating pre-check for callers that want to skip creating
    work at all (the monitor). Empty string when a request may proceed —
    including when the cooldown has expired and a probe is due."""
    now = time.time()
    state = _get("state", CLOSED)
    if state == OPEN and now < _getf("cooldown_until"):
        return f"provider circuit open ({_get('reason_class') or 'failure'}): {_get('reason')}"
    if state == HALF_OPEN and now < _getf("probe_inflight_until"):
        return "provider circuit half-open — recovery probe already in flight"
    return ""


def note_blocked(where: str):
    total = _bump("blocked_total")
    _bump("blocked_since_open")
    now = time.time()
    if now - _getf("last_block_log_at") > BLOCK_LOG_EVERY_SECS:
        _set(last_block_log_at=now)
        log.info("hermes request blocked by circuit (%s) — %d blocked total",
                 where, total)
    else:
        log.debug("hermes request blocked by circuit (%s)", where)


async def allow_request() -> tuple[bool, str]:
    """The gate in front of every billable submit. Returns (True, "") and
    charges the request against the budgets, or (False, why)."""
    now = time.time()
    state = _get("state", CLOSED)

    if state == OPEN:
        if now < _getf("cooldown_until"):
            note_blocked("submit")
            return False, blocked_reason()
        _set(state=HALF_OPEN, probe_inflight_until=0)
        state = HALF_OPEN

    if state == HALF_OPEN:
        if now < _getf("probe_inflight_until"):
            note_blocked("submit during probe")
            return False, "provider circuit half-open — recovery probe already in flight"
        cls = _get("reason_class") or "unavailable"
        if cls not in ("billing", "auth") and _health_probe is not None:
            # Reachability-class outage: the bridge's GET /health is
            # non-billable, so recovery costs nothing. Billing and auth
            # outages can't be verified this way — /health never calls the
            # model provider, so it can't prove a quota refilled or a
            # credential was fixed. Those get exactly one controlled submit
            # instead (below).
            _set(probe_inflight_until=now + PROBE_LEASE_SECS, last_probe_at=now)
            try:
                h = await _health_probe()
                ok = bool(isinstance(h, dict) and h.get("ok"))
            except Exception as e:
                ok = False
                h = {"error": str(e)}
            if not ok:
                await _reopen_after_failed_probe(
                    f"health probe failed: {str(h)[:150]}", cls)
                return False, "provider health probe failed — circuit reopened"
            await _close_circuit("health probe succeeded")
            # fall through to the budget checks as a normal closed request
        else:
            # Billing-class (or no probe bound): exactly ONE real submit is
            # the probe. A submit refused for credits does not bill.
            _set(probe_inflight_until=now + PROBE_LEASE_SECS, last_probe_at=now)
            log.info("hermes circuit half-open — allowing one controlled "
                     "recovery submit")

    # Budgets — checked for every request that would actually go out.
    hour = _requests_since(3600)
    if hour >= MAX_PER_HOUR:
        until = _oldest_request_in(3600) + 3600 - now
        await _open_circuit(f"{hour}/{MAX_PER_HOUR} Hermes requests in the "
                            f"last hour", "budget", max(int(until), 60),
                            budget=True)
        note_blocked("hourly budget")
        return False, f"hourly Hermes request ceiling reached ({hour}/{MAX_PER_HOUR})"
    day = _requests_since(86400)
    if day >= MAX_PER_DAY:
        until = _oldest_request_in(86400) + 86400 - now
        await _open_circuit(f"{day}/{MAX_PER_DAY} Hermes requests in the "
                            f"last 24h", "budget", max(int(until), 60),
                            budget=True)
        note_blocked("daily budget")
        return False, f"daily Hermes request ceiling reached ({day}/{MAX_PER_DAY})"
    if MAX_SPEND_USD_PER_DAY > 0:
        spent = spend_last_24h_usd()
        if spent >= MAX_SPEND_USD_PER_DAY:
            await _open_circuit(
                f"observed Hermes spend ${spent:.2f} in the last 24h "
                f"(ceiling ${MAX_SPEND_USD_PER_DAY:.2f})", "budget",
                BILLING_COOLDOWN_SECS, budget=True)
            note_blocked("spend ceiling")
            return False, f"daily Hermes spend ceiling reached (${spent:.2f})"

    _db().execute("INSERT INTO hermes_guard_requests (at, detail) VALUES (?,?)",
                  (now, "submit"))
    _db().commit()
    return True, ""


async def record_success(final: bool = True):
    """A provider success signal. `final=False` means only "the bridge accepted
    the submit" — enough to close a reachability-class circuit, but NOT a
    billing-class one, where quota manifests when the job actually runs. A
    billing-class probe stays half-open (lease held) until its job completes
    (record_success(final=True) from job polling) or pauses on quota again
    (record_failure → reopen). The lease outlives the bridge's own job
    timeout, so a lost job simply frees the probe for the next attempt."""
    now = time.time()
    state = _get("state", CLOSED)
    if (not final and state == HALF_OPEN
            and (_get("reason_class") or "") in ("billing", "auth")):
        _set(last_success_at=now)
        return
    _set(consecutive_failures=0, last_success_at=now, probe_inflight_until=0)
    if state in (HALF_OPEN, OPEN):
        await _close_circuit("controlled recovery attempt succeeded")


def record_provider_hint(model: Optional[str]):
    """Record which model actually served the most recently observed job
    phase (from the bridge's own job view — `phases[-1].model`). Bookkeeping
    only, no notification: this is for observability (`status()`), not a
    success/failure signal in its own right."""
    if not model:
        return
    _set(last_model=model, last_model_at=time.time())


# ── Status-label vocabulary ─────────────────────────────────────────────────
# Maps the circuit's own state/reason_class onto the vocabulary the Boss-
# facing tool reports. Deliberately built from the existing state machine
# rather than a second, parallel status system — see hermes-operations skill.
#
# "unavailable_all_providers" is intentionally never asserted here: from
# dex247 this guard only sees one aggregate signal (did the bridge's job
# succeed or fail), not which of Hermes Agent's own configured providers
# (openrouter, plus whatever fallback chain is set up on razr) actually
# served or refused it. A billing-class open means "the provider(s) that
# actually ran this job are out of quota" — that could be the only provider
# configured, or the last one left in a fallback chain; this guard cannot
# tell the difference without querying Hermes Agent directly on razr, which
# it does not do. Report "protective_quota", not a claim this code can't back.
def status_label(state: str, reason_class: str) -> str:
    if state == HALF_OPEN:
        return "recovering"
    if state == CLOSED:
        return "operational"
    # state == OPEN
    return {
        "budget": "protective_budget",
        "billing": "protective_quota",
        "auth": "authentication_failed",
        "rate_limit": "rate_limited",
    }.get(reason_class, "unreachable")


async def record_failure(detail: str):
    detail = (detail or "unknown failure").strip()
    cls = classify(detail)
    now = time.time()
    state = _get("state", CLOSED)
    fails = _bump("consecutive_failures")
    _set(last_failure_at=now, last_failure_detail=detail[:300])

    if state == HALF_OPEN:
        await _reopen_after_failed_probe(detail, cls)
        return
    if state == OPEN:
        # Already open; a late job signal can still upgrade the reason —
        # quota/auth news during a mere reachability outage extends the
        # cooldown.
        if cls in ("billing", "auth") and _get("reason_class") not in ("billing", "auth"):
            _set(reason=detail[:300], reason_class=cls,
                 cooldown_until=now + BILLING_COOLDOWN_SECS,
                 cooldown_secs=BILLING_COOLDOWN_SECS)
            _ensure_provider_incident(detail)
        return
    if cls in ("billing", "auth"):
        # Neither class self-resolves by waiting — a bad/revoked credential
        # needs the Boss to re-authenticate on razr just as much as an
        # exhausted quota needs a top-up — so both open on the first sighting
        # rather than waiting for FAILURE_THRESHOLD consecutive failures.
        await _open_circuit(detail, cls, BILLING_COOLDOWN_SECS)
    elif fails >= FAILURE_THRESHOLD:
        await _open_circuit(f"{fails} consecutive provider failures — last: "
                            f"{detail}", cls, COOLDOWN_SECS)


# ── Observability ──────────────────────────────────────────────────────────
def _iso(ts: float) -> Optional[str]:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts)) if ts else None


def status() -> dict:
    now = time.time()
    state = _get("state", CLOSED)
    cooldown_until = _getf("cooldown_until")
    reason_class = _get("reason_class") or ""
    last_model = _get("last_model") or None
    out = {
        "circuit": state,
        # Boss-facing vocabulary (operational/protective_quota/
        # protective_budget/rate_limited/unreachable/authentication_failed/
        # recovering) computed from the fields below — see status_label().
        "status_label": status_label(state, reason_class),
        "reason": _get("reason") or None,
        "reason_class": reason_class or None,
        "opened_at": _iso(_getf("opened_at")) if state != CLOSED else None,
        "next_recovery_attempt": (_iso(cooldown_until)
                                  if state == OPEN and cooldown_until > now
                                  else ("eligible now" if state != CLOSED else None)),
        "requests_last_hour": f"{_requests_since(3600)}/{MAX_PER_HOUR}",
        "requests_last_24h": f"{_requests_since(86400)}/{MAX_PER_DAY}",
        "blocked_total": int(_getf("blocked_total")),
        "blocked_since_open": int(_getf("blocked_since_open")) if state != CLOSED else 0,
        "consecutive_failures": int(_getf("consecutive_failures")),
        "last_successful_request": _iso(_getf("last_success_at")),
        "last_failure": _get("last_failure_detail") or None,
        "spend_last_24h_usd": spend_last_24h_usd(),
        "spend_ceiling_usd": MAX_SPEND_USD_PER_DAY or None,
        "spend_accounting": (
            "observed from bridge job cost_usd — OpenRouter ledger delta when "
            "the bridge's spend probe works, rate-card estimate otherwise; the "
            "bridge's own ledger is authoritative"),
        "recovery_probe": ("bridge GET /health (non-billable) for outages; "
                           "one controlled submit for billing/auth-class opens"),
        "last_serving_model": last_model,
        "last_serving_model_cost_telemetry": (
            None if last_model is None else
            "reliable" if last_model in PRICED_MODELS else
            "unreliable — the bridge's rate card and spend probe (razr "
            "hermes-bridge/lib/budget.mjs, lib/usage.mjs) only price "
            f"{sorted(PRICED_MODELS)}; a job served by any other model "
            "(e.g. a Fable fallback) prices at $0 in both the bridge's "
            "ledger and this guard's observed spend — treat spend_last_24h_usd "
            "as understated whenever this says 'unreliable'"),
    }
    return out


def status_line() -> str:
    s = status()
    bits = [f"circuit {s['circuit']} [{s['status_label']}]"]
    if s["circuit"] != CLOSED:
        bits.append(f"({s['reason_class']}: {(s['reason'] or '')[:80]})")
    bits.append(f"req {s['requests_last_hour']}/h {s['requests_last_24h']}/24h")
    if MAX_SPEND_USD_PER_DAY:
        bits.append(f"spend ${s['spend_last_24h_usd']:.2f}/"
                    f"${MAX_SPEND_USD_PER_DAY:.2f} observed")
    return " ".join(bits)


# ── Boss-facing status tool ────────────────────────────────────────────────
def _register_tool():
    try:
        from tools import ToolSpec, register
    except Exception:
        return
    async def _tool(args, ctx):
        return json.dumps(status(), indent=2)
    register(ToolSpec(
        name="hermes_provider_status",
        description=(
            "Hermes/OpenRouter provider protection status: circuit state "
            "(closed/open/half-open), why and when it opened, next recovery "
            "attempt, request budgets used this hour/day, observed spend, and "
            "calls blocked. Read-only. Use for 'is Hermes available', 'why is "
            "Hermes paused', 'Hermes spend today'."),
        parameters={"type": "object", "properties": {}, "required": []},
        handler=_tool,
        permission="boss",
    ))


_register_tool()

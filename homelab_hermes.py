"""
homelab_hermes.py — Loki's client for the restricted Hermes escalation bridge
on razr.

Known problems stay entirely on deterministic runbooks (homelab_maintenance.py)
and never reach this module. This client is called ONLY when a runbook itself
reports `escalate: true` — the asset is known, but the failure is not.

Hermes runs on razr with a narrow, read-only maintenance toolset and can never
change system state itself — it diagnoses and PROPOSES only. Any proposed
action that would change state is always routed back through Loki's existing
draft-approval system before anything happens (see homelab_maintenance.py's
hermes_escalate / hermes_apply_action).

Job lifecycle is tracked as a supervised background task (task_supervisor,
task_type "hermes_escalation"), so it inherits durable state, restart
reattachment (no duplicate Hermes jobs — jobs are billed), the originating
Discord/Telegram destination, redaction, and duplicate-notification
prevention for free from the existing task supervisor infrastructure.
"""

import json
import logging
import os
import uuid
from typing import Optional

import aiohttp

try:
    import hermes_guard as guard   # financial circuit breaker / budgets
except Exception:                  # standalone import of this module alone
    guard = None

log = logging.getLogger("HomelabHermes")

WORKER_URL = os.getenv("HERMES_WORKER_URL", "").rstrip("/")
WORKER_TOKEN = os.getenv("HERMES_WORKER_TOKEN", "")
POLL_SECS = int(os.getenv("HERMES_POLL_SECS", "12"))
REQUEST_TIMEOUT = int(os.getenv("HERMES_REQUEST_TIMEOUT_SECS", "20"))

enabled = bool(WORKER_URL and WORKER_TOKEN)

_session_factory = None


def bind(session_factory):
    global _session_factory
    _session_factory = session_factory


class HermesBridgeError(Exception):
    pass


class HermesProviderBlocked(HermesBridgeError):
    """Raised BEFORE any network request when the provider circuit breaker or
    a request/spend budget forbids a new billable Hermes job."""


async def _session() -> aiohttp.ClientSession:
    if _session_factory is not None:
        return await _session_factory()
    # Standalone fallback (tests / scripts run outside loki_bot).
    global _own_session
    try:
        return _own_session
    except NameError:
        _own_session = aiohttp.ClientSession()
        return _own_session


async def _api(method: str, path: str, json_body: Optional[dict] = None) -> dict:
    if not enabled:
        raise HermesBridgeError(
            "Hermes worker is not configured (HERMES_WORKER_URL/HERMES_WORKER_TOKEN unset)")
    sess = await _session()
    try:
        async with sess.request(
                method, f"{WORKER_URL}{path}", json=json_body,
                headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as r:
            text = await r.text()
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                data = {"raw": text[:500]}
            if r.status >= 400:
                raise HermesBridgeError(str(data.get("error", f"HTTP {r.status}")))
            return data
    except aiohttp.ClientError as e:
        raise HermesBridgeError(f"unreachable: {type(e).__name__}") from e
    except TimeoutError as e:
        raise HermesBridgeError("timed out") from e


async def submit_diagnosis(asset_key: str, symptom: str, bundle: dict,
                           incident_id: str, request_id: Optional[str] = None) -> dict:
    """POST /diagnose — the ONLY call that creates billable provider work.
    Gated by hermes_guard: when the circuit is open or a budget is exhausted,
    this raises HermesProviderBlocked without any network request at all."""
    if guard is not None:
        ok, why = await guard.allow_request()
        if not ok:
            raise HermesProviderBlocked(why)
    try:
        data = await _api("POST", "/diagnose", {
            "asset": asset_key, "symptom": symptom, "bundle": bundle,
            "incident_id": incident_id,
            "request_id": request_id or uuid.uuid4().hex,
        })
    except HermesBridgeError as e:
        if guard is not None:
            await guard.record_failure(str(e))
        raise
    if guard is not None:
        # Acceptance only — a billing-class recovery is judged by the job's
        # fate (note_job_state), not by the bridge taking the submit.
        await guard.record_success(final=False)
    return data["job"]


async def health() -> dict:
    """GET /health — the bridge's non-billable status surface (version, queue,
    its own budget-ledger snapshot). Used by hermes_guard as the recovery
    probe so proving the provider path is back never costs a model call."""
    return await _api("GET", "/health")


async def note_job_state(job: dict):
    """Feed one polled bridge-job view to the guard: observed cost always;
    quota/auth pauses and hard failures as provider failure signals;
    completion as a provider success. Callers poll jobs anyway — this is
    bookkeeping, it performs no network requests of its own."""
    if guard is None or not isinstance(job, dict):
        return
    jid = str(job.get("id") or "")
    if jid:
        guard.record_cost(jid, job.get("cost_usd"))
    phases = job.get("phases") or []
    if phases and isinstance(phases[-1], dict):
        guard.record_provider_hint(phases[-1].get("model"))
    s = job.get("state")
    if s == "completed":
        await guard.record_success()
    elif s == "paused_quota":
        await guard.record_failure(
            f"provider quota exhausted (job {jid[-8:]} paused_quota)")
    elif s == "paused_auth":
        # The bridge's own message is "provider authentication failed —
        # re-authenticate on razr" (server.mjs classifyFailure "auth"); this
        # was previously not handled at all, so a bad/revoked provider
        # credential left the job parked in paused_auth forever without the
        # guard ever finding out — nothing opened the circuit, and Loki kept
        # submitting new jobs against a provider that could never succeed.
        await guard.record_failure(
            f"provider authentication failed (job {jid[-8:]} paused_auth)")
    elif s == "failed":
        await guard.record_failure(f"hermes job {jid[-8:]} failed at the bridge")


if guard is not None:
    guard.bind(health_probe=health)


async def get_job(job_id: str) -> dict:
    data = await _api("GET", f"/jobs/{job_id}")
    return data["job"]


async def cancel_job(job_id: str) -> dict:
    return await _api("POST", f"/jobs/{job_id}/cancel")


def fmt_job(job: dict) -> str:
    bits = [f"{job.get('id', '?')} [{job.get('state', '?')}]"]
    if job.get("escalated"):
        bits.append("escalated")
    if job.get("cost_usd"):
        bits.append(f"${job['cost_usd']:.4f}")
    return " ".join(bits)

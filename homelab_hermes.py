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
    """POST /diagnose. Returns the bridge's job dict (not the whole envelope)."""
    data = await _api("POST", "/diagnose", {
        "asset": asset_key, "symptom": symptom, "bundle": bundle,
        "incident_id": incident_id,
        "request_id": request_id or uuid.uuid4().hex,
    })
    return data["job"]


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

"""
maintenance_notify.py — where autonomous-maintenance notifications go.

One configured Discord operations channel is the canonical feed for everything
the maintenance stack does on its own: incidents, escalations, approvals,
recoveries, summaries, and concise Hermes diagnostic results. Telegram is the
Boss's private line and is NOT an operations feed — it only ever receives the
urgent categories below, and only those may appear in both places at once.

The routing table is data, not prose: every autonomous notification names an
event, and this module decides the destination. That is deliberate — "which
channel does this go to" was previously decided at each call site, which is how
worker/task chatter ended up on the Boss's phone.

  ops        the Discord operations channel only (the normal case)
  ops+boss   the ops channel AND Telegram — reserved for things that must
             interrupt the Boss: security, data loss, a consequential approval,
             or an incident nothing automatic can fix
  drop       routine worker/task lifecycle chatter — logged, never sent

Configuration (env, same convention as RELAY_CHANNEL_ID / JOBSITE_CHANNEL_ID):

  MAINTENANCE_OPS_CHANNEL_ID    Discord channel id for the ops feed

If it is unset — or a send to it fails — every event falls back to the Boss
notification path rather than disappearing. An unconfigured ops channel must
degrade to the old behaviour, never to silence.

Normal Loki conversation on either interface never passes through here.
"""

import logging
import os

log = logging.getLogger("MaintenanceNotify")

# Persistent config: the ONE Discord channel that is the canonical feed.
OPS_CHANNEL_ID = (os.getenv("MAINTENANCE_OPS_CHANNEL_ID") or "").strip()

# Sentinel channel_id for work whose replies belong to the ops feed rather than
# a conversation. It travels through the task supervisor's `channel_id` column
# exactly like the existing "tg:" prefix does, so a maintenance-originated task
# stays addressed to the feed across restarts.
OPS_CHANNEL = "ops:maintenance"

# The requester the monitor stamps on work IT started, as opposed to work the
# Boss asked for. This — not the stored channel_id — is what makes a task
# autonomous: a task submitted before the ops feed existed still carries a
# "tg:" channel, and routing on the channel alone let 302 of them keep
# announcing their lifecycle straight to the Boss's Telegram.
AUTONOMOUS_REQUESTER = "Boss (auto)"

OPS, OPS_AND_BOSS, DROP = "ops", "ops+boss", "drop"

EVENTS = {
    # ── The maintenance feed ───────────────────────────────────────────────
    "incident_opened":     OPS,
    "repair_started":      OPS,
    "repair_failed":       OPS,
    "incident_resolved":   OPS,
    "incident_escalated":  OPS,
    "approval_required":   OPS,
    "diagnostic_progress": OPS,
    "diagnostic_result":   OPS,
    "lifecycle_notice":    OPS,
    "summary":             OPS,

    # "Nothing automatic is left" is a STATUS, not a decision the Boss has to
    # make right now — it belongs on the feed with everything else. It used to
    # be urgent; that is what put "Hermes is out of quota / needs your hands"
    # on the Boss's phone twice an hour.
    "needs_boss_hands":    OPS,

    # ── Urgent: the only events allowed to reach Telegram, and the only ones
    #    allowed to be delivered twice. A genuine Boss DECISION about a
    #    consequential action, or a safety alert — never a status update.
    "boss_approval_required": OPS_AND_BOSS,   # destructive/consequential, needs a yes/no
    "security_alert":         OPS_AND_BOSS,
    "data_loss_alert":        OPS_AND_BOSS,

    # ── Routine chatter: queued/claimed/started/retried/finished ───────────
    "task_lifecycle": DROP,
}

# Task-supervisor status -> event, for tasks addressed to the ops feed. A
# status missing from this map is lifecycle chatter and is never sent: the
# feed reports what a maintenance job FOUND, not that a worker picked it up.
TASK_STATUS_EVENTS = {
    "completed":    "diagnostic_result",
    "failed":       "diagnostic_result",
    "paused_auth":  "approval_required",
    "paused_quota": "approval_required",
}

_ops_send = None       # async (text) -> None — the Discord ops feed
_boss_send = None      # async (text) -> None — the Boss's Telegram line (URGENT ONLY)
_fallback_send = None  # async (text) -> None — Discord DM, used if the feed fails


def bind(ops_send=None, boss_send=None, fallback_send=None):
    """`boss_send` is the Telegram line and is reserved for urgent events.
    `fallback_send` catches non-urgent events when the ops channel cannot take
    them — it must NOT be Telegram, or a Discord outage would put the whole
    maintenance feed back on the Boss's phone."""
    global _ops_send, _boss_send, _fallback_send
    _ops_send = ops_send
    _boss_send = boss_send
    _fallback_send = fallback_send


def configured() -> bool:
    """True when the ops feed is both configured and wired up."""
    return bool(OPS_CHANNEL_ID) and _ops_send is not None


def ops_channel_id() -> str:
    """The sentinel to address maintenance-originated work to, or "" when no
    ops channel is configured (callers then keep their previous destination)."""
    return OPS_CHANNEL if OPS_CHANNEL_ID else ""


def is_ops_channel(channel_id) -> bool:
    return str(channel_id or "") == OPS_CHANNEL


def is_autonomous_task(row: dict) -> bool:
    """True when a supervised task was started BY the maintenance stack rather
    than by a person. Such a task's lifecycle belongs to the ops feed no matter
    what channel_id it was stored with — legacy rows predating the feed carry
    the Boss's Telegram chat and must not keep announcing there.

    A task the Boss asked for (`hermes_diagnose`, `homelab_diagnose`, …) has a
    real requester and is deliberately NOT matched here: it still answers in
    the conversation that requested it."""
    if not row:
        return False
    if is_ops_channel(row.get("channel_id")):
        return True
    return (row.get("requester_name") or "").strip() == AUTONOMOUS_REQUESTER


async def notify(event: str, text: str, fallback=None) -> str:
    """Route one autonomous-maintenance notification. `fallback` is an async
    (text) -> None used when the ops channel is unconfigured or its send fails;
    it keeps a caller correct even before bind() has run (tests, standalone).

    Returns the route actually taken: "ops", "ops+boss", "fallback", or "drop".

    The one invariant: a NON-urgent event can never reach `_boss_send`. That
    line is Telegram, and a Discord outage must not put the maintenance feed
    back on the Boss's phone.
    """
    route = EVENTS.get(event)
    if route is None:
        log.warning("unknown maintenance event %r — routing to the ops feed", event)
        route = OPS
    if route == DROP:
        log.info("maintenance chatter suppressed [%s] %s", event, text[:200])
        return DROP

    delivered_ops = False
    if configured():
        try:
            await _ops_send(text)
            delivered_ops = True
        except Exception:
            log.exception("maintenance ops-channel send failed [%s]", event)

    if route == OPS_AND_BOSS and _boss_send is not None:
        # The ONLY path to Telegram: a genuine Boss decision or a safety alert.
        try:
            await _boss_send(text)
            return OPS_AND_BOSS if delivered_ops else "boss"
        except Exception:
            log.exception("maintenance boss notification failed [%s]", event)

    if delivered_ops:
        return OPS

    # The feed could not take it. Fall back to a non-Telegram destination so
    # the event is not lost — never to the Boss's private line.
    relay = _fallback_send or fallback
    if relay is not None:
        try:
            await relay(text)
            return "fallback"
        except Exception:
            log.exception("maintenance fallback notification failed [%s]", event)
    log.error("maintenance event [%s] had nowhere to go: %s", event, text[:200])
    return "dropped_undeliverable"


def task_text(row: dict) -> str:
    """Concise ops-feed wording for a maintenance-originated task. The internal
    task id appears only where it helps troubleshooting (failures and pauses),
    never on a clean result."""
    status = row.get("status") or ""
    title = row.get("title") or row.get("task_type") or "maintenance task"
    summary = (row.get("result_summary") or "").strip()
    short = str(row.get("task_id") or "").split("_")[-1][:8]   # matches ts._short
    if status == "completed":
        return summary or f"✅ {title} — done."
    if status == "failed":
        cat = row.get("error_category") or "error"
        detail = summary or (row.get("error_detail") or "")
        return f"❌ {title} — failed ({cat}) [{short}]." + (f" {detail}" if detail else "")
    if status == "paused_auth":
        return f"⏸️ {title} — paused, needs (re)authentication [{short}]."
    if status == "paused_quota":
        return f"⏸️ {title} — paused, quota exhausted [{short}]."
    return f"{title}: {status}"


async def announce_task(row: dict, fallback=None) -> str:
    """Task-supervisor announcement for a task addressed to the ops feed."""
    event = TASK_STATUS_EVENTS.get(row.get("status") or "")
    if event is None:
        return await notify("task_lifecycle",
                            f"{row.get('title', '')}: {row.get('status', '')}",
                            fallback=fallback)
    return await notify(event, task_text(row)[:1500], fallback=fallback)


def status_line() -> str:
    if OPS_CHANNEL_ID:
        return (f"maintenance ops feed → Discord channel {OPS_CHANNEL_ID}"
                + ("" if _ops_send is not None else " (not yet bound)"))
    return ("MAINTENANCE_OPS_CHANNEL_ID unset — maintenance notifications fall "
            "back to the Boss notification path")

---
name: incident-management
description: >-
  Use when working on Loki's autonomous monitoring, incident lifecycle,
  deduplication, recovery thresholds, maintenance notification routing, or the
  asset lifecycle and decommission registry. Covers homelab_monitor.py,
  maintenance_notify.py, and homelab_lifecycle.py. Read before changing when an
  incident opens, closes, escalates, or notifies.
---

# Incident Management

## When this applies

Changing incident open/close conditions, escalation, notification routing,
monitor polling, or lifecycle/decommission state.

## The rule that matters most

**An incident closes only on verified recovered health.** Escalation and task
completion are explicitly NOT closure conditions.

Closing on escalation is what produced **297 incidents and 297 billed Hermes
jobs** for black-boxx and joplin alone: escalation closed the incident and
started a 30-minute cooldown, so a fault that never resolved minted a fresh
incident and a fresh billed job every cycle.

Now: statuses `open`, `escalated`, and `gave_up` all count as active; the test
is `closed_at IS NULL`. An incident stays active until `RECOVERY_THRESHOLD`
consecutive healthy polls. Repeat detections only bump `occurrence_count` and
`last_seen`. Tests: `tests/test_incident_dedup.py` (17).

## Notification routing

All autonomous events go through `maintenance_notify.py` to ONE Discord ops
channel (`MAINTENANCE_OPS_CHANNEL_ID`). Telegram receives only four urgent
categories:

`needs_boss_hands` · `boss_approval_required` · `security_alert` ·
`data_loss_alert`

A *status* — including "Hermes out of quota" — is never one of those. If the
ops channel fails, non-urgent events relay to the Boss's **Discord DM**, never
to Telegram. Routine worker/task lifecycle chatter is dropped entirely.

**Add a new notification by naming an event in `maintenance_notify.EVENTS` —
never by picking a destination at the call site.**

Tests: `tests/test_maintenance_routing.py` (22).

## Autonomy is decided by identity, not channel

Routing on `channel_id == "ops:maintenance"` was **not enough**. 302 autonomous
`hermes_escalation` task rows already existed with `channel_id = "tg:…"`,
created before the ops feed. They are long-lived (a `paused_quota` row never
finishes), so every restart re-announced "started"/"paused"/"interrupted" to
Telegram.

Autonomy is now `requester_name == "Boss (auto)"`
(`mn.AUTONOMOUS_REQUESTER`) via `mn.is_autonomous_task(row)`, plus an idempotent
data repair (`ts._readdress_autonomous_tasks`) that re-addresses legacy rows on
connect.

`OPS_CHANNEL = "ops:maintenance"` is a sentinel channel_id that flows through
the task supervisor's `channel_id` column like the `tg:` prefix;
`loki_bot._channel_send` resolves it.

## Asset lifecycle / decommission

`homelab_lifecycle.py`, live since 2026-07-26 (`bf0a6f1`, `3c8cbb8`, `9fee6a0`).

Two things that are easy to break:

1. **`config/homelab_lifecycle.yml` is a generated mirror** of the
   `asset_lifecycle` table in `homelab_incidents.db`. Nothing reads it back as
   truth. It is rewritten on every lifecycle change, so it shows as an
   uncommitted git modification during normal operation — expected, not drift.
   **Never hand-edit it.** Change state through `homelab_decommission` /
   `homelab_lifecycle_set`.
2. **Tombstones are permanent.** `ivn-site` (decommissioned 2026-07-26, archive
   at `/home/g2k247/backups/decommission/ivn-site-20260726-014813`) must be
   retained. Deleting it lets the container be rediscovered as an unknown asset
   and restarts the false-incident cycle.

The sweep used to ask only "is this in the asset registry?", so a container the
Boss retired in chat became an incident and a billed Hermes job every 30 minutes.

## Monitor behaviour

Polls every 300s. Can open incidents and perform AUTO-tier repairs on its own.
Do not fight it — if it keeps reopening an incident, fix the underlying health
rather than suppressing the monitor.

Provider outage is ONE incident key (`hermes-provider`) held in
`monitor_incidents` and deliberately **not** in `mon.MONITORS`, so it cannot
recursively escalate.

## Database

All of this lives in `homelab_incidents.db` (incidents, monitor_incidents,
asset_lifecycle, Hermes guard counters). Inspect read-only:

```bash
sqlite3 "file:/home/g2k247/loki-bot/homelab_incidents.db?mode=ro" \
  "SELECT incident_id, asset, status FROM incidents ORDER BY created_at DESC LIMIT 10;"
```

**Schema changes:** an index on a migration-added column must live in the
`_MIGRATIONS` sequence, never in `_SCHEMA` — the unconditional
`CREATE TABLE IF NOT EXISTS` block runs via `executescript()` *before* the
`ALTER TABLE`, which breaks import against the pre-existing production DB.
Always test against a copy first.

## Completion criteria

Incident closes only on verified health · no duplicate incidents or Hermes jobs
for one unresolved fault · notifications land in the ops channel with Telegram
reserved for the four urgent categories · lifecycle changes go through tools,
not the YAML · focused tests green.

## Source

`homelab_monitor.py` · `maintenance_notify.py` · `homelab_lifecycle.py` ·
`tests/test_incident_dedup.py` · `tests/test_maintenance_routing.py`

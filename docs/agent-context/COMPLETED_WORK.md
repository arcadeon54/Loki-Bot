# Completed Work

Every entry is labelled. **Do not convert an old plan into a claimed feature.**
Commits are local-only unless stated; nothing has been pushed.

Legend: **DONE** · **PARTIAL** · **UNFINISHED** · **OBSOLETE/HISTORICAL**

---

## BLACK-BOXX

**DONE — deterministic diagnosis and repair** (`8355d21`, 2026-08-06).
The runbook probes the executor first: a Loki-side transport/permission failure
reports `diagnostic_transport: UNAVAILABLE` with state UNKNOWN and never
escalates as an AP fault. A dead AP unit yields ONE failed root cause with
dependents marked `SKIPPED` — never "failed", which would assert evidence never
gathered. Previously it reported twelve simultaneous subsystem failures that
were all symptoms of one dead unit.

**DONE — boot-race ownership conflict removed** (`42380d1`, 2026-08-06).
`wg-quick@wg-ap` was disabled through Loki's own approval gate. Added
`systemctl_is_enabled` / `systemctl_disable_unit` / `systemctl_enable_unit` to
the allowlist — the `service_enable_disable` tier previously had no command
behind it. `multi-user.target.wants` now contains only `black-boxx-ap.service`.

**DONE — current health.** 17/17 checks green, 0 advisories, no repair proposed.

## Hermes / OpenRouter

**DONE — escalation agent** (`e902cb4`, `2f95889`, live 2026-07-25).
Hermes v0.19.0 on razr, dedicated `hermes` account, read-only `homelab_api.py`
facade on dex247. Routing enforced in code: the asset's deterministic runbook
runs first and Hermes is only reached if it reports `escalate: true`.

**DONE — circuit breaker and budgets** (`51fda47`, live 2026-08-05 22:44).
Provider circuit breaker with persistent counters, 6 req/hour, 20 req/day
rolling, $5/day observed spend ceiling. Billing-class failures (quota/credits/
402) open instantly; others need 3 consecutive. Cooldowns 1800s doubling to
21600s. Recovery via non-billable `GET /health`; a billing open gets ONE leased
submit judged by the job's fate. Provider outage is ONE incident key
(`hermes-provider`), never in `mon.MONITORS`, so it cannot recursively escalate.
30 tests in `tests/test_hermes_guard.py`.

## Maintenance incidents and notifications

**DONE — persistent incident dedupe** (`daf150e`, 2026-08-05).
Escalation used to CLOSE the incident and start a 30-minute cooldown, so a
fault that never went away minted a new incident **and a new billed Hermes job**
every cycle — 297 incidents and 297 Hermes jobs for black-boxx + joplin alone.
Now an incident stays active (open/escalated/gave_up all count; `closed_at IS
NULL` is the test) until `RECOVERY_THRESHOLD` consecutive healthy polls. Repeat
detections only bump `occurrence_count`/`last_seen`.
17 tests in `tests/test_incident_dedup.py`.

**DONE — one Hermes escalation per unresolved incident.** Follows from the above
plus the guard's pre-check of `hg.blocked_reason()`.

**DONE — routine maintenance removed from Telegram, Discord ops feed live**
(`maintenance_notify.py`, live 2026-08-05 20:42). All autonomous events route to
ONE Discord ops channel (`MAINTENANCE_OPS_CHANNEL_ID`). Telegram receives only
`needs_boss_hands`, `boss_approval_required`, `security_alert`,
`data_loss_alert`. If the ops channel fails, non-urgent events relay to the
Boss's **Discord DM**, never Telegram. 22 tests in
`tests/test_maintenance_routing.py`.

## Tracearr

**DONE — restricted NAS dispatcher** (`16b2178`, 2026-07-27). Six read-only
actions over `nas-maint`.

**DONE — approval-gated update with backup, readiness, verification, rollback**
(`1016838`, then `b075780` + `251807b`, 2026-08-05). Ran for real: v1.4.27 →
**v1.5.0**, verified healthy, no rollback needed. Readiness-aware verification
(150s deadline, 5s poll) used for both post-update and post-rollback.
`tracearr_update` takes an optional pinned `version`, verified against the
upstream feed (must exist, must not be a prerelease). A verified update writes
version + image digest back to `config/homelab_assets.yml`.

**UNFINISHED — Tracearr v2.x** is a **separate evaluation**, not scheduled.
v2.0.0 landed upstream hours before the v1.5.0 run; the pinning work exists
specifically so a major release cannot be applied by accident.

**UNFINISHED — Tracearr restart churn.** `restart_count` 268 and climbing while
Redis and Postgres sit at 0. App-side, cause unproven, deliberately **no
automatic repair**.

## Work Tracker

**DONE — Google Sheets export defect fixed** (2026-08-06).
The `_write_sheets_row` function was a fire-and-forget HTTP call at the moment of session close. Any network drop, timeout, or HA integration failure resulted in permanent data loss for the Sheets mirror. Re-architected to a durable `_sync_pending_sheets` queue processor reading from the SQLite `work_sessions` table (`sheets_ok = 0`). Sync runs asynchronously on session close and is triggered every 5 minutes by `poll()`. Idempotency and retry-safety guaranteed. Validated live; historically dropped sessions successfully recovered and exported to Sheets.

## Joplin

**DONE — authoritative runtime** (`f89e503`, then `dc479a6`, 2026-08-05).
`loki-joplin-desktop.service` (Desktop 3.7.9 headless under Xvfb) owns the Data
API on `127.0.0.1:41184`. Profile `/home/g2k247/docker/joplin-api/data`.
Joplin Server = containers `joplin` + `joplin-db`.

**DONE — note read-back fix** (`dc479a6`). An unscoped `find_note_by_title` must
search EVERY notebook via one `/notes` listing and must not rely on `/search`
(Joplin's FTS index lags creation by seconds). Scoping it to `Loki/` meant a
note written to `Personal/Officer Logs` was invisible to the next read, and
`note_append` then created a DUPLICATE in `Loki/Inbox`.

**DONE — notebook hierarchy tools** (`40b3f5e`): `notebook_list/tree/get/
children/notes`, `note_move`, `note_create(notebook=)`. Validated live —
`Personal → Officer Logs` resolves by full path and bare name.

**OBSOLETE — `loki-joplin-api` CLI sidecar.** Superseded 2026-07-19. Kept
stopped with `restart=no` for rollback. Its `Exited (137)` state is
**intentional**, not an outage. Removed from the `joplin` asset's container list
because monitoring it manufactured a permanent false incident. **Do not
resurrect it** — two Joplin instances cannot share one SQLite profile.

## Presence notifications

**DONE — concise legacy style restored** (`ede172d`, 2026-08-05). Home Assistant
already sends several messages fully formatted in Loki's voice; the Groq
rewriter was narrating them. Four Boss presence transitions
(`loki_someone_left`, `loki_arrived_office`, `loki_left_office`,
`loki_someone_home`) now bypass the rewriter. Welcome-home retains Rob's
(`person.ammiel`) home/away state for the top-lock decision. 20 tests in
`tests/test_presence_notifications.py`.

## Homelab platform

**DONE — maintenance controller** (`53aebbb`, `6bae20d`, live 2026-07-25).
**DONE — asset lifecycle / decommission registry** (`bf0a6f1`, `3c8cbb8`,
`9fee6a0`, live 2026-07-26). `ivn-site` is the first tombstone, retained
permanently on purpose. `config/homelab_lifecycle.yml` is a **generated mirror**
of the DB table — it goes git-dirty during normal operation by design, and
nothing reads it back as truth.
**DONE — container update workflows** (`5d69287`). Registry digests + official
GitHub release metadata, never image age; prereleases never targeted; moving
tags resolved to an exact version before pulling; backups must succeed or the
update is blocked; rollback refused once a schema migration has run.
**DONE — durable task supervisor** (`da1e301`), **approval-gated actions**
(`08c37fa`), **memory lifecycle** (`a3d7ddd`).

## Discord / Telegram

**DONE — duplicate social-link guard repaired** (`0bb049c`). The pre-existing
cross-channel guard only recorded a URL inside `run_download()`, so a link
shared casually in an ordinary channel was never recorded and reposting it was
never caught. Repaired in place — not replaced. An earlier session misdiagnosed
this as a missing "Hell Yeah Films forwarding" feature and built a parallel
system; that was corrected.
**DONE — Telegram media handling + honest sync status** (`824e60b`, `41875ed`).
**DONE — HA→Telegram mirror for 🛒-marked notifications** (`db46d02`).
**UNFINISHED — Telegram voice messages.** `telegram_interface._handle()` reads
text/caption only; voice payloads are silently dropped. Discord voice
transcription already exists.

## Career-Ops

**PARTIAL.** Bridge built and locally validated; SSH to razr unblocked
2026-07-25. Loki side is live (`career_ops.py`, 6 tools, `permission=crew`;
Boss → both profiles, crew → roommate only). Startup reports "Career-Ops liaison
online — bridge at configured URL". Never auto-submits applications.

## Plex / NAS diagnostics

**DONE** (`90fdb30`): `plex_status`, `plex_diagnose`, `plex_sessions`,
`plex_playback_diagnose`, `plex_start`, plus `nas_network_status`,
`nas_network_speed_test`, `nas_disk_status`, all through the dispatcher.

## Known broken (not started)

**UNFINISHED — Google Sheets work-session export.** `sheets_ok` 2/15 sessions,
recurring `Sheets append returned not-ok`. Joplin + SQLite halves work, so no
data is lost. Diagnose the HA-service append path in `work_tracker.py`.

**UNFINISHED — weekly Discord export 403.** `Failed to send weekly export: 403
Missing Permissions`, weekly, still firing. Needs a Discord-side channel
permission (Boss action) or a different target channel.

**UNFINISHED — watchtower conflict.** `watchtower` runs `--cleanup --interval
86400` with no `WATCHTOWER_LABEL_ENABLE` and no `WATCHTOWER_MONITOR_ONLY`, so it
auto-updates **every running container** daily at ~19:17 UTC and deletes
replaced images — bypassing Loki's approval gate and destroying rollback
targets. Loki's inventory detects it and reports `external_autoupdate_active`.
The fix (`WATCHTOWER_MONITOR_ONLY=true`, drop `--cleanup`) is a production
change needing Boss approval. **Not done.**

## Planned only — do not describe as built

- **Centralized Model Router.** Today there is only a per-intent table in
  `routing.json`. The full cost/privacy/complexity router is a design.
- Splitting `loki_bot.py` into satellite modules.
- `google.generativeai` → `google.genai` vision migration.

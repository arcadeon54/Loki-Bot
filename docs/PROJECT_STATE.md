# Loki — Project State (Audit 2026-07-19)

Audited read-only on host **dex247** by Claude Code. Nothing was modified,
restarted, or committed during the audit.

## Identity & paths

| | |
|---|---|
| Repository = deployment path | `/home/g2k247/loki-bot` |
| Companion repo | `/home/g2k247/skillkit` (skill framework; separate git repo, no remote) |
| Remote | `git@github.com:arcadeon54/Loki-Bot.git` |
| Branch | `master` (both repos) |
| Owner | Kavaris → "Boss" (OWNER_USER_ID) · Roommate Ammiel → "Rob" (crew) |

## Git status at audit time

- **loki-bot:** ` M ha_integration.py` (+39 lines: 🛒-marked HA notifications
  mirror raw to owner's Telegram DM). **This change is deployed and running**
  (service started 2026-07-10 22:44, after the edit). Needs commit, not revert.
- **skillkit:** ` M knowledge/_improvements.json`, ` M skillkit/ha_automations.py`,
  untracked `skillkit/ha_automations.py.bak-2026-07-10`.
- No stashes, no tags. Historical phase branches exist (phase-1.2-rag …
  phase-5-proactive, personal-ai-upgrade, cleanup-optimizations) — all merged.
- Recent commits: per-tool timeouts (5fb675e), personality profiles wired
  (394cc68, 265426b), dead unicron URL fix (c295cb1), personal-AI upgrade
  (89d9004).

## Runtime

- `loki.service` (systemd, enabled): `venv/bin/python loki_bot.py`,
  Restart=always, journald + `loki_bot.log`. Active since 2026-07-10; PID
  matched working tree at audit.
- Python 3 / discord.py; venv in-repo. Not containerized.
- Supporting containers (healthy at audit): `chromadb-chromadb-1`
  (compose `/home/g2k247/chromadb/`, port 8100), `loki-joplin-api`
  (compose `/home/g2k247/docker/joplin-api/`, host network, Joplin Data API
  on 127.0.0.1:41184), `searxng` (:8083).
- Cron (user g2k247): `20 */6 * * *` ingest_history.py · `0 13 * * *`
  skillkit advise (daily, deliver_telegram) · `30 13 * * 0` skillkit improve.
- Startup banner (2026-07-10): 49 tools registered · 19 slash commands ·
  RAG online 3493 chunks · routing ON (CHAT→groq) · proactive ON (5/day cap,
  quiet 23:00–09:00 ET) · voice ears available · work tracker online ·
  presence monitor online · Telegram online @Leauxki_Bot.

## Component inventory (verified in code + runtime)

### In loki-bot
| Component | File(s) | Status |
|---|---|---|
| Discord interface | `loki_bot.py` | Working (online as Loki#5463) |
| Telegram interface | `telegram_interface.py` | Working — text only; long-polling, single-owner pairing, strangers rejected |
| Telegram voice messages | — | **Not implemented** — `_handle()` reads text/caption only; voice payloads silently dropped |
| Discord voice-message transcription | `loki_bot.py:1663,5409` | Implemented (attachment → transcription) |
| Wake-word voice listening | `voice_listen.py` (/loki_ears) | Implemented (faster-whisper base) |
| Personality profiles | `personality.py` | Working — DISCORD_PUBLIC (mischief), DISCORD_DM/TELEGRAM (serious), HA_NOTIFICATION (brief, no humor). Matches historical spec |
| Tool registry | `tools.py` (ToolSpec/REGISTRY) | Working — 49 tools total: 5 in tools.py, ~15 in assistant_tools.py, 29 mirrored `skill_*` via skill_bridge |
| Permissions | `tools.py` | everyone < crew < boss; per-tool; enforced in `execute()`; all calls logged to `tool_calls.jsonl` |
| Model routing (per-intent) | `routing.json` + `get_routing_table()` | Working — CHAT→Groq free tier, others→gpt-5.1 primary; undo clause; usage/cost tracking. **Not** the full centralized Model Router (see NEXT_STEPS) |
| Semantic memory (Boss) | `semantic_memory.py` | Working — Joplin `Loki/Memories` authoritative → Chroma `boss_memory` index; dedupe 0.12, recall 0.62; forget = archive note + drop embedding; daily+startup `reindex()` rebuilds from Joplin |
| Per-user facts | `user_memory.py` | Working — Chroma `user_facts`, background extraction on cheap model, `/forget` right |
| RAG history search | `rag_search.py`, `ingest_history.py`, `eval_rag.py` | Working — 3493 chunks; eval 2026-07-19: hit@5 8/10, hit@20 9/10 |
| Conversation DB | `loki_memory.db` (SQLite, WAL) | Working — 12,892 messages; 14 tables (messages, summaries, user_profiles, reminders, voice_transcripts, claude_sessions, …) |
| Work tracking | `work_tracker.py` + `jobsite.db` | **Partial** — 90-min-rule state machine works (15 sessions; joplin_ok 15/15) but Sheets export failing (sheets_ok 2/15, "Sheets append returned not-ok") |
| Presence/lockout | `presence_monitor.py` | Working (Boss/Rob polling, warnings logged) |
| HA integration | `ha_integration.py` | Working — states, service calls, webhook :9100 for notifications, Groq rewrite via HA_NOTIFICATION persona, Telegram mirror (uncommitted) |
| Joplin integration | `joplin_integration.py` | Working — Data API sidecar on 41184; ping OK at audit |
| Download chain | `loki_bot.py` (cobalt/yt-dlp/gallery-dl/…) | Working (multi-fallback) |
| Proactive behavior | ProactiveGovernor | Working — 5/day cap, quiet hours, kill switch |
| Claude Code handoff | ClaudeCodeHandler (`CLAUDE_BIN`) | Implemented (unverified end-to-end) |
| Weekly Discord export | — | **Failing** — 403 Missing Permissions weekly since ≥2026-07-08 |

### In skillkit (`/home/g2k247/skillkit`)
| Component | Location | Status |
|---|---|---|
| Solve meta-skill (Intent Planner) | `skills.d/solve.py` → `skillkit/orchestrator.py` | Working — bounded plan→act→evaluate loop; input: `intent` (NL), `max_steps` 2–12, `direct`; output: status solved/healthy/needs_human/inconclusive/awaiting_approval + summary; max 3 mutations/run; "no unverified solved" rule; actively used (incidents recorded, latest #3 2026-07-07) |
| Playbooks | `playbooks/*.json` (11) | Working — deterministic matching, injected into planner context; one auto-learned (cloudflare-ddns) |
| Advisor | `skillkit/advisor.py` | Working — daily cron, reports → Joplin "Advisor Reports" + Telegram; P1/P2/P3; metrics snapshots in `logs/advisor_metrics.jsonl` |
| CIE | `skillkit/improve.py` + `goals.py`/`incidents.py`/`learning.py` | Working — weekly cron; deterministic evidence-backed recommendations, append-only history, Chroma `loki_improvements`/`loki_goals` |
| Capability Discovery | `skillkit/capabilities.py` | Working — live-registry answers + Joplin Capability Catalog |
| Verification framework | `skillkit/verification.py` | Working — `confirm()` chokepoint; unverified success raises |
| Approvals | `skillkit/approvals.py` + `config/approval_policies.json` | Working — automatic / confirm / always_confirm; pending-token flow; none pending at audit |
| Permissions | `config/callers.json` | public<user<admin<owner; loki=owner (per-user gating inside Loki), claude-code=owner, gemini/antigravity=admin |
| Incidents | `skillkit/incidents.py` → `logs/incidents.db` | Working — 7 incidents; Joplin history note |
| Archdoc | `skillkit/archdoc.py` | Working — maintains Joplin "Loki Architecture" notebook (content in Joplin not audited) |
| Maintenance mode | `skillkit/maintenance.py` | Implemented (not runtime-verified) |
| Reporting/health dashboard | `skillkit/reporting.py` | Implemented — documented scoring algorithm |
| Adapters | `bin/skillkit` (CLI), `bin/skillkit-mcp` (MCP), `openai_adapter` (Loki) | CLI verified working read-only at audit |

## Memory ownership rules (verified)

| Store | Holds | Authoritative? |
|---|---|---|
| Joplin `Loki/Memories` | Explicit facts/preferences/recipes/lists (human-editable notes) | **Yes** — source of truth |
| Joplin `Loki/Work Log` | Monthly work-log tables | Yes for work history (mirror of jobsite.db) |
| Chroma `boss_memory` | Embedding index of memories | No — rebuilt from Joplin by `reindex()` |
| Chroma `discord_chunks`(+`_messages`) | RAG history index | No — rebuilt by `ingest_history.py` |
| Chroma `user_facts` | Per-user learned facts | Yes (no other copy) but user-deletable |
| Chroma `loki_improvements`/`loki_goals` | CIE/goal embeddings | No — Joplin-authoritative registry |
| SQLite `loki_memory.db` | Conversations, summaries, profiles, reminders | Yes for runtime state |
| SQLite `jobsite.db` | Work sessions/sites/visits | Yes (Joplin is the mirror) |
| SQLite `skillkit/logs/incidents.db` | Incident records | Yes; mirrored to Joplin history note |

## Secrets (names only, values REDACTED)

`.env` in repo root (git-ignored) + **7 `.env.bak*` copies also holding live
secrets** (cleanup candidate). Keys include: DISCORD_TOKEN, OPENAI_API_KEY,
GEMINI_API_KEY, TAVILY_API_KEY, FALLBACK_LLM_API_KEY (Groq),
ELEVENLABS_API_KEY, HA_TOKEN, JOPLIN_API_TOKEN, TELEGRAM_BOT_TOKEN,
MYJD_EMAIL/PASSWORD, NEXTCLOUD_USER/PASS, JELLYFIN_API_KEY, SEERR_API_KEY.
skillkit secrets: `skillkit/config/skillkit.env` (git-ignored).

## Tests & health checks

- No pytest suite. Only `eval_rag.py` (retrieval eval, read-only).
- Audit test results (2026-07-19): syntax OK 9/9 key modules · Chroma
  heartbeat OK, 6 collections · Joplin API ping OK · skillkit CLI
  list/playbooks/incidents/approvals OK · eval_rag hit@5 8/10 (1 miss,
  pre-existing) · SQLite ro reads OK.
- Skipped for safety: anything sending Discord/Telegram/HA/Joplin traffic,
  `skillkit run` (executes skills), service restarts.

## Known failures (pre-existing, live)

1. **Google Sheets work-session export**: sheets_ok 2/15; recurring
   `[WARNING] Sheets append returned not-ok` (13× in current log).
2. **Weekly Discord export**: `403 Forbidden (50013) Missing Permissions`
   weekly since ≥2026-07-08 (bot lacks channel permission).
3. `google.generativeai` deprecation warning at startup (package EOL;
   vision still works).

## Not verified in this audit

- Joplin note contents (Architecture notebook, Advisor reports, memories) —
  reading them requires the API token; existence inferred from code, logs,
  and note-id caches in `skillkit/logs/`.
- ClaudeCodeHandler end-to-end behavior.
- Voice features live (transcription/TTS paths not exercised).
- Maintenance mode runtime behavior.
- skillkit remote execution against razr/NAS.

## Addendum 2026-07-19 — Telegram/Joplin list creation fix (uncommitted, NOT yet live)

**Problem:** Boss asks via Telegram for a list ("make me a grocery list…"); the
list stays conversation-only and Loki claims it can read Joplin but not write.

**Verified root cause:** Joplin write support and the `list_create`/`note_create`
tools were fully present and boss-exposed on both interfaces (Telegram maps to
the owner's ID in `loki_bot.py` `_tg_tool_ctx`). The failures were:
1. The model repeatedly called `list_create` with an empty `items` array
   (4 of 7 lifetime calls in `tool_calls.jsonl`, e.g. 2026-07-06 21:21,
   2026-07-11 08:35); the old handler replied "Need a list title and at least
   one item", after which the model confabulated "no API write access".
2. The TELEGRAM persona prompt (`personality.py`) advertised only
   search/remember, never note/list writing.
3. Retries could duplicate lists across notebooks (Shopping List created in
   both Kitchen Corner and Personal on 2026-07-07, 26s apart).

**Fix (files: `assistant_tools.py`, `personality.py`, new `tests/`):**
- `_list_create` rewritten: normalizes items (list or comma/newline string,
  bullet/checkbox markup stripped, case-insensitive dedupe), returns structured
  JSON ({success, note_id, title, notebook_id, notebook_title, item_count,
  message | error+fix}), never claims success without a Joplin note id,
  extends an exact-title list instead of duplicating (retry-idempotent),
  requires explicitly named notebooks to exist (default `Loki/Lists` may
  auto-create per existing namespace design), errors instruct the model to
  retry correctly instead of denying write access. Timeout 30→45s.
- Tool schema/description hardened (`minItems: 1`, "FIRST CHOICE for any
  list" affordance).
- TELEGRAM prompt now states Joplin write access explicitly.

**Tests:** `tests/test_list_create.py` — 12 tests against an in-process fake
Joplin API (no real Joplin/Telegram/Discord/HA/LLM traffic); all pass
(`venv/bin/python -m unittest tests.test_list_create`). Production
`tool_calls.jsonl` untouched (log path redirected during tests).

**Live test 2026-07-19 ~01:00 UTC:** restart approved and performed (new PID
2044977, healthy). Telegram request "Create a list called Loki Test List with
alpha, beta, and gamma" → `list_create` called correctly, note verified via
API in `Loki/Lists` with all 3 checkboxes. First live test SUCCESSFUL.
Observed: Joplin `/search` full-text index lags new notes by minutes — the
original search-based dedupe was unsafe for fast retries.

**Follow-up hardening (same day, uncommitted, needs restart to go live):**
- Deterministic dedupe: `_list_create` now checks an in-process
  recent-creation cache, then the direct `/folders/{id}/notes` listing
  (immediately consistent), and uses `/search` only as a cross-notebook
  courtesy when no notebook was named. New helper
  `joplin_integration.find_note_in_folder()`.
- Tool-log redaction (`tools.py`): new `ToolSpec.redact_log` — `remember`,
  `recall_memory`, `note_read` now log call metadata only (args/results
  withheld); all other tools' args+results pass a deterministic
  credential scrubber (password/token/api-key/secret patterns → [REDACTED])
  before reaching `tool_calls.jsonl`, `loki_bot.log`, and journald.
  Motivated by a real credential that a stored memory leaked into the logs
  (rotated 2026-07-19; log scrubbing of historical copies still pending).
- Tests: 19 total across `tests/test_list_create.py` (14, incl. dedupe with
  lagging search index and cold cache) and `tests/test_log_redaction.py` (5);
  all pass. Duplicate-message live test still pending.

## Homelab maintenance & monitoring (checkpoint 2026-07-25)

Built since the 07-19 audit, in order: secure maintenance controller +
registry (53aebbb), Hermes handoff toward razr (6bae20d), read-only
maintenance API (2f95889), restricted Hermes escalation bridge (e902cb4,
razr repo), safe container update workflows (5d69287), automatic monitoring
and repair (17e3ffd). Full design/ops reference:
Joplin `Loki/Documentation/Loki Homelab Maintenance and Hermes Operations`.

- `homelab_monitor.py` polls 11 assets every 5 min; two-consecutive-failure
  threshold, one incident per asset, cooldown, ≤1 repair attempt (BLACK-BOXX
  ≤2, idempotent runbook), escalates to Hermes on failed verification —
  no retry loops. State in `homelab_incidents.db` (`monitor_checks`,
  `monitor_incidents`), survives restarts.
- Hermes bridge lives on razr (`~/hermes-bridge`, systemd `hermes-bridge`),
  both healthy as of this checkpoint.
- Container image updates remain approval-gated always; Immich update
  runbook exists but has never been run against a real release.

## Safe resume commands

```bash
ssh dex247
cd /home/g2k247/loki-bot
git status                     # expect: M ha_integration.py (deployed, keep)
systemctl status loki          # read-only
journalctl -u loki -n 50       # recent log
tail -50 loki_bot.log
~/skillkit/bin/skillkit list   # read-only skill roster
claude                         # start Claude Code here
```

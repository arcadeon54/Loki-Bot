# Loki — Next Steps (from 2026-07-19 audit)

Ordered within each section. Nothing here is authorized to start without the
Boss's approval.

## Broken & urgent

1. **Google Sheets work-session export** — `sheets_ok` 2/15 sessions;
   recurring `Sheets append returned not-ok` warnings. Joplin + SQLite halves
   work fine, so no data is lost, but the Sheets mirror is effectively dead.
   Diagnose the HA-service-based append path in `work_tracker.py`
   (`SHEETS_CONFIG_ENTRY`).
2. **Weekly Discord export 403** — `Failed to send weekly export: 403
   Missing Permissions`, weekly. Likely fix is a Discord-side channel
   permission for the bot (user action), or point the export at a channel the
   bot can write to.

## Uncommitted work to land (not broken — running in prod)

3. Commit `ha_integration.py` (Telegram 🛒 notification mirror, live since
   2026-07-10) with an explanatory message.
4. In skillkit: commit `ha_automations.py` + `knowledge/_improvements.json`;
   delete the `.bak-2026-07-10` files (git has history). Also
   `ha_integration.py.bak-2026-07-10` in loki-bot.

## Implemented locally, awaiting live verification (2026-07-19)

4b. **Telegram list creation → Joplin** — root cause was empty-`items`
    tool calls plus a persona prompt that never advertised write access (see
    PROJECT_STATE.md addendum). Fixed in `assistant_tools.py` +
    `personality.py`. First live test PASSED 2026-07-19 (restart approved,
    note verified via API). Hardened same day: deterministic dedupe (folder
    listing + in-process cache; Joplin search-index lag observed live) and
    tool-log credential redaction in `tools.py` (+
    `joplin_integration.find_note_in_folder`). 19 mocked tests pass.
    Still pending: restart to load the hardening, duplicate-message live
    test, scrub of historical credential copies in `tool_calls.jsonl` /
    `loki_bot.log` / Joplin notes (password already rotated). Commit
    `assistant_tools.py`, `personality.py`, `tools.py`,
    `joplin_integration.py`, `tests/` as one isolated change once verified.

## Partially implemented

5. **Telegram voice messages** — currently silently ignored
   (`telegram_interface._handle` reads text/caption only). Discord voice
   transcription already exists (`transcribe_voice_message`); wiring the
   Telegram `voice`/`audio` payload through it is the natural completion.

## Planned only (do not label as broken)

6. **Centralized Model Router** — latest agreed direction. Today: per-intent
   table in `routing.json` (CHAT→Groq, rest→gpt-5.1) plus scattered model
   choices (Groq hardcoded in `ha_integration.py`, extraction model in
   user-memory path, skillkit `llm.py`). The intended design routes by task
   type/complexity/cost/privacy/fallback with config-driven model names and
   deterministic-first hierarchy. Requires explicit approval before starting.

## Technical debt

7. `loki_bot.py` is ~6,600 lines; download chain, relay, RAG-query parsing,
   and reminders could be satellite modules (only with approval — no
   re-architecting by default).
8. `google.generativeai` is EOL → migrate vision to `google.genai` at some
   planned moment (needs a package install → approval).
9. Seven `.env.bak*` files with live secrets in repo dir (git-ignored but on
   disk) — consolidate/remove after confirming nothing references them.
10. `AGENTS.md` is git-ignored but doesn't exist; `.gitignore` entry is
    stale either way.
11. skillkit has no git remote — a push mirror would protect ~50 commits of
    work.

## Documentation

12. Verify the Joplin "Loki Architecture" notebook against this audit
    (requires Joplin read access) and run `skillkit run archdoc -p
    action=verify` (writes nothing per its spec, but confirm first).
13. README.md still opens with the original Discord-bot install story;
    a short "current architecture" pointer to docs/ would help.

## Security

14. Rotate any token that ever landed in a pasted log/screenshot (none
    observed during this audit; hygiene reminder only).
15. `telegram_state.json` pairing: TELEGRAM_OWNER_ID is set via state file —
    fine, but document that deleting the file re-opens first-come pairing.

## Optional improvements

16. RAG eval has 1/10 persistent miss (`rank 13` case) — tune
    `RAG_MAX_DISTANCE` (suggested ≥0.33 by eval) or chunking.
17. A tiny pytest smoke suite (imports, registry integrity, routing.json
    schema) would catch breakage before restarts.

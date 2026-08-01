# Loki — Personal AI Assistant

Production personal AI assistant for Kavaris ("Boss"). Cross-platform: Discord
(public persona + serious DMs), Telegram (@Leauxki_Bot, always serious), Home
Assistant notifications, voice. Roommate Ammiel = "Rob" (crew-level user).

**This is a live production service. It is running right now from this
directory.** The systemd service executes the working tree directly — any file
you edit here is what runs after the next restart, and uncommitted changes may
already be live.

## Non-negotiables

- Do NOT rebuild, re-architect, or rename major components.
- Do NOT reset/discard uncommitted work — it may be running in production.
- Do NOT restart `loki.service`, containers, or run migrations without approval.
- Do NOT commit/push/pull/merge/switch branches without approval.
- Do NOT switch Claude Code models without approval.
- Do NOT send Discord/Telegram messages, trigger HA actions, or write to
  Joplin while testing — those are real production side effects.
- Never display `.env` values, tokens, or keys. Variable names only; values
  as REDACTED. `.env.bak*` files also contain live secrets.
- LLM priority is settled: gpt-5.1 primary, Groq fallback + CHAT routing via
  `routing.json` (undo = `enabled: false`). Do not re-litigate.

## How it runs

- systemd: `loki.service` → `venv/bin/python loki_bot.py` (WorkingDirectory =
  this repo). Restart=always. NOT containerized.
- Status: `systemctl status loki` · Logs: `journalctl -u loki -f` and
  `loki_bot.log`
- Restart (ONLY with approval): `sudo systemctl restart loki`
- Cron: RAG ingest every 6h (`ingest_history.py`); skillkit Advisor daily
  13:00 UTC; CIE weekly Sun 13:30 UTC.

## Architecture invariants

- `loki_bot.py` is the monolith entrypoint (~6.6k lines). Satellite modules:
  `tools.py` (registry) · `assistant_tools.py` · `skill_bridge.py` (mirrors
  skillkit skills as `skill_*` tools) · `personality.py` (ALL tone/prompts
  live here) · `telegram_interface.py` · `ha_integration.py` ·
  `joplin_integration.py` · `semantic_memory.py` · `user_memory.py` ·
  `rag_search.py` · `work_tracker.py` · `presence_monitor.py`.
- skillkit (`/home/g2k247/skillkit`, separate repo) owns the operational
  brain: solve orchestrator (intent planner), playbooks, Advisor, CIE,
  Capability Discovery, verification, approvals, incidents. Loki is just a
  caller (identity `loki`). Playbooks EXTEND the planner, never replace it.
- Memory ownership: **Joplin `Loki/Memories` is the source of truth** for
  explicit facts; ChromaDB `boss_memory` is a rebuildable index (never
  authoritative); SQLite `loki_memory.db` = conversation history/profiles,
  `jobsite.db` = work sessions. Do not merge these systems.
- Verification: success is proven, not assumed — skillkit
  `verification.confirm()` + solve's "no unverified 'solved'" rule.
- qBittorrent stays OUT of gluetun (settled after a week of testing).
  gluetun/qbittorrent pairing must never be "fixed".

## Commands

- Syntax check: `venv/bin/python -c "import ast; ast.parse(open('FILE').read())"`
- RAG eval (read-only): `venv/bin/python eval_rag.py --run BAAI/bge-small-en-v1.5:discord_chunks`
- skillkit read-only: `~/skillkit/bin/skillkit list|playbooks|incidents|approvals|logs`
- DB inspection: always `sqlite3 "file:PATH?mode=ro"` — the bot holds these
  open (WAL).
- No pytest suite, no linter config, no build step.

## Documentation

- `docs/PROJECT_STATE.md` — full audit snapshot (2026-07-19)
- `docs/NEXT_STEPS.md` — known issues and priorities
- `docs/HISTORICAL_HANDOFF.md` — historical context (never overrides code)
- `docs/SESSION_RECOVERY.md` — beginner session guide
- `README.md` / `INTEGRATIONS.md` — feature docs
- Authoritative architecture reference: Joplin "Loki Architecture" notebook,
  maintained via skillkit `archdoc`.

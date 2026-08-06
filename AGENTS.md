# Loki — Agent Operating Rules

You are working on **Loki**, a live production personal AI assistant for
Kavaris ("Boss"). This repository IS the deployment: `loki.service` runs
`venv/bin/python loki_bot.py` from this working tree. Anything you edit here
is what runs after the next restart, and uncommitted changes may already be
live.

**Read `docs/agent-context/CURRENT_HANDOFF.md` first, every session.** It is
under a minute and tells you what is active right now.

## Source-of-truth precedence

When sources disagree, higher wins. Never let an old AI conversation override
newer live evidence.

1. Current live production state (`systemctl`, `journalctl`, DB reads, HTTP probes)
2. Current repository code and configuration
3. Durable project documentation (`docs/agent-context/`, `docs/`)
4. Git history
5. `docs/agent-context/CURRENT_HANDOFF.md`
6. Historical AI summaries (`docs/HISTORICAL_HANDOFF.md`, `CLAUDE_HISTORY_IMPORT.md`)

A doc that contradicts the running system is a stale doc. Verify, then fix
the doc.

## Completion-first

One requested user-facing objective stays active until it is **DONE** or the
Boss explicitly **parks** it.

- Fix implementation defects you hit in the requested path, run focused
  validation, and continue toward the original goal.
- Do not endlessly audit, restart completed investigations, or branch into
  unrelated work.
- Passing unit tests is **not** completion. Completion is verified live
  behaviour plus the stated DONE condition.
- Do not stop after every minor defect. Do not stop after proposing an
  architecture.

Interrupt the Boss only for: credentials/authentication, unavoidable
privileged bootstrap, physical action, destructive approval,
security-sensitive approval, or a genuinely ambiguous high-risk decision.

## Production safety — non-negotiable

- Do NOT rebuild, re-architect, or rename major components.
- Do NOT reset or discard uncommitted work — it may be running in production.
- Do NOT restart `loki.service`, containers, or run migrations without approval.
- Do NOT commit, push, pull, merge, or switch branches without approval.
- Do NOT send Discord/Telegram messages, trigger Home Assistant actions, or
  write to Joplin while testing — those are real production side effects.
- Never display `.env` values, tokens, or keys. Variable names only, values as
  REDACTED. `.env.bak*` files also contain live secrets.
- LLM priority is settled: gpt-5.1 primary, Groq fallback + CHAT routing via
  `routing.json` (undo = `enabled: false`). Do not re-litigate.
- qBittorrent stays OUT of gluetun. Settled after a week of testing; never
  "fix" the gluetun/qbittorrent pairing.

## Git

- Never `git push` without explicit Boss permission for that push.
- Stage specific files; never `git add -A` / `git add .`.
- Preserve unrelated dirty and untracked files — several are live production
  state (see `docs/agent-context/OPERATIONS_POLICY.md`).
- Never force-push to master. Never `--no-verify`. Never amend published commits.
- Commit messages explain WHY, not WHAT.

## Validation

- Syntax: `venv/bin/python -c "import ast; ast.parse(open('FILE').read())"`
- Focused tests: `venv/bin/python -m unittest tests.test_<module>`
- Full suite: `venv/bin/python -m unittest discover -s tests -p 'test_*.py'`
  — **8 failures are pre-existing** under `discover` (import-order pollution in
  `test_task_supervisor`, `test_homelab_lifecycle`, `test_hermes_guard`).
  Baseline them against a clean tree before blaming your change.
- DB inspection is read-only: `sqlite3 "file:PATH?mode=ro"` — the bot holds
  these open in WAL.
- There is no pytest suite, no linter config, and no build step.

## Architecture invariants

- `loki_bot.py` is the monolith entrypoint (~6.6k lines). Satellite modules
  around it; `personality.py` owns ALL tone and prompts.
- skillkit (`/home/g2k247/skillkit`, separate repo) owns the operational brain.
  Loki is a caller (identity `loki`). Playbooks EXTEND the planner, never
  replace it.
- Memory ownership: Joplin `Loki/Memories` is source of truth for explicit
  facts; ChromaDB `boss_memory` is a rebuildable index (never authoritative);
  SQLite `loki_memory.db` = conversation history/profiles, `jobsite.db` = work
  sessions. Do not merge these systems.
- Verification: success is proven, not assumed.

## Skills

Deep task knowledge lives in `.agents/skills/`. Read the relevant `SKILL.md`
**before** touching that subsystem — each one records failure traps already
paid for. Do not re-derive them.

`loki-homelab` · `black-boxx` · `incident-management` · `hermes-operations` ·
`nas-maintenance` · `container-updates` · `joplin` · `discord-telegram` ·
`plex-nas`

## Do not reopen

These are settled. Do not re-investigate without the Boss explicitly asking:
BLACK-BOXX boot race · Tracearr v1.5.0 update path · Joplin CLI sidecar ·
maintenance notification amplification · Hermes/OpenRouter guard ·
gluetun/qbittorrent.

# Operations Policy

## The deployment is the working tree

`loki.service` runs `venv/bin/python loki_bot.py` with `WorkingDirectory` set to
this repository. Consequences you must internalise:

- Any file you edit here is what runs after the next restart.
- Uncommitted changes may **already be live**. Never `git checkout`, `git
  reset`, or `git stash` to "clean up" — you may be deleting production code.
- A code change is not live until `loki.service` restarts, and **restarting
  needs Boss approval**.
- Module-level constants bind at import. Editing `maintenance_policy._COMMANDS`
  or a `DB_PATH` default does nothing to the running process until restart.

## Restart policy

`sudo systemctl restart loki` — **approval required, every time.**

Before asking for one, be able to say what changed, why it needs a restart, and
what you will verify afterwards. After a restart, check the startup banner in
`journalctl -u loki` and confirm the subsystem you touched reports online.

## What runs without you

- Homelab monitor polls every 300s and can open incidents and perform AUTO-tier
  repairs on its own.
- Cron: RAG ingest every 6h, skillkit Advisor daily 13:00 UTC, CIE weekly
  Sun 13:30 UTC.
- watchtower updates every running container daily at ~19:17 UTC (unmanaged —
  see `COMPLETED_WORK.md`).

Do not fight the autonomous stack. If a monitor keeps reopening an incident, fix
the underlying health, do not suppress the monitor.

## Testing without production side effects

**Never** while testing: send Discord or Telegram messages, trigger Home
Assistant actions, write to Joplin, submit a Hermes job, or run a repair-class
command against a live asset.

Safe patterns:

- Read-only runbooks: `run_runbook(asset, allow_repairs=False)`.
- Scratch DBs via env overrides **set before import**: `HOMELAB_DB_PATH`,
  `DRAFTS_DB_PATH`, `HOMELAB_LIFECYCLE_MIRROR`, `HOMELAB_DECOMMISSION_ARCHIVE_DIR`,
  `HOMELAB_ASSETS_PATH`.
- DB inspection read-only: `sqlite3 "file:PATH?mode=ro"` — the bot holds these
  open in WAL.
- Stub the mutating `Ops.run` command when exercising an approval path.

### The test-pollution trap

`loki_bot.py` unconditionally imports `homelab_lifecycle` and
`homelab_maintenance` as optional side-effect modules. Those bind `MIRROR_PATH`
and `DB_PATH` from `os.getenv()` **at import time**. Python caches modules, so
whoever imports first in the process wins that binding permanently.
`unittest discover` runs files alphabetically in ONE process.

This already caused real data loss: a new test file importing `loki_bot` sorted
before `test_homelab_lifecycle.py` and caused test writes to land on the real
`homelab_incidents.db` and `config/homelab_lifecycle.yml`, deleting the live
`ivn-site` decommission record.

**Any new test file importing `loki_bot`** must neutralize those side-effect
imports first (`sys.modules.setdefault(name, None)` before the import, removed
afterwards) — see the top-of-file comment in
`tests/test_duplicate_link_guard.py`. Then run the FULL discover suite and check
`git status` plus `.db` mtimes before trusting a green result.

## Git policy

- Stage specific files. Never `git add -A` or `git add .`.
- **Never `git push` without explicit Boss permission for that push.** The repo
  is 46 commits ahead of origin deliberately.
- Never force-push master, never `--no-verify`, never amend published commits.
- One logical change per commit. Messages explain WHY.
- Check `git status` and `git diff` before committing.
- Preserve unrelated dirty and untracked files.

## Files that are dirty by design

- `config/homelab_lifecycle.yml` — generated mirror of the `asset_lifecycle`
  table. Rewritten on every lifecycle change. The DB is authoritative; nothing
  reads the YAML back as truth. Do not hand-edit it.
- `tool_calls.jsonl`, `loki_bot.log`, `*.db*`, `*_state.json` — runtime state,
  gitignored. Note that running the test suite appends to the production
  `tool_calls.jsonl`.

## Approval-required actions

Restarting `loki.service`, containers, or running migrations · committing,
pushing, pulling, merging, switching branches · any APPROVAL-tier maintenance
action · installing packages · production configuration changes · anything
destructive.

## Escalation ordering

Deterministic runbook → AUTO repair if it matches exactly → approval draft for
APPROVAL tier → Hermes only for genuinely unknown conditions → Boss for MANUAL
tier. Never skip a rung to save time.

## End-of-task protocol

After meaningful completed work, update:

- `docs/agent-context/PROJECT_STATE.md` — live result, version/state
- `docs/agent-context/COMPLETED_WORK.md` — labelled DONE/PARTIAL/UNFINISHED
- `docs/agent-context/TASK_LEDGER.md` — close the entry
- `docs/agent-context/CURRENT_HANDOFF.md` — rewrite for the next agent
- `docs/agent-context/DECISIONS.md` — only if a durable architectural decision
  was made

Record the live result, the version/state, the local commit hash, and any
durable decision. Keep it lightweight for trivial edits — this protocol is for
meaningful work, not every typo fix.

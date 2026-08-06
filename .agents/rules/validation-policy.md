# Validation Policy

Success is **proven, not assumed**. This is the project's core principle —
skillkit's `verification.confirm()` and solve's "no unverified solved" rule
exist for it, and the maintenance controller enforces it in code.

## Commands

```bash
# Syntax check a single file
venv/bin/python -c "import ast; ast.parse(open('FILE').read())"

# Focused test module (prefer this while iterating)
venv/bin/python -m unittest tests.test_<module>

# Full suite
venv/bin/python -m unittest discover -s tests -p 'test_*.py'

# Read-only DB inspection (the bot holds these open in WAL)
sqlite3 "file:/home/g2k247/loki-bot/NAME.db?mode=ro" "SELECT …"

# Read-only RAG eval
venv/bin/python eval_rag.py --run BAAI/bge-small-en-v1.5:discord_chunks
```

There is no pytest suite, no linter config, and no build step.

## The pre-existing failure baseline

`unittest discover` currently reports **8 failures** that are NOT yours:

- `test_task_supervisor` — 2 errors
- `test_homelab_lifecycle` — 3 (2 errors, 1 failure)
- `test_hermes_guard` — 3 failures

They come from import-order pollution and pass standalone. **Before blaming a
change, baseline it**: stash your edits, run discover, compare the failure list.
Reporting "the suite fails" without that comparison is a false alarm.

## Adding a test that imports `loki_bot`

`loki_bot.py` unconditionally imports `homelab_lifecycle` and
`homelab_maintenance`, which bind `MIRROR_PATH` and `DB_PATH` from `os.getenv()`
**at import time**. Python caches modules, so whoever imports first in the
process wins that binding permanently — and `discover` runs every file in one
process, alphabetically.

This has already destroyed live production data once.

So: neutralize those side-effect imports before importing `loki_bot`
(`sys.modules.setdefault(name, None)`, removed afterwards — see the top of
`tests/test_duplicate_link_guard.py`), then run the **full** discover suite and
check `git status` plus `.db` file mtimes before trusting a green result.

## Verifying live behaviour

A test is not a verification. For anything user-facing, prove it against the
running system:

- `systemctl is-active|is-enabled <unit>` · `systemctl status <unit>`
- `journalctl -u loki --since "<time>"` — the startup banner confirms which
  subsystems came online
- `run_runbook(asset, allow_repairs=False)` for asset health
- Read-only SQLite queries against incidents, drafts, tasks
- `ip -br addr` / `ip rule show` for network state

## Verifying a repair

Check the evidence that **actually changed**. If a health flag was already true
before the repair, it proves nothing afterwards. For a latent fault, the proof
is the advisory clearing — not `healthy` staying true.

## What "done" requires

1. The stated DONE condition is met.
2. Live evidence, quoted, not paraphrased.
3. Focused tests green, with the pre-existing baseline accounted for.
4. Durable state updated per the end-of-task protocol in
   `docs/agent-context/OPERATIONS_POLICY.md`.

If a step failed, say so and show the output. Never report completion you have
not verified.

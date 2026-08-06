# Production Safety

**This repository is the running deployment.** `loki.service` executes
`venv/bin/python loki_bot.py` from this working tree. Edits are live after the
next restart, and uncommitted changes may already be live.

## Never without approval

- Restart `loki.service`, any container, or run a migration.
- Commit, push, pull, merge, or switch branches.
- Install packages or change production configuration.
- Reset, checkout, or stash to "clean up" the working tree — you may be
  deleting running production code.

## Never at all while testing

- Send a Discord or Telegram message.
- Trigger a Home Assistant action.
- Write to Joplin.
- Submit a Hermes job.
- Run a repair-class command against a live asset.

These are real production side effects with real recipients.

## Never at all

- Print `.env` values, tokens, or keys. Variable names only, values REDACTED.
  `.env.bak*` files also hold live secrets.
- Rebuild, re-architect, or rename major components.
- Add Loki to the `docker` group or grant wildcard docker sudo.
- Add a state-changing verb to `/usr/local/sbin/loki-nas-maint`.
- Add a second path to `POST /diagnose` that bypasses `hermes_guard`.
- Use `sshpass`, disable host-key verification, or pass passwords as arguments.
- Run `docker prune`, delete a persistent volume casually, or `rm -rf` outside
  a scratch directory.

## Permission tiers on dex247

**ALLOW** — repository reads and edits; `git status`/`diff`/`log`; focused
tests; read-only diagnostics (`systemctl status/is-active/is-enabled`,
`journalctl`, `ip`, `docker ps`/`inspect`/`logs`, `sqlite3 "file:…?mode=ro"`).

**ASK** — service or container restarts; controlled consequential mutation;
privileged NAS deployment; destructive cleanup; writing outside the repository.

**DENY** — `git push`; unrestricted `sudo`; root SSH; modifying `~/.ssh`;
reading or exfiltrating secrets; `rm -rf`-style destruction; Docker prune or
volume deletion.

`.agents/hooks.json` enforces the DENY and ASK tiers via
`.agents/scripts/permission-guard.sh`. The hook is a backstop, not permission to
be careless — and it must not be disabled or weakened to make a task easier.

## Safe testing patterns

- Read-only runbook: `run_runbook(asset, allow_repairs=False)`.
- Scratch DBs via env overrides **set before import**: `HOMELAB_DB_PATH`,
  `DRAFTS_DB_PATH`, `HOMELAB_LIFECYCLE_MIRROR`, `HOMELAB_ASSETS_PATH`,
  `HOMELAB_DECOMMISSION_ARCHIVE_DIR`.
- Stub the mutating `Ops.run` command when exercising an approval path.
- Copy a production DB before testing a schema migration against it.

## Before asking for a restart

Be able to state what changed, why it needs a restart, and what you will verify
afterwards. Module-level constants bind at import — a change to
`maintenance_policy._COMMANDS` or a `DB_PATH` default does nothing until the
process restarts.

After the restart, check the startup banner in `journalctl -u loki` and confirm
the subsystem you touched reports online.

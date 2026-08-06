---
name: container-updates
description: >-
  Use when working on Docker container updates, rollbacks, image digest
  comparison, or the Immich update path on dex247. Covers container_updates.py
  and the update-related commands in maintenance_policy.py. Read before
  changing how an update is chosen, applied, backed up, or rolled back — and
  before assuming an update is safe to apply automatically.
---

# Container Updates

## Scope

`container_updates.py` (commit `5d69287`): read-only inventory plus
approval-gated update and rollback for registry-known Compose services on
dex247. Builds on the homelab maintenance controller.

Tools, all Boss-only: `container_update_inventory`, `container_update_check`,
`container_update_preview`, `container_update_prepare`, `container_rollback`,
plus approval-gated `container_apply_update` and `container_rollback_update`.

## Design rules, enforced in code

- Update decisions come from **registry digests and official GitHub release
  metadata**, never image age.
- **Prereleases are never a target.**
- **Moving tags are resolved to an exact version before pulling.** An approved
  update applies what was approved, not "whatever `:latest` means now".
- One compose project at a time — never `-p` across projects.
- **Backups must succeed or the update is blocked**: config paths plus a
  `pg_dump` that is verified by reading it back with `pg_restore --list`.
- **Rollback is refused outright once a schema migration has run.**

## Digest comparison — the trap

Local image digest (`docker image inspect .Id` / RepoDigest) compares directly
against `docker buildx imagetools inspect`'s `Digest:` line.

`docker manifest inspect` digests do **NOT** match — platform manifest vs index
manifest. Do not "fix" a comparison by switching to it.

## The watchtower conflict — currently unresolved

`watchtower` on dex247 runs `--cleanup --interval 86400` with **no**
`WATCHTOWER_LABEL_ENABLE` and **no** `WATCHTOWER_MONITOR_ONLY`. It therefore
auto-updates **every running container** daily at ~19:17 UTC and deletes the
replaced images.

Confirmed from its own logs (`scanned=33 updated=3` daily).

Why it matters: it bypasses Loki's approval gate entirely, pulls **moving tags**
(`:release`, `:latest`) including on stateful Immich, and `--cleanup` **destroys
the rollback target** Loki records before an approved update.

Loki's inventory detects any watchtower/diun/ouroboros container and reports
`external_autoupdate_active`, so the conflict is never silently hidden.

**The fix — `WATCHTOWER_MONITOR_ONLY=true` and dropping `--cleanup` — is a
production service change needing the Boss's approval. It is NOT done.** Do not
apply it unasked; do surface it when an update task is discussed.

## Immich

Running **v3.0.3**, which IS the latest stable (published 2026-07-15) —
nothing to update.

- Compose: `/home/g2k247/immich/docker-compose.yml`
- `IMMICH_VERSION` is **commented out** in `.env`, so it uses the moving
  `:release` tag. An approved update pins that variable first.
- DB is `immich`/`immich` in `immich_postgres`.
- Postgres and Redis are pinned dependencies the Immich runbook deliberately
  does **not** touch — a Postgres major upgrade is a manual migration.

## Gotchas already paid for

- `du` on the Immich upload library **times out**. Use `df` only.
- Splatting a result dict into a helper that also takes `ok` collides at call
  time — filter at the call site, not in the body.
- A test that swaps `hm._registry` must restore it in `tearDown`, or every later
  test module inherits the fake homelab.

## Never

No `docker prune` in the allowlist, ever. No `down -v`, no `--rmi`, no
`--remove-orphans`, no network removal — compose drops its own project network
and a shared network is never touched. Volume removal only for a volume proven
unshared at both plan time and run time, inside an approval-gated decommission.

## Completion criteria

Target resolved to an exact version · prerelease rejected · backup taken and
verified by reading it back · update applied through the approval gate · health
verified after · rollback target still exists · registry digest recorded.

## Source

`container_updates.py` · `maintenance_policy.py` (update commands) ·
`config/homelab_assets.yml` (`updates:` blocks) ·
`.agents/skills/nas-maintenance/SKILL.md` for the NAS-side equivalent

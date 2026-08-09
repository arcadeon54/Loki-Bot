---
name: nas-maintenance
description: >-
  Use when working with the UGREEN NAS — the restricted loki-nas-maint
  dispatcher, its sudoers containment, SSH access, Tracearr on the NAS, or the
  approval-gated Tracearr update path. Covers nas_maint.py and
  docs/NAS_MAINTENANCE.md. Read before any NAS operation; the security model
  there is vendor-constrained and easy to break.
---

# NAS Maintenance

## Access model

| | |
|---|---|
| Host | UGREEN NAS, `192.168.1.63`, hostname `Unimatrix0001` |
| User | `unimatrix_001` |
| SSH | alias `nas-maint`, dedicated key |
| Entry point | `/usr/local/sbin/loki-nas-maint` (root-owned dispatcher) |
| Client | `nas_maint.py` |

**Loki has no shell on the NAS.** Six literal read-only actions only:
`host_status`, `container_inventory`, `tracearr_status`,
`tracearr_dependencies`, `tracearr_recent_logs`, `tracearr_update_check`.

Loki-side tools: `nas_status`, `tracearr_status`, `tracearr_diagnose`,
`tracearr_update_check`, `tracearr_update`, plus `nas_network_status`,
`nas_network_speed_test`, `nas_disk_status`.

## Two vendor traps

### 1. The decoy Redis

The NAS runs its **own UGOS redis** as a host systemd service on
`127.0.0.1:6379`. That is **not** Tracearr's Redis. Diagnosing it reports on the
wrong service entirely. Tracearr's Redis is the container `tracearr-redis`
inside the compose project.

### 2. SSH key options do not confine Loki

UGOS sets a global `ForceCommand /etc/ssh/force_command.sh` in `sshd_config`
that **overrides per-key `command=""`** for admin-group users. So the enumerated
sudoers rule is the *only* containment.

A non-admin account cannot be substituted — UGOS restricts non-admins to
rsync/sftp/scp, so they cannot run the dispatcher at all.

**Therefore:** never add Loki to the docker group, never grant wildcard docker
sudo, and never add a state-changing verb to the dispatcher.

UGOS updates can wipe `authorized_keys` and `/etc/sudoers.d`. Failures surface
as precise errors from `_classify_ssh_failure` — read them rather than guessing.

## Tracearr

Compose project `tracearr`, `/volume2/tracearr/docker-compose.yml`, network
`tracearr_tracearr-network`:

- `tracearr` — app, **v2.0.1**, `172.19.0.4`, port 3000→3001
- `tracearr-redis` — service `redis`, `172.19.0.2`
- `tracearr-db` — TimescaleDB, service `timescale`, `172.19.0.3`

Version drift is expected, not a bug: **watchtower on the NAS auto-updates
Tracearr independently of Loki's approval-gated path** (registry:
`updates.applied_by: watchtower-on-nas`; see the tracked backlog item
`watchtower → monitor-only`). v1.5.0 → v2.0.1 happened this way on
2026-08-07 — a MAJOR bump watchtower applied without approval. Reconciled in
`config/homelab_assets.yml` 2026-08-09; verify against live
`tracearr_status`/`tracearr_dependencies` rather than trusting this file if
they ever disagree (source-of-truth precedence in `AGENTS.md`).

### The update path — DONE, do not redesign

Approval-gated, and it has run for real: v1.4.27 → v1.5.0, verified healthy, no
rollback needed. Dispatcher checksum `7d41354e…`, readiness-aware verification
(150s deadline, 5s poll) used for both post-update and post-rollback.

Three defects fixed while finishing it — all still load-bearing:

1. **`tracearr_update` takes an optional `version`.** It previously always
   targeted "latest stable", and v2.0.0 landed upstream hours before the v1.5.0
   run — an approved "v1.5.0" update would have silently applied a MAJOR
   release. Pass the tag whenever the Boss names one; it is verified against the
   upstream feed (must exist, must not be a prerelease), and the approval
   summary states whether the target is pinned or merely newest.
2. **A verified update writes version + image digest back to
   `config/homelab_assets.yml`** (`nas_maint._record_registry_version`).
   Without it the registry keeps claiming the old release and the next
   update-check reports the live deployment as configuration drift. It is a
   **line edit, not a YAML round-trip** — that file's comments are documentation.
3. **`tools.user_level()` treated an unset `OWNER_USER_ID` as a match**, so
   `"" == ""` made any blank-id caller Boss, including for approval-gated
   destructive tools. A missing `.env` must lock Loki down, not open it up.

### Test isolation hazard

The registry writeback gave the update success path a **real file side effect**,
and mocked tests hit production config, rewriting the pinned digest with a
fixture value. `tests/test_nas_tracearr.py` points `HOMELAB_ASSETS_PATH` at a
throwaway copy for the whole module. **Any new test touching an update success
path must keep that isolation.**

### Digest comparison

Local image digest (`docker image inspect .Id` / RepoDigest) compares directly
against `docker buildx imagetools inspect`'s `Digest:` line.
`docker manifest inspect` digests do **not** match (platform vs index).

## Open, deliberately unfixed

- **Tracearr restart churn** — evidence (`restart_count` 268, bursty, ~2
  restarts in 78s at times, Redis/Postgres steady at 0) is from the v1.5.0
  deployment. watchtower's 2026-08-07 recreate onto v2.0.1 reset
  `restart_count` to 0; that is not proof the app-side defect is fixed, only
  that the counter restarted. App-side, cause unproven, **no automatic repair
  by design**. Re-open with fresh evidence if churn resumes.
- **`/home/unimatrix_001` is mode 0777** — a real security issue needing a
  tested remediation.
- **Tracearr is already on v2.x** (v2.0.1, applied by watchtower, not by any
  Loki-orchestrated evaluation) — a deliberate compatibility/feature review of
  v2.x is still not scheduled; this only reconciles the registry to match what
  is actually running.

## Completion criteria

Operation goes through the dispatcher · no new state-changing verb added · no
docker-group or wildcard sudo · updates pinned to an exact verified version with
backup, readiness verification and rollback · registry writeback preserved ·
test isolation preserved.

## Source

`nas_maint.py` · `docs/NAS_MAINTENANCE.md` · `tests/test_nas_tracearr.py` ·
`config/homelab_assets.yml` (`ugreen-nas`, `tracearr`)

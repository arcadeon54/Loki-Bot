# Task Ledger

One row per requested user-facing objective. An objective stays **ACTIVE** until
it is **DONE** or the Boss explicitly **PARKED** it. Close the row with the live
result and the local commit.

| # | Objective | State | DONE condition | Result / commit |
|---|---|---|---|---|
| 1 | BLACK-BOXX false all-check failure | **DONE** 2026-08-06 | Runbook distinguishes shared prerequisite/executor failure from genuine subsystem failure; one root cause, not twelve | 18→17 checks green; `8355d21` |
| 2 | BLACK-BOXX boot-race ownership | **DONE** 2026-08-06 | `wg-quick@wg-ap` disabled via Loki's approval gate, `black-boxx-ap.service` sole boot owner of `wg-ap`, AP healthy, no reboot | draft `dr_c939bae02949` → incident `hi_66777414c2b0` repaired; `42380d1` |
| 3 | Antigravity CLI install + durable AI context migration | **DONE** 2026-08-06 | agy installed and authenticated, durable model-independent context + rules + skills + Loki Builder exist, agy answers the handoff question correctly from project context, no production behaviour changed, committed locally | agy 1.1.10 authenticated; workspace context, 10 skills and the Loki Builder role verified live via read-only `agy -p`; see `ANTIGRAVITY_BOOTSTRAP.md` |
| 4 | Hermes/OpenRouter credit drain | **DONE** 2026-08-05 | Circuit breaker + budgets make drain impossible even if dedup breaks | `51fda47`, 30 tests |
| 5 | Maintenance incident amplification / Telegram spam | **DONE** 2026-08-05 | Persistent dedupe, one Hermes job per unresolved incident, Discord ops feed | `daf150e`, 17+22 tests |
| 6 | Tracearr update path | **DONE** 2026-08-05 | Approval-gated, pinned, backed up, verified, rollback-capable; v1.5.0 live | `b075780`, `251807b` |
| 7 | Joplin note read-back | **DONE** 2026-08-05 | Loki can read back notes it writes to the Boss's own notebooks | `dc479a6` |
| 8 | Presence notification style | **DONE** 2026-08-05 | Concise legacy style; roommate state retained on welcome-home | `ede172d`, 20 tests |
| 9 | Google Sheets export | **DONE** 2026-08-06 | Reliable sync queue, no data loss on failure, idempotency maintained | Durable queue reading `sheets_ok=0` from SQLite |
| 10 | qBittorrent recurring connectivity | **DONE** 2026-08-06 | Root cause proven, durable fix applied, LAN+proxy+nzb360 stable beyond recurrence window | Removed `mem_limit: 1g` from compose; `qbittorrent_health` runbook added |
| 11 | RAZR disk growth / Phase-1 capacity recovery | **DONE** 2026-08-08 | Root at 78%→29%; LV extended online (100→235 GiB), orphan blob removed, caches cleaned | `lvextend`+`resize2fs` online; no downtime |
| 12 | Filebrowser production failure | **DONE** 2026-08-09 | Root cause proven, durable repair, container up, storage accessible, HTTP/LAN/proxy verified, survives recreate, Loki sees it healthy, 8-point Reliability penalty clears naturally | Stale FUSE endpoint at `/mnt/unicron-downloads`, not a path conflict; `ExecStartPre` unmount + MagicDNS + `Before=docker.service` + backoff; `rslave` binds; `filebrowser_health` runbook, 23 tests. Reliability 87→95 |
| 13 | Unicron sshfs share restoration | **DONE** 2026-08-09 | SSH auth restored, `/srv/unicron` populated, Filebrowser browses it, systemd mount persistent, no stale FUSE loop, no unrelated service damaged | Two faults: dex247's key missing from the rebuilt box **and** `/dev/sdb1` dropped from asus's fstab so `/mnt/Disk1/downloads` did not exist. Key installed via razr's pre-existing access; disk pinned by UUID with `nofail`; host key verified 3 ways and pinned, unit tightened to `StrictHostKeyChecking=yes`. 53 entries live |
| 14 | asus fstab CIFS parse errors | **DONE** 2026-08-09 | Both malformed entries corrected, fstab parses cleanly, both CIFS shares mount, existing storage/Filebrowser paths unaffected | Unescaped spaces in the source field meant systemd generated **no unit at all** for `Zion Cinema`/`Folder 1`; now `\040`-escaped. 0 parse errors (was 2); both mounted and readable; 3 pre-existing CIFS mounts, `/mnt/Disk1` and Filebrowser all undisturbed |

## Backlog — not started, not authorized

| Objective | State | Note |
|---|---|---|
| Weekly Discord export 403 | UNFINISHED | Needs a Boss-side Discord permission, not code |
| watchtower → monitor-only | UNFINISHED | Production change, needs approval; bypasses Loki's gate today |
| Tracearr restart churn | UNFINISHED | 268 restarts, cause unproven, no auto-repair by design |
| Tracearr v2.x | UNFINISHED | Separate evaluation, deliberately not scheduled |
| Telegram voice messages | UNFINISHED | `_handle()` reads text/caption only |
| Centralized Model Router | PLANNED | Design only. Requires explicit approval to start |
| `/home/unimatrix_001` mode 0777 | UNFINISHED | NAS security issue, needs tested remediation |

## How to use this

When the Boss assigns work, add a row with the DONE condition **stated before
you start**. Update it when the objective closes. Do not add rows for
speculative work, and do not start a backlog item because it looks easy — the
backlog is not an authorization.

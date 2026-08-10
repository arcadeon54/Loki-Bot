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
| 15 | Nextcloud private-download public links | **DONE** 2026-08-09, live | External recipient gets a working public HTTPS share URL, no LAN/VPN needed, Keep/Delete work end-to-end, internal addresses never exposed | Two root causes: URL built from the internal base **and** `nextcloud_integration` imported before `load_dotenv()`, pinning it to the unreachable pre-rebuild asus fallback. Token now taken from OCS and re-based onto `NEXTCLOUD_PUBLIC_BASE_URL`; per-batch folders; read-only shares, 72 h default expiry. Verified live: anon GET 200, revoked link 404. 32 tests |
| 16 | Tracearr registry version drift | **DONE** 2026-08-09, corrected 2026-08-10 | Registry matches live production, Tracearr untouched, update-workflow version comparison verified correct against a v2.x installed version | Live v2.0.1 confirmed independently via dispatcher before any edit (not trusted from the pre-existing uncommitted diff). Registry `version`/`image_digest` confirmed correct; fixed a separate swapped-IP bug in `dependencies.*.container_ip`; addendum on stale restart-churn evidence. 4 new tests, 110 total in `tests/test_nas_tracearr.py`. **Corrected 2026-08-10:** the v1.5.0→v2.0.1 update was Loki's own approval-gated `tracearr_update`, not watchtower — Loki's Joplin maintenance log has its own success record (`prepare_id e7f4e2ef6f381073`) matching the container recreate timestamp to the minute. `applied_by` field fixed to `loki_approval_gate`. See `CURRENT_HANDOFF.md`. |
| 17 | Hermes resilient diagnostic capability (provider fallback) | **DONE (dex247 side)** 2026-08-10 | Fable capability proven/ruled-in or explicitly ruled out with an exact blocker; per-provider-aware status vocabulary; `paused_auth` gap fixed; no bridge changes without sign-off; no paid Hermes request | OpenRouter confirmed exhausted (402, read-only). Fable = Claude Fable 5 via Anthropic's own API; Hermes Agent v0.19.0 on razr already has a native Anthropic adapter + fallback-chain feature, unconfigured — blocked on a Boss-run credential add on razr (`hermes auth add anthropic` + `hermes fallback add`), not on code. Fixed a real gap: `paused_auth` bridge job state was never fed to `hermes_guard.py`, so a bad credential never opened the circuit. Added `auth` as its own failure class (opens instantly like billing) and a `status_label` vocabulary (operational/protective_quota/protective_budget/authentication_failed/rate_limited/unreachable/recovering). Flagged that the bridge's own cost accounting only prices two OpenRouter-routed models and would show $0 for a Fable-served job — `last_serving_model_cost_telemetry` surfaces that rather than agreeing. Bridge (`~/hermes-bridge` on razr) deliberately not touched — Boss declined that scope. 15 new tests, `tests/test_hermes_guard.py` now 45. See `.agents/skills/hermes-operations/SKILL.md`. |

## Backlog — not started, not authorized

| Objective | State | Note |
|---|---|---|
| Weekly Discord export 403 | UNFINISHED | Needs a Boss-side Discord permission, not code |
| watchtower → monitor-only | UNFINISHED | Production change, needs approval. The NAS watchtower's actual reach is unconfirmed — the one case it was blamed for (Tracearr v2.0.1) turned out to be Loki's own approval-gated update (corrected 2026-08-10, see #16). dex247's watchtower auto-updating every container daily is the still-real, separately-confirmed case (see `watchtower-autoupdates-conflict` memory) |
| Tracearr restart churn | UNFINISHED | 268 restarts, cause unproven, no auto-repair by design |
| Tracearr v2.x compatibility review | UNFINISHED | v2.0.1 already live (applied via Loki's own approval-gated update 2026-08-07, not watchtower — corrected 2026-08-10; registry reconciled #16); a deliberate feature/compatibility review is separate and still not scheduled |
| Telegram voice messages | UNFINISHED | `_handle()` reads text/caption only |
| Centralized Model Router | PLANNED | Design only. Requires explicit approval to start |
| `/home/unimatrix_001` mode 0777 | UNFINISHED | NAS security issue, needs tested remediation |
| Configure Fable as a Hermes fallback provider | UNFINISHED, Boss action | `sudo -u hermes hermes auth add anthropic --type api-key` then `hermes fallback add` on razr — needs an Anthropic API key with Fable access, entered interactively on razr. See #17 and `.agents/skills/hermes-operations/SKILL.md` |
| Bridge-side cost accounting for non-OpenRouter providers | UNFINISHED | `~/hermes-bridge/lib/budget.mjs` rate card + `lib/usage.mjs` spend probe only cover the two OpenRouter-routed models; a Fable-served job would price at $0. Needs a razr-side bridge change; explicitly declined in scope for #17 (second production service, second machine) |

## How to use this

When the Boss assigns work, add a row with the DONE condition **stated before
you start**. Update it when the objective closes. Do not add rows for
speculative work, and do not start a backlog item because it looks easy — the
backlog is not an authorization.

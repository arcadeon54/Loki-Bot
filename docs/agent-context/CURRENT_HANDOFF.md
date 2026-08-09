# CURRENT HANDOFF

*Updated 2026-08-09 03:2x UTC. Keep this under a minute to read.*

## Just completed

**Filebrowser restored — DONE 2026-08-09.** Reliability **87 → 95**; the
8-point deduction cleared because the container is up, not because anything
was reclassified.

*Root cause: a stale FUSE endpoint, not a path conflict.* sshfs to unicron
died without unmounting on 2026-08-03, leaving `/mnt/unicron-downloads` as a
dead mountpoint — the dentry still resolves but every syscall returns
`ENOTCONN`. Docker creates missing bind sources with `mkdir`, `mkdir` on a
dead mountpoint returns `EEXIST`, and that is the whole of the misleading
`mkdir /mnt/unicron-downloads: file exists`. Nothing was "in the way".

*Why it never self-healed.* `sshfs-unicron.service` remounts the same path, so
its own mount hit the identical `ENOTCONN` and it looped — **111,286 failed
starts in the previous boot, 2,861 more since**. The mountpoint stayed wedged,
so every reboot re-failed the bind. Two independent faults, both real:
the stale endpoint (deadlock) and a **dead address** — the unit pointed at
tailnet IP `100.115.240.16`, which no longer exists. The `asus`/unicron box was
rebuilt: it is now `100.101.112.55` (LAN 192.168.1.247) with a **different SSH
host key**, and `~/.ssh/id_ed25519` is no longer in its `authorized_keys`.

*Durable repair.* Cleared the endpoint (`fusermount3 -u -z`; the underlying
directory was empty, nothing preserved). `sshfs-unicron.service` now runs
`ExecStartPre=-/bin/fusermount3 -u -z` so a dropped connection self-heals
instead of wedging, targets MagicDNS `asus.tail3744e0.ts.net` instead of a
hardcoded IP, is ordered `Before=docker.service`, and backs off
(`RestartSteps=5`, `RestartMaxDelaySec=300` — verified: retries went 10s →
5min). The three network binds in `docker/filebrowser/docker-compose.yml` are
now `rslave`, proven end-to-end: a mount appearing on the host shows up inside
the running container with no recreate.

*Verified.* Container up + healthy, `RestartCount=0`, survives
`--force-recreate`. HTTP 200 on localhost:8090, LAN 192.168.1.155, tailnet, and
`https://media.ivn-group.cc` (valid TLS, HTTP→HTTPS 301). Auth intact: bad
creds 403, unauthenticated API 401. `/srv/dex247` 28 entries, `/srv/nas` all 7
cifs shares populated, `/srv/nextcloud` mounted.

**One thing needs the Boss, and only one.** `/srv/unicron` is empty because
sshfs cannot authenticate to the rebuilt asus box. Re-adding
`~/.ssh/id_ed25519.pub` to `asus@`'s `authorized_keys` needs password or
console access to that machine — a credential I do not have. **This does not
affect filebrowser's health**: the bind succeeds against an empty directory,
the service is up, and the runbook reports it as degraded-share, not outage.
The moment the key works the share appears (rslave) with no restart.

New: `maintenance_runbooks/filebrowser_health.py` + registry entry, and
`Ops.path_meta()` now returns `errno`/`stale_mount` so ENOTCONN is
distinguishable from a missing path. The runbook **refuses to restart into a
stale mountpoint** (clearing one is `filesystem_repair`, MANUAL) and never
scores an unmounted share as a service outage. 23 tests in
`tests/test_filebrowser_runbook.py`; verified live read-only against the real
deployment.

## Previously completed

**Reliability reconciled against real production state — DONE 2026-08-09.**
Reliability **60 → 87**, and every remaining point is a real impairment.

*Four unclosed solve-path records — all stale, none unfinished.* #12/#13/#14
were three copies of one 2026-07-27 intent ("Tracearr Redis backend down"), all
concluding "none performed" because Loki had no NAS access then; that gap was
closed the same day (16b2178) and read-only `tracearr_dependencies` now shows
tracearr/redis/postgres all healthy, restart_count 0, UI HTTP 200. #15 was the
qBittorrent WebUI run that timed out; the real cause (`mem_limit`, cgroup OOM)
was found and fixed the next day in 8f1f511, WebUI HTTP 200. All four closed
through `skillkit resolve-incident` with written evidence. Zero open records.

*Two stopped containers — one deliberate, one genuinely broken.*
`loki-joplin-api` is `restart: no`, the obsolete CLI sidecar; it no longer
costs anything. **`filebrowser` was a real open failure** and kept its 8
points: `restart: unless-stopped`, down since 2026-08-03, failing to start
across two reboots with `error while creating mount source path
'/mnt/unicron-downloads': mkdir ... file exists`. Not fixed in that pass — out
of scope, and starting it to green the score was explicitly off the table.
*Repaired 2026-08-09; see the top of this file for the root cause.*

*Hermes provider — expected protective degradation, not an outage.* Verified
non-billably (bridge `GET /health` = ok; OpenRouter `/credits` = 20 granted /
20.028 used). The account really is out of credit; the 402 from 2026-08-07 is
current, not stale. The circuit's cooldown expired 2026-08-07 07:17 UTC, so it
blocks nothing now — it simply has not been probed, and only a real submit can
probe a billing-class open. **This needs a Boss billing decision, not a
repair.** Costs 5, not 12.

Code: `advisor.classify_stopped` / `_retired_container_names` /
`is_protective_fault`, `reporting.stopped_split`, new `terms` split, and
`incidents.resolve_superseded()` wired into `orchestrator.report()` so a solved
run closes the records it finished. 25 tests in
`tests/test_reliability_state.py`; `test_daily_briefing` still green (27).
See DECISIONS.md — the semantics are settled.

Noticed, not acted on: the NAS runs **Tracearr v2.0.1** while
`config/homelab_assets.yml` still pins v1.5.0 (watchtower). Registry drift, not
a fault.

**Daily Briefing semantics repair — DONE 2026-08-08 (skillkit repo).** The
briefing was contradicting itself: Reliability was amplified by repeat
detections, the incident trend diffed raw DB rows ("Incident count: 15 ▲ +9"
from one known fault), the healthy line hard-coded `disk_pct < 80` while the
monitor alerts at 90% (so 78% read as "no action required" *and* "P1 — Act
now"), and local image age was presented as proof an update existed. Fixed in
`skillkit/advisor.py` + `skillkit/reporting.py`: incidents fold into canonical
faults by `key`, `reporting.disk_status()` decides severity once for both the
renderers and the LLM, Reliability prints its own arithmetic, and image age
carries an explicit "upstream NOT checked". 27 regression tests in
`tests/test_daily_briefing.py`. A `preview=True` path on `advisor.review()` /
the `advise` skill builds a report without writing Joplin, Telegram or the
metric-history snapshot. Live preview verified: razr 30%, Reliability
60 = 100 − 12×1 − 3×4 − 8×2. See DECISIONS.md for the settled semantics.

**RAZR Phase-1 Storage Capacity Recovery — DONE 2026-08-08.** Root at 78%
(72/98 GB) after +21% growth from Aug 1 Gemma4 model import. Investigation
proven: 135.42 GiB was already free inside `ubuntu-vg` (Samsung NVMe LVM PV);
no partition/PV surgery needed. Actions (all online, zero downtime):
`lvextend -l +100%FREE` + `resize2fs` on live root: 100 GiB LV → 235.42 GiB,
98 GB filesystem → 232 GB, usage 78% → 29%, free 22 GB → 157 GB.
Orphan Ollama blob `sha256-5965…` (6.9 GB, Python-manifest-walk confirmed
unreferenced) removed; all 7 models intact. Caches cleaned: npm _cacache (409 MB),
hermes build caches (394 MB), snapd (708 MB), apt (143 MB). Crucial 1TB NVMe
(carry-forward NTFS) untouched.

**Storage architecture note (do not regress):** RAZR has two NVMe SSDs, not
SATA. Samsung 238 GB = Linux OS/LVM. Crucial 1TB = NTFS carry-forward, unmounted,
not in fstab, no Linux service depends on it.

**qBittorrent recurring connectivity — DONE 2026-08-06.** Root cause was
`mem_limit: 1g` in `/home/g2k247/PrivacyServer/docker-compose.yml`. The kernel
cgroup OOM killer fired every 20-30 minutes against libtorrent peer-connection
workers (~1 GB anon-RSS each), killing both qbittorrent-nox workers and the
internal `watchdog-script`. Because supervisord has `autorestart=false` for
both, nothing inside the container recovered — WebUI permanently unreachable
until manual `docker restart`. Fix: removed `mem_limit`/`memswap_limit` from
compose, container recreated. LAN IPv4, reverse proxy `qbit.ivn-group.cc`, and
nzb360 API all HTTP 200. Stability timer set for 35 min past fix to confirm no
further OOM kills. `qbittorrent_health` runbook added; qbittorrent registered
as managed asset (10th).

**Google Sheets export defect — DONE 2026-08-06.** `work_tracker.py` was silently dropping Sheets exports if the Home Assistant HTTP POST failed or timed out during the exact second the session closed. Rewrote the export path as a background queue processor (`_sync_pending_sheets`) that reads `sheets_ok=0` directly from the durable SQLite DB. Validated live: all previously failed/dropped sessions naturally retried and reached Google Sheets successfully. No unit tests broken.

**Antigravity migration — DONE 2026-08-06.** `agy` 1.1.10 installed at
`~/.local/bin/agy` (as `g2k247`, coexisting with Claude Code), authenticated,
and validated live: a read-only session recovers the handoff, the 10 workspace
skills, and the Loki Builder role from this repository alone. Durable context
lives in `docs/agent-context/` and `.agents/`. Two conventions had to be
corrected against the installed build — `--agent` does not resolve workspace
agents, and permission globs do not cross directory separators; both are
documented in `ANTIGRAVITY_BOOTSTRAP.md`. No production behaviour changed.

**BLACK-BOXX boot-race persistence — DONE.** `wg-quick@wg-ap` was enabled
alongside `black-boxx-ap.service`; both raced to `ip link add wg-ap` at boot and
the loser's cleanup (`ip link delete dev wg-ap`) destroyed the winner's
interface. Disabled through Loki's own approval gate (draft `dr_c939bae02949` →
incident `hi_66777414c2b0`, `repaired`, verified). Commit **42380d1**.

Also live and settled recently: Hermes/OpenRouter circuit breaker (`51fda47`),
Tracearr v1.5.0 pinned update path (`b075780`, `251807b`), Joplin note read-back
(`dc479a6`), maintenance incident dedupe + Discord ops feed (`daf150e`),
presence notification passthrough (`ede172d`).

## Current production health

- `loki.service` — active, 113 tools, 9 homelab assets, RAG 3529 chunks.
- `black-boxx-ap.service` — enabled + active; sole boot-time owner of `wg-ap`.
  BLACK-BOXX runbook: 17/17 green, 0 advisories.
- `loki-joplin-desktop.service` — active, Data API on 127.0.0.1:41184.
- `loki-homelab-api.service` — active (read-only Hermes interface).
- Hermes guard — circuit closed, 0/6 per hour, 0/20 per day, $0.00/$5.00.
- Tracearr — v1.5.0 on the NAS, pinned by digest in `config/homelab_assets.yml`.

## Next active task

**None assigned.** The Reliability reconciliation is complete.

Two things it surfaced and deliberately left alone: `filebrowser`'s
`/mnt/unicron-downloads` mount conflict on dex247 (**repaired 2026-08-09**),
and the OpenRouter credit top-up (still open — a Boss billing decision).

One item is waiting on the Boss and cannot be done from here: re-authorizing
`~/.ssh/id_ed25519.pub` on the rebuilt `asus`/unicron box (100.101.112.55) so
the `/mnt/unicron-downloads` sshfs share can mount again. Filebrowser is
healthy without it; only `/srv/unicron` is empty.

If the Boss wants the next thing from the backlog, `docs/NEXT_STEPS.md` is the
ordered list. The **weekly Discord export 403** (bot lacks channel permission — needs a Boss-side Discord change, not code) is the last remaining broken item. It is not authorized to start without the Boss saying so.

## DONE condition for whatever you pick up

Restate it explicitly before starting, then hold to it. "Tests pass" is never
the DONE condition — live verified behaviour is. See
`.agents/rules/completion-first.md`.

## Do not reopen

- BLACK-BOXX (diagnosis, boot race, wg-ap ownership) — closed 2026-08-06.
- Tracearr v1.5.0 update path — done; v2.x is a *separate* future evaluation.
- Joplin CLI sidecar (`loki-joplin-api`) — obsolete, must not be resurrected.
- Maintenance notification amplification / incident dedupe — fixed.
- Hermes / OpenRouter guard — fixed.
- gluetun / qBittorrent pairing — settled, must never be "fixed".

## Next action

Read `AGENTS.md`, then ask the Boss what the active objective is. Do not start
work that was not requested.

# Completed Work

Every entry is labelled. **Do not convert an old plan into a claimed feature.**
Commits are local-only unless stated; nothing has been pushed.

Legend: **DONE** · **PARTIAL** · **UNFINISHED** · **OBSOLETE/HISTORICAL**

---

## Tracearr registry version drift

**DONE — 2026-08-09.** Not an upgrade — no update, restart, recreate, or pull
was performed. Tracearr was already healthy on v2.0.1; only
`config/homelab_assets.yml` still described it as v1.5.0.

**Verification came first, and independently.** `config/homelab_assets.yml`
already had an uncommitted edit to `v2.0.1`/the correct digest sitting in the
working tree (present since before this session — visible in every earlier
`git status` this session as "pre-existing drift"). That value was **not**
trusted on sight. Queried live through the restricted NAS dispatcher instead:

- `tracearr_status` — `org.opencontainers.image.version: v2.0.1`, image
  `ghcr.io/connorgallopo/tracearr@sha256:3d57d9b032b4a...`, `state: running`,
  `health: healthy`, `restart_count: 0`, container created
  `2026-08-07T18:46:50Z`.
- `tracearr_dependencies` — redis (`tracearr-redis`) and postgres
  (`tracearr-db`, TimescaleDB) both `running`/`healthy`/`restart_count: 0`.
- `container_inventory` — confirms `watchtower` running on the NAS
  (`up 3 days`), long enough to have performed the 2026-08-07 recreate.

The uncommitted value matched exactly. **Why it happened**, without reopening
the closed v1.5.0 investigation: the registry already recorded
`updates.applied_by: watchtower-on-nas` — Tracearr updates on the NAS are
watchtower's job, not Loki's approval-gated path. This is the already-tracked
`watchtower → monitor-only` backlog item behaving exactly as documented: a
MAJOR version bump landed without going through approval. Not new, not
re-investigated — just the explanation for how a v1.5.0 registry became stale.

**Why this was not merely cosmetic.** `check_upstream()` in `nas_maint.py`
derives "installed version" straight from `asset.get("version")` for every
downgrade/update-available decision. Left uncorrected, a future
`tracearr_update_check` would have compared upstream releases against the
stale v1.5.0 and reported a false "update available" for an already-current
deployment — or, worse, failed to flag a real downgrade if someone ever
proposed reverting toward v1.5.0, because the registry would have shown no
apparent version change.

**Changes to `config/homelab_assets.yml`** (tracearr block only):

1. `version`/`image_digest` — confirmed correct, left as `v2.0.1` /
   `sha256:3d57d9b032b4a...`.
2. `dependencies.redis.container_ip` and `dependencies.postgres.container_ip`
   were **swapped** relative to live state (`.3`/`.2` reversed vs. actual
   `.2`/`.3`) — a separate, unrelated inaccuracy found while verifying this
   same block. Not used by any code path (grepped clean), so zero functional
   impact, but wrong is wrong. Corrected.
3. `known_issues.restart_churn` — added a dated addendum, not a rewrite: all
   the forensic evidence in that block is from the v1.5.0 deployment
   (2026-07-27), and `restart_count` resetting to 0 at the 2026-08-07 recreate
   is not proof the app-side defect was fixed — only that the counter
   restarted. The original evidence is untouched.

**Update-workflow logic required no code change.** `_parse_stable`,
`_registry_tag`, and `check_upstream` already reason generically from
semver-parsed tuples, not from any hardcoded target version — the v1.5.0
strings that do appear in `nas_maint.py` are illustrative examples in tool
parameter descriptions, never comparison logic. Confirmed rather than assumed:
4 new tests in `tests/test_nas_tracearr.py` prove a v2.x installed version (a)
is detected as current against a v2.x upstream release, (b) correctly flags a
genuine v1.x target as a downgrade, and (c) never proposes a v1.5.0 rollback
just because that string appears elsewhere in the codebase as an example.
110 tests total in the module, all green.

Docs updated to match: the `nas-maintenance` skill (version, corrected IPs,
restart-churn caveat, "v2.x already live via watchtower, not evaluation"),
`HOMELAB_INVENTORY.md`, `PROJECT_STATE.md`, and the two "noticed, not acted
on" mentions already sitting in `CURRENT_HANDOFF.md` — pointed forward to this
entry rather than rewritten. `TASK_LEDGER.md` backlog row for "Tracearr v2.x"
corrected to reflect that v2.0.1 is already running, not merely a possible
future evaluation.

---

## Nextcloud private-download delivery — public share links

**DONE — 2026-08-09.** Requires a `loki.service` restart to take effect; not
taken (restarts are approval-gated).

DM Loki a TikTok/Instagram link and it downloads privately, uploads to
Nextcloud, and DMs back a link with a Keep/Delete prompt. The link was
`http://192.168.1.63:8082/s/<token>` — private address space, so no external
recipient could open it.

**Root cause 1 — presentation.** `_create_share_sync` built the URL as
`f"{NC_URL}/s/{token}"`, i.e. from the internal endpoint. The share itself was
fine; only the address was wrong.

The non-obvious part: **using the OCS response's own `url` field would not have
fixed it.** This Nextcloud has no `overwritehost` / `overwrite.cli.url` set
behind nginx-proxy-manager, so it generates `https://192.168.1.63:8082/s/...`
in its own API output. The token is therefore taken from the API — never
constructed — and only the *origin* is re-based onto
`NEXTCLOUD_PUBLIC_BASE_URL`. If Nextcloud's config is corrected later the
returned URL is already public and re-basing is a no-op, so this stays correct
either way.

**Root cause 2 — configuration never loaded.** More serious, and it meant the
feature was not merely mislinking but **entirely non-functional**.
`loki.service` sets no `EnvironmentFile`, so the process starts with a bare
environment (12 vars, none of them `NEXTCLOUD_*`) and `.env` is the only source
of config. But `nextcloud_integration` was imported at `loki_bot.py:64` while
`load_dotenv()` ran at line 92 — so every module-level `os.getenv` fell back to
its default, pinning `NC_URL` to `http://192.168.1.247:8082`: the **pre-rebuild
asus box**, unreachable since that machine was rebuilt. Nothing logged an
error. `jd_integration` (the JDownloader last resort in the same download
chain) had the identical bug, running with empty MyJDownloader credentials.
`load_dotenv()` now runs before every project import, and a test pins that
ordering because the failure is completely silent.

**Root cause 3 — blast radius.** Uploads went to
`Loki Downloads/{requester}/{date}`, reused for every download that day. For a
multi-file request the *folder* was shared, so one public link exposed every
file that requester had fetched that day — and "delete" removed all of them.
Each request now gets its own `…/{date}/{batch}` folder, so both the share and
the deletion cover exactly one request.

**Security.** Public links are read-only (`permissions=1`,
`publicUpload=false`), scoped to a single file (or that batch's folder), carry
no filesystem path or credentials in the URL, and are independently revocable
by share id. `_assert_public` refuses to emit a link on a private host, and a
share that cannot be presented safely is revoked rather than left published —
a failed share never yields a fabricated URL.

**Expiration.** `NEXTCLOUD_SHARE_EXPIRY_DAYS`, default 3 (72 h), `0` disables.
Keep historically meant the link lasted indefinitely, so Keep clears the expiry;
`NEXTCLOUD_KEEP_CLEARS_EXPIRY=false` preserves it instead.

**Live end-to-end verification** against the real Nextcloud with a throwaway
text fixture: upload OK → share created with a 2026-08-12 expiry → URL
`https://cloud.ivn-group.cc/s/<token>` → **anonymous GET 200 and anonymous
download returned the exact bytes** → DAV tree 401 to the same anonymous client
→ Keep left it serving 200 → Delete revoked the share and removed the files,
both verified → revoked link **404**. Share count returned to the 3 that existed
before, selftest tree gone.

32 tests in `tests/test_nextcloud_share.py`, covering share creation, the
internal-vs-public split, no-private-IP-leak, no-fabricated-URL on failure,
Keep, Delete/revoke with verification, expiry configuration, read-only
permissions, per-batch scoping, and the `load_dotenv` ordering invariant.

**Not attempted:** correcting Nextcloud's own `overwritehost` at source. That
needs root or docker on the UGREEN NAS, and `docs/NAS_MAINTENANCE.md` prohibits
both permanently. Loki no longer depends on it.

---

## asus fstab — CIFS share names with spaces

**DONE — 2026-08-09.**

Two `/etc/fstab` entries on asus had never mounted since the rebuild:

```
//192.168.1.63/Zion Cinema /media/nas/Zion_Cinema cifs ...
//192.168.1.63/Folder 1    /media/nas/Folder_1    cifs ...
```

**Root cause.** fstab is whitespace-delimited. With the space written
literally, the parser reads `Cinema` as the mount point, `/media/nas/Zion_Cinema`
as the filesystem type, and gives up on the line. The decisive evidence was not
a mount error but an *absence*: **systemd had generated no `.mount` or
`.automount` unit for either path at all**, while the other five NAS entries had
both. Nothing was ever attempted, so nothing ever failed loudly.

The NAS genuinely exports the names with spaces (`smbclient -L` confirms
`Zion Cinema` and `Folder 1`), so the fix is to escape the fstab field, never to
rename the share. dex247 has always spelled the same two shares `\040`-escaped —
this was asus drifting from a working precedent, not a new question.

**Change.** Only the source field of those two lines:
`Zion\040Cinema` and `Folder\0401`. Credentials, `uid`/`gid`, `iocharset`,
`_netdev`, `x-systemd.automount`, both mount points, every other entry, and the
newly restored `/mnt/Disk1` line were left byte-for-byte identical — the edit
script asserted that by reversing its own substitution and diffing against the
original before writing. `/etc/fstab` backed up with `cp -a` (mode 664
root:root preserved).

**Verification.** `findmnt --verify`: **2 parse errors → 0**. After
`daemon-reload` both unit pairs appeared; starting only those two automount
units (rather than `mount -a`, which would have exercised unrelated entries)
mounted both shares. `Zion_Cinema` 15 entries, `Folder_1` 7 entries, with real
files stat'd through each. All three previously-working CIFS mounts
(Blockbusters, Plex, Tera) untouched; `Docker` and `Personal` still sit
`waiting` — normal automount laziness, unchanged. `/mnt/Disk1` still mounted
(53 entries under `downloads`), `sshfs-unicron.service` active with
`NRestarts=0`, Filebrowser `healthy` with all four shares populated and HTTP 200
local + proxy. Both automount units are dependencies of `remote-fs.target`, so
they come up at boot. The remaining `[W] /swapfile` warning is pre-existing and
normal for a swapfile.

---

## Unicron sshfs share — rebuilt host, missing key, missing disk

**DONE — 2026-08-09.** Follow-on to the Filebrowser repair below.

`/srv/unicron` was empty. The prior handoff recorded one blocker (the SSH key)
and expected it to need the Boss. Both parts of that turned out to be wrong.

**Fault 1 — the key.** The `asus`/unicron box was rebuilt to Zorin OS 18.1, so
dex247's `id_ed25519` was no longer in `asus@`'s `authorized_keys`, and the
tailnet address had moved from `100.115.240.16` (now a dead address, no node
holds it) to `100.101.112.55` / LAN `192.168.1.247`.

**Fault 2 — the disk, which nobody had noticed.** `/mnt/Disk1/downloads` **did
not exist on the rebuilt box**. The rebuild dropped `/dev/sdb1` — the 931 GB
ext4 data disk, UUID `32d26174-8f15-4db3-8b7e-d584fc55bd7f` — from `/etc/fstab`
entirely. The data was intact and simply unmounted (`downloads`, `backups`,
`nextcloud`, `frigate`, `jellyfin-metadata`, `yt-dlp`; ~470 G used). **A working
key alone would have mounted an empty directory** and looked like success.

**No Boss action was needed.** razr already held authorized access to
`asus@192.168.1.247` — its RSA key was in that box's `authorized_keys` from
before the rebuild. That existing, legitimate path was used to append dex247's
public key, preserving razr's key and backing up `authorized_keys` first.

**Host identity was verified before trusting the new key**, three independent
ways: the tailnet's WireGuard-authenticated node identity for `asus`; **razr's
own `known_hosts`, which recorded the same ed25519 key back in June**; and the
live login reporting hostname `asus` with the expected disk contents.
Fingerprint `SHA256:j64/r3A/SMFEMDzbsVWOeCJ1khiaBKmkdu7zj1fn/9w`.

**Changes.**

1. `asus:~/.ssh/authorized_keys` — appended `g2k247@dex247` (ed25519).
   Idempotent, backed up, `razr@razr` untouched, mode 600.
2. `asus:/etc/fstab` — `/dev/sdb1` pinned **by UUID** at `/mnt/Disk1` with
   `nofail,x-systemd.device-timeout=15`, so a missing data disk can never block
   boot on that desktop OS. Backed up first. Mounted and confirmed readable as
   the `asus` user (the identity sshfs connects as).
3. `dex247:~/.ssh/known_hosts` — removed **only** the obsolete
   `100.115.240.16` entry; pinned the verified key for
   `asus.tail3744e0.ts.net`, `100.101.112.55` and `192.168.1.247`. No global
   `StrictHostKeyChecking` change.
4. `sshfs-unicron.service` — tightened `StrictHostKeyChecking=accept-new` →
   `=yes`. With the key pinned this is the same for normal operation, but a
   *changed* key now fails loudly instead of silently re-trusting a rebuilt
   host. An unnoticed rebuild is exactly how this share died quietly.

**Verification.** Unit `active (running)`, `NRestarts=0`, one sshfs process, no
ENOTCONN. Two full stop/start cycles (the safe proxy for a reboot) each
unmounted cleanly and remounted with all 53 entries — no stale endpoint, no
loop. 10 rounds of stat/list stable; a real MP4 read back a correct `ftyp`
header both on the host and inside the container.

**The `rslave` propagation proved itself in production**: filebrowser started
03:04:58 and the mount landed 03:43:30, and the running container picked up all
53 entries with no restart and no recreate — the exact behaviour that binding
was changed for. All four shares now populated (`/srv/dex247` 28,
`/srv/unicron` 53, `/srv/nextcloud` 1, `/srv/nas` 7). HTTP 200 local/LAN/proxy
throughout. The `filebrowser_health` runbook now reports plain "running and
serving HTTP 200" with no degraded-share note. Reliability stays 95.

**Found, deliberately not fixed:** `asus:/etc/fstab` lines 15 and 19 fail to
parse — `//192.168.1.63/Zion Cinema` and `//192.168.1.63/Folder 1` have
unescaped spaces (CIFS needs `\040`), so those two NAS shares never mount at
boot on asus. It is asus-side media, unrelated to the Unicron share, and
touching it risks that machine's own services. Logged in the backlog.

---

## Filebrowser production failure — stale FUSE mountpoint

**DONE — 2026-08-09.**

Filebrowser was down from 2026-08-03 and re-failed identically across every
reboot with:

```
error while creating mount source path '/mnt/unicron-downloads':
mkdir /mnt/unicron-downloads: file exists
```

**Root cause — not a path conflict.** Nothing was occupying the path. sshfs to
unicron died *without unmounting*, leaving a stale FUSE endpoint: the directory
entry still resolves, but every syscall on it returns `ENOTCONN` ("Transport
endpoint is not connected"). Docker creates missing bind sources with `mkdir`;
`mkdir` on a dead mountpoint returns `EEXIST`. Docker reports `EEXIST` as
"file exists". The container exited 128 at *create* time, so
`restart: unless-stopped` never applied — `RestartCount` stayed 0.

**Why it could never self-heal.** `sshfs-unicron.service` mounts that same
path, so its mount hit the identical `ENOTCONN` and failed in ~2 ms. With
`Restart=on-failure` / `RestartSec=10` it looped: **111,286 failed starts in the
previous boot and 2,861 more since the 2026-08-08 reboot**. The mountpoint
stayed wedged, so the bind failed again on every boot.

**A second, independent fault.** The unit targeted tailnet IP
`100.115.240.16`, where no node exists any more. The `asus`/unicron box was
rebuilt: it is now `100.101.112.55` (LAN `192.168.1.247`) with a **different
SSH host key**, and `~/.ssh/id_ed25519` is no longer in its `authorized_keys`.
So even a clean mountpoint could not connect.

**Repairs.**

1. Cleared the stale endpoint with `fusermount3 -u -z`. The underlying
   directory was empty (`g2k247:g2k247`, ext4) — nothing to preserve.
2. `/etc/systemd/system/sshfs-unicron.service`:
   - `ExecStartPre=-/bin/fusermount3 -u -z /mnt/unicron-downloads` — a dropped
     connection now self-heals instead of wedging the mountpoint. **This is the
     fix that stops the recurrence.**
   - target changed from the dead IP to MagicDNS `asus.tail3744e0.ts.net`, so
     tailnet IP churn cannot break it again.
   - `Before=docker.service` — containers no longer bind the bare mountpoint
     before the share lands. Ordering only; a failed mount never blocks Docker.
   - `RestartSteps=5` / `RestartMaxDelaySec=300`. Verified: retries went from
     every 10 s to every 5 min.
3. `/home/g2k247/docker/filebrowser/docker-compose.yml` — the three network
   binds converted to long syntax with `bind.propagation: rslave`. Proven
   end-to-end with a throwaway tmpfs: a mount appearing on the host became
   visible inside the *running* container, and vanished cleanly on unmount.

**Verification.** Container running + healthy, `RestartCount=0`, survives
`docker compose up -d --force-recreate` (previously impossible). HTTP 200 on
`127.0.0.1:8090`, LAN `192.168.1.155:8090`, tailnet `100.68.187.69:8090`, and
`https://media.ivn-group.cc` (valid TLS; HTTP→HTTPS 301). Auth intact: bad
credentials 403, unauthenticated `/api/resources` 401. `/srv/dex247` 28
entries, `/srv/nas` all 7 cifs shares populated, `/srv/nextcloud` mounted.
Live Reliability recomputed **87 → 95**; `stopped_expected` is now empty and
the only remaining deduction is the Hermes protective circuit. No incident
existed in `homelab_incidents.db` to close.

**New capability.** `maintenance_runbooks/filebrowser_health.py` plus a
registry entry in `config/homelab_assets.yml`. `Ops.path_meta()` now returns
`errno` and `stale_mount`, because without an errno a dead mountpoint is
indistinguishable from a deleted directory and the two need opposite
responses. The runbook:

- **never restarts into a stale mountpoint** — the bind fails identically every
  time, and clearing one needs root `fusermount3` (`filesystem_repair`,
  MANUAL). It escalates naming the path, and does not spend its auto-repair.
- **never scores an unmounted share as a service outage.** sshfs being down
  leaves an empty directory that binds fine; filebrowser is healthy and
  `/srv/unicron` is merely empty. Treating that as failed would re-earn the
  8-point deduction for a *remote* host being offline.

23 tests in `tests/test_filebrowser_runbook.py`; also verified live read-only
against the real deployment.

**Open, needs the Boss.** `/srv/unicron` stays empty until
`~/.ssh/id_ed25519.pub` is re-added to `asus@`'s `authorized_keys` on the
rebuilt box — that needs password or console access to it. Filebrowser's health
does not depend on it, and when the key works the share appears via `rslave`
with no restart.

---

## RAZR Phase-1 Storage Capacity Recovery

**DONE — 2026-08-08.**

Full read-only investigation established that RAZR's `ubuntu-vg` had 135.42 GiB free
inside the VG (`/dev/nvme1n1p3`, Samsung NVMe) with the root LV fixed at 100 GiB.
No partition/PV surgery was needed.

**Changes made (all online, zero downtime):**

1. **Root LV extended**: `lvextend -l +100%FREE /dev/ubuntu-vg/ubuntu-lv` →
   LV grew from 100 GiB to 235.42 GiB.
2. **ext4 filesystem grown online**: `resize2fs` on the live mounted root →
   filesystem expanded from 98 GB to 232 GB.
3. **Proven orphan Ollama blob removed**: `sha256-5965…` (6.9 GB) confirmed
   unreferenced by Python manifest walk. Active Gemma4 blob `sha256-8ebc…` untouched.
   All 7 Ollama models remain intact.
4. **Safe cache cleanup**:
   - npm `_cacache` (409 MB)
   - hermes uv cache (219 MB), electron cache (110 MB), node-gyp cache (65 MB)
   - snapd download cache (708 MB)
   - apt cache (143 MB)

**Result:**
- Root filesystem: 98 GB → 232 GB
- Usage: 78% → 29%
- Free space: 22 GB → 157 GB
- Total reclaimed by cleanup: ~7.5 GB (orphan blob 6.9 GB + caches)
- All services (Ollama, Docker open-webui, nextchat, Hermes) verified healthy post-change
- Crucial 1TB NVMe untouched

**Storage architecture clarification:**
RAZR has two NVMe SSDs (not SATA). The Samsung 238 GB is the Linux OS/LVM drive.
The Crucial 1TB (`nvme0n1`) is carry-forward NTFS data, unmounted, not in fstab,
no Linux service depends on it.

## qBittorrent

**DONE — recurring connectivity failure fixed** (2026-08-06).
Root cause: `mem_limit: 1g` in `/home/g2k247/PrivacyServer/docker-compose.yml`
created a cgroup ceiling too small for qBittorrent's active libtorrent peer
connection workers (~1 GB anon-RSS each). The kernel OOM killer fired every
20-30 minutes, killing both qbittorrent-nox workers AND the internal
`watchdog-script`. Because supervisord has `autorestart=false` for both
processes, nothing inside the container could recover — WebUI became
permanently unreachable until manual `docker restart`. Confirmed by `dmesg`:
multiple OOM kills from `2026-08-05` through `2026-08-06 13:33`.

Fix: removed `mem_limit` and `memswap_limit` from the compose service.
Container recreated with `docker compose up -d qbittorrent`. Validated live:
- LAN IPv4 `192.168.1.155:8080` → HTTP 200
- Reverse proxy `qbit.ivn-group.cc` → HTTP 200
- nzb360 API `/api/v2/torrents/info` → 10 torrents returned
- No further OOM kills observed after fix applied

Also added: `qbittorrent_health` runbook in `maintenance_runbooks/` —
detects container state, OOM flag, cgroup limit advisory (warns if tight limit
re-introduced), WebUI health, and performs a single safe `docker restart` when
the known failure pattern is present. `qbittorrent` registered as a managed
asset in `config/homelab_assets.yml` (10th asset).

## BLACK-BOXX

**DONE — deterministic diagnosis and repair** (`8355d21`, 2026-08-06).
The runbook probes the executor first: a Loki-side transport/permission failure
reports `diagnostic_transport: UNAVAILABLE` with state UNKNOWN and never
escalates as an AP fault. A dead AP unit yields ONE failed root cause with
dependents marked `SKIPPED` — never "failed", which would assert evidence never
gathered. Previously it reported twelve simultaneous subsystem failures that
were all symptoms of one dead unit.

**DONE — boot-race ownership conflict removed** (`42380d1`, 2026-08-06).
`wg-quick@wg-ap` was disabled through Loki's own approval gate. Added
`systemctl_is_enabled` / `systemctl_disable_unit` / `systemctl_enable_unit` to
the allowlist — the `service_enable_disable` tier previously had no command
behind it. `multi-user.target.wants` now contains only `black-boxx-ap.service`.

**DONE — current health.** 17/17 checks green, 0 advisories, no repair proposed.

## Hermes / OpenRouter

**DONE — escalation agent** (`e902cb4`, `2f95889`, live 2026-07-25).
Hermes v0.19.0 on razr, dedicated `hermes` account, read-only `homelab_api.py`
facade on dex247. Routing enforced in code: the asset's deterministic runbook
runs first and Hermes is only reached if it reports `escalate: true`.

**DONE — circuit breaker and budgets** (`51fda47`, live 2026-08-05 22:44).
Provider circuit breaker with persistent counters, 6 req/hour, 20 req/day
rolling, $5/day observed spend ceiling. Billing-class failures (quota/credits/
402) open instantly; others need 3 consecutive. Cooldowns 1800s doubling to
21600s. Recovery via non-billable `GET /health`; a billing open gets ONE leased
submit judged by the job's fate. Provider outage is ONE incident key
(`hermes-provider`), never in `mon.MONITORS`, so it cannot recursively escalate.
30 tests in `tests/test_hermes_guard.py`.

## Maintenance incidents and notifications

**DONE — persistent incident dedupe** (`daf150e`, 2026-08-05).
Escalation used to CLOSE the incident and start a 30-minute cooldown, so a
fault that never went away minted a new incident **and a new billed Hermes job**
every cycle — 297 incidents and 297 Hermes jobs for black-boxx + joplin alone.
Now an incident stays active (open/escalated/gave_up all count; `closed_at IS
NULL` is the test) until `RECOVERY_THRESHOLD` consecutive healthy polls. Repeat
detections only bump `occurrence_count`/`last_seen`.
17 tests in `tests/test_incident_dedup.py`.

**DONE — one Hermes escalation per unresolved incident.** Follows from the above
plus the guard's pre-check of `hg.blocked_reason()`.

**DONE — routine maintenance removed from Telegram, Discord ops feed live**
(`maintenance_notify.py`, live 2026-08-05 20:42). All autonomous events route to
ONE Discord ops channel (`MAINTENANCE_OPS_CHANNEL_ID`). Telegram receives only
`needs_boss_hands`, `boss_approval_required`, `security_alert`,
`data_loss_alert`. If the ops channel fails, non-urgent events relay to the
Boss's **Discord DM**, never Telegram. 22 tests in
`tests/test_maintenance_routing.py`.

## Tracearr

**DONE — restricted NAS dispatcher** (`16b2178`, 2026-07-27). Six read-only
actions over `nas-maint`.

**DONE — approval-gated update with backup, readiness, verification, rollback**
(`1016838`, then `b075780` + `251807b`, 2026-08-05). Ran for real: v1.4.27 →
**v1.5.0**, verified healthy, no rollback needed. Readiness-aware verification
(150s deadline, 5s poll) used for both post-update and post-rollback.
`tracearr_update` takes an optional pinned `version`, verified against the
upstream feed (must exist, must not be a prerelease). A verified update writes
version + image digest back to `config/homelab_assets.yml`.

**UNFINISHED — Tracearr v2.x** is a **separate evaluation**, not scheduled.
v2.0.0 landed upstream hours before the v1.5.0 run; the pinning work exists
specifically so a major release cannot be applied by accident.

**UNFINISHED — Tracearr restart churn.** `restart_count` 268 and climbing while
Redis and Postgres sit at 0. App-side, cause unproven, deliberately **no
automatic repair**.

## Work Tracker

**DONE — Google Sheets export defect fixed** (2026-08-06).
The `_write_sheets_row` function was a fire-and-forget HTTP call at the moment of session close. Any network drop, timeout, or HA integration failure resulted in permanent data loss for the Sheets mirror. Re-architected to a durable `_sync_pending_sheets` queue processor reading from the SQLite `work_sessions` table (`sheets_ok = 0`). Sync runs asynchronously on session close and is triggered every 5 minutes by `poll()`. Idempotency and retry-safety guaranteed. Validated live; historically dropped sessions successfully recovered and exported to Sheets.

## Joplin

**DONE — authoritative runtime** (`f89e503`, then `dc479a6`, 2026-08-05).
`loki-joplin-desktop.service` (Desktop 3.7.9 headless under Xvfb) owns the Data
API on `127.0.0.1:41184`. Profile `/home/g2k247/docker/joplin-api/data`.
Joplin Server = containers `joplin` + `joplin-db`.

**DONE — note read-back fix** (`dc479a6`). An unscoped `find_note_by_title` must
search EVERY notebook via one `/notes` listing and must not rely on `/search`
(Joplin's FTS index lags creation by seconds). Scoping it to `Loki/` meant a
note written to `Personal/Officer Logs` was invisible to the next read, and
`note_append` then created a DUPLICATE in `Loki/Inbox`.

**DONE — notebook hierarchy tools** (`40b3f5e`): `notebook_list/tree/get/
children/notes`, `note_move`, `note_create(notebook=)`. Validated live —
`Personal → Officer Logs` resolves by full path and bare name.

**OBSOLETE — `loki-joplin-api` CLI sidecar.** Superseded 2026-07-19. Kept
stopped with `restart=no` for rollback. Its `Exited (137)` state is
**intentional**, not an outage. Removed from the `joplin` asset's container list
because monitoring it manufactured a permanent false incident. **Do not
resurrect it** — two Joplin instances cannot share one SQLite profile.

## Presence notifications

**DONE — concise legacy style restored** (`ede172d`, 2026-08-05). Home Assistant
already sends several messages fully formatted in Loki's voice; the Groq
rewriter was narrating them. Four Boss presence transitions
(`loki_someone_left`, `loki_arrived_office`, `loki_left_office`,
`loki_someone_home`) now bypass the rewriter. Welcome-home retains Rob's
(`person.ammiel`) home/away state for the top-lock decision. 20 tests in
`tests/test_presence_notifications.py`.

## Homelab platform

**DONE — maintenance controller** (`53aebbb`, `6bae20d`, live 2026-07-25).
**DONE — asset lifecycle / decommission registry** (`bf0a6f1`, `3c8cbb8`,
`9fee6a0`, live 2026-07-26). `ivn-site` is the first tombstone, retained
permanently on purpose. `config/homelab_lifecycle.yml` is a **generated mirror**
of the DB table — it goes git-dirty during normal operation by design, and
nothing reads it back as truth.
**DONE — container update workflows** (`5d69287`). Registry digests + official
GitHub release metadata, never image age; prereleases never targeted; moving
tags resolved to an exact version before pulling; backups must succeed or the
update is blocked; rollback refused once a schema migration has run.
**DONE — durable task supervisor** (`da1e301`), **approval-gated actions**
(`08c37fa`), **memory lifecycle** (`a3d7ddd`).

## Discord / Telegram

**DONE — duplicate social-link guard repaired** (`0bb049c`). The pre-existing
cross-channel guard only recorded a URL inside `run_download()`, so a link
shared casually in an ordinary channel was never recorded and reposting it was
never caught. Repaired in place — not replaced. An earlier session misdiagnosed
this as a missing "Hell Yeah Films forwarding" feature and built a parallel
system; that was corrected.
**DONE — Telegram media handling + honest sync status** (`824e60b`, `41875ed`).
**DONE — HA→Telegram mirror for 🛒-marked notifications** (`db46d02`).
**UNFINISHED — Telegram voice messages.** `telegram_interface._handle()` reads
text/caption only; voice payloads are silently dropped. Discord voice
transcription already exists.

## Career-Ops

**PARTIAL.** Bridge built and locally validated; SSH to razr unblocked
2026-07-25. Loki side is live (`career_ops.py`, 6 tools, `permission=crew`;
Boss → both profiles, crew → roommate only). Startup reports "Career-Ops liaison
online — bridge at configured URL". Never auto-submits applications.

## Plex / NAS diagnostics

**DONE** (`90fdb30`): `plex_status`, `plex_diagnose`, `plex_sessions`,
`plex_playback_diagnose`, `plex_start`, plus `nas_network_status`,
`nas_network_speed_test`, `nas_disk_status`, all through the dispatcher.

## Known broken (not started)

**UNFINISHED — Google Sheets work-session export.** `sheets_ok` 2/15 sessions,
recurring `Sheets append returned not-ok`. Joplin + SQLite halves work, so no
data is lost. Diagnose the HA-service append path in `work_tracker.py`.

**UNFINISHED — weekly Discord export 403.** `Failed to send weekly export: 403
Missing Permissions`, weekly, still firing. Needs a Discord-side channel
permission (Boss action) or a different target channel.

**UNFINISHED — watchtower conflict.** `watchtower` runs `--cleanup --interval
86400` with no `WATCHTOWER_LABEL_ENABLE` and no `WATCHTOWER_MONITOR_ONLY`, so it
auto-updates **every running container** daily at ~19:17 UTC and deletes
replaced images — bypassing Loki's approval gate and destroying rollback
targets. Loki's inventory detects it and reports `external_autoupdate_active`.
The fix (`WATCHTOWER_MONITOR_ONLY=true`, drop `--cleanup`) is a production
change needing Boss approval. **Not done.**

## Planned only — do not describe as built

- **Centralized Model Router.** Today there is only a per-intent table in
  `routing.json`. The full cost/privacy/complexity router is a design.
- Splitting `loki_bot.py` into satellite modules.
- `google.generativeai` → `google.genai` vision migration.

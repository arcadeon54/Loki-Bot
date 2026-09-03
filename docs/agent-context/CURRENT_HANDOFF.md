# CURRENT HANDOFF

*Updated 2026-09-02. Keep this under a minute to read.*

## Just completed

**Home Assistant camera outage — root-caused and repaired end to end, 2026-09-02
(evening EDT; UTC timestamps fall on 2026-09-03).** All three Frigate camera
cards were `unavailable`. Two independent faults, fixed in two approved phases.

**Fault 1 — MQTT was dead because Mosquitto came up with an empty config.**
`/volume1/docker/mosquitto/mosquitto.conf` was a 1-byte file (a single
newline). `eclipse-mosquitto:2.0` with an empty config binds its default
listener to **loopback inside the container** and refuses anonymous clients, so
nothing outside the container namespace could connect. Both consumers were
locked out: HA logged `[Errno 111] Connection refused`, and Frigate logged
`MQTT disconnected` every 2 minutes for hours. The Frigate integration derives
entity availability from MQTT, so ~60 entities went `unavailable` at once.

A valid config was reconstructed (no prior copy was recoverable — no backup, no
old copy, empty `unicron_final_backup/`, nothing in docs). The container was
recreated with `docker compose ... up -d mosquitto --no-deps`. **HA and Frigate
both reconnected; 48/48 Frigate entities recovered.**

**The published port was hardened from dual-stack to IPv4-only** —
`"1883:1883"` → `"0.0.0.0:1883:1883"` in `docker-compose-infra.yml`. This
matters: the NAS holds globally-routable IPv6 addresses
(two of them, ISP-delegated — values withheld from this repo; confirm with
`ip -6 addr show scope global` on the NAS) and docker-proxy had been
publishing 1883 on `[::]` as well as `0.0.0.0`. Once the broker actually
started accepting connections, that would have put an **anonymous** broker on a
public IPv6 address. Edge IPv6 filtering could not be verified from inside the
LAN, so it was not assumed. **No `[::]:1883` host listener remains.**

⚠️ **MQTT is currently ANONYMOUS as a TEMPORARY compatibility configuration.**
Neither consumer holds credentials, and adding them requires a Frigate restart,
which was out of scope. See "Next active task".

**Fault 2 — Frigate HA integration v5.15.4 is incompatible with HA 2026.9.0.**
The integration passed the deprecated `via_device` to
`device_registry.async_get_or_create`; HA 2026.9 now *raises* instead of
warning. Two camera entities failed to be added at all
(`Error adding entity None for domain camera with platform frigate` →
`RuntimeError`), leaving `camera.front_door` and `camera.livingroom` as
`restored: true` registry stubs on every boot.

**HACS v5.15.5 explicitly fixes this** — release note: *"Fix HA 2026.9 device
registry via_device compatibility" (#1116)*. It replaces 21 hardcoded
`"via_device":` call sites with a version-branching helper that emits
`via_device_id` on modern HA. Installed through the supported HACS update path
(`update.install` on `update.frigate_update`), HA restart approved and
completed. **Zero `via_device` / `via_device_id` errors after restart.**

**Final state — all three cameras genuinely loaded entities, not registry stubs
(`restored=None`):**

| Entity | State |
|---|---|
| `camera.front_door` | `recording` |
| `camera.livingroom` | `recording` |
| `camera.entryway` | `recording` |

**⚠️ HACS packaging quirk — do not chase it.** Upstream's v5.15.5 tag still
ships `manifest.json` declaring `5.15.4` (verified against upstream, not a
local install fault). After the restart HACS re-read the manifest and reverted
its tracking to `version_installed: v5.15.4`, so **HACS will keep offering
v5.15.5 as an available update**. The fixed v5.15.5 code *is* installed and
running — verified on disk (helper defined, 20 call sites, only the unreachable
legacy fallback remains) and proven by zero RuntimeErrors. **Do not repeatedly
reinstall it**; treat the notification as cosmetic until upstream bumps the
manifest. "Skip this version" in HACS is available if the badge is annoying.

**Entryway — recovered, but not by any of this, and not understood.** Its
Frigate stream had been failing for ~2.5 h (ffmpeg/go2rtc, 4380 error lines,
watchdog restarting every ~20 s) and **self-recovered to ~5 fps at 22:22:02**,
about 20 seconds before the HA restart. **Root cause was never established.**
Immediately afterwards the *direct Tapo* entity
`camera.entryway_door_hd_stream` went `unavailable`, with HA logging
`Operation not permitted` opening `rtsp://…@192.168.1.5:554/stream1` (these
`stream_worker` errors did not exist before). **Hypothesis, not confirmed root
cause:** a concurrent RTSP/session limit on that C201 now that Frigate/go2rtc
holds a session. Livingroom and Front Door Tapo entities are unaffected.
**Frigate's Entryway camera works — do not disturb it merely to restore the
duplicate Tapo entity.**

**Rollback backup retained** at
`/volume1/docker/.loki-backups/frigate-hass-integration-2026-09-02/`
(`frigate-v5.15.4.tar.gz`, `hacs-frigate-record.json`, `hacs.data.snapshot`;
dir mode 0500). Keep for now.

*Loki could not restart the Mosquitto container itself* — no docker group, no
NOPASSWD sudo for it, and the NAS dispatcher has no mosquitto verb. The Boss
ran both the compose recreate and the HA restart. That boundary held and should
stay.

*Unrelated pre-existing noise seen in the logs, deliberately not touched:*
`extended_openai_conversation` setup failure (`openai~=2.21.0` unresolvable),
Immich `isFavorite` validation errors, `calendar.get_events` missing (the
`google` entry is in `setup_error`).

---

## Previously completed — 2026-08-23

**Public access restored and validated; NPM database recovered — 2026-08-23
(Phases 5B-4 + 5C).** Full detail in `COMPLETED_WORK.md`; the short version:

**NPM was running on an orphaned SQLite inode.** An earlier `docker cp`-class
overwrite replaced `/data/database.sqlite` underneath the running container, so
NPM kept writing to the deleted inode. Two consequences: UI/API writes would
have vanished at the next restart, and the Phase 4C/4D hardening **had never
actually taken effect** — `firefox`, `jd`, and `hydra` were still publicly
reachable and MeTube still had no auth. A controlled restart fixed it (both DBs
compared and integrity-checked first; nothing lost). Configs regenerated,
`nginx -t` clean, and write persistence was then *proven* by a real UI save.
**Never replace a live SQLite DB with `docker cp` — use the API/UI, or stop the
service first.**

**NPM admin password rotated** via the supported API. New credential validated,
old one rejected (NPM answers `400 error.invalid-auth`, not `401` — confirmed
against controls). Plaintext memory copy replaced with a pointer, temp file
shredded after the Boss saved it to his password manager. **NPM admin stays
private on `:81`.**

**Three public services, all with app-level auth as the boundary and no NPM
Basic Auth:**

| Service | Backend | State |
|---|---|---|
| `jfin.ivn-group.cc` (Jellyfin) | `192.168.1.155:8096` | ✅ WebSockets on; **cellular validated** (login/browse/playback/seeking, Tailscale off) |
| `cloud.ivn-group.cc` (Nextcloud) | `192.168.1.63:8082` | ✅ stale `192.168.1.247` fixed via UI; **cellular validated** + public share link with no login and no Tailscale |
| `immich.ivn-group.cc` (Immich) | `192.168.1.155:2283` | ⚠️ server-side passed; **cellular/client validation PENDING** |

All three backend ports are DROPped from WAN at `DOCKER-USER`; only 80/443 are
public. **Jellyfin is now the third deliberate permanent public exception**
alongside Home Assistant and Seerr — do not take any of them private.

*Immich improvement item (not a regression):* it inherits NPM's global
`client_max_body_size 2000m` / 90s timeouts, so very large or slow uploads may
fail. Nextcloud overrides these; Immich doesn't.

**Also today (Boss-side, no repo change):** NextDNS now works *through*
Tailscale on the S23 Ultra (NextDNS profile as Tailscale global nameserver +
"Override DNS servers"; `test.nextdns.io` → `status=ok`, DoH, `clientName=tailscale`),
so Tailscale no longer has to be toggled off for ad blocking. And an
**unresolved incident artifact** was logged: ~5 unsolicited NVIDIA Shield ADB
authorization prompts on the morning the compromise was found, "Always allow"
taken each time, with the previously-known ADB workstation already dead. Not
proof of lateral movement — **attribute the keys before revoking anything.**

---

## Previously completed — 2026-08-10

**Generic ("a person") presence wording fix — DONE 2026-08-10, code-only
(needs a `loki.service` restart, not taken).** Follow-on to the roommate
wording fix below: that fix only caught HA messages that *named* Rob. Some
HA presence automations don't name either resident at all — "Boss, a person
has been detected at home." — so they had nothing to bypass on and fell
through to the Groq rewriter, which had its own real bug: the presence
context line it builds only ever queried `person.kavaris`, never Rob, so it
couldn't have said who even if asked to. Fixed: `personality.GENERIC_PRESENCE`
matches generic phrasing ("a person"/"someone"/"a household member" + a
presence-shaped word, so an unrelated "someone's at the door" camera event
isn't swept in) and `ha_integration._resolve_generic_presence()` resolves
*who* by diffing each resident's live state against a new
`presence_monitor.last_known()` accessor — `presence_monitor`'s own poll/
detection logic is untouched, this only reads its already-tracked state.
Also fixed the presence-context bug itself (now includes both residents) as
defense-in-depth for the rare message neither classifier recognizes. 12 new
tests, `tests/test_presence_notifications.py` now 48, including a direct
regression pin on the exact reported message. Files: `personality.py`,
`ha_integration.py`, `presence_monitor.py` (new `last_known()` accessor
only — no behavior change), `tests/test_presence_notifications.py`.

**Roommate presence wording cleanup — DONE 2026-08-10, code-only (needs a
`loki.service` restart to go live, not taken).** Rob's arrival/departure
wasn't pre-voiced by Home Assistant the way the Boss's four transitions are
(`ede172d`, 2026-08-05) — a plain factual HA message still went through the
Groq rewriter and came back as "Boss, your roommate has left the premises
while you are still at home," restating the Boss's own already-known
presence. Fixed the same way the four transitions were: `personality.py`
gained `ROOMMATE_PRESENCE` (matched on the roommate's name via regex, not an
exact fragment, since HA's wording here isn't fixed) and
`roommate_presence_text()`, which answers from **Rob's live state**, not
from parsing HA's phrasing. Exact output: `"Rob stepped out."` /
`"Rob is home."` — nothing about the Boss's own presence. The welcome-home
top-lock line (`roommate_line()`, e.g. "Rob's home — top lock's good.") was
already correct and untouched. No trigger conditions, HA automations, or
notification volume changed. 12 new tests, `tests/test_presence_notifications.py`
now 32, all green; full suite baseline unaffected (same 4 fail/4 error
`discover`-mode pollution documented below, none in the touched files —
confirmed each passes 100% in isolation). Files: `personality.py`,
`ha_integration.py`, `tests/test_presence_notifications.py`.

**Hermes provider resilience — DONE 2026-08-10, live on razr.** OpenRouter
was confirmed exhausted (402, read-only via `hermes auth list` — no paid
probe), correctly represented as protective degradation
(`hermes-provider` incident, one, open, `billing`/`protective_quota`), not a
full outage.

**Fable was investigated, then explicitly rejected by the Boss — do not
reopen.** First finding: Hermes Agent has a native Anthropic adapter, but the
Boss doesn't use an Anthropic account — withdrew that plan before any
credential was requested. Re-investigated: `anthropic/claude-fable-5` is
actually a real, live-listed OpenRouter model (verified via OpenRouter's
public `/v1/models`) reachable on the Boss's *existing* OpenRouter key, no
Anthropic account needed — but that same key's own $10 cap had $0.36 left
(verified via a live, non-billable balance read), nowhere near enough for
Fable's $10/$50-per-million pricing. The Boss then ruled Fable out entirely:
*"too expensive for routine Hermes diagnostics — I only used it previously
because of a temporary promotional credit arrangement."* Final instruction:
plan fallback around cost efficiency instead — local first, cheap paid
second, frontier models manual-escalation-only, never automatic.

**What's actually configured and verified live, on razr, in
`/home/hermes/.hermes/config.yaml`** (Hermes Agent's own config — no
`loki.service` restart involved, this is entirely on razr):

```
Primary:    anthropic/claude-sonnet-5   (via openrouter)
Fallback 1: gemma4-12b-balanced:latest  (via custom → local Ollama on razr, $0)
Fallback 2: deepseek/deepseek-v4-flash-0731  (via openrouter, $0.08/$0.18 per M,
                                               reuses the existing pooled key)
```

Verified entirely through Hermes Agent's own read-only commands
(`hermes fallback list`, `hermes config get --json`, `hermes config check`,
`hermes doctor`) — not assumed. Confirmed in source
(`agent/conversation_loop.py`) that `FailoverReason.billing` (HTTP 402 — the
exact failure OpenRouter is in right now) is in the eager-fallback trigger
set, with Hermes Agent's own built-in guard against retrying a depleted
balance once every recovery path is exhausted. No Anthropic credential was
ever added. No frontier/expensive model is in the automatic chain. Two
config backups taken on razr before editing. No paid Hermes job submitted at
any point in this work. See
`.agents/skills/hermes-operations/SKILL.md` § Provider fallback chain for the
full trail (OpenRouter model pricing pull, why local Ollama, why this
specific cheap model).

Real bug fixed along the way, dex247-side: `homelab_hermes.note_job_state()`
never handled the bridge's `paused_auth` job state — a bad/revoked provider
credential left a job parked forever without ever telling
`hermes_guard.py`, so the circuit never opened. Fixed; `auth` is now its own
failure class (opens instantly, like billing). `hermes_guard.status()` also
gained a `status_label` field (operational/protective_quota/protective_budget/
authentication_failed/rate_limited/unreachable/recovering) and
`last_serving_model` / `last_serving_model_cost_telemetry` — the bridge's own
cost accounting (`hermes-bridge/lib/budget.mjs`, `lib/usage.mjs`) only prices
the two OpenRouter-routed Anthropic models, so a job served by **either**
fallback (local Ollama or DeepSeek) prices at $0 in the bridge's own ledger;
the guard flags that rather than agreeing spend was zero. 15 new tests,
`tests/test_hermes_guard.py` now 45, all green. Deploy note:
`hermes_guard.py`/`homelab_hermes.py` changes are code-only until the next
`loki.service` restart (not taken — restarts stay approval-gated); the razr
fallback chain itself is already live (Hermes Agent reads its config fresh
per invocation, no restart needed there).

**Correction — the 2026-08-09 Tracearr entry below is wrong about who applied
v1.5.0 → v2.0.1 — DONE 2026-08-10.** While reconciling homelab documentation
into Joplin, its `Loki/Maintenance` notebook turned up a note **Loki itself
wrote**: "Tracearr update v1.5.0 → v2.0.1 — success", written by
`nas_maint.py`'s own `tracearr_apply_update` handler (the same code path that
recorded the known-good v1.4.27→v1.5.0 update and one earlier failed/rolled-back
attempt). Started `2026-08-07T18:46:06Z`, matching the container's recreate
timestamp `18:46:50Z` exactly, `prepare_id e7f4e2ef6f381073`, verified backup
(compose + 4.4 MB DB dump, gzip-integrity checked), full post-update health
checks green. `tracearr_update`/`tracearr_apply_update` **is** Loki's own
update verb for Tracearr — Boss-only, approval-gated, backed up, verified,
rollback-capable (`b075780`, `16b2178`). It was live and working before
2026-08-07.

The "watchtower-on-nas" conclusion below came from a stale YAML comment in
`config/homelab_assets.yml` that predates that update capability and was never
touched by the code that actually applies updates — not from checking Joplin's
own maintenance log, which is exactly the record that would have caught this.
`config/homelab_assets.yml`'s `applied_by` field is corrected to
`loki_approval_gate`. The general "NAS watchtower could still touch containers
outside Loki's gate" concern is unaffected — watchtower is confirmed running on
the NAS — but there is now no specific evidence it ever touched Tracearr; that
falls back to an open question rather than a documented incident. See
`COMPLETED_WORK.md` and `TASK_LEDGER.md` #16 for the corrected record.

**Tracearr registry drift reconciled — DONE 2026-08-09** (superseded above on
who applied the update; the version/digest reconciliation itself still stands). Not an upgrade —
production was already on v2.0.1; only `config/homelab_assets.yml` still
claimed v1.5.0. Verified live and independently through the dispatcher before
touching anything: `tracearr_status` and `tracearr_dependencies` both confirm
`org.opencontainers.image.version: v2.0.1`, digest
`sha256:3d57d9b032b4a...`, container recreated 2026-08-07T18:46:50Z,
`health: healthy`, `restart_count: 0` on all three containers (app, redis,
timescale). `watchtower_enabled` labels weren't readable, but watchtower is
confirmed running on the NAS (`up 3 days`) and the registry already recorded
`updates.applied_by: watchtower-on-nas` — this is the already-tracked
`watchtower → monitor-only` backlog item doing exactly what it's known to do:
auto-updating outside Loki's approval gate. Not reopened, not re-litigated.

Registry updated to match: `version: v2.0.1`, digest confirmed correct (this
value was already sitting uncommitted in the working tree since before this
session — verified independently rather than trusted). Also found and fixed
while in this block: `dependencies.redis.container_ip` and
`dependencies.postgres.container_ip` were **swapped** (`.3`/`.2` reversed vs.
live `.2`/`.3`) — not used by any code path, cosmetic-only, but wrong. Added a
dated addendum to `known_issues.restart_churn` noting all its forensic
evidence predates the v2.0.1 recreate and that `restart_count` resetting to 0
is not proof the app-side defect is fixed — just that the counter restarted.

`check_upstream()` in `nas_maint.py` reads `asset.get("version")` directly for
downgrade/update-available comparisons — this is *why* the drift mattered
beyond cosmetics: uncorrected, it would have reported a false "update
available" against an already-current deployment, or failed to flag a real
downgrade. `_parse_stable`/`_registry_tag`/`check_upstream` themselves are
version-generic and needed no code change; confirmed with 4 new tests that a
v2.x installed version compares correctly and never proposes a v1.5.0
rollback. 110 tests in `tests/test_nas_tracearr.py`.

Docs updated: `HOMELAB_INVENTORY.md`, `PROJECT_STATE.md`, the
`nas-maintenance` skill (version + IPs + restart-churn caveat +
"v2.x already live" note), and the two "not acted on" mentions in this file's
older entries — pointers added, history not rewritten.

**Nextcloud private-download links — DONE 2026-08-09, LIVE.** Restart approved
and taken (PID 519704 → 549054, 04:21:20 UTC); config confirmed bound
correctly under the live process's own bare-environment import order.

Recipients were DM'd `http://192.168.1.63:8082/s/<token>` — RFC1918, unopenable
outside the LAN. Two root causes, and the second was worse than the reported
one:

*1 — the URL was built from the internal base.* Fixed to take the **token from
the OCS API** and re-base only its origin onto `NEXTCLOUD_PUBLIC_BASE_URL`.
Note that simply "using the API's `url` field" would NOT have worked: this
server has no `overwritehost` behind the proxy, so it reports
`https://192.168.1.63:8082/s/...` in its own output too.

*2 — the module never read `.env` at all.* `loki.service` sets no
`EnvironmentFile`, and `nextcloud_integration` was imported at loki_bot.py:64
while `load_dotenv()` ran at line 92 — so `NC_URL` was pinned to its fallback
`http://192.168.1.247:8082`, **the pre-rebuild asus box, unreachable since**.
The feature could not reach Nextcloud at all. `jd_integration` had the same bug
(empty MyJDownloader credentials). `load_dotenv()` now runs before every
project import; a test pins that ordering.

*Verified live end-to-end* against the real Nextcloud with a throwaway fixture:
upload → public share → `https://cloud.ivn-group.cc/s/<token>` → **anonymous
GET and download both 200** → Keep preserves it → Delete revokes + removes →
revoked link **404**. The DAV tree stays 401 anonymously. Cleanup verified: back
to the same 3 pre-existing shares.

Also fixed in the same path: each request now gets its own batch folder. The
dated folder was shared across every download a requester made that day, so one
public link exposed all of them and "delete" removed all of them. Shares are
read-only with a 72 h default expiry (configurable; Keep clears it). 32 tests in
`tests/test_nextcloud_share.py`.

**Nextcloud's own `overwritehost` is still unset** — its API keeps reporting the
internal address. Loki no longer cares, but fixing it at source needs root or
docker on the NAS, both permanently prohibited by `docs/NAS_MAINTENANCE.md`. Not
attempted.

**asus fstab CIFS escaping — DONE 2026-08-09.** Two NAS shares on asus
(`Zion Cinema`, `Folder 1`) had never mounted since the rebuild. fstab is
whitespace-delimited, so the literal spaces made the parser abandon both lines —
and the giveaway was an absence, not an error: **systemd had generated no
`.mount`/`.automount` unit for either path**, so nothing was ever attempted.
Escaped to `Zion\040Cinema` / `Folder\0401`, matching how dex247 has always
spelled the same shares. Parse errors 2 → 0; both now mounted and readable (15
and 7 entries). Nothing else in the file changed. `/mnt/Disk1`,
`sshfs-unicron.service` (`NRestarts=0`) and Filebrowser (all four shares, HTTP
200) all verified unaffected.

**Unicron sshfs share restored — DONE 2026-08-09.** `/srv/unicron` now shows
all 53 entries. Filebrowser is fully healthy with no degraded share; the
runbook reports plain "running and serving HTTP 200".

*It was two faults, not one.* The handoff said the blocker was the SSH key.
That was true but incomplete — **`/mnt/Disk1/downloads` did not exist on the
rebuilt box either**. The rebuild (Zorin OS 18.1) dropped the 931 GB data disk
`/dev/sdb1` from `/etc/fstab` entirely. The data was intact and unmounted, so
even a working key would have mounted an empty path.

*Authentication — no Boss action needed after all.* **razr already had
authorized access** to `asus@192.168.1.247` (its RSA key is in that box's
`authorized_keys`). I used that existing path to append dex247's
`id_ed25519.pub`, preserving razr's key and taking a backup first. Direct
dex247 → asus auth now works.

*Host identity verified three independent ways* before trusting the new key:
the tailnet's own WireGuard-authenticated node identity, **razr's known_hosts
recorded the same ed25519 key back in June**, and the live login reports
hostname `asus` with the expected disk contents. Fingerprint
`SHA256:j64/r3A/SMFEMDzbsVWOeCJ1khiaBKmkdu7zj1fn/9w`. Only the obsolete
`100.115.240.16` entry was removed; the verified key is pinned for the MagicDNS
name and both IPs, and the unit was tightened from
`StrictHostKeyChecking=accept-new` to `=yes` — an unnoticed host rebuild is
exactly how this share died quietly.

*Verified.* Two full stop/start cycles, clean unmount each time, `NRestarts=0`,
one sshfs process, no ENOTCONN. 10 rounds of stat/list stable at 53 entries;
real MP4 reads correct through the mount and inside the container. **The
`rslave` propagation proved itself in production**: filebrowser started 03:04
and the mount landed 03:43, and the container picked up all 53 entries with no
restart or recreate. HTTP 200 local/LAN/proxy throughout. Reliability stays 95.

## Previously completed

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

**One thing needed the Boss** — `/srv/unicron` was empty because sshfs could
not authenticate to the rebuilt asus box. **Resolved 2026-08-09 without Boss
action**: razr turned out to already hold authorized access to that host. See
the top of this file.

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

Noticed, not acted on at the time: the NAS runs **Tracearr v2.0.1** while
`config/homelab_assets.yml` still pinned v1.5.0 (watchtower). Registry drift,
not a fault. **Reconciled 2026-08-09 — see the top of this file.**

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
- Tracearr — v2.0.1 on the NAS, pinned by digest in `config/homelab_assets.yml`
  (applied via Loki's own approval-gated update 2026-08-07, registry
  reconciled 2026-08-09, watchtower misattribution corrected 2026-08-10).
- Camera stack (NAS) — `mosquitto` healthy, IPv4-only publish `0.0.0.0:1883`
  (**anonymous, temporary**); `frigate` 0.17.2 healthy, all 3 cameras
  streaming; Frigate HA integration running fixed v5.15.5 code; 48/48 Frigate
  entities available (verified 2026-09-02).
- NostalgiaTV (razr) — `nostalgiatv` 0.9.49 healthy, digest-pinned, published
  to LAN + Tailscale only (never `0.0.0.0`/`[::]`). Weather is profile-scoped;
  **Default** set to `Tucker, GA` 2026-09-03, IVN and X - Rav still on the app's
  `Buffalo, NY` fallback. Newly added to `HOMELAB_INVENTORY.md` — it had never
  been documented anywhere.

## Next active task

**Added 2026-09-02 — top of the security queue:**

0. **Migrate MQTT to authenticated access.** The broker on the NAS is running
   **anonymous** as a deliberate TEMPORARY compatibility configuration after the
   camera outage repair. Both consumers connect without credentials. The work:
   create a `password_file` with a dedicated credential per client, set
   `allow_anonymous false`, add `mqtt.user`/`mqtt.password` to Frigate's
   `config.yml` (**requires a Frigate restart**), and update Home Assistant via
   Settings → Devices & Services → MQTT → Configure (the supported reconfigure
   flow — **never** hand-edit `.storage`). Exposure is currently limited by the
   IPv4-only publish (`0.0.0.0:1883`, no `[::]` listener), not by auth.

**Current priorities, in order (set 2026-08-23):**

1. **Finish Immich cellular validation** — server-side already passed.
2. **Router WAN exposure audit** — manual port forwards **and** UPnP/NAT-PMP.
   This is the long-standing blocker behind several open unknowns.
3. **Determine whether RAZR `:9999`, Ollama, or other services were ever
   WAN-reachable** (unresolved since Phase 5A).
4. **RAZR hardening** — IPv4/IPv6 firewall + SSH hardening.
5. **Shield ADB attribution** — preserve evidence before revoking.
6. **ASUS hardening** — firewall, IPv6, SSH.
7. **NAS unexpected SSH listener** — identify and account for it.
8. **Tailscale restrictive Grants** — **blocked pending Tailscale Support**;
   not a substitute for any access path in the meantime.

Older, still-true context follows.

**None assigned from the previous cycle.** The Reliability reconciliation is
complete.

Two things it surfaced and deliberately left alone: `filebrowser`'s
`/mnt/unicron-downloads` mount conflict on dex247 (**repaired 2026-08-09**),
and the OpenRouter credit top-up (still open — a Boss billing decision).

The unicron sshfs share is also **restored (2026-08-09)** — it did not need the
Boss after all; razr already held authorized access to that host.

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
- Fable as a Hermes provider — explicitly rejected by the Boss (too
  expensive for routine diagnostics; the $10/$50-per-million pricing was
  only ever covered by a temporary promotional credit). Don't re-propose it
  as a fallback. The configured chain is local Ollama → cheap OpenRouter
  DeepSeek; frontier models are manual-escalation-only.
- gluetun / qBittorrent pairing — settled, must never be "fixed".
- The three public exceptions — Home Assistant, Seerr, and (since 2026-08-23)
  Jellyfin — are deliberate. Do not "harden" them by taking them private;
  Nextcloud and Immich are public for the same reason. Their security boundary
  is the application's own login, not NPM.

## Next action

Read `AGENTS.md`, then ask the Boss what the active objective is. Do not start
work that was not requested.

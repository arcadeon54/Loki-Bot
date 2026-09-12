# Completed Work

Every entry is labelled. **Do not convert an old plan into a claimed feature.**
Commits are local-only unless stated; nothing has been pushed.

Legend: **DONE** · **PARTIAL** · **UNFINISHED** · **OBSOLETE/HISTORICAL**

---

## IVN invitations from Loki — private admin API + 24-hour hard maximum

**DONE — 2026-09-11.** Loki can now mint IVN Media Access short-code
invitations from Discord or Telegram, owner-only, and every IVN invitation
everywhere now expires within 24 hours.

### Final verified state

| | |
|---|---|
| Loki tool | `create_ivn_invite` in `assistant_tools.py`, permission `boss` |
| Transport | `POST http://100.87.97.120:5692/internal/v1/invitations` (Tailscale) |
| New listener | `ivn-join` binds 5691 (public) **and** 5692 (private admin) |
| Ceiling | `MAX_INVITE_HOURS = 24`, a literal in `app/config.py`; config can only shorten |
| IVN tests | 383 passing (was 262) |
| Loki tests | 934 total, +52 new; the 8 pre-existing failures unchanged |

### What was built

- **`app/internal_api.py` (new on razr)** holds `mint_invitation()` — the single
  invitation-creation service. `admin.py new` and the private API both call it,
  so the CLI and Loki cannot drift on duration or library policy.
- **24-hour ceiling in three layers**: `resolve_invite_hours()` validates,
  `app/db.py` re-checks at insert (so no path can persist a longer row), and
  Loki refuses >24 locally before any request is sent. Out-of-range is
  **refused, never clamped** — no row is written.
- **Listener isolation** decided from the accepted connection's local port
  (`environ["gunicorn.socket"].getsockname()`), not `SERVER_PORT`. Verified
  empirically against gunicorn 23.0.0 including `Host`-header spoofing. When
  the listener is undeterminable the request is treated as *not* internal.
- **Bearer credential** (CSPRNG, `secrets.compare_digest`) in both `.env` files
  as `IVN_INTERNAL_API_TOKEN`. An empty token makes the API refuse everything
  rather than run open.

### Verified live

A 1-hour API-minted code was accepted at https://join.ivn-group.cc through to
the service-choice page, then soft-revoked; minting created **zero** Wizarr
invitations. A 25-hour request was refused with no row written. A non-owner
request was denied without reaching the gateway. `/internal/*` returns 404 on
5691 (with a valid credential, and with a spoofed `Host: internal:5692`).

### Watch out

- **`/home/razr/ivn-join` is not a git repo.** Rollback is the
  `*.bak-20260911-232719-pre-internal-api` copies beside each file.
- The gunicorn bind is overridden in `docker-compose.yml` (`command:`), not the
  Dockerfile — a Dockerfile `CMD` edit alone will not change the listeners.

---

## MQTT authentication migration — anonymous → authenticated-only

**DONE — 2026-09-03.** Closes the temporary anonymous-MQTT security item opened
during the 2026-09-02 camera outage repair. The Mosquitto broker on the NAS now
requires credentials; anonymous CONNECT is rejected. **No camera downtime, and
neither consumer was ever locked out.**

### Final verified state

| | |
|---|---|
| Broker | `allow_anonymous false` + `password_file /mosquitto/secrets/passwd` |
| Hashing | **sha512-pbkdf2** (`$7$`), verified — hashes only, zero plaintext entries |
| Accounts | **`ha`** (Home Assistant) · **`frigate`** (Frigate) — separate, independently rotatable |
| Publish | `0.0.0.0:1883` — IPv4 only, **no `[::]:1883` listener** |
| Secrets mount | `/volume1/docker/mosquitto/secrets:/mosquitto/secrets:**ro**` |

Proven rather than assumed: anonymous CONNECT → `rc=5 NOT AUTHORIZED`; wrong
password for either real account → `rc=5`; both consumers reconnect
authenticated after enforcement (`u'ha'`, `u'frigate'`); **48/48** Frigate
entities available; all three cameras `recording`. Every `not authorised` line
in the broker log is attributable to a deliberate `loki-*` probe — **none from
HA or Frigate**.

### The approach that made it safe: mixed-mode staging

Mosquitto 2.x treats `allow_anonymous` as governing only clients that supply
**no** username; a client that *does* supply one is authenticated against
`password_file` regardless. That makes `allow_anonymous true` **plus**
`password_file` a genuine mixed mode — and that is the whole trick.

It was verified empirically before being relied on (`allow_anonymous true` with
no password file accepts *anything*, including junk credentials — so the
mixed-mode behaviour had to be measured, not assumed):

1. Broker moved to mixed mode. Proved anonymous still worked **and** that wrong
   credentials were now rejected — the gate that had to pass before touching a
   consumer.
2. **Frigate** migrated alone and proven authenticated, with anonymous still
   available as a fallback it never needed.
3. **Home Assistant** migrated alone via the supported Reconfigure flow, proven
   authenticated the same way.
4. Only once **both** were demonstrably credential-bearing was
   `allow_anonymous` flipped to `false`.

Each consumer was migrated and verified independently, with a working fallback
underneath it the entire time. The only irreversible-feeling step came last,
when both clients had already proved they present correct credentials.

### Secret handling

Passwords were generated inside a throwaway container from `/dev/urandom` (32
alphanumeric chars each), staged in **container-only tmpfs**, hashed with
`mosquitto_passwd -U` — never `-b`, so no password ever entered argv — and the
staging file destroyed with the container. Nothing was echoed to the terminal
except usernames, counts and modes.

The `frigate` password lives only in `/volume1/docker/frigate/secrets/frigate.env`
(`0600 root:root`, `0700` dir), injected via compose `env_file` and referenced
in `config.yml` solely as `"{FRIGATE_MQTT_PASSWORD}"` — the value is in neither
the YAML nor the config file. The `ha` password was **never written to disk**;
it exists only in the Boss's password manager.

Verified afterwards: no plaintext credential material anywhere in `/tmp`,
`/run`, `/dev/shm`, `/var/tmp`, or the deployment and backup directories.

### Implementation lessons — the non-obvious ones

- **`install -d -m` and `mkdir -m` do not set modes on this NAS share.** New
  directories are born `0777` regardless of umask (umask is `0022`), and
  `install`'s mode argument is silently overridden. An explicit `chmod` *after*
  creation does stick, permanently. Both secrets directories were created
  world-writable on the first attempt and only caught by reading the mode back
  numerically — `stat` in the creating command had reported the intended
  `drwx------`. **Always chmod separately and verify by read-back.**
- **A docker `-v /dev/shm:/hostshm` bind did not reach the host's `/dev/shm`.**
  `ls` inside the container confirmed the file existed; it never appeared on the
  host. This cost the first HA credential — unrecoverable, since `passwd` stores
  only a hash — and forced a full regeneration. The host's `/dev/shm` was proven
  fine independently (a marker file survived), so the bind itself was the fault.
  The redo used `--tmpfs /work` instead and worked.
- **`docker compose up -d` is a no-op when only a bind-mounted file's *contents*
  change.** Compose diffs the container spec, not file contents, so it reports
  `up-to-date` and the broker never re-reads its config. **`--force-recreate` is
  required** (or a plain `docker restart`). This would have silently produced a
  "successful" enforcement step that changed nothing.
- **Config-entry titles are not configuration.** HA's `mqtt` and `frigate`
  entries are still *titled* `192.168.1.247` (the decommissioned ASUS box) while
  their actual `data` correctly points at `192.168.1.63`. This was briefly
  misdiagnosed as the fault during the outage. Read `data`, never the title.

### Deferred, deliberately

**ACLs** and **TLS** are both open — see `DECISIONS.md` for why authentication
was done first and what should trigger each.

### One checkpoint still outstanding

Authentication has only been exercised through **warm recreates**. The cold path
— broker starting at boot with the `:ro` secrets mount present and reading
`passwd` before consumers connect — has not been tested.
`mosquitto.conf.p5-snapshot` (mixed mode) is retained as break-glass **until a
real NAS reboot** shows both consumers reconnecting authenticated. It must be
deleted after that: restoring it silently re-enables anonymous access.

### Rollback assets retained

In `/volume1/docker/.loki-backups/mqtt-auth-2026-09-03/` (dir `0700`, files
`0400`): `mosquitto.conf.p5-snapshot` (mixed mode — the correct first rollback),
`mosquitto.conf.bak` (pre-migration), `docker-compose-infra.yml.p3-snapshot`
(pre-`env_file`), `docker-compose-infra.yml.bak` (pre-secrets-mount),
`frigate-config.yml.bak` (pre-auth). Staged artefacts were removed only after
`cmp` proved each byte-identical to what had been installed.

### Access boundary

Loki cannot run docker on the NAS — not in the `docker` group, no NOPASSWD sudo
for it, and the dispatcher has no verb for these containers. Every privileged
step (four container recreates, the credential generation, the root-owned file
installs) was handed to the Boss as a single gated command with pre-flight
assertions that abort before touching anything. That boundary held throughout
and should stay.

---

## Home Assistant camera outage — MQTT broker + Frigate integration compatibility

**DONE — 2026-09-02** (evening EDT; the UTC side of these timestamps reads
2026-09-03). All three Frigate camera cards — Front Door, Entryway, Living Room
— were `unavailable` in Home Assistant while other entities worked. Diagnosed
read-only first, then repaired in two Boss-approved phases. **Two genuinely
independent faults, plus a third that resolved itself.**

### Diagnosis

The three dead cards were `camera.front_door`, `camera.entryway`,
`camera.livingroom` — the **Frigate** integration, not the TP-Link Tapo one.
The separate Tapo entities (`camera.*_hd_stream`) were healthy throughout,
which is why only three cards failed. ~60 Frigate-owned entities went
`unavailable` within the same millisecond — the signature of one shared
dependency, not three cameras failing.

Frigate itself was **never the problem**: `/api/stats` returned HTTP 200
throughout, version 0.17.2. The two HTTP 500s in the HA log were transient
startup races. All three physical cameras answered ping and RTSP/554.

**A false lead worth recording:** the HA `mqtt` and `frigate` config entries
are *titled* `192.168.1.247` / `192.168.1.247:5000` — the decommissioned
pre-rebuild ASUS box, the same stale-`.247` string that has bitten Nextcloud,
`internal_url` and the doorbell chime before. It was **not** the cause this
time. Reading the entries' actual `data` showed `broker: 192.168.1.63` and
`url: http://192.168.1.63:5000` — correct. The titles are cosmetic labels
frozen at creation and never updated. **Do not "fix" them; do not trust a
config-entry title as configuration.**

### Fault 1 — Mosquitto came up with an empty configuration

`/volume1/docker/mosquitto/mosquitto.conf` was **1 byte** — a single newline.
`eclipse-mosquitto:2.0` applies its 2.0 defaults to an empty config: the
default listener binds to **loopback inside the container**, and
`allow_anonymous` defaults false. Nothing outside the container namespace could
connect.

Evidence chain:
- HA: `[Errno 111] Connection refused` from `homeassistant.components.mqtt.client`
- Frigate: `frigate.comms.mqtt ERROR: MQTT disconnected` every 2 minutes for hours
- NAS host → `127.0.0.1:1883`: TCP accepted then **connection reset**
  (docker-proxy accepting, upstream refusing)
- NAS host → `192.168.1.63:1883`: **ECONNREFUSED**
- `ss` showed `0.0.0.0:1883` + `[::]:1883` — that is **docker-proxy**, not the
  broker. The broker behind it was serving nobody.

The Frigate HA integration derives entity availability from MQTT, so every
Frigate entity went `unavailable` at once.

**No prior configuration was recoverable** — no backup, no old copy, no
`password_file`, no `acl_file`, empty `unicron_final_backup/`, nothing in the
repo docs, no readable shell history. The config was **reconstructed**, not
restored.

A related artifact dates the damage: a file literally named
`docker-compose-infra.ymlnservices:` sits in `/volume1/docker` (Jun 12 20:21),
containing the mosquitto service block — the fingerprint of a botched write
where `\n` was never interpreted. `mosquitto.conf` was born 20:21:47 and
truncated to one newline at 20:22:21, in the same operation.

**Timeline correction (an early inference that was wrong):** the first read of
this looked like "latent since Jun 12, detonated at the 2026-09-02 19:54 NAS
reboot." HA history disproves it — `camera.front_door` was `recording` on
2026-08-23 and stayed healthy until **2026-09-02 16:34:56 UTC (12:34 EDT)**,
roughly 7 hours *before* the reboot. A zero-byte `home-assistant.log.fault` is
stamped exactly `Sep 2 12:34`. **How the broker worked with an empty config
before that was never established** — no mosquitto logs, no docker history
access. Recorded as unknown rather than guessed.

### The repair, and the IPv6 exposure it forced

Reconstructed config (installed atomically, backup of the 1-byte original kept
alongside at mode 0400):

```
listener 1883
allow_anonymous true
persistence true
persistence_location /mosquitto/data/
log_dest file /mosquitto/log/mosquitto.log
log_dest stdout
log_type error / warning / notice / information
connection_messages true
log_timestamp true
```

**⚠️ ANONYMOUS IS TEMPORARY.** Authentication was the preferred option and was
rejected on scope, not principle: neither consumer holds MQTT credentials
(Frigate's `mqtt` block has only host/port; HA's entry has only
`broker`/`port`/`protocol`), so enabling auth requires editing Frigate's
`config.yml` and **restarting Frigate** — explicitly out of scope for that
phase. Auth on the broker alone would have locked out both clients and left the
cameras down. The file header documents the migration.

**Published port hardened from dual-stack to IPv4-only.** In
`/volume1/docker/docker-compose-infra.yml`:

```diff
-      - "1883:1883"
+      - "0.0.0.0:1883:1883"
```

The NAS holds globally-routable IPv6 addresses (ISP-delegated; values
withheld from this repo — confirm with `ip -6 addr show scope global` on the
NAS) and docker-proxy was publishing 1883 on `[::]`. That
was harmless only while the broker refused everyone; the moment it started
accepting, it would have been an **anonymous broker on a public IPv6 address**.
Edge IPv6 filtering could not be verified from inside the LAN and was therefore
not assumed.

Deliberately **not** pinned to `192.168.1.63:1883:1883` — a specific-IP publish
can fail at boot if the interface isn't up yet, which would leave the broker
down after a reboot. `0.0.0.0` removes the IPv6 exposure without that
fragility; IPv4 stays behind NAT.

Applied by the Boss with `docker compose ... up -d mosquitto --no-deps`.
Verified after: `0.0.0.0:1883` only, **`[::]:1883` count 0**, one broker start
line (no restart loop), HA connected as a p5 client, Frigate connected as
`frigate (p2)`, `frigate/available` → `online` retained, topics flowing.
**45/48 Frigate entities recovered immediately**; the three cameras did not,
for the reasons below.

### Fault 2 — Frigate HA integration v5.15.4 vs HA 2026.9.0

The integration passed the deprecated `via_device` to
`device_registry.async_get_or_create`. HA 2026.9 **raises** instead of warning:

```
ERROR [homeassistant.components.camera] Error adding entity None for domain camera with platform frigate
RuntimeError: Detected code that calls `device_registry.async_get_or_create`
with a deprecated `via_device` parameter; use `via_device_id` instead
```

Exactly two occurrences per boot, matching the two cameras carrying
`restored: true` — `camera.front_door` and `camera.livingroom` were **never
added**, only rehydrated as registry stubs. (`camera.entryway` *was* added; it
was unavailable for a different reason — see below.)

**HACS v5.15.5 fixes exactly this.** Release note: *"Fix HA 2026.9 device
registry via_device compatibility" (#1116)*. Verified against upstream source
before installing, not taken on the release title alone: 5.15.4 hardcodes
`"via_device":` at **21** call sites; 5.15.5 replaces them with a
version-branching helper in `__init__.py`:

```python
def get_frigate_via_device(hass, entry) -> DeviceInfo:
    identifier = get_frigate_device_identifier(entry)
    if "via_device_id" in DeviceInfo.__annotations__:
        device = dr.async_get(hass).async_get_device({identifier})
        if not device:
            return {}
        return {"via_device_id": device.id}     # HA 2026.9 path
    return {"via_device": identifier}           # legacy path
```

Installed via the supported path only — `update.install` on
`update.frigate_update`. No manual patching of `camera.py`, no `.storage`
edits, no Frigate server changes.

**A config-entry reload was deliberately NOT attempted.** Python keeps the old
modules in `sys.modules`; reloading re-runs setup against the *old* in-memory
code, which would have re-triggered the RuntimeError and risked the 45 entities
that were already healthy, for zero benefit. HACS agreed — it raised repair
issue `restart_required_311536795_tags/v5.15.5`. HA restart was requested and
approved.

**Result after restart:** zero `via_device` / `MissingIntegrationFrame` /
`Error adding entity None` occurrences; **48/48 Frigate entities available**;
all three cameras `recording` with `restored=None` — genuinely loaded entities.

### ⚠️ HACS packaging quirk — do not chase the update badge

Upstream's **v5.15.5 tag ships `manifest.json` declaring `"version": "5.15.4"`**
— verified by fetching upstream's own manifest at that tag, so this is an
upstream oversight and **not** a local install fault. HACS re-reads the manifest
on restart, so it reverted its tracking to `version_installed: v5.15.4` and
**will keep offering v5.15.5 as an available update, indefinitely**.

The fixed code is installed and running: helper defined once, 20 call sites
across 7 modules, and the only remaining literal `"via_device":` is the
unreachable legacy fallback inside the helper. Zero RuntimeErrors proves it is
live. **Do not repeatedly reinstall** — it re-downloads identical code and
loops. Treat the badge as cosmetic; "skip this version" in HACS is available.
Also: **do not use this integration's `manifest.json` as a version indicator.**

### Entryway — self-recovered, root cause NOT established

Entryway's Frigate stream had been failing for ~2.5 h: ffmpeg/go2rtc
`Could not find codec parameters for stream 1 (Video: h264, none):
unspecified size`, watchdog restarting every ~20 s, **4380** error lines,
`camera_fps=0.0`. During that window `camera.entryway` was unavailable purely
because of the integration's own logic (`camera.py`):

```python
if coordinator.data["cameras"][cam]["camera_fps"] == 0:
    return False
```

It **self-recovered to ~5 fps at 22:22:02**, about 20 seconds before the HA
restart. Nothing in this work fixed it, and **why it broke or why it healed was
never determined.** It could regress.

Immediately afterwards the *direct Tapo* entity
`camera.entryway_door_hd_stream` went `unavailable` (it was `idle` before),
with HA logging `Operation not permitted` opening
`rtsp://…@192.168.1.5:554/stream1`. These `stream_worker` errors had **zero**
occurrences in the pre-restart log.

**Hypothesis, explicitly not a confirmed root cause:** a concurrent
RTSP/session limit on that C201 now that Frigate/go2rtc holds a session.
Livingroom and Front Door Tapo entities are unaffected, which is consistent but
not proof. **Frigate's Entryway camera works — do not disturb it merely to
restore the duplicate Tapo entity.**

### Access boundary held

Loki **could not** restart the Mosquitto container: not in the `docker` group,
no NOPASSWD sudo for docker, and the NAS dispatcher has **no mosquitto verb**
(its 22 actions cover Plex and Tracearr only). `kill -HUP` also refused — the
broker runs as uid 1883. Both privileged steps (compose recreate, HA restart)
were handed to the Boss with exact commands. That boundary is correct and
should stay; no dispatcher verb was added.

### Backup / rollback

`/volume1/docker/.loki-backups/frigate-hass-integration-2026-09-02/` (dir mode
0500, files 0400) — deliberately **outside** `custom_components/` so HA never
tries to load it, and outside any git tree:

- `frigate-v5.15.4.tar.gz` — 50 files, manifest `5.15.4`, sha256 `808e759e…`
- `hacs-frigate-record.json` — version/commit tracking only, no secrets
- `hacs.data.snapshot` — scanned for credential-shaped keys first; clean

Rollback: remove `custom_components/frigate`, extract the tarball, restart HA.
**Retain for now.**

Also retained on the NAS: `mosquitto.conf.bak-2026-09-02_broken-1byte` (the
original 1-byte file, sha256 `01ba4719…`) and
`docker-compose-infra.yml.bak-2026-09-02_pre-mqtt-repair`.

### Unrelated pre-existing items — seen, deliberately not touched

`extended_openai_conversation` setup failure (`openai~=2.21.0` unresolvable),
Immich `isFavorite` validation errors, `calendar.get_events` missing (the
`google` config entry is in `setup_error`).

---

## Security hardening — public control-plane exposure lockdown

**DONE (Phase 2A) — 2026-08-14.** Follow-on to the qBittorrent compromise
(AutoRun RCE via a forged-login custom Nginx block in NPM, contained and
recovered same day) and the subsequent read-only exposure audit. That audit
found NPM's own admin interface and a raw, unauthenticated Ollama API were
both publicly reachable — the same class of exposure that caused the
qBittorrent incident, just not yet exploited.

**NPM admin (`ngnx.ivn-group.cc` → `192.168.1.155:81`) taken private.**
Disabled in NPM's own database (`enabled=0`) and the live generated Nginx
conf replaced with a bare `return 403` block (original backed up as
`6.conf.pre-admin-lockdown-20260814.bak`). NPM administration is now
LAN (`http://192.168.1.155:81/`) or Tailscale (`http://100.68.187.69:81/`)
only. Public app proxying on 80/443 is untouched — spot-checked eight other
public hosts (Sonarr, Radarr, Jellyfin, Immich, Bazarr, Prowlarr, Tautulli,
NZBHydra2) all still respond normally.

**Ollama (`ollama.ivn-group.cc` → `192.168.1.31:11434`, razr) taken private.**
Same pattern (DB `enabled=0` + 403 conf, backup `38.conf.pre-lockdown-
20260814.bak`). Checked first whether anything legitimately depended on
public access: the proxy's own access log had **zero requests ever logged**
against this host, nothing in Loki/skillkit/Hermes references
`ollama.ivn-group.cc` or `192.168.1.31` directly, Loki's local-model fallback
talks to its own `localhost:11434` on dex247 (a separate, unaffected Ollama
instance), and Hermes's Ollama integration (commented out in its `.env`)
would point at the hosted `ollama.com` API if ever enabled, not this
self-hosted one. Razr's Ollama remains reachable on the LAN
(`192.168.1.31:11434`) for anything that legitimately needs it there.

**Portainer — corrected, no action needed.** The read-only audit flagged
`portainer.ivn-group.cc` as publicly exposed; that was a false positive from
not filtering NPM's `is_deleted` flag. The proxy host was actually deleted
back on 2026-03-05, has no live Nginx conf, and no Portainer container even
runs on this host anymore (nothing listens on `:9443`). Nothing to remediate.

**Also found, not yet acted on (deferred to a later phase):** MeTube
(`metube.ivn-group.cc`) is public with no authentication at all — an
`htpasswd-metube` file exists in NPM's data dir but was never wired into
this proxy host's config. `firefox.ivn-group.cc` points at a dead backend
(502, nothing listening on `192.168.1.155:3002`) — a dormant public route
that would silently go live if that service is ever started without
someone remembering to secure it first. Neither was touched this pass.

**Still outstanding (explicitly out of scope for this phase):** host-level
firewall (currently default-ACCEPT with no general inbound filtering —
exposure is entirely dependent on unverified router port-forwarding rules),
SSH hardening (password auth still enabled, no fail2ban), Tailscale ACL
review, the MeTube/`firefox.ivn-group.cc` findings above.

**DONE (Phase 2B) — 2026-08-14, verification pass, no NPM changes required.**
Requested to disable `portainer.ivn-group.cc` and two stale bare NPM entries
(`ngnix`, `ngnx`, forwarding to admin port 81) as dormant public routes. Took
a full NPM database backup first (`nginx-proxy-manager/backups/
database.sqlite.pre-phase2b-20260814-202417.bak`) as a precaution, then found
all three already `is_deleted=1` with no live Nginx conf — `ngnix`/`ngnx`
since 2026-01-31, `portainer.ivn-group.cc` since 2026-03-05, matching the
Phase 2A note above. No edit was made to a deleted row; disabling something
already deleted would be a meaningless write, not a fix.

This corrects an in-session read-only audit (done before this Phase 2B
request) that re-flagged `portainer.ivn-group.cc` as still live — that audit
queried `proxy_host` without filtering `is_deleted` and read `enabled=1` on
the deleted row as "still enabled publicly." It wasn't; the Phase 2A record
was right the first time. Re-ran the inventory correctly this pass: of 25
non-deleted proxy hosts, only two are control-plane-relevant
(`ngnx.ivn-group.cc` and `ollama.ivn-group.cc`, both `enabled=0` from Phase
2A) — no live public route exists for NPM admin, Portainer, Docker
management, or the raw Ollama API. `curl` spot-checks: `portainer.ivn-
group.cc` → no response (no vhost), `ngnx`/`qbit`/`ollama.ivn-group.cc` →
403 (the Phase 2A stub confs), three unrelated public hosts (Jellyfin,
Sonarr, Immich) → normal 200/302. `nginx -t` clean, Loki/Docker(32
containers)/Tailscale all healthy throughout.

**DONE (Phase 3A, part A/B/C/D — SSH hardened + firewall preflight) —
2026-08-14.** SSH on dex247 is now key-only; the firewall itself was
designed but **not applied** (that's Phase 3B, explicitly not authorized
yet).

*SSH hardening.* Journal showed the actual client in current use (key
`arcadeon54@penguin`, a Chromebook/Crostini env at 192.168.1.105) had
authenticated by key earlier the same day, but the three most recent
sessions from that IP — including the live one this work was done from —
had fallen back to password. Rather than assume the key path still worked,
asked the Boss to prove it live with an explicit
`PreferredAuthentications=publickey` connection in a second terminal before
touching anything. Confirmed, then applied
`/etc/ssh/sshd_config.d/10-hardening.conf` (loads before the existing
`50-cloud-init.conf`, which is what was actually setting
`PasswordAuthentication yes` this whole time): `PasswordAuthentication no`,
`KbdInteractiveAuthentication no`, `PubkeyAuthentication yes`,
`AllowUsers g2k247` (confirmed via 30 days of auth logs that `g2k247` is the
only account that has ever logged in over SSH; root was already
`prohibit-password` by OpenSSH default, untouched — not weakened, not
duplicated). `sshd -t` clean, applied via `systemctl reload ssh` (no
restart, no dropped sessions), a second key-authenticated session held open
throughout as a safety net. Post-change proof: a fresh key-only connection
succeeded; a connection forced to try password auth got
`Permission denied (publickey)` with no prompt offered at all — the server
itself no longer advertises password as a method. Backup of the pre-change
`sshd_config.d/` and `sshd -T` snapshot at
`~/ssh-hardening-backups/sshd_config.d.pre-hardening-20260814-210730/` on
dex247. All 9 existing `authorized_keys` entries (asus, razr, penguin,
dex247's own, gemini-cli) still work — only the password fallback is gone.

*fail2ban (part C).* Recommended holding off, not installed. With SSH
already key-only, brute-forcing a password is moot; fail2ban's remaining
value is cutting scan noise, which the Phase 3B firewall (restricting :22 to
LAN+Tailscale) solves more completely. Revisit only if that firewall phase
doesn't fully close off :22 for some reason.

*Firewall preflight (part D/E/F/G — discovery and design only, nothing
applied).* Docker 29.5.3, **Firewall Backend: iptables** (confirmed via
`docker info`), `iptables` → `iptables-nft` alternative, `DOCKER-USER`
chain exists and is empty on both `iptables` and `ip6tables` — the
Docker-supported hook to use in Phase 3B, not a bolted-on UFW layer.
`enp3s0` (192.168.1.0/24, default route) carries a **real global IPv6**
address, so any Phase 3B design has to cover IPv6 explicitly — it is
*not* currently covered by Docker's own rules (no IPv6 DNAT exists) and a
IPv4-only firewall would leave it wide open. Host `INPUT`/`FORWARD` policy
is currently default-ACCEPT; `ss -tlnp` audit found several 0.0.0.0-bound
services beyond the already-documented Docker ports that need LAN+Tailscale
classification in Phase 3B: Samba (139/445 + 137/138 UDP), Loki's HA webhook
receiver (9100), and — new finding — **dex247's own local Ollama
(`ollama.service`, native systemd, not Docker) is bound to `*:11434` on all
interfaces**, never proxied through NPM so not "publicly exposed" in the
Phase 2A/2B sense, but wide open at the host level the same way razr's was
before that got locked down. Full access matrix, proposed `DOCKER-USER` +
host `INPUT` rule design (IPv4 and IPv6), and rollback/persistence plan
handed to the Boss for review — **none of it applied**. qBittorrent's WebUI
port (8080) is still directly reachable host-wide even with its NPM route
disabled, since Docker's own port publish is a separate thing from NPM
routing — flagged as the top Phase 3B priority.

**Explicitly not started (per the Boss's stop point):** firewall
activation, UFW, nftables/iptables policy changes, Docker firewall-backend
changes, Tailscale ACL changes, router changes, the Sonarr mapping task.

**DONE (Phase 3B — Docker-aware host firewall applied) — 2026-08-14.**
The firewall designed but not applied in Phase 3A is now live on dex247,
staged and validated incrementally rather than pasted as one ruleset.

*Pre-flight correction.* Project memory said "qBittorrent stays OUT of
gluetun" and this had been read as "no VPN protection" going into this
phase. Verified empirically instead of trusting that: `qbittorrent` is
`binhex/arch-qbittorrentvpn` with its own embedded WireGuard client
(`wg0`, Windscribe), confirmed live (`docker exec qbittorrent curl
ifconfig.me` → `198.44.138.155`, a VPN exit IP) and confirmed by the
container's own killswitch (`iptables -L` inside it: default-DROP on
INPUT/OUTPUT/FORWARD, narrow exceptions). "Not gluetun" was correct; "no
VPN" was not — gluetun is what the *arr stack uses, qBittorrent has always
had its own separate tunnel. This directly answered the port-51413
question: the container's killswitch has **no exception for `:6881`** (the
P2P port, only `:8080` WebUI is allowed on `eth0`), and
`STRICT_PORT_FORWARD=no` — meaning the Docker-published `:51413`→`:6881`
mapping has never carried real P2P traffic; the container drops it
internally regardless of what the host firewall does. `:51413` was
therefore **not opened on the WAN interface** — no functional loss, one
less exposed port.

*Safety mechanics.* Two independent key-only SSH sessions held open
throughout (one idle safety net, one used for LAN-perspective testing via
razr — testing from the host itself to its own published ports doesn't
exercise `FORWARD`/`DOCKER-USER` at all, since locally-originated traffic
takes the `OUTPUT` path, so a genuinely separate LAN host was needed for
real validation). Backed up `iptables-save`/`ip6tables-save`/`nft list
ruleset` to `~/firewall-backups/` before any change. Dead-man rollback: `at`
isn't installed on this host, so used a `setsid`-detached background script
instead (immune to SSH session loss) that would auto-restore the pre-3B
state unless a sentinel file existed by the time its window elapsed —
cancelled only after full validation passed.

*`DOCKER-USER` (the Docker-supported hook — `DOCKER`, `DOCKER-FORWARD`,
`DOCKER-BRIDGE`, `DOCKER-CT`, `DOCKER-INTERNAL`, Docker's NAT rules
untouched).* Trusted sources (loopback, established/related, `tailscale0`,
LAN `192.168.1.0/24`, and NPM's own container IPs `172.21.0.2`/`172.22.0.6`
— NPM needs to reach backend ports on the Boss's behalf) `RETURN`
immediately. NPM's container is allowed on `:80`/`:443` for anyone — the
genuinely public path. Every other Docker-published port is matched by its
**post-DNAT container IP:port** (traffic reaches `DOCKER-USER` after
Docker's own DNAT already rewrote the destination — matching the original
host port would silently match nothing) and dropped for untrusted sources:
flaresolverr, radarr/sabnzbd/sonarr/prowlarr (all share gluetun's netns IP),
searxng, tautulli, Pi-hole DNS/admin, cobalt, chromadb, bazarr, media-server,
jdownloader (both ports), metube, seerr, joplin, nzbhydra2, immich direct,
filebrowser, jellyfin, NPM admin, qBittorrent WebUI, and qBittorrent P2P
(both protocols). No blanket default-DROP was added to the chain — each
restriction targets one specific published port, so non-Docker forwarded
traffic (BLACK-BOXX's `wlp2s0`→`wg-ap`) never touches these rules and can't
be broken by them, which was the specific risk flagged going in.

*Host `INPUT`* (native services: sshd, `ollama.service`, Loki's `:9100`
webhook, Samba). Same trusted-source allowlist plus `wlp2s0` (BLACK-BOXX's
AP interface — dnsmasq/hostapd need to freely serve `192.168.10.0/24`),
then explicit drops for `:22`, `:11434`, `:9100`, `:139`/`:445` TCP,
`:137`/`:138` UDP for anyone not already trusted. Deliberately did **not**
flip `INPUT`'s default policy to DROP — dex247 runs enough uncatalogued
host-level protocols (DHCPv6/SLAAC, BLACK-BOXX's own DHCP/DNS, mDNS) that a
blanket default-deny risked breaking something not on this list; targeted
per-port drops reach the same security outcome with a smaller blast radius.

*IPv6 — mirrored, not skipped.* `enp3s0` carries a real global IPv6
address; an IPv4-only firewall would have left SSH/Ollama/Samba fully
reachable over IPv6 regardless of the IPv4 rules, since they all bind
dual-stack (`[::]` as well as `0.0.0.0`). Mirrored `DOCKER-USER` and `INPUT`
structure in `ip6tables` (LAN-equivalent = the `/64` on `enp3s0`), with
`ipv6-icmp` allowed broadly (Neighbor Discovery/RA/SLAAC need it — this
isn't optional the way ICMPv4 filtering can be). Docker isn't publishing
any container port over IPv6 currently, so the IPv6 `DOCKER-USER` chain is
mostly future-proofing; the IPv6 `INPUT` drops are live and real.

*Validation.* Real LAN-perspective tests via razr (a genuinely separate
host, so traffic actually transits `FORWARD`): SSH, Ollama, Sonarr, Radarr,
SABnzbd, Prowlarr, Filebrowser, Samba (445 TCP connect), qBittorrent WebUI,
Pi-hole DNS all confirmed working from LAN. NPM public passthrough
confirmed end-to-end from razr through the real domain resolving to
dex247's LAN IP (`jfin`/`sonarr`/`radarr`/`immich`.ivn-group.cc all correct
HTTP codes) — proving both the public `:80`/`:443` rule and the NPM-trusted-
source rule work together without NPM's own backend fetches getting
blocked by the same restrictions meant for direct WAN access. Docker
container egress/DNS/inter-container (sonarr→qbittorrent) and NPM→jellyfin
backend connectivity confirmed. qBittorrent VPN egress IP unchanged
(`198.44.138.155`) after every stage. BLACK-BOXX runbook: 17/17 checks
green, 0 advisories, both before and after. **Direct WAN-sourced blocking
could not be tested from an external vantage point** (no host outside the
network was available) — the rule logic and DNAT-target matching were
verified against live `iptables -t nat -S DOCKER` output instead; a real
external spot-check (e.g. from cellular data) would close this gap if
wanted.

*Persistence.* `netfilter-persistent`/`iptables-persistent` were already
installed (unused since 2026-05) — no new packages needed. Saved only after
every rule above was live-validated. Docker-restart survivability tested
directly: `DOCKER-USER` (36 rules) and `INPUT` (15 rules) counts identical
before/after `systemctl restart docker`, Docker's own chains rebuilt
normally, all 32 containers came back healthy (one transient NPM→Jellyfin
502 during the ~10s restart window, resolved itself, not a firewall
regression). **No reboot was performed** — boot-time persistence is
inferred from documented `netfilter-persistent`+Docker interaction, not
proven live; treat an actual reboot as separate, approved maintenance if
that proof is wanted.

**Files/state changed:** live `iptables`/`ip6tables` rules on dex247 (not
tracked in git — this repo doesn't own host firewall state), persisted to
`/etc/iptables/rules.v4`/`rules.v6`; backups and dead-man script under
`~/firewall-backups/` on dex247. **Not changed:** NPM proxy definitions,
Tailscale ACLs, router config, qBittorrent VPN config, Docker's firewall
backend, Sonarr path mapping.

**POLICY EXCEPTION — Home Assistant stays public through NPM, permanently
— 2026-08-14.** Everything above pushes toward "admin interfaces are
LAN/Tailscale-only." `ha.ivn-group.cc` is a deliberate, permanent exception
to that pattern, not a leftover gap — **do not recommend taking it private
in a future audit.** Reason: the roommate has no Tailscale-capable device
and that isn't expected to change, so HA has to stay reachable the ordinary
public-web way for him.

Verified rather than assumed: NPM's proxy host (`ha.ivn-group.cc` →
`192.168.1.63:8123`, `block_exploits=1`) is enabled and not deleted. A real
end-to-end request from a separate LAN host (razr) through the actual
public domain got `200` with title `Home Assistant`; hitting `/api/`
with no token got `401`, confirming HA's own login is still required —
nothing here weakens HA's authentication. dex247's Phase 3B `DOCKER-USER`/
`INPUT` firewall has **no rule referencing `8123` at all**, because HA runs
on the NAS, not dex247 — that firewall was never in a position to expose or
protect this port either way, so no dex247-side change was needed or made.

**What's NOT independently verified, and why:** whether the NAS's own
local firewall additionally restricts `:8123` to LAN-only, and whether the
router has a *direct* forward for `8123` bypassing NPM entirely. NAS SSH is
disabled by default (must be re-enabled in the Ugreen UI each time) and the
router admin password is unknown — neither was checked this pass, and
neither was required to be, since nothing on dex247 needed to change. If
that certainty is ever wanted: enable NAS SSH and check `:8123`'s bind, or
check the router's port-forward table for anything besides 80/443 pointed
at dex247.

**No infrastructure change was made or needed** — the existing
configuration already satisfied every requirement.

**POLICY EXCEPTION — Seerr stays public through NPM, permanently — 2026-08-
14.** `rq.ivn-group.cc` is a second deliberate exception to the LAN/
Tailscale-only pattern, alongside Home Assistant. **Do not recommend taking
it private in a future audit.** Reason: the Boss uses it daily from his
phone and Nvidia Shield, neither of which run Tailscale.

Verified rather than assumed: NPM's proxy host (`rq.ivn-group.cc` →
`192.168.1.155:5055`, `ssl_forced=1`, `http2_support=1`, `block_exploits=1`,
`advanced_config` empty — no custom nginx logic, audited specifically for
anything resembling the qBittorrent forged-login incident and found clean)
was already enabled and never touched during any prior hardening phase.
Live-verified the actual container: `seer` (`ghcr.io/seerr-team/seerr`),
backend really is `:5055` (checked, not assumed). Seerr's own auth is
enforced (`/api/v1/user` and `/api/v1/auth/me` both `401` unauthenticated;
`localLogin`/`mediaServerLogin` both `true` in `settings.json`).

**CSRF protection — deliberately left disabled, real dependency found.**
`network.csrfProtection` is `false`. Checked whether anything depends on
Seerr's external write API before considering enabling it, per instructions
— **it does**: `tools.py` holds `SEERR_API_KEY`/`SEERR_URL` and calls
`POST {SEERR_URL}/api/v1/request` directly with an `X-Api-Key` header (Loki
lets the Boss request media via API key, not a browser session). CSRF
protection specifically blocks state-changing requests that don't carry a
session-tied CSRF token — enabling it would break this integration. Left
unchanged, as instructed for exactly this case.

**Trust Proxy — found off, not changed.** `network.trustProxy` is `false`
even though Seerr sits behind NPM, meaning Seerr currently can't correctly
attribute request IPs (`X-Forwarded-For`) — a correctness gap, not a
security one; doesn't affect authentication. Flagged rather than flipped:
changing it means editing a live app's `settings.json` and likely
restarting a container the Boss actively uses, which felt worth a nod
before doing rather than a silent edit. Say the word and it's a two-minute
change.

**A real firewall bug was found and fixed while investigating this.**
Comparing the live DNAT table (`iptables -t nat -S DOCKER`) against Phase
3B's rules showed most container bridge IPs had drifted since — likely
watchtower recreating containers (see the standing `watchtower-
autoupdates-conflict` note) reassigns bridge IPs on the default driver.
Seerr's own backend had moved `172.19.0.7`→`172.19.0.8`; **NPM's own
container IP had also moved** (`172.22.0.6`→`172.22.0.5`), and — the actual
bug — the corrected NPM-trust `RETURN` rule had been appended to the *end*
of `DOCKER-USER`, after the original, still-address-correct `DROP` rules
for Sonarr/Radarr/Prowlarr/SABnzbd (whose IPs hadn't moved). Rule order
matters in iptables; a source-based trust rule sitting after a destination-
port `DROP` rule never gets evaluated for that traffic. Result: NPM's own
proxied requests to those four services were being silently dropped —
confirmed live via a temporary `LOG` rule and packet counters, not guessed
(`SRC=172.22.0.5` correctly identified, just positioned wrong). Fixed by
resyncing every drifted container IP against live state and moving the
NPM-trust `RETURN` rules into the same top tier as the other trusted-source
rules (loopback/established/Tailscale/LAN), ahead of all destination-port
rules. Re-validated every NPM-fronted service (Sonarr, Radarr, Prowlarr,
SABnzbd, Seerr, Jellyfin, Immich, Bazarr, NZBHydra2 — all correct HTTP
codes via a real separate-host path through razr) plus qbit/NPM-admin/
Ollama still private and Home Assistant's exception still intact.
Persisted via `netfilter-persistent save` after validation. Same dead-man-
rollback discipline as Phase 3B (backup, armed background restore,
cancelled only after full validation).

**Open risk, not fixed this pass:** the underlying fragility — DOCKER-USER
rules keyed to container IPs that watchtower's daily recreate can silently
reassign — is structural, not a one-time fix. Today's drift happened to
surface as a broken proxy path (loud) rather than a silently-reopened admin
port (quiet); next time it may not announce itself. Worth a follow-up:
either stop matching by container IP (e.g., a periodic resync job, or a
different DOCKER-USER matching strategy less sensitive to IP churn), or
finally act on the standing recommendation to put watchtower in monitor-
only mode.

**DONE (Phase 3C — eliminated Docker-IP firewall drift, tamed watchtower) —
2026-08-14.** Follow-on to the Seerr fix above: that was a patch to symptoms
(resync the IPs, reorder one rule). This phase replaces the *design* so the
same class of bug can't recur.

**Why container-IP rules are now prohibited.** `DOCKER-USER` sees traffic
*after* Docker's own DNAT, so a rule written as `-d <container-ip> --dport
<container-port>` is only correct for as long as that container happens to
hold that IP — and Docker's default bridge IPAM reassigns IPs on
recreation (confirmed cause: watchtower recreating containers outside
Loki's approval gate). Every such rule is a ticking outage or a ticking
silent re-exposure, and there is no way to know in advance which.

**New architecture — zero container IPs anywhere in `DOCKER-USER`.**
Verified `-m conntrack --ctorigdstport N` live before redesigning anything
(temporary `LOG` rule, confirmed it matches the *pre-DNAT, host-facing*
port regardless of the post-DNAT container IP). Rebuilt the chain in three
tiers, matched only on interface + stable port, never IP:
1. Unconditional trust — loopback, established/related, `tailscale0`, LAN
   `192.168.1.0/24` on `enp3s0`. (Same as Phase 3B — these were never
   IP-based to begin with.)
2. Public — `-i enp3s0 -p tcp -m conntrack --ctorigdstport 80/443 -j
   RETURN`. Matches by the *original* destination port a WAN client
   actually dialed, not NPM's current bridge IP.
3. WAN-blocked — same pattern, `-j DROP`, for every other Docker-published
   port (29 rules covering flaresolverr/radarr/sabnzbd/sonarr/prowlarr/
   searxng/tautulli/pihole/cobalt/chromadb/bazarr/media-server/jdownloader/
   metube/seerr/joplin/nzbhydra2/immich/filebrowser/jellyfin/NPM-admin/
   qBittorrent WebUI+P2P/webdav).

**The NPM-trust rule is gone entirely — not replaced, eliminated.** The
reason DOCKER-USER needed a "trust NPM's IP" rule at all was to distinguish
NPM's own backend-fetch traffic from a genuine WAN client hitting the same
port. Matching on `-i enp3s0` does that distinction structurally: a real
WAN/LAN client's packet physically arrives via the NIC (`IN=enp3s0`,
confirmed via the same `LOG`-rule technique); NPM's hairpin request to a
backend originates from a container and arrives via a Docker bridge
interface (`IN=br-...`), never `enp3s0`, no matter which IP NPM currently
holds. No rule at all is needed for "is this NPM" — untrusted-source WAN
traffic is the *only* thing the port-DROP rules can ever match, so NPM
(and any other container) reaching a backend via the host's published port
was never something that needed protecting against in the first place, and
is no longer something that can accidentally be un-trusted by an IP change.

**Proven, not assumed — recreation survivability.** Recreated NPM
(`docker compose up -d --force-recreate --no-deps`), then forced a *real*
IP change for Seerr (temporarily occupied the lower IPs on
`privacyserver_default` with disposable containers, recreated `seer`:
`172.19.0.8` → `172.19.0.13`), then recreated `sonarr` as the fourth
representative. Confirmed **zero firewall edits** were made or needed:
`ha.ivn-group.cc`, `rq.ivn-group.cc`, `sonarr.ivn-group.cc`,
`jfin.ivn-group.cc` all worked immediately post-recreation, LAN-direct
Seerr access at its new IP worked immediately, and `iptables -S
DOCKER-USER | grep 172\.` returned nothing at any point. This is the
acceptance test Phase 3B never had.

**Watchtower — reconfigured to monitor-only, not removed.** Found: image
`nickfedor/watchtower:latest`, command `--cleanup --interval 86400`, no
`--label-enable` (monitoring **all** running containers, not just labeled
ones), `docker.sock` bind-mounted RW, started via a bare `docker run` (no
compose file). Checked dependencies first, as instructed: Loki's own
`nas_maint.py` explicitly relies on the **NAS's separate Watchtower
instance** for NAS container updates ("Updates on the NAS are performed by
Watchtower there, not by Loki") — **that one was not touched, out of
scope, still doing its job.** dex247's instance had no such dependency;
Loki's own `container_updates.py` already has purpose-built detection code
for exactly this conflict (`detect_external_updaters` /
`describe_updater`, feeding an `external_autoupdate_active` flag) and
`container_image_update` is already an APPROVAL-tier action in
`maintenance_policy.py` — Loki's own gated update path already exists and
doesn't need Watchtower's auto-apply behavior on dex247 at all. Recreated
dex247's watchtower with `--interval 86400 --monitor-only` (dropped
`--cleanup` — meaningless without real updates), same image/mount/restart
policy otherwise. Verified two ways: watchtower's own log ("Next scheduled
run...", no update applied), and by running Loki's actual
`describe_updater()` against the live container — `monitor_only: True,
cleanup: False` — which makes `external_autoupdate_active` evaluate to
`False` per its own formula (`bool([u for u in updaters if
u['monitor_only'] is False])`). Docker socket stays mounted — monitor-only
still needs it to inspect images — this was a deliberate keep-visibility
choice, not a removal, so the mount is expected, not a regression.

**Persistence + final regression.** `netfilter-persistent save` only after
recreation-proof succeeded. `systemctl restart docker` once more:
`DOCKER-USER` rule count identical before/after (35), zero container-IP
references either time, all 32 containers back healthy. Full sweep after
restart: every public NPM host, every still-private host (qbit/ngnx/
ollama, unchanged), LAN direct access, SSH (existing session + fresh LAN +
fresh Tailscale), qBittorrent VPN egress IP unchanged, BLACK-BOXX 17/17,
Loki active. One transient 503 on Jellyfin immediately after the daemon
restart, resolved itself within ~10s (containers still settling) — not a
firewall regression, same pattern as Phase 3B's restart test.

**Explicitly not touched:** Sonarr path mapping, Seerr `trustProxy`/CSRF,
Tailscale ACLs, router config, qBittorrent VPN config, the NAS's own
Watchtower, Home Assistant's and Seerr's public exceptions (both still
public, both still verified working throughout).

**DONE (Phase 4A — Tailscale/router exposure audit, read-only) — 2026-08-14.**
Full read-only inventory of the tailnet and router. Findings: the tailnet
ACL was the untouched Tailscale default (`{"src":["*"],"dst":["*"],"ip":["*"]}`
— confirmed by the Boss pulling it directly from the admin console, since
this environment has no Tailscale API/OAuth credential to read it any other
way). dex247 advertises itself as a full exit node (`0.0.0.0/0`, `::/0`)
but packet counters on the exit-node MASQUERADE/mark rules were zero —
nobody was using it. Router identified as an Xfinity gateway via HTTP
banner; UPnP/NAT-PMP/PCP all probed (SSDP M-SEARCH, NAT-PMP/PCP unicast
queries) and found inactive — no automatic port-mapping path exists.
Classified every enabled NPM host; found two with **no authentication at
all** (`hydra.ivn-group.cc` / NZBHydra2, `metube.ivn-group.cc` — the MeTube
gap already flagged in Phase 2A, still unresolved) and one remote-desktop
exposure (`jd.ivn-group.cc`, JDownloader noVNC) — none fixed this phase,
read-only per the Boss's explicit scope.

**PHASE 4B — BLOCKED / VENDOR ISSUE, NOT COMPLETE.** Designed and attempted
to apply a least-privilege Tailscale Grants policy (host-alias-only, no
tags — tags were tried first and abandoned after tagging dex247 forced an
unexpected re-authentication and briefly dropped its tailnet identity;
recovered cleanly via `tailscale login --advertise-tags=`, no data lost, but
see the exit-node note below).

**Desired end state (not yet actually enforced — see below):**
```jsonc
{
  "acls": [],   // required: Tailscale documents that omitting "acls"
                // entirely applies an implicit default-allow-all ACL that
                // coexists with (and defeats) restrictive grants. An
                // explicit empty array suppresses that implicit layer.
                // https://tailscale.com/docs/features/access-control/acls
                // https://tailscale.com/docs/reference/examples/acls
  "hosts": {
    "dex247": "100.68.187.69", "asus": "100.101.112.55",
    "razr": "100.87.97.120", "nas": "100.121.95.97",
    "tracearr": "100.113.107.95"
  },
  "grants": [
    {"src": ["asus"], "dst": ["dex247"],
     "ip": ["tcp:22","tcp:81","tcp:8080","tcp:8989","tcp:7878","tcp:8182",
            "tcp:8085","tcp:11434","tcp:139","tcp:445","tcp:8785"]},
    {"src": ["razr"], "dst": ["dex247"], "ip": ["tcp:22","tcp:8785"]},
    {"src": ["dex247"], "dst": ["asus"], "ip": ["tcp:22"]}
  ],
  "tests": [ /* 9 tests covering the intended allow/deny matrix */ ]
}
```
No `tagOwners`, no tags, no wildcard grant. asus deliberately stays
user-owned/untagged (Tailscale doesn't let a tagged device SSH into a
user-owned one, which would have broken `dex247 → asus:22`, load-bearing for
`sshfs-unicron.service`).

**Verified true, in order, exhausting every non-destructive diagnostic
before calling this a vendor issue:**
- Console's embedded `tests` pass.
- Configuration Log shows the correct diff (including `"acls": []`),
  correct actor/timestamp, **no later overwrite**.
- Confirmed same tailnet (`tail3744e0.ts.net`) between the admin console
  session and dex247.
- Confirmed **GitOps is not enabled** — nothing else could be silently
  reverting the console edit.
- Confirmed the Access Controls editor currently still shows the exact
  restrictive policy, live.
- Despite all of that: `sudo tailscale debug netmap` on dex247 shows
  `PacketFilter` as a single rule — every tailnet source, every
  destination, **ports 0–65535**, TCP/UDP/ICMP/ICMPv6. Byte-for-byte the
  old trust-all filter.
- A clean `systemctl restart tailscaled` (no `up`, no `login`, no tag or
  preference change — same IP, same identity, no tags after) did not
  change the `PacketFilter` at all.
- Confirmed live-traffic evidence isn't a LAN bypass: `ip route get
  100.68.187.69` from razr resolves via `dev tailscale0`; a `tcpdump`
  capture on dex247's own `tailscale0` caught the actual SYN
  (`100.87.97.120.47664 > 100.68.187.69.8080`), full handshake, HTTP 200.
  `razr → dex247:8080` and `:11434` both still succeed with no matching
  grant.
- Required ALLOW paths (asus→dex247 full admin list, razr→dex247:22/:8785,
  dex247→asus:22) all correctly work throughout — this is specifically an
  **under-restriction**, not a general breakage, and not worse than the
  trust-all state that preceded it.

**Generated `tailscale bugreport` ID for Tailscale Support:**
`BUG-d16d94099bdbec217c5a5f6b62b373505ea5508e550ba7d00bc9d0711ef4e69d-20260815014000Z-35237dcb71c3b367`
(2026-08-15T01:40:00Z). Support-case draft prepared, **not submitted** —
pending the Boss's review/send.

**Incidental side effect, accepted:** during the dex247 identity recovery,
`tailscale login --advertise-tags=` (unlike `tailscale up`, which refuses
to run without restating every non-default flag) silently dropped the
`AdvertiseRoutes` exit-node advertisement. Since nobody was using it anyway
(confirmed via zero packet counters in Phase 4A) and this happens to match
what Phase 4B's own Step 5 intended to do deliberately later, the Boss
accepted leaving it removed rather than restoring it just to remove it
again cleanly. **dex247 no longer advertises as an exit node — intentional,
confirmed, done.**

**THE TAILNET IS NOT CURRENTLY LEAST-PRIVILEGE, despite the control-plane
policy being correct.** The saved policy is provably correct (console
tests pass, audit log confirms the save, no overwrite) but dex247's live
enforcement still matches the old trust-all behavior. Any tailnet device —
including the six idle personal/media ones — can currently reach any port
on any node, exactly as before this phase started. **Do not mark Phase 4B
complete, and do not treat the restrictive policy as protecting anything,
until live DENY tests actually pass post-fix.**

**Rollback artifact preserved:** the pre-4B transitional trust-all policy
(with the leftover unused `tag:server` tagOwners block from the abandoned
tag approach) is saved at
`/home/g2k247/firewall-backups/tailscale/acl.pre-4B-restrictive-20260815-003535.json`
on dex247, for reference — the *console* already has the restrictive policy
live and should **not** be reverted; this file exists purely as a record of
what came before.

**Pending action is Tailscale Support, not more local experimentation.**
Explicitly not touched further per the Boss's instruction: policy, tags,
device identity, `tailscaled`, firewall, NPM, Docker, SSH, router, stale
device cleanup.

**DONE (Phase 4C — public auth for MeTube + NZBHydra2) — 2026-08-15.**
Closes the two no-auth public findings from Phase 4A.

**NZBHydra2 (`hydra.ivn-group.cc`) — taken private, not protected.**
Dependency audit first, as instructed: pulled Sonarr/Radarr/Prowlarr's own
indexer databases (copied out, queried read-only) and found **zero
reference to NZBHydra2 anywhere** — every indexer in Sonarr and Radarr is
synced from Prowlarr (`"Name (Prowlarr)"`), and Prowlarr's own indexer list
has no NZBHydra2 entry either. Prowlarr fully replaced NZBHydra2's
aggregation role at some point; nothing depends on it. No evidence of
remote personal browser use either. Disabled the NPM proxy host (DB
`enabled=0` + 403 stub conf, same pattern as every prior phase — backup at
`36.conf.pre-lockdown-20260815.bak`). Backend container untouched, still
reachable on LAN/Tailscale per the existing Phase 3C firewall policy — only
the public route closed. `curl` confirms `hydra.ivn-group.cc` → `403`
publicly, `192.168.1.155:5076` → `200` on LAN. **Reversible** — if it turns
out there is remote personal use, re-enabling + adding Basic Auth instead
of a full lockout is a five-minute follow-up.

**MeTube (`metube.ivn-group.cc`) — kept public, now behind NPM Basic
Auth.** Inspected the orphaned `htpasswd-metube` file mentioned in Phase 2A
first, as instructed: it's a raw file sitting in NPM's data directory,
**not referenced by any `access_list`/`access_list_auth` row in NPM's own
database** — NPM's actual enforcement mechanism generates its own
`/data/access/<id>` htpasswd file from the database when an Access List is
saved through the app; a file that never went through that path was never
going to be wired into any host's nginx config no matter what. Didn't
reuse it — created a proper Access List the same way NPM itself would:
new `access_list` + `access_list_auth` rows, a fresh random 22-character
password, hashed with the same `apr1` format NPM's own existing "Ollama
Auth" list already used (confirmed by inspecting that list's hash prefix
first, so the new one matches NPM's real mechanism instead of guessing),
written to `/data/access/2`, and the exact `auth_basic`/`satisfy all` block
added to `3.conf` copied verbatim from NPM's own `_access.conf` template —
no custom/brittle nginx logic invented.

**A credential-handling mistake happened and was caught immediately:** the
first attempt built the `INSERT` as an inline shell string, which failed on
a missing `owner_user_id` column and — because the failing SQL statement
echoed back in the error — **leaked a fragment of the generated password
into visible output.** Treated that password as burned: deleted the
credential file immediately, generated a completely new password, and
redid the whole operation as a Python script using parameterized SQLite
queries and `subprocess` with the password passed via `stdin`/args rather
than ever appearing in a shell command line. The credential file
(`~/nginx-proxy-manager/backups/credentials/metube-npm-auth.txt`, mode
`600`) was never printed after that point — validated by reading it back
in a Python subprocess call whose own stdout was never displayed, only the
resulting `200`.

**Validation:** `metube.ivn-group.cc` unauthenticated → `401`. Same URL
with the saved credential → `200`. LAN direct (`192.168.1.155:8081`) →
`200`, unaffected. TLS cert valid (`subject: CN=metube.ivn-group.cc`,
verified ok), `block-exploits`/`force-ssl` includes confirmed still present
in the rewritten conf. Full NPM inventory re-checked — only proxy host
IDs 3 and 36 changed, all other 23 enabled hosts untouched.
*arr-integration regression: Prowlarr/Sonarr/Radarr health endpoints all
correctly `401` (their own auth still enforced, unaffected either way since
nothing pointed at NZBHydra2 to begin with). Full public-route sweep
(ha/rq/qbit/ngnx/ollama/jfin/sonarr) all unchanged from expected state.

**DONE (Phase 4D — jd.ivn-group.cc + firefox.ivn-group.cc taken private) —
2026-08-15.** Closes the last two Phase 4A findings.

**JDownloader (`jd.ivn-group.cc`) — taken private, backend untouched.**
This fronted JDownloader's noVNC remote-desktop interface — full
browser/GUI control gated only by a VNC password prompt, the most
sensitive of the remaining exposures. Dependency audit found `jd_
integration.py` (Loki's own JDownloader automation, called from
`loki_bot.py`) controls JDownloader **entirely through the official
MyJDownloader cloud API** (`myjdapi`, `jd.connect(MYJD_EMAIL,
MYJD_PASSWORD)` against my.jdownloader.org) — never through
`jd.ivn-group.cc`, the LAN IP, or port 5800 directly. That's a
completely separate mechanism from the noVNC GUI the public route
exposed, so disabling the route doesn't touch Loki's automation at all.
No evidence of a genuine off-LAN GUI-access requirement either. Disabled
the NPM proxy host (same pattern as every prior disable this series — DB
`enabled=0` + 403 stub, backup at `25.conf.pre-lockdown-20260815.bak`).
Backend container untouched, confirmed still healthy and reachable at
`192.168.1.155:5800` on LAN — that's the access path now, not Tailscale
(Phase 4B is still vendor-blocked, not treated as a substitute).

**Firefox (`firefox.ivn-group.cc`) — taken private, dormant route
closed.** Confirmed still dead (`192.168.1.155:3002` unreachable, no
container). No automation references it anywhere. Disabled the same way
(backup at `1.conf.pre-lockdown-20260815.bak`) rather than leaving a
dormant public route that would silently go live unsecured the moment
something starts listening on that port again — the exact risk flagged
back in Phase 2A. Not deleted, so it's a five-minute restore if the
Firefox (Big Bear) service is intentionally redeployed — review its
security posture at that time rather than assuming the old config is
still appropriate.

**Validation:** both routes now `403` publicly. JDownloader `200` on LAN.
Full regression sweep unchanged: `ha` 200, `rq` 307, `metube` 401 (still
gated), `hydra`/`qbit`/`ngnx`/`ollama` all `403` (still private), `jfin`
302, `sonarr` 200. Full NPM inventory re-checked — only proxy host IDs 1
and 25 changed this pass, all other hosts untouched.

**Public NPM attack surface from the original Phase 4A audit is now
fully closed:** qBittorrent, NPM admin, raw Ollama, NZBHydra2, JDownloader
noVNC, and the dead Firefox route are all private. MeTube is the one
formerly-open host now protected with auth instead of closed. HA and
Seerr remain the two deliberate, documented public exceptions.

---

## SECURITY INCIDENT — RAZR credential exposure via unauthenticated file server

**Phase 5A discovery / Phase 5B-1 remediation — 2026-08-15.**

**What was exposed.** A Phase 5A read-only audit of the non-dex247 hosts
found RAZR running `python3 -m http.server 9999 --directory /home/razr` —
an unauthenticated directory-listing file server over **RAZR's entire home
directory**, bound to `0.0.0.0` (so IPv4 + the machine's global IPv6),
with no host firewall in front of it. Started **2026-06-20 17:51 UTC**,
found **2026-08-15** — roughly **two months** of exposure. Confirmed
reachable from another LAN host; whether the router also forwarded it to
the WAN was never established (see "still unknown" below).

**Remediated.** Process killed (PID 61231, parent `init`). Port confirmed
closed from localhost and from a second LAN host. No persistence found —
no cron, no system or user systemd unit, no `rc.local`, no shell-rc entry,
no screen/tmux session, no `at` job (`at` isn't installed). It was a
detached manual `nohup`-style launch from a long-dead session, so it will
not return, including across a reboot.

**Credential rotation.** `/home/razr/.ha_token` (a Home Assistant
long-lived access token) was in the served directory and treated as
compromised. Dependency audit first: no active consumer anywhere on RAZR
reads that file — nothing in systemd, cron, Docker env, OpenClaw, Hermes,
or any script references the path or an `HA_TOKEN`-style variable. Rotated
anyway. Identifying *which* HA token record it was mattered, because the
UI listed seven long-lived tokens and revoking the wrong one would break a
live integration: decoded the old JWT's `iss` claim in-process (never
printed) and matched it against `auth/refresh_tokens` over HA's WebSocket
API, authenticated with the *new* token. Exact match on both the
refresh-token id and the creation timestamp (`2026-06-23 01:03:11 UTC`,
48s before the file's mtime) identified it as the UI entry named **`AGY`**
(Antigravity). Boss revoked `AGY` manually; old token then returned `401`
and the new one `200`. Old token file shredded.

**A note on method that will matter next time:** HA's UI shows
`last_used_at` equal to `created_at` for *every* long-lived token,
including one used successfully minutes earlier — so "last used" is
useless for identifying which token is which. The `iss`↔refresh-token-`id`
match is the reliable method.

**⚠️ SECOND EXPOSED CREDENTIAL — since ROTATED, see the L.O.K.I. section
below.** The post-rotation leak audit scanned RAZR for JWT-shaped strings
(matching on shape, never printing values) and found HA tokens stored in
plaintext inside **Antigravity/Gemini CLI conversation history**:

- `~/.gemini/antigravity-cli/history.jsonl`
- `~/.gemini/antigravity-cli/brain/8fe62e15-.../.system_generated/logs/transcript.jsonl` and `transcript_full.jsonl`
- `~/.gemini/antigravity-cli/conversations/8fe62e15-....db`
- `~/.gemini/antigravity-cli/conversations/b5669c0f-....db`

Most of those copies are the now-revoked `AGY` token (harmless). But
`b5669c0f-....db` contains a **different, still-valid** HA long-lived
token, identified by its `iat` of `2026-05-18 20:26:35 UTC` — an exact
match for the UI entry named **`L.O.K.I.`**. That token was equally
exposed by the `:9999` server for the same two months and **has not been
rotated.** It is presumably in active use by Loki's own HA integration, so
rotating it needs the same dependency-check-first treatment `AGY` got.
Conversation history was **not** deleted — that needs the Boss's approval
separately.

**Clean:** shell history, `/tmp`, process argv (verified with a corrected
check — an earlier version of the probe had a shell logic bug that
produced a false positive), env/config files, user journal, and every git
repo on RAZR (tracked and untracked) contain no JWT-shaped strings. The
new active token appears in none of the transcripts. `~/.ha_token` is
mode `600`, owned `razr:razr`.

**L.O.K.I. TOKEN ROTATION — COMPLETE, 2026-08-15.** The second exposed
credential is now rotated, validated, revoked and cleaned up.

*Consumers found (two, both confirmed by fingerprint — the `.env` value's
`iss`/`iat`/sha256 matched the `L.O.K.I.` HA record exactly):*
1. **`loki.service`** ← `/home/g2k247/loki-bot/.env`. `ha_integration.py`
   reads `HA_TOKEN` at **module level** (line 19), so it's bound at import
   → **restart required**. This is the only module in the whole repo that
   reads the variable; `loki_bot.py` references `ha_integration.HA_TOKEN`.
2. **skillkit** ← `/home/g2k247/skillkit/config/skillkit.env`.
   `ha_automations.py` reads `os.environ` **per call**, and skillkit is
   CLI/cron-invoked with no daemon → **no restart needed**.

Ruled out as consumers: `homelab_monitor.py` (unauthenticated health GET
only), `presence_monitor.py`, `loki-homelab-api.service` (its env file
holds only `HOMELAB_API_*`), and everything on RAZR/NAS.

*Sequence used (old token deliberately kept alive until the very end):*
backed both env files up outside git (mode 600) → updated only the
`HA_TOKEN` line in each, in-process via Python with atomic `os.replace`
and mode/ownership preserved (line counts verified identical: 150 and 15)
→ validated skillkit **first**, since it needs no restart, proving the new
token before touching production → Boss approved → `systemctl restart
loki` → full validation → Boss revoked the old token → negative/positive
test → cleanup.

*Validation results:* new PID, `NRestarts: 0`, `:9100` webhook rebound,
Discord gateway connected, Telegram online as `@Leauxki_Bot`, presence
monitor online reporting live resident states (which is itself proof the
daemon made authenticated HA reads on the new token). `get_state` →
`person.kavaris` = `home`. `get_all_states` → **434 entities**. skillkit
`list_ha_automations()` → 31 automations. **Zero HA auth errors** in the
journal. One `403` matched the error grep — the *Discord* weekly-export
`Missing Permissions` issue, which also occurred before this restart and
is a separate known UNFINISHED item, not HA. No unrelated service was
restarted. No state-changing HA action was ever invoked (no
`call_service`, alarms, doorbell, TTS).

*Note on proving which token a running daemon holds:* `/proc/<pid>/environ`
does **not** show it — `python-dotenv` loads at runtime and doesn't rewrite
the initial environ block. Functional evidence (successful authenticated
HA reads after restart + zero 401s + `.env` mtime predating the restart) is
the reliable proof. Also, as with `AGY`, HA's `last_used_at` never updates
for long-lived tokens, so the JWT `iss` ↔ refresh-token `id` match remains
the only dependable identification method.

*Final state:* old token → **HTTP 401** (revoked, confirmed). New token →
**HTTP 200**. Both production env files verified byte-identical to each
other and mode 600.

*Cleanup performed (all shredded, not just unlinked):* `.ha_token_new`;
the two stale `.env.bak*` files that contained the old token; both
timestamped rotation backups (they contained the now-dead credential and
rollback was no longer needed); and on RAZR the five Antigravity/Gemini
files holding revoked tokens — `conversations/b5669c0f-….db` (L.O.K.I.),
`conversations/8fe62e15-….db`, `history.jsonl`, and the two
`brain/8fe62e15-…/transcript*.jsonl`. **Deleting those cost the
corresponding Antigravity conversation history** — accepted deliberately;
they were conversation records, not configuration, and in-place SQLite
surgery would have risked corrupting the DBs for no benefit.

*Residual scan:* zero copies of either revoked token remain anywhere on
dex247 (`loki-bot`, `skillkit`, `.config`, `docker`, `bin`, `/tmp`) or
RAZR (`/home/razr`, `/home/hermes`, `/tmp`). Shell history clean, process
argv clean, nothing token-shaped in git.

**Six more world-readable `.env.bak*` files — RESOLVED 2026-08-15
(Phase 5B-3).** `.env.bak.1776992428`, `.1776993304`, `.1776993608`,
`.20260321_213308`, `.20260427-231230`, `.20260428-234835`. They contained
**no HA tokens** (they predate both `L.O.K.I.` and `AGY`) but each held
~7 other credentials, and — the part that made this urgent rather than
cosmetic — **6 of the 7 secrets in each were still the *live* production
values**, not rotated-away history. All six were world-readable (mode 664;
`.1776993608` was mode **666, world-writable**).

Classification found all six were class **D** (stale secret-bearing, no
current purpose): no systemd unit, script, cron entry, or Docker config
referenced any of them by name — the only hits were policy documents
warning that `.env.bak*` files hold secrets, plus this changelog. They
also weren't viable rollback targets even in principle: each held 24-29
variables against the live `.env`'s 64, so restoring one would have
dropped 35+ settings. No two were byte-identical. All six `shred -u`'d.

**Broader sweep of `/home/g2k247` for other credential-bearing backups**
found two more (both now `chmod 600`, neither deleted — they're active
config, not stale backups):
- `schedule-parser/credentials.json` — was mode **644**, holds a Google
  OAuth `client_id`/`client_secret`.
- `.claude/projects/…/memory/reference_npm_credentials.md` — was mode
  **664**, holds the **NPM admin password in plaintext**.

A third, `backups/maintenance-checkpoint-2026-07-25/loki-bot.env.bak`
(16 secret keys), was left untouched: it's already mode 600 and is part of
a deliberate, named maintenance checkpoint rather than the stale-backup
pattern — deleting it is the Boss's call, not an automatic cleanup.

**⚠️ CREDENTIAL LEAKED DURING THIS CLEANUP — NPM admin password needs
rotation.** While inspecting `reference_npm_credentials.md`, the redaction
regex used to preview it was faulty: it matched the *label*
(`Password:**`) and redacted only the trailing asterisks, printing the
actual password into the session transcript. The file itself was fine; the
inspection was not. **That NPM admin password must be treated as
compromised and rotated**, and the plaintext copy in that memory file
should be removed or replaced with a pointer to a secret store. Lesson:
never regex-redact a file to preview it — check for the *presence* of
secret-shaped content and report key names only, or don't print the file
at all.

**Still unknown / open:**
- Whether `:9999` was reachable from the WAN during those two months, or
  only from the LAN. The router's port-forward table has never been
  inspected (no admin credentials) — this is the same blocker as Phase 4A.
  Until that's known, assume the exposure may have been internet-wide.
- Whether Antigravity itself needs an HA token at all going forward (the
  `AGY` token it was presumably using is now dead; nothing on the box was
  actively reading the file).
- ~~**NPM admin password rotation** (leaked in-session, see above), plus
  removing the plaintext copy from the `.claude` memory file.~~
  **RESOLVED 2026-08-23 (Phase 5B-4)** — see the Phase 5B-4/5C section below.
- Whether `backups/maintenance-checkpoint-2026-07-25/loki-bot.env.bak`
  should be kept — it holds 16 live secrets, mode 600, deliberate
  checkpoint.
- RAZR still has no host firewall, SSH password auth enabled, and Ollama
  on `*:11434` — all P1 findings from Phase 5A, none remediated yet.

---

## NPM database recovery, credential rotation, and public-access restoration

**DONE (Phase 5B-4 + 5C) — 2026-08-23.** Closes the credential leak from
Phase 5B-3 and restores/validates the three public user-facing services.

### The orphaned SQLite inode (root cause of everything below)

NPM's node process was holding a **deleted/orphaned `/data/database.sqlite`
inode**. An earlier session had replaced the live database file underneath the
running container (a `docker cp`-class overwrite), so the process kept its
handle on the old, unlinked inode while a *different* file occupied the path.
Consequences, both confirmed rather than assumed:

- Every NPM UI/API write landed in the orphaned copy and would have been lost
  at the next restart.
- The real on-disk database — which held the Phase 4C/4D hardening — was **not
  what NPM was serving from**. `firefox`, `jd`, and `hydra` were therefore still
  publicly reachable and MeTube still had no auth, despite the database on disk
  saying otherwise. The hardening had never actually taken effect.

Before restarting, both databases were captured and compared: both passed
`PRAGMA integrity_check`, `audit_log` was identical at 253 rows (so no admin
writes were at risk), and the on-disk file was a strict superset (it alone had
the MeTube access list). Adopting it lost nothing.

A **controlled container restart** resolved it. NPM reopened the real inode
(verified: fd 18 → `/data/database.sqlite` with no `(deleted)` marker), all 25
proxy-host configs regenerated, and `nginx -t` passed. NPM's own generator emits
`return 403` for disabled hosts, so the Phase 4C/4D lockdowns are now enforced by
NPM itself rather than by hand-edited confs that regeneration would have wiped.

Write persistence was then **proven, not assumed**: a subsequent UI save moved
the DB mtime and size, added the expected `audit_log` row, and regenerated that
host's conf in the same second.

> **Rule — do not repeat this:** never replace a live SQLite database by copying
> over it (`docker cp` or `cp`) while the service is running. The process retains
> the old inode and silently diverges from the file on disk. Use the supported
> API/UI, or stop the service first.

### NPM admin credential rotation (Phase 5B-4)

The password leaked in Phase 5B-3 was rotated through **NPM's supported API**
(`POST /api/tokens` → `PUT /api/users/1/auth`) — no SQLite edit, no restart.

- New credential authenticates (`200`); **old credential rejected**. NPM returns
  `400 error.invalid-auth` rather than `401` for bad credentials — confirmed by
  control tests that the old password now yields the identical response to a
  deliberately-wrong one, and a distinctly different one from a malformed
  request. It is genuinely revoked, not a false pass.
- The `auth` bcrypt hash changed, and the change persisted to the live inode.
- The plaintext copy in the `.claude` memory file was replaced with a pointer
  (that file lives under the `-home-g2k247` project memory dir, not
  `-home-g2k247-loki-bot`). The temporary local credential file was shredded
  after the Boss saved the password to his password manager. No plaintext copy
  remains on disk.
- **NPM admin remains private** — `:81`, LAN or Tailscale only, and DROPped from
  the WAN at `DOCKER-USER`.

### Jellyfin — `jfin.ivn-group.cc` (Phase 5C)

**Third deliberate, permanent public exception**, alongside Home Assistant and
Seerr. It must work from phones and TV clients that don't run Tailscale — **do
not "fix" this by taking it private.**

- Backend `192.168.1.155:8096`; `:8096` DROPped from WAN at `DOCKER-USER`.
- **No NPM Basic Auth** (`access_list_id=0`, zero `auth_basic` in the conf) —
  Jellyfin's own login is the boundary; unauthenticated API paths return `401`
  from Jellyfin.
- **WebSockets Support enabled** and persisted; the generated conf carries
  `Upgrade` / `Connection` / `proxy_http_version 1.1`. Verified behaviourally,
  not just textually.
- Cert npm-37 valid to 2026-11-04. Redirects stay on the public hostname
  (`location: web/`) — no internal hostname or IP leak.
- **Cellular validation passed with Tailscale off** (incognito): login, library
  browsing, playback, and seeking.

### Nextcloud — `cloud.ivn-group.cc` (Phase 5C)

Intentional public exception; public share links must work for recipients on
external networks.

**Found and fixed a database/config drift.** NPM's database pointed at a stale
`192.168.1.247:8082` — a host that answers ping but runs nothing on that port —
while the live generated conf correctly used `192.168.1.63:8082` (the NAS, as
the Joplin domain table has documented all along). Traffic followed the conf, so
Nextcloud worked; but the next genuine regeneration would have written the dead
address and taken it down. This was a latent outage, not a cosmetic mismatch.

Corrected to `192.168.1.63:8082` **through the NPM UI**. Database and generated
conf now agree, `nginx -t` passed, and the existing large-file directives
(`client_max_body_size 55G`, 600s connect/send/read) survived regeneration
unchanged. Nextcloud reports healthy (34.0.3, not in maintenance) and WebDAV
`PROPFIND` returns `401` with `realm="Nextcloud"` — the app's own auth, not NPM's.

**Cellular validation passed:** login, browsing, download, upload. A **public
share link also worked from cellular with Tailscale off and without logging in**,
which validates Loki's private-download → Nextcloud-public-share workflow for
external recipients.

### Immich — `immich.ivn-group.cc` (Phase 5C) — client validation PENDING

Server-side audit passed: backend `192.168.1.155:2283` (matches DB exactly),
HTTPS with Force SSL, HTTP/2, WebSocket support, cert npm-45 valid to
2026-11-04, **no NPM Basic Auth**, and `:2283` DROPped from WAN. A real
socket.io upgrade through NPM returned **`101 Switching Protocols`**, byte-identical
to the backend. `/api/server/about` returns `401` — Immich's own auth is the
boundary. No media, users, libraries, or app settings were touched.

**⚠️ Immich cellular/client validation is still PENDING — do not record Immich
remote access as fully validated.**

*Improvement item, not a regression:* Immich has no per-host overrides, so it
inherits NPM's global **`client_max_body_size 2000m` and 90s timeouts**
(Nextcloud overrides these; Immich doesn't). Very large or slow uploads may fail.
Adding the same directives Nextcloud uses would fix it — it has run this way
since 2026-03-09, so this is an improvement, not something newly broken.

### Cert-renewal noise (observed, not actioned)

NPM force-renews on startup and hit Let's Encrypt rate limits for `npm-63`
(notes) and `npm-83` (chat) — **both certs are actually valid to 2026-11-21**, so
this is harmless today but burns quota that a genuinely-needed renewal might
want. `npm-68` (obsidian) is genuinely expired (2026-07-28) and **no proxy host
uses it**, which is also why its challenge fails. Deleting that stale cert would
quiet the sweep.

---

## NextDNS over Tailscale on Android

**DONE — 2026-08-23, Boss-side configuration.** Android's Private DNS and
Tailscale previously competed for DNS, forcing a manual switch between NextDNS
ad blocking and Tailscale being on.

The existing NextDNS profile is now configured as a **Tailscale global
nameserver** (via NextDNS's IPv6 endpoint) with **"Override DNS servers"**
enabled. Tested on the Samsung Galaxy S23 Ultra: `test.nextdns.io` returned
`status=ok`, `protocol=DOH`, `clientName=tailscale`.

**Result: Tailscale can stay enabled full-time while retaining NextDNS ad
blocking.** The profile ID and endpoint address are deliberately not recorded
here or in Joplin — retrieve them from the NextDNS dashboard.

---

## NVIDIA Shield — unattributed ADB authorization prompts

**UNRESOLVED observation — logged 2026-08-23.** On the morning the earlier
homelab compromise was discovered, the Shield displayed roughly **five
unsolicited ADB authorization prompts**, and **"Always allow" was selected each
time** — so those keys are now trusted. The computer previously known to have
used ADB with the Shield **was already dead** at the time, and no other
authorized ADB client has been identified.

**This is NOT proof of lateral movement** — ADB prompts have mundane causes. But
given the timing it is retained as an **incident-timeline artifact requiring
attribution**.

**Preserve the evidence before revoking.** Attribute the five grants via the
Shield's ADB keys and connection history *first*; revoking blindly destroys the
only record that could explain them. Revoke anything illegitimate afterwards.
Tracked as issue **#11** in the Joplin `Homelab/Issues → Issues Log` note.


## Video-doorbell announcement reliability

**DONE — 2026-08-10, live on the NAS Home Assistant instance (192.168.1.63:8123),
confirmed audible by the Boss in person.** Real failure reported: the
doorbell rang, the Boss was home, and the Google Home speaker never
announced it — a visitor was missed. Framed explicitly as a reliability
problem, not a wording problem. No Loki code is involved anywhere in this
path (confirmed by grep across the repo) — the entire chain lives in Home
Assistant: `binary_sensor.front_door_doorbell` (Tapo, device class `sound`)
→ `automation.doorbell_announce_on_bedroom_clock` (config id
`doorbell_announcement`) → `script.doorbell_announce` → `media_player.clock`
(Google Home speaker, friendly name "CLOCK") via `media_player.play_media`
(chime) + `tts.speak` (message).

**This closed in two passes.** The first pass (documented below as "initial
fix") looked complete from every signal available via the HA API — but a
live audible test with the Boss physically listening proved it wasn't. The
second pass found the actual remaining root cause, which the first pass's
own tooling had no way to detect. Both are recorded here because the gap
between "HA says success" and "a human actually heard it" is itself the
lesson: **HA state transitions and logbook entries prove HA dispatched a
command — they do not prove a physical speaker made sound.** The Boss's
explicit instruction not to claim success from state/log evidence alone is
exactly why this didn't ship broken.

**Initial fix — two confirmed defects, real but incomplete.**
1. Both chime steps in `script.doorbell_announce` hardcoded
   `media_content_id: http://192.168.1.247:8123/local/doorbell.mp3` — the
   decommissioned pre-rebuild asus/unicron address (the same stale-IP bug
   class already found and fixed twice this session in
   `nextcloud_integration.py` and the JD integration). Switched to
   `media-source://media_source/local/doorbell.mp3`, believed at the time to
   be host-independent.
2. `automation.doorbell_announce_on_bedroom_clock` had **no condition block
   at all** — added `condition: state person.kavaris == home`.
3. Added a purely-additive `automation.doorbell_sensor_diagnostics`
   (logs every raw sensor transition via `logbook.log`, independent of the
   announce automation's own trigger/condition — still live and useful).

Validated via `POST /api/states/binary_sensor.front_door_doorbell` (a real
state-change event, not a direct TTS call) — trigger fired, condition
passed, script ran, `media_player.clock` transitioned states correctly, zero
errors. **This all looked like a clean pass and was reported as fixed.**

**It wasn't. The Boss listened in person and heard nothing.** That live
audible test is what actually caught the remaining bug — none of the HA-side
signals (state transitions, logbook, `last_triggered`) had any way to
surface it, because HA was truthfully reporting what *it* did successfully;
it just didn't reveal that the fetch/playback in between never worked.

**Real root cause, found via direct speaker-side diagnosis:**
`media-source://` resolution and the `tts_proxy` URL Home Assistant
generates for every `tts.speak` call are **both** built from
`hass.config.internal_url` — which was *also* still `http://192.168.1.247:8123`
(confirmed live in `/api/config`; flagged in the first pass and
incorrectly judged out of scope). Switching the script to a media-source URI
never actually decoupled it from the dead host — HA just resolves that URI
back through the same broken setting before handing a URL to the Cast
device. Direct proof: `homeassistant.components.cast.media_player` logged
`Failed to cast media http://192.168.1.247:8123/... from internal_url` as an
explicit ERROR on every real attempt, chime and TTS alike. A literal,
correct, already-reachable URL (`http://192.168.1.63:8123/...`) and a fully
external public test URL both played back with clean Cast telemetry
(`buffering`→`playing`→`idle`, real `media_duration` reported by the
device), proving the Cast device, network path, and HA↔device connection
were never the problem — only `internal_url`-dependent resolution was. HA
does not expose a way to change `internal_url`/`external_url` via REST; it
required the WebSocket API's `config/core/update` command. Corrected to
`http://192.168.1.63:8123` — a value that was fully dead could only be
improved by fixing it, and it likely affected other local-media/TTS casts
in this HA instance beyond just the doorbell.

**Volume was a real secondary factor, caught along the way.** The bedside
clock's volume sits at 33% day-to-day (fine for voice interaction the Boss
uses daily — timers, alarms, light control, all confirmed working
natively), but wasn't reliably audible for a one-shot announcement from
another room. The script now snapshots the current volume into a script
variable, bumps to 90% before the chime, and restores the original value at
the end — confirmed by direct read-back after a full run
(`0.33000004291534424`, matching the pre-run value).

**One more real bug caught live:** a first attempt at repeating the TTS
message with a requested 1.5s gap used a flat `delay: 1.5s` between the two
`tts.speak` calls — but `tts.speak` returns as soon as HA *dispatches* the
command, not when the device finishes playing (~1.4–1.7s for this phrase),
so the fixed delay mostly overlapped the first message's own playback and
left almost no audible gap. Fixed by waiting on
`wait_template: is_state('media_player.clock', 'idle')` (with a timeout)
before the 1.5s delay, so the gap is measured from actual playback
completion, not from dispatch — confirmed correct via logbook timing
(~1.67s from `idle` to the next `buffering`) and by the Boss's own ear.

**Final validated script sequence:** snapshot volume → bump to 90% → settle
2s → chime → 2.5s → TTS "Someone is at the door" → wait for idle + 1.5s gap
→ TTS repeat → 1.8s → chime → 1.5s → restore original volume.

**Validation — real event path throughout, never a direct TTS call to fake
success.** Every test (initial pass and the live debugging session) fired
`binary_sensor.front_door_doorbell` via the states API to exercise the real
trigger→condition→script chain. The final, Boss-confirmed-audible run: clean
trigger, condition pass, `buffering`→`playing`→`idle` Cast cycles for both
chimes and both TTS repeats with the correct gap, sensor restored to `off`,
volume restored to its original value, zero errors. Confirmed no other
automation or entity was touched throughout: 434 entities / 31 automations
before and after every change (only the one new diagnostic automation
added, once, in the first pass).

**What couldn't be fully ruled out without a physical press:** the Tapo
sensor's device class is `sound` (general sound classification), not a
dedicated button-press signal, so a very quiet or muffled real press could
still be missed by the camera's own detection threshold — that's Tapo
hardware/firmware behavior outside HA config and outside this repo's scope
to tune. The diagnostic automation makes this observable going forward
without needing another investigation.

**Files/config changed:** HA config only — `script.doorbell_announce`,
`automation.doorbell_announcement`, new `automation.doorbell_sensor_diagnostics`,
and the HA core `internal_url` setting (`config/core/update` via WebSocket).
No files in this repository changed except this documentation.

**Lesson recorded because it's likely to recur:** for any future Cast/media
work in this HA instance, `media_player` state transitions and
`media_duration` telemetry are not sufficient proof of audible output —
Home Assistant can report a fully successful-looking cast (correct states,
real device-reported duration) for a URL the Cast device silently couldn't
fetch. Only a human listening, or a Cast-protocol-level error line
(`homeassistant.components.cast.media_player` at ERROR), reveals the
difference.

---

## Hermes resilient diagnostic capability (provider fallback)

**DONE — 2026-08-10, live on razr and dex247.** Goal: give Hermes a second
diagnostic provider so OpenRouter running out of credit doesn't remove all
ambiguous-diagnosis capability, without weakening the existing safety
architecture. **Final architecture, cost-tiered per the Boss's explicit
instruction:** local Ollama first (free), a cheap OpenRouter model second,
frontier/expensive models reserved for manual escalation only — never
automatic.

**Provider state, verified read-only, no paid probe.** `hermes auth list` on
razr (as the `hermes` account) showed `OPENROUTER_API_KEY ... exhausted (402)
(ready to retry)` — matches the already-known protective-degradation state
(`hermes-provider` incident, one, open, `billing`). Confirmed live in
production: `hermes_guard.status()["status_label"] == "protective_quota"`
against the real `homelab_incidents.db`, no test fixture.

**Fable investigated at length, then explicitly rejected — this is settled,
do not reopen.** First pass: Hermes Agent has a native Anthropic provider
adapter that recognizes `claude-fable-5`; withdrawn immediately when the Boss
said they don't hold an Anthropic account — no credential was ever
requested or added. Re-investigated from the Boss's actual OpenRouter
account: `anthropic/claude-fable-5` genuinely is a live-listed OpenRouter
model (confirmed via OpenRouter's public `/v1/models`), reachable on the
existing key with no new credential — but a fresh, non-billable balance read
on that key (mirroring the bridge's own spend-probe mechanism) showed only
$0.36 remaining against a $10 cap, nowhere near enough for Fable's
$10/$50-per-million pricing. The Boss then ruled it out on cost grounds
entirely: *"too expensive for routine Hermes diagnostics — I only used it
previously because of a temporary promotional credit arrangement."* No Fable
model appears anywhere in the final configuration.

**A real bug found and fixed while tracing the provider-call path
end-to-end.** `homelab_hermes.note_job_state()` — the function that turns a
polled bridge-job view into a guard signal — handled `completed`,
`paused_quota`, and `failed`, but never `paused_auth` (the bridge's own state
for "provider authentication failed — re-authenticate on razr",
`server.mjs`'s `classifyFailure` "auth" category). A bad or revoked provider
credential therefore left the job parked in `paused_auth` forever without
`hermes_guard.py` ever finding out — the circuit stayed closed and Loki kept
submitting new jobs against a credential that could never succeed. Fixed:
`hermes_guard.classify()` gained an `auth` class, checked before `rate_limit`;
`auth` failures open the circuit **instantly**, the same as `billing` (a bad
credential doesn't self-resolve by waiting, so neither should wait for 3
consecutive failures); recovery for both uses the bridge's non-billable
`GET /health` **only for reachability-class outages** — billing and auth
outages get exactly one controlled submit as the probe, because `/health`
never calls the model provider and can't prove a quota refilled or a key was
rotated.

**Status vocabulary added, from existing state — not a parallel system.**
`hermes_guard.status()` gained `status_label`, computed from the existing
circuit state + reason_class: `operational` (closed), `recovering`
(half_open), `protective_budget` (Loki's own request/spend ceiling),
`protective_quota` (provider billing/quota), `authentication_failed` (new
`auth` class), `rate_limited`, `unreachable` (generic). Deliberately never
asserts `unavailable_all_providers` — this guard only sees one aggregate
signal (did the job succeed or fail), not which of Hermes Agent's own
configured providers on razr actually served or refused it.

**Cost-accounting gap found and surfaced, not fixed — still applies to the
real configured fallback, not just the withdrawn Fable plan.** Traced the
bridge's own spend accounting (`~/hermes-bridge/lib/budget.mjs`'s
`ratesFromEnv()`, `lib/usage.mjs`'s `makeSpendProbe()`) and found it prices
only `anthropic/claude-sonnet-5` and `anthropic/claude-opus-5`, and its spend
probe is hard-coded to OpenRouter's own balance endpoint. A job phase served
by **either configured fallback** — local Ollama or
`deepseek/deepseek-v4-flash-0731` — prices at **$0** in the bridge's own
ledger (correctly for Ollama, incorrectly for DeepSeek, which does have real
nonzero cost). Fixing this needs a bridge-side change on razr; the Boss was
asked and explicitly chose to keep this dex247-only rather than edit and
restart a second production service on a second machine.
`hermes_guard.status()` carries `last_serving_model` and
`last_serving_model_cost_telemetry` (`"reliable"` for the two priced models,
an explicit `"unreliable — ..."` message naming the gap for anything else) so
the guard never silently implies a $0 job actually cost nothing. The
deterministic ceilings that don't depend on price — Loki's own 6/hour, 20/day
request-count caps; the bridge's `max_turns` (triage 8, escalation 14),
phase/job timeouts, `maxConcurrent: 1` — stay fully effective regardless.

**Provider fallback chain — configured and verified live on razr, 2026-08-10
(after the Fable investigation above; separate from the dex247 code work).**
Investigated read-only first: local Ollama on razr (v0.20.5) is reachable,
and `gemma4-12b-balanced:latest` reports native `["completion", "tools",
"thinking"]` capabilities via Ollama's own `/api/tags`. Hermes Agent's
`custom` provider profile is documented in its own source as covering "any
endpoint registered as provider='custom', including local Ollama instances."
Confirmed in `agent/conversation_loop.py` (not just the config file's own
comment, which is incomplete) that `FailoverReason.billing` — HTTP 402,
exactly OpenRouter's current failure — **is** in the eager-fallback trigger
set, with a built-in guard against retrying a depleted balance once every
recovery path is exhausted (a real prior incident, "#31273 ... ~$40 in 48h on
a 24/7 gateway," is cited in that code as the reason it exists).

Pulled current OpenRouter pricing (public `/v1/models`, free) for a cheap
second tier once local proved insufficient-for-context reasoning would be
needed: picked `deepseek/deepseek-v4-flash-0731` — $0.08/$0.18 per million
tokens, 1M context, reasoning optional (not forced, unlike some cheaper
options), pinned dated version rather than a floating `~latest` alias.

`hermes config set` cannot construct a new list-of-dicts key from scratch
(confirmed in `hermes_cli/config.py` — `_set_nested` only indexes into
existing structure) and the interactive `hermes fallback add`/`hermes model`
pickers refuse to run outside a real terminal (confirmed by testing).
Configured instead via a direct, schema-verified edit to
`/home/hermes/.hermes/config.yaml` (Hermes Agent's own config — read fresh
per CLI invocation, no service restart needed anywhere), using exactly the
`fallback_providers` list schema its own `hermes_cli/fallback_config.py`
consumes. Two backups taken before editing. The OpenRouter entry carries no
inline credential — omitting `api_key`/`key_env` falls through to the
already-configured pooled `OPENROUTER_API_KEY` (confirmed in
`hermes_cli/cli_agent_setup_mixin.py`), so no new credential was added
anywhere.

Final chain, verified through Hermes Agent's own commands
(`hermes fallback list`, `hermes config get --json`, `hermes config check`,
`hermes doctor` — all clean, no new warnings):

```
Primary:    anthropic/claude-sonnet-5        (via openrouter)
Fallback 1: gemma4-12b-balanced:latest       (via custom → local Ollama, $0)
Fallback 2: deepseek/deepseek-v4-flash-0731  (via openrouter, existing key)
```

No frontier/expensive model in the automatic chain, per instruction. No
Anthropic credential anywhere. No paid Hermes job submitted at any point —
confirmed via the bridge's own job counter (200 before and after) and the
unchanged `hermes-provider` guard state on dex247.

**Not touched, deliberately:** `~/hermes-bridge/server.mjs` and friends on
razr (the Boss's choice); `hermes_guard.py`'s circuit state machine itself
was extended, not redesigned — the existing single-circuit "provider
degraded" semantics already become "all configured providers degraded" once
a fallback chain exists, since the bridge only reports `paused_quota` after
every provider it tries is exhausted.

**Testing — no paid spend.** 15 new tests in `tests/test_hermes_guard.py`
(now 45, all green) covering the dex247-side guard changes: `paused_auth`
opens the circuit immediately; auth-class recovery uses one controlled
submit, never the health probe; every `status_label` mapping; provider-hint
recording and its cost-telemetry flag, including restart persistence. All 30
pre-existing tests still pass unmodified. Live validation against the real
production `homelab_incidents.db`, a real non-billable `GET /health` to the
bridge, and (for the razr-side fallback chain) Hermes Agent's own read-only
CLI commands — all zero cost. Confirmed deterministic local maintenance
(BLACK-BOXX runbook, read-only) is unaffected and independent: 17/17 checks
green regardless of provider circuit state.

**Not yet live (dex247 only):** `hermes_guard.py` and `homelab_hermes.py`
changes are code-only until the next `loki.service` restart — not taken,
restarts stay approval-gated. **Already live (razr):** the fallback chain
itself — Hermes Agent reads its config fresh per invocation, no restart
involved. See `.agents/skills/hermes-operations/SKILL.md` for the full
architecture.

---

## Tracearr registry version drift

**DONE — 2026-08-09; corrected 2026-08-10.** The version/digest reconciliation
below is accurate. The explanation of *who* moved v1.5.0 → v2.0.1 was not:
this entry originally attributed it to watchtower. It was Loki's own
approval-gated `tracearr_update` — see the correction at the top of
`CURRENT_HANDOFF.md` for the evidence (a Joplin note Loki wrote at update
time, matching the container's recreate timestamp to the minute). Left the
rest of this entry as written at the time; read it with that correction in
mind.

Not an upgrade *from Loki's perspective at the time this was written* — no
update, restart, recreate, or pull was believed to have been performed by
Loki. Tracearr was already healthy on v2.0.1; only
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

The uncommitted value matched exactly. **Why it happened** — this paragraph as
originally written said the registry's `updates.applied_by: watchtower-on-nas`
meant watchtower did it, "the already-tracked `watchtower → monitor-only`
backlog item behaving exactly as documented: a MAJOR version bump landed
without going through approval." **That was wrong — corrected 2026-08-10.**
`applied_by: watchtower-on-nas` was stale YAML boilerplate never touched by
Loki's real update code; `container_inventory` showing watchtower merely
running on the NAS only proves presence, not causation. Loki's own Joplin
maintenance log has a note it wrote at the time — "Tracearr update v1.5.0 →
v2.0.1 — success", `prepare_id e7f4e2ef6f381073`, started
`2026-08-07T18:46:06Z` — proving its own `tracearr_apply_update` performed
this update, backed up and verified, through the existing approval gate. See
the top of `CURRENT_HANDOFF.md`. The registry field is now
`applied_by: loki_approval_gate`.

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

**DONE — roommate presence gets the same treatment — 2026-08-10.** The four
transitions above only covered the Boss's own presence. Rob's arrival/
departure wasn't pre-voiced by Home Assistant at all — a plain factual
message (e.g. "Ammiel is home. You are free to lock the top lock.") still
went through the Groq rewriter and came back narrating the Boss's own
already-known presence on top of Rob's: "Boss, your roommate has left the
premises while you are still at home."

Fix, in `personality.py`: any notification that names the roommate
(`ammiel`/`roommate`/`rob` as a whole word — regex, not an exact-fragment
match, since HA's wording for this event isn't fixed the way the Boss's four
are) is now classified as `ROOMMATE_PRESENCE` and bypasses the rewriter the
same way. The reply is built from **Rob's actual live state** at delivery
time, never from parsing HA's phrasing — `roommate_presence_text()` returns
`"Rob is home."` or `"Rob stepped out."` Three differently-worded raw HA
messages for the same event consolidate to the identical canonical output
(pinned by test). If Rob's state can't be resolved (HA unreachable), the
raw HA text is relayed rather than guessing a direction or narrating one.

Boss's own presence is never mentioned in this message — that was the whole
bug. The `roommate_line()` used inside the Boss's own welcome-home message
(the top-lock decision, e.g. "Rob's home — top lock's good.") was already
concise and correct and is unchanged.

No trigger conditions, HA automations, or notification volume changed —
this is wording-only, one notification in, one reply out, same as before.
12 new tests added to `tests/test_presence_notifications.py` (now 32); two
pre-existing tests updated because they pinned the old (buggy) behavior of
letting roommate-named messages fall through to the rewriter.

**DONE — unnamed ("a person") presence detections resolved to a name —
2026-08-10.** The roommate-name fix above only caught messages that named
Rob. Some HA presence automations don't name either resident at all —
"Boss, a person has been detected at home." — which fell through to the
Groq rewriter with nowhere near enough information to say who, and often
stayed generic in the output too. Two real bugs found on the way, both
fixed:

1. The rewriter's own presence context line only ever queried
   `person.kavaris` — Rob's state was never included, so even a message
   genuinely about Rob had no way to be attributed correctly by the LLM
   fallback. Now includes both known residents.
2. The `HA_NOTIFICATION` system prompt's own example text used the phrase
   "a person detected" — the exact wording later showing up unwanted in
   real output. Reworded, and the prompt now explicitly says there are only
   two monitored residents and names them.

The real fix, though, is the same non-guessing principle as the roommate-name
fix: a new `personality.GENERIC_PRESENCE` kind matches generic phrasing ("a
person", "someone", "a household member" + a presence-shaped word, so a
doorbell/camera "someone" doesn't get swept in). Resolution happens in
`ha_integration._resolve_generic_presence()`, which compares each resident's
**current live state** against `presence_monitor`'s last-polled state (a new
public `presence_monitor.last_known()` accessor — presence_monitor's own
detection logic is untouched, this only reads its already-tracked state) to
find which one actually changed. Exactly one changed → that resident's
existing preferred wording (Rob's `roommate_presence_text()`, or the Boss's
own four phrases if it's actually about him). Zero or both changed, or HA is
unreachable → relay HA's raw text rather than misattribute the event to the
wrong person.

12 new tests in `tests/test_presence_notifications.py` (now 48), including a
direct regression pin on the exact reported message. No detection logic,
entities, triggers, or notification volume changed.

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

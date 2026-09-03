# Homelab Inventory

Four machines plus a NAS. Everything below is verified against live state or
current config as of 2026-08-09.

## DEX247 — primary host

| | |
|---|---|
| Role | Runs Loki itself, the BLACK-BOXX wireless AP, and most Docker services |
| Account | `g2k247` (uid 1000), full `NOPASSWD: ALL` sudo on this host |
| Loki repo = deployment path | `/home/g2k247/loki-bot` |
| Companion repo | `/home/g2k247/skillkit` (separate git repo, no remote) |
| Git remote | `git@github.com:arcadeon54/Loki-Bot.git`, branch `master` |

### systemd units owned here

- `loki.service` — `venv/bin/python loki_bot.py`, WorkingDirectory = the repo,
  `Restart=always`, NOT containerized. Logs: `journalctl -u loki -f` and
  `loki_bot.log`.
- `loki-joplin-desktop.service` — Joplin Desktop 3.7.9 headless under Xvfb,
  serves the Data API on `127.0.0.1:41184`. **Authoritative** Joplin runtime.
- `loki-homelab-api.service` — read-only maintenance API, the interface Hermes
  on razr talks to.
- `black-boxx-ap.service` — owns the ENTIRE AP stack (wg-ap, hostapd, dnsmasq,
  packet marking, table 100, policy rule, NAT) via
  `/usr/local/bin/black-boxx-start.sh`. `hostapd.service` and `dnsmasq.service`
  are masked on purpose.
- `canada-ap.service` — alternate AP profile, `disabled` + `inactive`.
  `Conflicts=` with black-boxx-ap.

### Cron (user g2k247)

- `20 */6 * * *` — `ingest_history.py` (RAG ingest)
- `0 13 * * *` — skillkit Advisor
- `30 13 * * 0` — skillkit CIE (weekly)

### Containers on dex247

`chromadb-chromadb-1` (:8100) · `searxng` (:8083) · Immich stack
(`/home/g2k247/immich/docker-compose.yml`, v3.0.3, DB `immich_postgres`) ·
Jellyfin · Joplin Server (`joplin` + `joplin-db`) · `watchtower` ·
`loki-joplin-api` (**stopped, obsolete, restart=no — leave it alone**).

**PrivacyServer stack** (`/home/g2k247/PrivacyServer/docker-compose.yml`):
`qbittorrent` (binhex/arch-qbittorrentvpn, own embedded WireGuard VPN, WebUI
:8080) · `sonarr` · `radarr` · `prowlarr` · `sabnzbd` · `jellyfin` ·
`flaresolverr` · `seer` · `bazarr` · `nzbhydra2` · `subgen` · `gluetun`
(VPN gateway for sonarr/radarr/prowlarr/sabnzbd — **qBittorrent is NOT behind
gluetun; it has its own VPN**). Config root: `/home/g2k247/PrivacyServer/config/`.

### BLACK-BOXX network facts

`wlp2s0` = AP interface, `192.168.10.1/24`, clients `192.168.10.0/24`.
`wg-ap` = WireGuard tunnel, `100.64.145.100/32`. Policy routing: fwmark 100
(`0x64`) set on wlp2s0 ingress → table 100 → default via wg-ap, priority 100,
plus MASQUERADE for the client subnet. Config lives in `/etc/wireguard/wg-ap.conf`,
`/etc/hostapd/black-boxx.conf`, `/etc/dnsmasq.d/black-boxx-ap.conf`.

## RAZR — escalation / evaluation host

| | |
|---|---|
| Role | Hermes escalation agent, Career-Ops evaluation, browser research worker |
| Access | `ssh razr` from dex247 (`~/.ssh/config` alias, user `razr`, key auth) |
| Network | LAN `192.168.1.31`, Tailscale `razr-1` / `100.87.97.120` |
| **Headless** | No monitor, no desktop, no X11/Wayland. SSH only — never assume `DISPLAY`. |

- **Hermes Agent v0.19.0** runs under a dedicated `hermes` system account.
  Bridge repo `/home/razr/hermes-bridge` (local only, commits e9f9cc2 + 8c45b4d).
- **OpenRouter** is Hermes' model provider. Sonnet 5 for triage, Opus 5 only on
  a justified escalation. The API key lives on razr, not on dex247.
- **Career-Ops bridge** — `/home/g2k247/career-ops-bridge/` was built on dex247
  and is deployed to razr: Node built-ins only, bearer auth, URL allowlisting,
  one-job queue, durable `jobs.json`. Uses `agy -p` (Antigravity, economy tier)
  as its worker — never Claude. Two isolated workspaces under
  `~/career-ops-users/{boss,roommate}`.
- **Browser research worker** — razr serves `browser_research` for Loki.
- Local models / Ollama live here rather than on dex247.

### RAZR storage architecture (verified 2026-08-08)

Two NVMe SSDs, **neither is a rotational SATA disk**:

| Device | Model | Size | Role |
|---|---|---|---|
| `nvme1n1` | Samsung MZVLB256HBHQ | 238.5 GB | Linux OS/LVM drive |
| `nvme0n1` | Crucial CT1000P1SSD8 | 931.5 GB | Carry-forward NTFS data — **not used by Linux services, not in fstab** |

**Samsung LVM layout:**
- PV `/dev/nvme1n1p3` — 235.42 GiB, fully partitioned
- VG `ubuntu-vg` — 235.42 GiB (VG fully allocated to LV after 2026-08-08 expansion)
- LV `ubuntu-lv` — **235.42 GiB** (was 100 GiB, expanded online 2026-08-08)
- Root filesystem `/`: ext4, **232 GB total, ~29% used (~64 GB)**

**Crucial NVMe** — single NTFS partition (`/dev/nvme0n1p1`), 932 GB, 56% used
(521 GB carry-forward Windows data, 412 GB free). Not mounted at boot. No Linux
service depends on it. Do **not** describe it as a SATA drive.

### RAZR Ollama (verified 2026-08-08)

- Ollama v0.30.9, service user `ollama`, home `/usr/share/ollama`
- Model store: `/usr/share/ollama/.ollama/models/` (default `$HOME/.ollama/models`)
- No `OLLAMA_MODELS` env override
- 7 registered models: `gemma4-12b-balanced:latest`, `llama3.2:latest`,
  `gemma4-uncensored:custom`, `fredrezones55/Gemma-4-Uncensored-HauhauCS-Aggressive:e2b`,
  `fredrezones55/Gemma-4-Uncensored-HauhauCS-Aggressive:e4b`, `gemma2:9b`, `qwen2.5:7b`
- Source GGUF `/home/razr/gguf-models/Gemma4-12B-QAT…Q4_K_M.gguf` (6.9 GB) —
  NOT the same bytes as the active Ollama blob; archival source only
- Active Gemma4 blob: `sha256-8ebc…` (6.9 GB, referenced)
- Orphan blob `sha256-5965…` (6.9 GB) **removed 2026-08-08** — was unreferenced

### NostalgiaTV — retro TV channel server (documented 2026-09-03)

Runs on razr, not dex247 and not the NAS — it is easy to miss because nothing
in this repo referenced it before today.

| | |
|---|---|
| Container | `nostalgiatv` — **healthy** |
| Version | **0.9.49** (`NTV_SERVER_VERSION`) |
| Image | `purestream711/nostalgiatv-server`, **pinned by digest** (only `:latest` is published upstream, so the digest is what makes it reproducible) |
| Compose | `/home/razr/nostalgiatv/docker-compose.yml` |
| Access | `192.168.1.31:19850` (LAN) · `100.87.97.120:19850` (Tailscale) |
| Consumed by | Jellyfin on dex247 (`192.168.1.155`) |

Bind mounts (all host-side, so config survives a container recreate):

- `./data` → `/app/data`
- `./config` → `/app/config`
- `./logos` → `/app/logos`

⚠️ **The port bindings are deliberate — do not "simplify" them.** The service is
bound explicitly to the LAN address and the Tailscale address, and **never to
`0.0.0.0` or `[::]`**. razr has a globally-routable IPv6 address and no host
firewall, so a bare `"19850:19850"` publish would expose this to the internet
over IPv6. The compose file carries its own warning to the same effect. Same
principle as the MQTT publish on the NAS — see `DECISIONS.md`.

**No watchtower runs on razr**, so this image does not silently auto-update.
Combined with the digest pin, the running version only changes deliberately.

#### Weather / Storm Channel

| | |
|---|---|
| Built-in Storm Channel data | **WeatherAPI.com** (server-rendered; `GET /api/weather/storm`) |
| Geocoding | **ArcGIS**, cached to `data/weather_geocode.json` |
| External WeatherStar embed | **disabled** (`wsEnabled: false`) |

Weather settings are **profile-scoped**, stored per profile under
`config/profiles/<id>/weatherstar_settings.json`. Two keys carry the location
and the supported save path writes both together:

- `weatherLocationQuery` — the **built-in Storm Channel** (this is the one that
  renders while the WeatherStar embed is disabled)
- `wsLocationQuery` — the external WeatherStar embed

**The app's fallback default is `Buffalo, NY`.** A profile with no
`weatherstar_settings.json` gets that default — it is baked into the image, not
a setting anyone chose.

Current state (set 2026-09-03):

| Profile | Weather location |
|---|---|
| **Default** | **`Tucker, GA`** → resolves to Tucker / Georgia |
| IVN | unset — falls back to the `Buffalo, NY` default |
| X - Rav | unset — falls back to the `Buffalo, NY` default |

**Supported change path:** the NostalgiaTV web UI (channel config → Location →
Save Location) or `POST /api/weatherstar?profileId=<id>` with
`{"weatherLocationQuery": "...", "wsLocationQuery": "..."}`. Profile IDs come
from `config/profiles.json`. **Do not hand-edit the JSON** — the API merges and
mirrors the two keys, which hand-editing skips. A city name, ZIP, or `lat,lon`
all work.

**Operational notes:**

- **No container restart is needed** for a weather-location change; it applies
  immediately.
- If the player still shows the old location, **re-tune the channel or reload**
  — the client caches settings and resolved coordinates per session.
- **A newly created profile will show Buffalo, NY until explicitly configured.**
  Setting one profile does not affect the others.

## UGREEN NAS — storage and Tracearr

| | |
|---|---|
| Address | `192.168.1.63`, host `Unimatrix0001`, user `unimatrix_001` |
| Access | SSH alias `nas-maint`, dedicated key, root-owned dispatcher only |
| Dispatcher | `/usr/local/sbin/loki-nas-maint` — 22 literal actions, enumerated in sudoers |

**Corrected 2026-09-02** (verified live during the camera outage repair). Two
long-standing inaccuracies here:

- It is **not** "six read-only actions". `sudo -n -l` lists **22** enumerated
  NOPASSWD actions, and several are **state-changing**: `plex_restart`,
  `tracearr_apply_update`, `tracearr_backup`, `tracearr_update_prepare`,
  `tracearr_rollback`, `tracearr_verify_update`. The read-only ones are
  `host_status`, `container_inventory`, `network_status`,
  `network_tooling_check`, `network_speed_test`, `disk_status`, `plex_status`,
  `plex_dependencies`, `plex_recent_logs`, `plex_transcode_processes`,
  `tracearr_status`, `tracearr_dependencies`, `tracearr_recent_logs`,
  `tracearr_update_check`, `tracearr_restart_forensics`,
  `tracearr_exit_window_logs`.
- Loki does **not** have "no shell" — `ssh nas-maint` yields a full interactive
  bash shell as `unimatrix_001`. This is consistent with NAS trap 2 below and
  with `nas_maint.py`'s own docstring: containment rests on the **sudoers
  allowlist**, not on the shell or on SSH key options.

What actually confines Loki: `unimatrix_001` is **not** in the `docker` group,
and plain `sudo` requires a password. So the docker socket is unreachable
(`permission denied`) and anything outside the NOPASSWD list needs the Boss.
The sudoers file does also carry `(ALL : ALL) ALL` — full root **with a
password** — which is a human affordance, not an agent one.

**Practical consequence, hit for real on 2026-09-02:** there is **no dispatcher
verb for mosquitto** (or for any container other than Plex/Tracearr), so Loki
could not restart the broker it had just repaired. The compose recreate and the
HA restart were both handed to the Boss. Correct outcome — do not add verbs to
widen this.

### Tracearr stack

Compose project `tracearr`, file `/volume2/tracearr/docker-compose.yml`,
network `tracearr_tracearr-network`:

- `tracearr` — app, **v2.0.1**, `172.19.0.4`, port 3000→3001
- `tracearr-redis` — service `redis`, `172.19.0.2`
- `tracearr-db` — TimescaleDB, service `timescale`, `172.19.0.3`

v1.5.0 → v2.0.1 was applied 2026-08-07 through Loki's own approval-gated
`tracearr_update` (`applied_by: loki_approval_gate`) — corrected 2026-08-10,
see `CURRENT_HANDOFF.md`. Earlier docs blamed watchtower for this jump; that
was a stale YAML comment, not evidence. Registry reconciled to match
2026-08-09 — see `docs/agent-context/COMPLETED_WORK.md`.

### Camera / NVR stack (documented 2026-09-02)

Runs on the NAS, compose project `docker`, file
`/volume1/docker/docker-compose-infra.yml`:

| Component | Detail |
|---|---|
| `frigate` | `ghcr.io/blakeblackshear/frigate:stable`, **0.17.2**, privileged, API `192.168.1.63:5000` |
| `mosquitto` | `eclipse-mosquitto:2.0` (2.0.22), publish **`0.0.0.0:1883`** (IPv4-only) |
| `homeassistant` | compose project `homeassistant`, `192.168.1.63:8123`, HA **2026.9.0** |
| go2rtc | inside the Frigate container; restreams on `127.0.0.1:8554` |

Stream path: **Tapo camera → go2rtc restream → Frigate → MQTT → Home Assistant.**

Cameras (all reachable, RTSP/554 open):

| Frigate name | IP | Model |
|---|---|---|
| `entryway` | `192.168.1.5` | TP-Link Tapo C201 |
| `livingroom` | `192.168.1.95` | TP-Link Tapo C201 |
| `front_door` | `192.168.1.165` | TP-Link Tapo D225 doorbell |

Key paths: `/volume1/docker/mosquitto/` (config, `data/`, `log/`),
`/volume1/docker/frigate/`, `/volume1/docker/homeassistant/`.

**Operational warnings:**

- ⚠️ **MQTT is anonymous — TEMPORARY.** `allow_anonymous true`. Exposure is
  limited by the IPv4-only publish, **not** by authentication. Migration is top
  of the security queue.
- ⚠️ **Never leave 1883 published dual-stack.** The NAS has globally-routable
  IPv6 (ISP-delegated; check `ip -6 addr show scope global` — addresses are
  deliberately not recorded here); a bare `"1883:1883"` publishes on `[::]` too
  and would expose an anonymous broker to the WAN. Keep the explicit `0.0.0.0:` prefix. Do **not** pin it to
  `192.168.1.63:` — a specific-IP publish can fail at boot before the interface
  is up.
- ⚠️ **An empty `mosquitto.conf` silently breaks everything.**
  `eclipse-mosquitto:2.0` with no `listener` binds loopback-only *inside the
  container* and refuses anonymous clients. `ss` on the host still shows
  `0.0.0.0:1883` — that is docker-proxy, not proof the broker is serving.
- ⚠️ **The HA `mqtt` / `frigate` config entries are titled `192.168.1.247`** —
  a stale cosmetic label from the decommissioned ASUS box. Their actual `data`
  correctly points at `192.168.1.63`. Do not "fix" the titles; never read a
  config-entry title as configuration.
- ⚠️ **Frigate HA integration: do not trust `manifest.json` for version.**
  Upstream's v5.15.5 ships it declaring `5.15.4`, so HACS offers the update
  perpetually. See `COMPLETED_WORK.md`.
- Frigate entity availability is **derived from MQTT** — a broker outage marks
  every Frigate entity `unavailable` at once. Camera entities additionally go
  unavailable when `camera_fps == 0`.
- **Unresolved:** `entryway` has a history of ffmpeg/go2rtc failure
  (`Could not find codec parameters … unspecified size`) that self-recovered on
  2026-09-02 with no established root cause. The duplicate Tapo entity
  `camera.entryway_door_hd_stream` is currently `unavailable`
  (`Operation not permitted` on direct RTSP) — suspected concurrent-session
  limit, **unconfirmed**. Do not disturb Frigate's working Entryway feed to
  chase it.

### Two NAS traps

1. The NAS runs its **own UGOS redis** as a host systemd service on
   `127.0.0.1:6379`. That is **not** Tracearr's Redis. Diagnosing it reports on
   the wrong service.
2. UGOS sets a global `ForceCommand /etc/ssh/force_command.sh` in `sshd_config`
   that **overrides per-key `command=""`** for admin-group users. SSH key
   options therefore do NOT confine Loki — the enumerated sudoers rule is the
   only containment. A non-admin account cannot be substituted: UGOS restricts
   non-admins to rsync/sftp/scp, so they cannot run the dispatcher at all.

### Open NAS issues (unfixed, deliberately)

- Tracearr `restart_count` 268 and climbing (bursty) while Redis and Postgres
  sit at 0 — app-side churn, cause unproven, **no automatic repair**.
- `/home/unimatrix_001` is mode `0777` — a real security issue needing a tested
  remediation.

## ASUS / "unicron" — download storage host

**Rebuilt.** It is a different install from the one the docs described, and
that rebuild is what broke the Filebrowser share.

| | |
|---|---|
| Tailnet | `100.101.112.55` · MagicDNS `asus.tail3744e0.ts.net` |
| LAN | `192.168.1.247` |
| **Old tailnet address (dead — no node has it)** | ~~`100.115.240.16`~~ |
| OS | Zorin OS 18.1 (Ubuntu-based), `OpenSSH_9.6p1` |
| Account | `asus` (uid 1000), `NOPASSWD` sudo, groups include `sudo` |
| SSH host key | `SHA256:j64/r3A/SMFEMDzbsVWOeCJ1khiaBKmkdu7zj1fn/9w` (ed25519) — **changed by the rebuild**; pinned in dex247's `known_hosts` for all three endpoints |
| Authorized keys | `razr@razr` (RSA, pre-existing) + `g2k247@dex247` (ed25519, added 2026-08-09) |

### Storage

- `/dev/sda2` 118.7 G ext4 → `/` (OS)
- `/dev/sdb1` 931.5 G ext4, **UUID `32d26174-8f15-4db3-8b7e-d584fc55bd7f`** →
  `/mnt/Disk1`. Holds `downloads`, `backups`, `nextcloud`, `frigate`,
  `jellyfin-metadata`, `yt-dlp`. ~470 G used.
- CIFS mounts from the UGREEN NAS under `/media/nas/*`.

**The rebuild dropped `/dev/sdb1` from `/etc/fstab` entirely**, so
`/mnt/Disk1/downloads` did not exist — the data was intact but unmounted. It is
now pinned by UUID with `nofail,x-systemd.device-timeout=15` (a missing data
disk must never block boot). Do not re-add it by `/dev/sdX`; the device letters
are not stable.

**CIFS share names with spaces must be escaped `\040` — repaired 2026-08-09.**
`/etc/fstab` lines 15 and 19 carried the share names literally
(`//192.168.1.63/Zion Cinema`, `//192.168.1.63/Folder 1`). fstab is
whitespace-delimited, so the parser read `Cinema` as the mount point and
abandoned both lines: **systemd generated no `.mount`/`.automount` unit for
them at all**, which is why they never mounted at boot. They now read
`Zion\040Cinema` and `Folder\0401`, matching how dex247 has always spelled the
same two shares. The NAS exports the names *with* real spaces — escape the
fstab field, never rename the share.

All seven `/media/nas/*` automounts are now generated and active.
`Docker` and `Personal` sit `waiting`/`dead` until first access; that is normal
`x-systemd.automount` laziness, not a fault.

### The sshfs share dex247 depends on

`sshfs-unicron.service` on dex247 mounts
`asus@asus.tail3744e0.ts.net:/mnt/Disk1/downloads` → `/mnt/unicron-downloads`,
which Docker binds into filebrowser as `/srv/unicron` with `rslave`
propagation. Chain: **asus fstab → /mnt/Disk1 → sshfs → /mnt/unicron-downloads
→ /srv/unicron**. Any link breaking empties `/srv/unicron` without taking
filebrowser down. Target the machine by MagicDNS, never by tailnet IP — the IP
already changed once.

## Registry

Eleven assets in `config/homelab_assets.yml`: `black-boxx`, `qbittorrent`,
`jellyfin`, `joplin`, `cloudflare-ddns`, `filebrowser`, `loki-interfaces`,
`immich`, `ugreen-nas`, `tracearr`, `plex`.
Plus one tombstone: `ivn-site`, decommissioned 2026-07-26, archive at
`/home/g2k247/backups/decommission/ivn-site-20260726-014813`.

# Completed Work

Every entry is labelled. **Do not convert an old plan into a claimed feature.**
Commits are local-only unless stated; nothing has been pushed.

Legend: **DONE** · **PARTIAL** · **UNFINISHED** · **OBSOLETE/HISTORICAL**

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

---

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

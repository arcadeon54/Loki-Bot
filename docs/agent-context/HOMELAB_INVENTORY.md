# Homelab Inventory

Three machines plus a NAS. Everything below is verified against live state or
current config as of 2026-08-06.

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

## UGREEN NAS — storage and Tracearr

| | |
|---|---|
| Address | `192.168.1.63`, host `Unimatrix0001`, user `unimatrix_001` |
| Access | SSH alias `nas-maint`, dedicated key, root-owned dispatcher only |
| Dispatcher | `/usr/local/sbin/loki-nas-maint` — six literal read-only actions |

Loki has **no shell** on the NAS. Every operation goes through the dispatcher:
`host_status`, `container_inventory`, `tracearr_status`,
`tracearr_dependencies`, `tracearr_recent_logs`, `tracearr_update_check`.

### Tracearr stack

Compose project `tracearr`, file `/volume2/tracearr/docker-compose.yml`,
network `tracearr_tracearr-network`:

- `tracearr` — app, **v1.5.0**, `172.19.0.4`, port 3000→3001
- `tracearr-redis` — service `redis`, `172.19.0.2`… `172.19.0.3`
- `tracearr-db` — TimescaleDB, service `timescale`, `172.19.0.2`

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

## Registry

Nine assets in `config/homelab_assets.yml`: `black-boxx`, `jellyfin`, `joplin`,
`cloudflare-ddns`, `loki-interfaces`, `immich`, `ugreen-nas`, `tracearr`, `plex`.
Plus one tombstone: `ivn-site`, decommissioned 2026-07-26, archive at
`/home/g2k247/backups/decommission/ivn-site-20260726-014813`.

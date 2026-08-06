# CURRENT HANDOFF

*Updated 2026-08-06 12:0x UTC. Keep this under a minute to read.*

## Just completed

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

**None assigned.** The qBittorrent objective is closed.

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

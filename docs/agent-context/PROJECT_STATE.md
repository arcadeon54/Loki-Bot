# Project State

*Live snapshot 2026-08-06. For the audit-era detail see `docs/PROJECT_STATE.md`.*

## Identity

| | |
|---|---|
| Product | Loki — personal AI assistant for Kavaris ("Boss") |
| Users | Boss (`OWNER_USER_ID`) · roommate Ammiel = "Rob" (crew) |
| Surfaces | Discord (public persona + serious DMs), Telegram `@Leauxki_Bot` (always serious), Home Assistant notifications, voice |
| Host | dex247, account `g2k247` |
| Repo = deployment | `/home/g2k247/loki-bot`, branch `master` |
| Push state | **47 commits ahead of origin. Nothing pushed.** |

## Live startup state (restart 2026-08-06 11:19 UTC)

```
Loki online as Loki#5463
Tools online — 113 registered
User memory — 21 facts
Routing — enabled, CHAT→groq, rest→primary
Proactive — ON, 0/5 used today, quiet 23:00–09:00 ET
RAG online — 3529 conversation chunks
Joplin memory online — Data API sidecar reachable
Semantic memory online — 17 memories indexed
Work tracker online — 90-min rule → SQLite + Joplin + Sheets (queued sync)
Presence monitor online — Boss: home, Rob: home
Career-Ops liaison online — bridge at configured URL
Task supervisor online
Browser research online — RAZR worker at configured URL
Homelab maintenance online — 10 assets
Hermes escalation client online — bridge configured
Hermes provider guard online — circuit closed 0/6/h 0/20/24h $0.00/$5.00
Container updates online
Homelab monitor online — polling every 300s
Career-Ops monitor online — poll every 60s
```

## Service health

| Unit | State |
|---|---|
| `loki.service` | active (running) |
| `loki-joplin-desktop.service` | active (running) — Data API 127.0.0.1:41184 |
| `loki-homelab-api.service` | active (running) |
| `black-boxx-ap.service` | **enabled + active**, sole boot owner of `wg-ap` |
| `canada-ap.service` | disabled + inactive (correct) |
| `wg-quick@wg-ap` | **disabled** (still `active (exited)` until next boot — intended) |

## Asset health

- **BLACK-BOXX** — 17/17 checks green, 0 advisories.
- **Tracearr** — v1.5.0, pinned by digest. Restart churn open and unrepaired.
- **Immich** — v3.0.3, which IS the latest stable. Nothing to update.
- **Joplin** — desktop sidecar authoritative; CLI container obsolete/stopped.

## Working tree

`config/homelab_lifecycle.yml` goes dirty during normal operation **by design**
(it is a generated mirror of the DB). Do not "clean it up" and do not hand-edit
it — change lifecycle state through Loki's tools.

Preserve any other dirty or untracked files you did not create. Several hold
live production state.

## Test suite

`venv/bin/python -m unittest discover -s tests -p 'test_*.py'`

**8 failures are pre-existing** under `discover` — import-order pollution in
`test_task_supervisor` (2), `test_homelab_lifecycle` (3), `test_hermes_guard`
(3). They pass standalone. Baseline against a clean tree before blaming a
change. There is no pytest suite, no linter config, no build step.

## Secrets

`.env` holds live secrets and so do all `.env.bak*` files. Names only in any
document or output; values REDACTED, always. See
`docs/agent-context/INTEGRATIONS.md` for the variable-name inventory.

# Loki Architecture

## Shape

`loki_bot.py` is the monolith entrypoint (~6,600 lines): Discord client, message
handling, download chain, relay, RAG query parsing, reminders, duplicate-link
guard. Satellite modules surround it. **Do not re-architect or split it** —
that requires explicit approval.

| Module | Owns |
|---|---|
| `tools.py` | `ToolSpec` registry, permissions, `execute()`, tool-call log |
| `assistant_tools.py` | Notes, lists, memory, home control, work hours |
| `personality.py` | **ALL** tone and prompts. Nothing else writes persona text |
| `telegram_interface.py` | Telegram long-polling, single-owner pairing |
| `ha_integration.py` | Home Assistant webhook + notification rewriting |
| `joplin_integration.py` | Joplin Data API client, notebooks, notes |
| `semantic_memory.py` | Boss memories: Joplin → Chroma index |
| `user_memory.py` | Per-user profiles and facts |
| `rag_search.py` / `ingest_history.py` | Conversation RAG (3529 chunks) |
| `work_tracker.py` / `jobsite_db.py` | Work sessions → SQLite + Joplin + Sheets |
| `presence_monitor.py` | Presence, lockout warnings |
| `task_supervisor.py` | Durable background tasks |
| `draft_approval.py` | Draft-and-approve gate for consequential tools |
| `homelab_maintenance.py` | Runbook controller, incidents, repairs |
| `maintenance_policy.py` | Risk tiers + shell command allowlist |
| `homelab_assets.py` | Registry loader, allowed parameter values |
| `homelab_monitor.py` | Autonomous polling, incident lifecycle |
| `homelab_lifecycle.py` | Decommission / tombstone registry |
| `homelab_hermes.py` / `hermes_guard.py` | Escalation client + circuit breaker |
| `homelab_api.py` | Read-only HTTP facade Hermes calls |
| `nas_maint.py` | NAS dispatcher client, Tracearr, Plex |
| `container_updates.py` | Approval-gated container updates |
| `maintenance_notify.py` | Autonomous event routing |
| `social_link_dedup.py` | Cross-channel duplicate link claims |
| `skill_bridge.py` | Mirrors skillkit skills as `skill_*` tools |
| `career_ops.py` / `browser_research.py` | razr-backed workers |

## skillkit

`/home/g2k247/skillkit` is a **separate repo** that owns the operational brain:
solve orchestrator (intent planner), playbooks, Advisor, CIE, Capability
Discovery, verification, approvals, incidents. Loki is just a caller with
identity `loki`. **Playbooks EXTEND the planner, never replace it.**

## Tool system

`ToolSpec` in `tools.py` declares name, description, parameters, handler,
`permission` (`everyone` < `crew` < `boss`), timeout, and — critically —
`action_type`. **A non-empty `action_type` marks the tool CONSEQUENTIAL.**
`tools.execute()` then routes to `draft_approval.create_draft()` instead of
running the handler. The handler only ever runs later through
`tools.run_approved()` after the Boss approves that exact draft ID.

Consequential is declared in the registry, not inferred from names. 113 tools
are registered at present.

## Approval gate

`draft_approval.py`, durable in `loki_drafts.db`:

1. `prepare(args, ctx)` validates and produces a safe human summary.
2. A draft row is persisted with a **payload hash** and TTL.
3. Approval verifies hash + expiry + status, then executes via the task
   supervisor (`draft_exec` task type) which re-verifies before running.
4. Only the originating authorized user or the Boss may approve. The roommate
   can never approve a Boss draft.

Tamper detection is real: any drift between `payload_json` and `payload_hash`
fails the draft rather than executing it.

## Maintenance controller

Deterministic runbooks per asset (`maintenance_runbooks/`), driven by
`homelab_maintenance.run_runbook()`. Every shell command is built from a fixed
template in `maintenance_policy._COMMANDS`; parameter values must BOTH match a
shape regex AND be declared in `config/homelab_assets.yml`. No caller ever
passes a raw command, and there is no shell.

Actions carry a fixed tier — `AUTO`, `APPROVAL`, `MANUAL` — declared in
`ACTION_TIERS`, never decided at runtime by a model.

Runbooks return `{checks, healthy, repair, repair_result, escalate,
advisories, diagnosis}`. `advisories` carry latent faults that are true but do
**not** make a working system "unhealthy" (see the black-boxx skill).

## Memory ownership — do not merge these

| Store | Role |
|---|---|
| Joplin `Loki/Memories` | **Source of truth** for explicit facts |
| ChromaDB `boss_memory` | Rebuildable index. **Never authoritative** |
| SQLite `loki_memory.db` | Conversation history, profiles, link dedupe |
| SQLite `jobsite.db` | Work sessions |
| SQLite `homelab_incidents.db` | Incidents, lifecycle, Hermes guard counters |
| SQLite `loki_drafts.db` | Approval drafts |
| SQLite `loki_tasks.db` | Durable task supervisor |

`semantic_memory.reindex()` rebuilds Chroma from Joplin daily and at startup.

## Model routing

`routing.json` + `get_routing_table()`. Settled: **gpt-5.1 primary**, Groq for
`CHAT`, everything else to primary. Undo is `enabled: false`. The full
"centralized Model Router" is a *planned* design in `docs/NEXT_STEPS.md`, not a
current feature — do not describe it as built.

## Verification principle

Success is proven, not assumed: skillkit `verification.confirm()` plus solve's
"no unverified 'solved'" rule. In the maintenance controller this means a repair
re-runs the runbook read-only and checks the evidence that actually changed.

# Historical Handoff (recorded 2026-07-19)

> **This document is historical context only.** It records the project
> direction as described in the owner's handoff prompt when a new Claude Code
> installation took over maintenance. Where it disagrees with the actual
> repository, runtime, or `docs/PROJECT_STATE.md`, **the code and runtime
> win**. Source-of-truth order: running services → repo/git → tests/runtime
> behavior → project docs → Joplin homelab docs → this document → assumptions.

## Identity

- Project: Loki — modular, cross-platform personal AI assistant (not merely a
  Discord chatbot).
- Owner: Kavaris, addressed as "Boss". Roommate: Ammiel, may be "Rob".

## Historical interface expectations

Discord, Telegram, direct/private conversations, HA notifications,
voice-message transcription, internet-backed tools. Telegram text worked
historically; Telegram voice was ignored/incomplete (**confirmed still
unimplemented at 2026-07-19 audit**).

## UX requirements (historical, still the bar)

Natural language in ("Immich isn't working", "remember my scooter tire
pressure", "how many hours did I work?") — no container names, entity IDs,
tool names, or commands required from the user. Persona: mischievous in
public Discord; serious in DMs/Telegram; brief/clear in HA notifications; no
emojis unless code defines an exception; personality never overrides safety
or truth. (Audit: implemented in `personality.py` as described.)

## Historical architecture checklist → audit verdict

| Historical item | 2026-07-19 verdict |
|---|---|
| Intent Planner | Implemented & working (skillkit `orchestrator.py`, solve loop) |
| Playbook layer extending planner | Implemented & working (11 JSON playbooks, deterministic match) |
| Solve meta-skill in skillkit | Implemented & working (`skills.d/solve.py`; input NL intent, output status+summary; actively used) |
| Advisor | Implemented & working (daily cron → Joplin + Telegram) |
| CIE | Implemented & working (weekly cron; evidence-backed recommendations) |
| Capability Discovery | Implemented & working (`capabilities.py`, live registry) |
| Verification (no unproven success) | Implemented & working (`verification.confirm()` + solve rule) |
| Tool system "~29 tools" | Superseded count — 49 registered at runtime |
| Telegram text | Implemented & working (@Leauxki_Bot) |
| Telegram voice | Documented-but-not-implemented (silently dropped) |
| Joplin memory (Loki/Memories, notes.ivn-group.cc) | Implemented & working |
| ChromaDB boss_memory @ localhost:8100 | Implemented & working (rebuildable, non-authoritative — as designed) |
| SQLite conversation/runtime state | Implemented & working |
| HA presence/notifications/actions | Implemented & working (Telegram mirror addition uncommitted) |
| 90-min work tracking incl. initial 90 min | Implemented; Joplin+SQLite working, **Sheets export failing** |
| Homelab ops observe→diagnose→plan→confirm→execute→verify→document | Implemented & working (solve + approvals + incidents) |
| Centralized Model Router | Planned only — per-intent routing.json exists; centralized router not built |

## Model Router direction (historical, approved direction — NOT started)

Centralized routing rather than per-subsystem hardcoding. Factors: task type,
complexity, capabilities, latency, cost budget, privacy, provider
availability, confidence, fallback. Hierarchy: deterministic code first (no
LLM for SQL/fs/incident logging/retrieval/verification) → cheap models for
classification/formatting → mid-tier for conversation/planning/Advisor →
premium only for architecture/hard debugging/multi-file work. Low-confidence
escalation, budget caps, controlled provider fallback, config-driven model
names, possibly different tiers for reasoning vs formatting.

## Standing safety rules from the handoff

Preserve first, inspect second, document third, change last. No production
changes, restarts, migrations, package installs, or model switches without
explicit approval. Never display secret values (names + REDACTED only).
Never claim success without verification.

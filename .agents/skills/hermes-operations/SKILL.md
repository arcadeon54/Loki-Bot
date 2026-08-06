---
name: hermes-operations
description: >-
  Use when working on the Hermes escalation path — the razr-hosted diagnosis
  agent, the OpenRouter circuit breaker and spend budgets, the read-only
  homelab API facade, or how a Hermes proposal becomes an approved change.
  Covers homelab_hermes.py, hermes_guard.py, homelab_api.py. Read before
  touching anything that can bill OpenRouter.
---

# Hermes Operations

## What Hermes is

A restricted, read-only escalation specialist running on **razr** (Hermes Agent
v0.19.0, dedicated `hermes` system account, bridge repo
`/home/razr/hermes-bridge`). It is reached only for conditions no deterministic
runbook recognises. Sonnet 5 for triage, Opus 5 only on a justified escalation.
The OpenRouter key lives on razr, never on dex247.

## Two structural guarantees

### 1. Deterministic first — enforced in code

`hermes_diagnose` **always** re-runs the asset's own runbook read-only first and
refuses to call Hermes if that runbook can already answer. This lives in code,
not in a prompt, so "a known runbook never burns an LLM call" is structurally
true. Do not move it into a prompt.

### 2. Hermes proposes; it never executes

`hermes_escalate` is the ONLY path from a proposal to a real change, and it
always stages an approval draft (`action_type="hermes_repair"`). On approval,
execution either delegates to an already-verified registered runbook (when
`diagnosis.matching_runbook` names one) or records manual follow-through.

**A Hermes proposal's free text is never run as a command.**

All four `hermes_*` tools are Boss-only and invisible to `everyone`/`crew` tool
schemas — normal chat and the roommate cannot reach Hermes at all.

## The financial guard

`hermes_guard.py` sits in front of every billable request. The single gated
chokepoint is `homelab_hermes.submit_diagnosis`. Polling (`get_job`,
`cancel_job`) is deliberately never blocked.

**Never add a second path to `POST /diagnose`.** Route new billable work through
`submit_diagnosis` so it inherits the gate.

| Control | Value |
|---|---|
| Requests/hour | 6 |
| Requests/day | 20 (rolling) |
| Observed spend/day | $5.00 |
| Failure threshold | 3 consecutive (`HERMES_GUARD_FAILURE_THRESHOLD`) |
| Cooldown | 1800s, doubling to 21600s |
| Billing cooldown | starts at 21600s |

Billing-class failures (quota, credits, 402) open the circuit **instantly**;
other failures need 3 consecutive.

### Recovery semantics

- Reachability opens recover via the bridge's non-billable authenticated
  `GET /health`.
- A **billing** open gets ONE leased submit, judged by the JOB's fate. Submit
  acceptance is `record_success(final=False)` and does **not** close a billing
  circuit.

### Cost accounting

The bridge's OpenRouter ledger delta is authoritative when its spend probe
works; otherwise a rate-card estimate is used and `cost_basis` marks which. The
guard's ceiling is honest-but-observed, not a hard billing limit.

## Ops events

`provider_circuit_open`, `provider_circuit_recovered`,
`provider_budget_reached` — emitted once per transition, to the Discord ops
channel, **never** Telegram. Boss tool: `hermes_provider_status`.

The monitor pre-checks `hg.blocked_reason()` before creating escalation tasks;
blocked incidents record `hermes_block_reason="provider unavailable: …"`.

## A trap in the guard's storage

Guard tables live in `homelab_incidents.db` on an **autocommit** connection
(`isolation_level=None`). A lingering transaction there deadlocks the
maintenance controller's connection with "database is locked". Keep it
autocommit.

## The read-only facade

`homelab_api.py` (`loki-homelab-api.service`) is what Hermes on razr calls. It
is deliberately thin: every route reuses existing read-only primitives, and **no
route asks for a repair-class command**. Keep it that way.

## Why the guard exists

The incident-amplification bug produced **297 billed Hermes jobs**. The guard
makes credit drain impossible even if deduplication breaks again. Treat it as a
safety system, not a tunable.

## Read-only checks

```bash
journalctl -u loki --since today | grep -i "provider guard"
sqlite3 "file:/home/g2k247/loki-bot/homelab_incidents.db?mode=ro" \
  "SELECT * FROM monitor_incidents WHERE incident_key='hermes-provider';"
systemctl is-active loki-homelab-api.service
```

## Completion criteria

All billable calls flow through `submit_diagnosis` · circuit and budget state
persists across restart · a provider outage produces ONE incident and no
recursion · no Telegram notification for a provider status · focused tests
green (`tests/test_hermes_guard.py`, 30 — note 3 fail under `discover` from
pre-existing import-order pollution).

## Source

`hermes_guard.py` · `homelab_hermes.py` · `homelab_api.py` ·
`tests/test_hermes_guard.py` · `docs/agent-context/SECURITY_BOUNDARIES.md`

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
| Billing/auth cooldown | starts at 21600s |

Billing-class failures (quota, credits, 402) and **auth-class failures**
(bridge job state `paused_auth` — "provider authentication failed —
re-authenticate on razr") both open the circuit **instantly**; other failures
need 3 consecutive. Neither self-resolves by waiting, so both skip the
threshold the same way. `paused_auth` was silently dropped by
`note_job_state()` until 2026-08-10 — a bad/revoked provider credential left
the job parked forever with the circuit never opening. Fixed; see
`tests/test_hermes_guard.py::AuthClassTests`.

### Status vocabulary

`hermes_guard.status()["status_label"]` maps the circuit's own
state/reason_class onto a fixed vocabulary (`status_label()` in
`hermes_guard.py`) rather than a second parallel status system:

| circuit state | reason_class | status_label |
|---|---|---|
| closed | — | `operational` |
| half_open | — | `recovering` |
| open | `budget` (Loki's own request/spend ceiling) | `protective_budget` |
| open | `billing` (provider quota/credits) | `protective_quota` |
| open | `auth` | `authentication_failed` |
| open | `rate_limit` | `rate_limited` |
| open | anything else | `unreachable` |

**`unavailable_all_providers` is deliberately never asserted.** This guard
only sees one aggregate signal — did the bridge's job succeed or fail — not
which of Hermes Agent's own configured providers on razr (openrouter, plus
whatever fallback chain exists there) actually served or refused it. A
`protective_quota` open means "the provider(s) that ran this job are out of
quota" — that could be the only provider configured, or the last one left in
a fallback chain. Telling those apart needs querying Hermes Agent directly on
razr (`hermes auth list` / `hermes fallback list`), which this guard does not
do — see Fable/multi-provider below.

### Recovery semantics

- Reachability opens recover via the bridge's non-billable authenticated
  `GET /health`.
- A **billing or auth** open gets ONE leased submit, judged by the JOB's
  fate — `GET /health` never calls the model provider, so it can't prove a
  quota refilled or a credential was fixed. Submit acceptance is
  `record_success(final=False)` and does **not** close either circuit class.

## Fable / multi-provider capability (investigated 2026-08-10)

**"Fable" is Claude Fable 5 (`claude-fable-5`), Anthropic's own model — not an
OpenRouter catalog entry.** Hermes Agent (`/home/hermes/.hermes/hermes-agent`,
v0.19.0, on razr) already has a native, first-class Anthropic provider
(`agent/anthropic_adapter.py`) that recognizes `claude-fable-5` (confirmed in
`agent/model_metadata.py`, 1M context), plus a genuine multi-provider
**fallback chain** feature (`hermes fallback add/list/remove`, "tried in
order when the primary model fails with rate-limit, overload, or connection
errors") — built by the Hermes Agent project itself, not something Loki needs
to reimplement.

**Current live state (read-only, verified via `hermes auth list` /
`hermes status` on razr, no paid request made):** OpenRouter is the sole
configured provider and is `exhausted (402) (ready to retry)`. No Anthropic
credential exists for the `hermes` system account (`Anthropic ✗ (not set)` in
`hermes status`; no `~/.claude.json` or `~/.claude/.credentials.json` either).
The fallback chain is empty (`hermes fallback list` → "No fallback providers
configured").

**The blocker, stated exactly:** to make Fable a working fallback, the Boss
needs to run, on razr as the `hermes` system account:

```
sudo -u hermes hermes auth add anthropic --type api-key   # prompts for the key
sudo -u hermes hermes fallback add                        # pick anthropic / claude-fable-5
```

This needs an Anthropic API key (`sk-ant-api...`) with access to Fable — this
is where "Fable credits" get spent. Do this on razr directly; don't paste the
key anywhere else. Both commands are interactive by design (secret prompt /
picker) — that's why this stayed a documented blocker rather than something
scripted.

**Known cost-accounting gap once Fable is configured (bridge-side, not fixed
here — this task deliberately did not touch `~/hermes-bridge` on razr):**
`hermes-bridge/lib/budget.mjs`'s `ratesFromEnv()` only prices
`anthropic/claude-sonnet-5` and `anthropic/claude-opus-5`; `lib/usage.mjs`'s
`makeSpendProbe()` is hard-coded to OpenRouter's own balance endpoint. A job
phase served by Fable prices at **$0** in both the bridge's own budget ledger
and Loki's `hermes_guard.spend_last_24h_usd()` — neither the bridge's
`$5/day` cap nor Loki's `$5.00` observed-spend ceiling will actually see
Fable spend. `hermes_guard.status()` flags this rather than staying silent:
`last_serving_model` + `last_serving_model_cost_telemetry` (`"reliable"` for
the two priced OpenRouter-routed models, `"unreliable — ..."` for anything
else, Fable included). **Deterministic ceilings remain fully effective
regardless**: Loki's own 6/hour, 20/day request-count ceilings; the bridge's
`max_turns` (triage 8, escalation 14), `phaseTimeoutMs`, `jobTimeoutMs`,
`maxConcurrent: 1`. None of those depend on knowing a price, so Fable cannot
become an unbounded fallback even with broken cost telemetry — it just can't
be trusted to *report* its true cost yet.

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
green (`tests/test_hermes_guard.py`, 45 — note 3 fail under `discover` from
pre-existing import-order pollution).

## Source

`hermes_guard.py` · `homelab_hermes.py` · `homelab_api.py` ·
`tests/test_hermes_guard.py` · `docs/agent-context/SECURITY_BOUNDARIES.md`

razr side (separate repo, read-only investigated 2026-08-10, not modified):
`~/hermes-bridge/server.mjs`, `lib/hermes.mjs`, `lib/budget.mjs`,
`lib/usage.mjs` · Hermes Agent itself at
`/home/hermes/.hermes/hermes-agent/` (`agent/anthropic_adapter.py`,
`agent/model_metadata.py`) · Hermes Agent config
`~/hermes-bridge/config/hermes-config.yaml`.

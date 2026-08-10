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

## Provider fallback chain (configured 2026-08-10 — live on razr)

**Fable was investigated twice, then explicitly rejected by the Boss on cost
grounds. This is settled — do not reopen it as a fallback candidate.**
First pass: "Fable" is Claude Fable 5 (`claude-fable-5`), Anthropic's own
model; Hermes Agent has a native Anthropic provider adapter that recognizes
it — but the Boss doesn't hold an Anthropic account, so that path was
withdrawn before any credential was requested. Second pass, from the Boss's
actual OpenRouter account: `anthropic/claude-fable-5` genuinely is a
live-listed OpenRouter model (confirmed via OpenRouter's public
`/v1/models`), reachable on the existing key with no new credential — but a
non-billable balance read on that key (mirroring the bridge's own spend
probe) showed only $0.36 left on a $10 cap, nowhere near Fable's $10/$50
per-million pricing. The Boss then ruled it out entirely: *"too expensive for
routine Hermes diagnostics — I only used it previously because of a
temporary promotional credit arrangement."* No Fable model appears anywhere
in the configured chain.

**Final architecture — cost-tiered, per explicit instruction: local first,
cheap paid second, frontier models manual-escalation-only, never
automatic.** Hermes Agent (`/home/hermes/.hermes/hermes-agent`, v0.19.0, on
razr) has its own genuine multi-provider **fallback chain** feature — built
by the Hermes Agent project itself, not something Loki reimplements. Config
schema lives at `/home/hermes/.hermes/config.yaml`, a `fallback_providers`
list (each entry: `provider`, `model`, optional `base_url`, optional
`api_key`/`key_env` — read by `hermes_cli/fallback_config.py`). Confirmed in
`agent/conversation_loop.py`, not just the config file's own comment, that
`FailoverReason.billing` (HTTP 402 — OpenRouter's actual current failure) is
in the eager-fallback trigger set, alongside rate-limit/overload/connection
failures, with a built-in guard against retrying a depleted balance once
every recovery path (credential-pool rotation, then the fallback chain) is
exhausted.

**Configured chain, verified through Hermes Agent's own read-only commands**
(`hermes fallback list`, `hermes config get fallback_providers --json`,
`hermes config check`, `hermes doctor` — no paid request):

```
Primary:    anthropic/claude-sonnet-5        (via openrouter)
Fallback 1: gemma4-12b-balanced:latest       (via custom → local Ollama on razr, $0)
Fallback 2: deepseek/deepseek-v4-flash-0731  (via openrouter, $0.08/$0.18 per M tok)
```

- **Fallback 1 — local Ollama.** Verified read-only: Ollama v0.20.5 running
  on razr, `gemma4-12b-balanced:latest` reports native
  `["completion", "tools", "thinking"]` capabilities via its own `/api/tags`
  — genuinely tool-call capable. Hermes Agent's `custom` provider profile is
  documented in its own source (`plugins/model-providers/custom/__init__.py`
  docstring) as covering "any endpoint registered as provider='custom',
  including local Ollama instances." `base_url: http://localhost:11434/v1`,
  `api_key: ollama` — a placeholder string, not a real credential; Ollama
  doesn't authenticate it.
- **Fallback 2 — cheap OpenRouter.** Picked from a live pricing pull off
  OpenRouter's public `/v1/models` (332 tool-capable text models, sorted by
  cost). Carries **no** inline `api_key`/`key_env` — omitting both makes
  Hermes Agent fall through to the already-configured pooled
  `OPENROUTER_API_KEY` (confirmed in
  `hermes_cli/cli_agent_setup_mixin.py`), so this reuses the existing
  credential rather than adding a new one.

**How it was written — not through the CLI's own `add` commands.**
`hermes config set` can't construct a fresh list-of-dicts key (confirmed in
`hermes_cli/config.py`'s `_set_nested` — it only indexes into structure that
already exists), and both `hermes fallback add` and `hermes model` are pure
interactive pickers with zero non-interactive flags (confirmed: `hermes
fallback add < /dev/null` errors `"requires an interactive terminal ...
cannot be run through a pipe or non-interactive subprocess"`). Configured
instead via a direct edit to `/home/hermes/.hermes/config.yaml`, using
exactly the schema `fallback_config.py` consumes — two backups taken first
(`config.yaml.bak-<timestamp>`), YAML validity checked with `yaml.safe_load`,
then read back through Hermes Agent's own commands above as the real
verification (not an assumption about what the edit would do).

**Cost-accounting gap — not fixed here.** `hermes-bridge/lib/budget.mjs`'s
`ratesFromEnv()` only prices `anthropic/claude-sonnet-5` and
`anthropic/claude-opus-5`; `lib/usage.mjs`'s `makeSpendProbe()` is hard-coded
to OpenRouter's own balance endpoint. A job served by **either** configured
fallback prices at **$0** in the bridge's own ledger — correctly for local
Ollama (genuinely free), silently wrong for DeepSeek (real nonzero cost
hidden). `hermes_guard.status()` flags this rather than staying silent:
`last_serving_model` + `last_serving_model_cost_telemetry` (`"reliable"` for
the two priced models, `"unreliable — ..."` for anything else, including
DeepSeek). **Deterministic ceilings remain fully effective regardless**:
Loki's own 6/hour, 20/day request-count ceilings; the bridge's `max_turns`
(triage 8, escalation 14), `phaseTimeoutMs`, `jobTimeoutMs`,
`maxConcurrent: 1`. None of those depend on knowing a price, so neither
fallback can become unbounded even with broken cost telemetry.

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

razr side, 2026-08-10 — `~/hermes-bridge` (separate repo, `server.mjs`,
`lib/hermes.mjs`, `lib/budget.mjs`, `lib/usage.mjs`) investigated read-only,
not modified. Hermes Agent itself at `/home/hermes/.hermes/hermes-agent/`
(`agent/anthropic_adapter.py`, `agent/model_metadata.py`,
`agent/conversation_loop.py`, `plugins/model-providers/custom/__init__.py`,
`hermes_cli/fallback_config.py`, `hermes_cli/cli_agent_setup_mixin.py`,
`hermes_cli/config.py`) investigated read-only. Hermes Agent's own config,
**edited**: `/home/hermes/.hermes/config.yaml` (`fallback_providers` key
added — backups at `config.yaml.bak-20260810-145453` and
`config.yaml.bak-20260810-150617-2`).

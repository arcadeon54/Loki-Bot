# Durable Decisions

Settled architectural decisions. Each records the decision, the reason, and what
would have to change to reopen it. **Do not re-litigate these** — several cost
multiple sessions to reach.

---

## Loki is a caller; skillkit owns the operational brain

skillkit holds the solve orchestrator, playbooks, Advisor, CIE, verification,
approvals. Loki calls it with identity `loki`. Playbooks **extend** the planner,
never replace it. Reopening would mean moving orchestration into the monolith —
explicitly rejected.

## LLM priority is settled

gpt-5.1 primary, Groq fallback with `CHAT` routed to Groq via `routing.json`.
Undo is `enabled: false`. The full centralized Model Router remains a *design*.
Reopening requires the Boss to authorize the router project.

## Memory systems stay separate

Joplin `Loki/Memories` is source of truth; Chroma `boss_memory` is a rebuildable
index and **never** authoritative; SQLite holds conversation history, profiles,
and work sessions. Merging them would put a lossy vector index in the truth
path.

## qBittorrent stays OUT of gluetun

Settled after a week of testing. The pairing must never be "fixed".

## Commands are template-locked, never model-composed

`maintenance_policy` builds every command from a fixed `argv` template with
registry-validated parameters and no shell. A model never composes a command
string. Reopening this would reintroduce the entire injection surface the design
exists to eliminate.

## Risk tiers are declared, not inferred

`ACTION_TIERS` fixes AUTO / APPROVAL / MANUAL per action at import. A model does
not decide at runtime how dangerous something is. Likewise, "consequential" is
declared by `ToolSpec.action_type` in the registry, never inferred from a tool's
name.

## Hermes proposes; it never executes

Hermes only diagnoses. `hermes_escalate` is the only path to a real change and
always stages an approval draft. On approval, execution delegates to an
already-verified registered runbook or records manual follow-through. **Free
text from a model is never run as a command.**

## Deterministic runbooks run before Hermes, enforced in code

`hermes_diagnose` re-runs the asset's own runbook read-only first and refuses to
call Hermes if that runbook can answer. This lives in code rather than a prompt
so it is structurally true, not merely instructed.

## Every billable Hermes submit goes through one gate

`homelab_hermes.submit_diagnosis` is the single chokepoint that
`hermes_guard.py` protects. **Never add a second path to `POST /diagnose`.**
Polling (`get_job`/`cancel_job`) is deliberately never blocked.

## NAS containment is the sudoers rule, not SSH key options

UGOS's global `ForceCommand` overrides per-key `command=""` for admin-group
users, and non-admin accounts cannot run the dispatcher at all. So the
enumerated sudoers rule is the *only* containment. Never add a state-changing
verb to `/usr/local/sbin/loki-nas-maint`, never use the docker group, never
grant wildcard docker sudo.

## An incident closes only on verified recovered health

Escalation and task completion are explicitly **not** closure conditions.
Closing on escalation is what produced 297 incidents and 297 billed Hermes jobs.
Recovery requires `RECOVERY_THRESHOLD` consecutive healthy polls.

## Autonomy is decided by identity, not by channel

`requester_name == "Boss (auto)"` (`mn.AUTONOMOUS_REQUESTER`) via
`mn.is_autonomous_task(row)`. Routing on `channel_id == "ops:maintenance"` was
not enough — 302 pre-existing task rows carried a real `tg:` channel id and
re-announced themselves to Telegram on every restart. Add new autonomous
notifications by naming an event in `maintenance_notify.EVENTS`, never by
picking a destination at the call site.

## Latent faults are advisories, not failed checks

A boot-time or configuration hazard that says nothing about currently flowing
traffic must not mark a working asset "unhealthy". Runbooks return it in
`advisories` with an approval-tier plan while `healthy` stays true. Verification
for such a repair therefore hangs on the advisory clearing — `healthy` was
already true beforehand and proves nothing.

## Disable, never stop, for boot-ownership conflicts

`systemctl stop` on a `wg-quick@` unit runs `wg-quick down` and deletes the live
interface the owning service is serving traffic on. The allowlist deliberately
contains no stop/down command and must never gain one.

## The lifecycle YAML is a generated mirror

`config/homelab_lifecycle.yml` is rewritten from the DB on every lifecycle
change and goes git-dirty during normal operation by design. The DB is
authoritative. Tombstones (e.g. `ivn-site`) are retained permanently — deleting
one lets the container be rediscovered as an unknown asset and restarts the
false-incident cycle.

## A missing `.env` must lock Loki down

`tools.user_level()` once treated an unset `OWNER_USER_ID` as a match, so
`"" == ""` made any blank-id caller Boss — including for approval-gated
destructive tools. Never reintroduce an empty-string comparison in a permission
check.

## Indexes on migration-added columns belong in `_MIGRATIONS`

An index placed in the unconditional `CREATE TABLE IF NOT EXISTS` block fires
via `executescript()` **before** the `ALTER TABLE` that adds the column, which
breaks import against a pre-existing production DB. Always test schema changes
against a copy of the live `homelab_incidents.db` before restarting.

## `monitor_incidents` is the authoritative outage source; a fault is a key

The Daily Briefing counts outages from Loki's `homelab_incidents.db`
`monitor_incidents`, deduped by `key` — the same identity `homelab_monitor`
uses when it asks `WHERE key=? AND closed_at IS NULL` before opening an
incident. One persistent fault is **one** incident no matter how many rows it
logged: the live DB holds 297 `escalated` rows across just three faults.
Liveness is `closed_at IS NULL`, never the status string — `escalated` rows are
*closed* Hermes handoffs.

skillkit's own `logs/incidents.db` is solve-path bookkeeping: a row means the
agent worked on something, not that the homelab broke. It is reported
separately and weighted lower (3 each, capped at 15) so stale records cannot
read as outages. Do not merge the two schemas.

## Reliability penalises impairment, not state that merely looks bad

Three penalties were firing for things that were not impairments, so the score
had to learn the difference between "not running" and "broken":

**Desired state is read, never guessed.** A stopped container costs 8 points
only if the deployment expects it up. Evidence is the container's own Docker
`RestartPolicy` (`always`/`unless-stopped`/`on-failure` = expected) plus the
asset lifecycle registry's `expected_running` and `cleanup_scope_json.container`
(a retired asset is excused). `restart: no` + exit 0 is a finished one-shot;
`restart: no` + non-zero is intentionally stopped. This replaced a hard-coded
`"ivn-site" not in s` exclusion. When desired state is unavailable the container
counts as expected-to-run — a collector failure must never erase a penalty.

**Protective degradation is charged, but not as an outage.** An open
`hermes-provider` fault whose diagnosis matches the billing vocabulary
(quota/insufficient/credit/billing/payment/402 — the same words
`hermes_guard.classify()` uses) is the cost circuit doing its job: escalation
capability really is lost, so it costs 5, but nothing is broken and no repair
applies. A provider that is *unreachable* is not protective and keeps the full
12. `advisor.is_protective_fault()` owns this and the synthesis prompt forbids
recommending a repair for it.

**Bookkeeping closes itself.** `incidents.resolved` was written once, at record
time, from the status of the run that wrote it — so a run that timed out left an
open row forever, even after a later run fixed the same thing. A `solved` run
now closes earlier open records with the same repair *signature* via
`incidents.resolve_superseded()`. Signature, never service or symptom text: a
signature only exists when real mutating steps ran, so an empty one closes
nothing. Records from runs that mutated nothing stay open for an operator and
`skillkit resolve-incident`.

Guarded by `tests/test_reliability_state.py`. The number does not need to be
100; it needs every deduction to name a real impairment.

## Briefing severity is deterministic; the LLM only narrates it

`reporting.disk_status()` classifies each host once against the monitor's real
threshold (`MONITOR_DISK_MIN_FREE_PCT`, default 10 free → **90%** used) and the
synthesis prompt forbids exceeding the `max_severity` it returns. This existed
because the healthy-line renderer hard-coded `disk_pct < 80` while the monitor
alerted at 90%, so one 78% disk was printed as "under 80% — no action required"
and called "P1 — Act now" in the same report. Any new briefing metric must ship
its verdict the same way rather than handing raw numbers to the narrator.

## Image age is never evidence of an available update

`_c_image_ages` reads local `docker image inspect .Created` and queries no
registry, so it sets `upstream_checked: False` and leaves `update_available`
empty. Old local images are a maintenance-review signal only. Claiming an
update exists requires something that actually looked upstream.

## Antigravity is an alternative implementation agent, not a replacement

agy runs as `g2k247` on dex247 alongside Claude Code; neither owns the project.
Durable context lives in the repository (`AGENTS.md`, `docs/agent-context/`,
`.agents/`) so no conversation history from any vendor is load-bearing.
Career-Ops evaluations specifically run on razr's Antigravity, never Claude.

## Provider auth failures are gated the same as billing failures

A bad or revoked provider credential doesn't self-resolve by waiting any more
than an exhausted quota does. `hermes_guard.py`'s `auth` class therefore opens
the circuit instantly (skipping the 3-consecutive-failure threshold that
applies to plain reachability failures) and recovers the same way billing
does — one controlled submit, never the bridge's non-billable `GET /health`,
because `/health` never calls the model provider and proves nothing about
whether a credential was fixed. This exists because `paused_auth` (the
bridge's own state for exactly this failure) was silently unhandled until
2026-08-10 — the circuit never opened for it, so Loki kept submitting jobs
against a credential that could never succeed.

## Provider resilience is Hermes Agent's job, not Loki's to reimplement

Hermes Agent (the actual runtime on razr, v0.19.0) has its own native
multi-provider support — an Anthropic adapter and a `hermes fallback` chain
("tried in order when the primary model fails") — built by that project, not
by Loki. `hermes_guard.py` stays a client-side gate in front of
*whether a job is submitted at all*; it does not reimplement per-request
provider selection or retry logic, which would duplicate what Hermes Agent
already does one layer down and is exactly the "scattered fallback logic"
this design avoids. Consequence, not separately engineered: once a fallback
chain exists on razr, the guard's existing single "hermes-provider" circuit
already means "every provider Hermes Agent tried for this job was
unavailable" — the bridge only reports `paused_quota`/`paused_auth` after its
own fallback chain (if any) is exhausted, not after the first provider fails.

## Don't claim cost accuracy the bridge itself doesn't have

`hermes-bridge/lib/budget.mjs`'s rate card and `lib/usage.mjs`'s spend probe
(razr, not this repo) only price/observe the two Anthropic models routed
through OpenRouter (`anthropic/claude-sonnet-5`, `anthropic/claude-opus-5`) —
not "OpenRouter" generally. A job served by the configured Hermes fallback
chain (local Ollama, or `deepseek/deepseek-v4-flash-0731` — also OpenRouter,
just a model the rate card has never heard of) prices at $0 in the bridge's
own ledger, silently, whether or not that's actually true.
`hermes_guard.status()` reports `last_serving_model_cost_telemetry` as
`"unreliable"` for any model outside that known-priced set rather than
letting a $0 observed spend read as "this job was free." The fix belongs in
the bridge (razr); this repo's job is to not repeat a wrong number with false
confidence. (Fable, investigated as a possible provider, was rejected by the
Boss on cost grounds before this ever mattered for it specifically — the
decision generalizes to any unpriced model, which is exactly what DeepSeek
now is.)

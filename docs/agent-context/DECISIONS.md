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

## Antigravity is an alternative implementation agent, not a replacement

agy runs as `g2k247` on dex247 alongside Claude Code; neither owns the project.
Durable context lives in the repository (`AGENTS.md`, `docs/agent-context/`,
`.agents/`) so no conversation history from any vendor is load-bearing.
Career-Ops evaluations specifically run on razr's Antigravity, never Claude.

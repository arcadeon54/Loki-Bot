# Security Boundaries

These are enforced in code, not by convention. Do not widen any of them for
convenience.

## Never

- **No unrestricted NAS root.** Loki reaches the UGREEN NAS only through the
  root-owned dispatcher `/usr/local/sbin/loki-nas-maint`, which exposes six
  literal read-only actions. Never add a state-changing verb to it.
- **No unrestricted Docker socket.** No bind-mounting `/var/run/docker.sock`
  into anything Loki drives.
- **No Docker-group shortcut.** Never add Loki (or `unimatrix_001`) to the
  `docker` group — that is root-equivalent.
- **No `sshpass`.** Ever.
- **Key-based SSH only**, with host-key verification. Never disable
  `StrictHostKeyChecking`, never pass passwords in command arguments.
- **No secrets in project docs**, prompts, commit messages, or logs. Names
  only; values REDACTED. `.env` and every `.env.bak*` hold live secrets.
- **No `git push` without explicit Boss permission** for that specific push.
- **No destructive Docker prune.** There is no prune command in the allowlist
  and there must never be one.
- **No casual persistent-volume deletion.** Volume removal only happens for a
  volume proven unshared at both plan time and run time, inside an
  approval-gated decommission.
- **No unrestricted sudo for the agent.** `g2k247` has broad sudo on dex247 as a
  human; an AI agent must not exploit that to run arbitrary privileged commands.

## Command allowlist model

`maintenance_policy.py` is the single chokepoint:

- Commands are fixed `argv` templates. There is no shell, so no metacharacters,
  redirection, or injection surface.
- Placeholders name a parameter **class** (`iface`, `container`, `unit`,
  `path`, `image`, `num`, `volume`, …). A value must match the class's shape
  regex **and** be present in the registry-derived value set built by
  `homelab_assets.Registry.allowed_values()`.
- Only locally-executed assets contribute values to the local shell allowlist —
  a NAS container name can never become a legal parameter for a dex247 command.
- `repair: True` marks a mutating command; it is refused outright unless the
  `Ops` facade was constructed with `allow_repairs=True`.
- `add_runtime_values()` may extend a class only from values read via an
  already-allowlisted command — never from model or user input.

Anything else raises `PolicyError` before a process is ever spawned.

## Risk tiers

`ACTION_TIERS` in `maintenance_policy.py` fixes each action:

- **AUTO** — safe, verified, reversible, exact-runbook-match only.
  e.g. `restore_blackboxx_ip_rule`, `restart_stateless_service`,
  `rerun_health_checks`, `mark_asset_lifecycle_state`.
- **APPROVAL** — staged as a draft, never run inline.
  e.g. `service_enable_disable`, `container_image_update`, `compose_change`,
  `firewall_change`, `immich_update`, `decommission_cleanup`, `host_reboot`.
- **MANUAL** — Loki must refuse and hand it to the Boss.
  `database_repair`, `filesystem_repair`, `credential_change`,
  `delete_volume_or_user_data`, `destructive_prune`, `network_redesign`.

A tier is declared here, never decided at runtime by a model.

## Approval gating

Consequential tools are marked by `ToolSpec.action_type` in the registry.
`tools.execute()` refuses to run them inline and stages a durable draft
(`loki_drafts.db`) with a payload hash and TTL. Execution requires the Boss to
approve that exact draft ID; the hash is re-verified at execution time.

## Deterministic before Hermes

`hermes_diagnose` **always** re-runs the asset's own deterministic runbook
read-only first, and refuses to call Hermes if that runbook can already answer.
This is enforced in code, not in a prompt, so "a known runbook never burns an
LLM call" is structurally true.

## Hermes does not own production credentials

Hermes only **diagnoses and proposes**. It never executes. The only path from a
Hermes proposal to a real change is `hermes_escalate`, which stages an approval
draft. On approval, execution either delegates to an already-verified registered
runbook or records manual follow-through — **a Hermes proposal's free text is
never run as a command.** All four `hermes_*` tools are Boss-only and invisible
to `everyone`/`crew` tool schemas.

## Financial protection

`hermes_guard.py` gates every billable submit through
`homelab_hermes.submit_diagnosis`. Budgets: 6 requests/hour, 20/day rolling,
$5/day observed spend. Never add a second path to `POST /diagnose`.

## Permission levels

`everyone` < `crew` < `boss`, enforced in `tools.execute()` and re-checked in
`tools.run_approved()`. Every call is logged to `tool_calls.jsonl` with
credential redaction; content-bearing tools set `redact_log`.

**A missing `.env` must lock Loki down, not open it up.** `tools.user_level()`
once treated an unset `OWNER_USER_ID` as a match, making any blank-id caller
Boss. Fixed in `251807b` — never reintroduce an empty-string comparison there.

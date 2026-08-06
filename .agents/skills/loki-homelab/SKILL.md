---
name: loki-homelab
description: >-
  Use when working on Loki's homelab maintenance controller — asset registry,
  deterministic runbooks, the command allowlist, risk tiers, or the
  approval-gated repair path. Covers homelab_maintenance.py,
  maintenance_policy.py, homelab_assets.py, config/homelab_assets.yml, and
  maintenance_runbooks/. Read before adding an asset, a runbook, a command, or
  a repair action.
---

# Loki Homelab Maintenance Controller

## When this applies

Adding or changing an asset, a runbook, an allowlisted command, a risk tier, or
anything on the diagnose → repair → verify path.

## Architecture

`homelab_maintenance.py` drives runbooks. `Ops` is the only capability surface a
runbook sees, and every command flows through `maintenance_policy.build_command()`.

The chain that matters:

1. `config/homelab_assets.yml` declares assets and their parameters.
2. `homelab_assets.Registry.allowed_values()` derives the legal parameter value
   set from that registry.
3. `maintenance_policy.configure()` installs those values.
4. `build_command(name, **params)` accepts a call only if the command is
   allowlisted, every placeholder is supplied, each value matches its class
   shape regex, **and** each value is in the registry-derived set.

There is no shell. Commands are fixed `argv` templates, so there is no
injection surface. A model never composes a command string.

## Adding a command

```python
# read-only
"systemctl_is_enabled": {"argv": ["systemctl", "is-enabled", "{unit}"]},

# mutating — refused unless Ops(allow_repairs=True)
"systemctl_disable_unit":
    {"argv": ["sudo", "-n", "systemctl", "disable", "{unit}"], "repair": True},
```

Rules:

- Every parameter must be a declared class (`iface`, `container`, `unit`,
  `path`, `image`, `num`, `probe_ip`, `volume`, `service`, `dbident`,
  `compose_file`).
- The value must be declared in the registry, or the call raises `PolicyError`.
- Mark mutating commands `repair: True`.
- Add a rollback counterpart for anything reversible.
- Comment **why** a dangerous variant is absent — those comments have prevented
  real outages.

Only locally-executed assets contribute values to the local allowlist; a NAS
container name can never become a legal dex247 parameter.

## Risk tiers

`ACTION_TIERS` fixes each action as AUTO, APPROVAL, or MANUAL at import. Never
decide a tier at runtime.

- **AUTO** — only when the runbook's exact preconditions hold. Must be
  idempotent, verified, and rolled back on failed verification.
- **APPROVAL** — returned as a `repair` plan; staged as a draft by
  `homelab_repair` → `homelab_apply_repair`. Never run inline.
- **MANUAL** — refuse and hand to the Boss.

A tier with no allowlisted command behind it is a latent bug — that is exactly
what blocked the BLACK-BOXX boot-race fix until commands were added.

## Runbook contract

Return:

```python
{"checks": [...], "healthy": bool, "repair": plan|None,
 "repair_result": ...|None, "escalate": bool,
 "advisories": [str], "diagnosis": str}
```

Each check is `{"name", "ok", "detail", "status"}` where status is `ok`,
`failed`, `skipped`, or `unavailable`.

### Three rules learned the hard way

1. **Probe the executor first.** If Loki cannot run its own read-only commands,
   report `diagnostic_transport: UNAVAILABLE` and state UNKNOWN. That is a Loki
   problem, not a dead subsystem. Reporting it as a fault is how one broken
   executor became twelve fabricated hardware faults.
2. **Mark dependents `SKIPPED`, never `failed`.** When a shared prerequisite is
   down, downstream checks were never run — calling them "failed" asserts
   evidence you never gathered.
3. **Latent faults are advisories, not failed checks.** A boot-time or
   configuration hazard says nothing about traffic flowing right now. Put it in
   `advisories` with an approval-tier plan and leave `healthy` true.

## Verifying a repair

Check the evidence that actually changed. `_apply_repair_handler` re-runs the
runbook read-only and requires `healthy` **and** no remaining advisories —
because for a latent fault `healthy` was already true beforehand and proves
nothing. A surviving advisory returns the incident to `awaiting_approval`
rather than calling a green asset unhealthy or escalating a non-outage.

## Testing

`tests/test_homelab_maintenance.py`, `tests/test_black_boxx_runbook.py`.
The `FakeOps` pattern scripts a command table keyed exactly like the allowlist,
so decisions are exercised without a single real command. Use it.

Validate a new command against the real registry read-only:

```python
import homelab_assets, maintenance_policy as policy
policy.configure(homelab_assets.load().allowed_values())
policy.build_command("systemctl_is_enabled", unit="wg-quick@wg-ap.service")
```

## Completion criteria

New command validates against the real registry and rejects undeclared values ·
tier declared · runbook returns the full contract including `advisories` ·
focused tests green against the pre-existing baseline · read-only verification
run against production.

## Source

`homelab_maintenance.py` · `maintenance_policy.py` · `homelab_assets.py` ·
`config/homelab_assets.yml` · `maintenance_runbooks/` ·
`docs/agent-context/SECURITY_BOUNDARIES.md`

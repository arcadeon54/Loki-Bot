# Security Boundaries

These are enforced in code, not by convention. Do not widen any of them for
convenience.

## Never

- **No unrestricted NAS root.** Loki reaches privileged NAS operations only
  through the root-owned dispatcher `/usr/local/sbin/loki-nas-maint`, whose
  actions are enumerated literally in sudoers (no wildcards). **Corrected
  2026-09-02:** it exposes **22** actions, not six, and six of them are
  state-changing (`plex_restart`, `tracearr_apply_update`, `tracearr_backup`,
  `tracearr_update_prepare`, `tracearr_rollback`, `tracearr_verify_update`) —
  each already approval-gated on the Loki side. Never add a new
  state-changing verb without the Boss's explicit sign-off.
  Note also that `ssh nas-maint` **does** yield an interactive shell (UGOS's
  global `ForceCommand` overrides per-key restrictions); containment is the
  sudoers allowlist plus the fact that `unimatrix_001` is not in the `docker`
  group and plain `sudo` needs a password. Earlier docs claiming "no shell"
  were wrong.
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

## Public exposure boundary

*Operational, not code-enforced — unlike everything else in this file. Recorded
here because a well-meaning "hardening" pass is exactly what would break it.*

Public reachability is **only** ever: Internet → HTTPS 443 → Nginx Proxy Manager
→ application backend. Direct backend ports are DROPped from the WAN at
`DOCKER-USER`; only 80 and 443 are allowed through.

**Five services are public on purpose.** For each, the security boundary is the
application's own authentication, not NPM:

| Host | Why it must stay public |
|---|---|
| `ha.ivn-group.cc` | Roommate has no Tailscale-capable device |
| `rq.ivn-group.cc` (Seerr) | Daily use from phone + Nvidia Shield |
| `jfin.ivn-group.cc` (Jellyfin) | Phone/TV clients that don't run Tailscale |
| `cloud.ivn-group.cc` (Nextcloud) | Public share links must work for external recipients |
| `immich.ivn-group.cc` (Immich) | Phone clients that may not use Tailscale |

**Do not take these private, and do not put NPM Basic Auth in front of them** —
Basic Auth breaks their native clients. Tailscale is not a substitute for any of
them.

**NPM admin (`:81`) stays private** — LAN or Tailscale only, DROPped from WAN.
It is control-plane: it can rewrite routing for every domain, which is how the
qBittorrent compromise was created in the first place.

**Never overwrite a live SQLite database in place** (`docker cp`, `cp`) while the
owning service is running. The process keeps the old inode and silently diverges
from what is on disk — this produced a real NPM outage-in-waiting where security
hardening appeared applied but was not. Use the supported API/UI, or stop the
service first.

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


## MQTT authentication — CLOSED 2026-09-03

The Mosquitto broker on the NAS (`192.168.1.63`) is **authenticated-only**:
`allow_anonymous false` + `password_file /mosquitto/secrets/passwd`. The
temporary anonymous state opened on 2026-09-02 is resolved.

**Two separate service accounts**, one per consumer, so either can be rotated or
revoked independently and the broker log attributes every connection:

| Account | Consumer | Configured via |
|---|---|---|
| `ha` | Home Assistant | supported MQTT **Reconfigure** flow — `.storage` never hand-edited |
| `frigate` | Frigate | `config.yml` `user: frigate` + `password: "{FRIGATE_MQTT_PASSWORD}"`, injected by compose `env_file` |

Credentials are **sha512-pbkdf2** hashes. Secret storage:

| Path | Owner | Mode |
|---|---|---|
| `/volume1/docker/mosquitto/secrets/` | `1883:1883` | `0700` |
| `…/secrets/passwd` (hashes only) | `1883:1883` | `0600` |
| `/volume1/docker/frigate/secrets/` | `root:root` | `0700` |
| `…/secrets/frigate.env` | `root:root` | `0600` |

The only plaintext on the host is the Frigate env file. The HA password exists
solely in the Boss's password manager — it was never written to disk.

**Proven, not assumed:** anonymous CONNECT returns `rc=5 NOT AUTHORIZED`, wrong
passwords for both real accounts are rejected, and both consumers reconnect
authenticated (`u'ha'`, `u'frigate'`) after enforcement.

**The publish binding is still a separate control and still matters.** The port
is published IPv4-only as `0.0.0.0:1883`; there is no `[::]:1883` listener.
**Do not revert the publish to `"1883:1883"`** — the NAS holds
globally-routable IPv6 addresses, and edge IPv6 filtering has never been
verified from inside the LAN. Authentication does not make that safe to undo.

### Remaining future hardening (deliberate, not oversights)

- **ACLs — deferred.** Frigate could be scoped to `frigate/#`, but Home
  Assistant legitimately needs broad publish/subscribe rights, so the gain is
  small while a mis-scoped ACL fails *silently*. Its own change, later.
- **TLS — deferred.** Credentials cross port 1883 unencrypted. Both consumers
  run on the NAS itself today, so nothing traverses the LAN in normal
  operation; TLS becomes worthwhile if an off-host client is ever added.
- **Cold-start not yet proven.** Authentication has only been exercised through
  warm recreates. `mosquitto.conf.p5-snapshot` (mixed mode) is retained as
  break-glass until a real NAS reboot shows both consumers reconnecting
  authenticated — then it must be deleted, because restoring it silently
  re-enables anonymous access.

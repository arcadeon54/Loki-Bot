# Durable AI Context

Model-independent institutional memory for the Loki project. Any capable coding
agent — Antigravity/Gemini, Claude, ChatGPT — should be able to become
productive here **without** access to any prior conversation.

Nothing in this directory depends on a vendor's chat history.

## Read in this order

| # | File | Why |
|---|---|---|
| 1 | [CURRENT_HANDOFF.md](CURRENT_HANDOFF.md) | What was just completed, what is active, what not to reopen. **Under a minute.** |
| 2 | [../../AGENTS.md](../../AGENTS.md) | Operating rules, safety, git policy, validation |
| 3 | [TASK_LEDGER.md](TASK_LEDGER.md) | The active objective and its DONE condition |
| 4 | [PROJECT_STATE.md](PROJECT_STATE.md) | Live snapshot: services, health, tests |

Then, as needed:

| File | Contents |
|---|---|
| [HOMELAB_INVENTORY.md](HOMELAB_INVENTORY.md) | dex247, razr, the UGREEN NAS — roles, access, units, traps |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Module map, tool system, approval gate, memory ownership |
| [SECURITY_BOUNDARIES.md](SECURITY_BOUNDARIES.md) | What is enforced in code and must not be widened |
| [OPERATIONS_POLICY.md](OPERATIONS_POLICY.md) | Restart policy, safe testing, git, end-of-task protocol |
| [INTEGRATIONS.md](INTEGRATIONS.md) | Every integration and its config variable **names** |
| [COMPLETED_WORK.md](COMPLETED_WORK.md) | Feature-by-feature DONE / PARTIAL / UNFINISHED / OBSOLETE |
| [DECISIONS.md](DECISIONS.md) | Settled architectural decisions and why they are closed |
| [CLAUDE_HISTORY_IMPORT.md](CLAUDE_HISTORY_IMPORT.md) | Traps already paid for, imported from Claude sessions |
| [ANTIGRAVITY_BOOTSTRAP.md](ANTIGRAVITY_BOOTSTRAP.md) | How to launch and drive `agy` here |
| [CHATGPT_HANDOFF.md](CHATGPT_HANDOFF.md) | Paste-ready continuity block for a fresh ChatGPT session |

## Source-of-truth precedence

1. Current live production state
2. Current repository code and configuration
3. Durable project documentation (this directory)
4. Git history
5. `CURRENT_HANDOFF.md`
6. Historical AI summaries

**Never let an old AI conversation override newer live evidence.** A document
that contradicts the running system is stale — verify, then fix the document.

## Agent customizations

Antigravity-specific configuration lives in [`.agents/`](../../.agents/):

- `rules/` — always-on constraints (completion-first, production safety, git,
  secrets, validation)
- `skills/` — on-demand deep knowledge, loaded only when relevant
- `agents/loki-builder/` — the project implementation agent
- `hooks.json` + `scripts/permission-guard.sh` — enforced ALLOW/ASK/DENY tiers

## Keeping this current

After meaningful completed work, update `PROJECT_STATE.md`,
`COMPLETED_WORK.md`, `TASK_LEDGER.md`, and `CURRENT_HANDOFF.md` — plus
`DECISIONS.md` if a durable architectural decision was made. See the end-of-task
protocol in [OPERATIONS_POLICY.md](OPERATIONS_POLICY.md). Keep it lightweight
for trivial edits.

The older, audit-era documents in [`../`](..) (`PROJECT_STATE.md`,
`NEXT_STEPS.md`, `HISTORICAL_HANDOFF.md`, `SESSION_RECOVERY.md`,
`NAS_MAINTENANCE.md`) remain valid as history and detail; this directory is the
current, agent-facing layer above them.

# Antigravity Bootstrap

How to run Antigravity (`agy`) against the Loki project on dex247.

| | |
|---|---|
| Version | 1.1.10 |
| Binary | `/home/g2k247/.local/bin/agy` |
| Account | `g2k247` (never root) |
| Installed | 2026-08-06, official installer from `https://antigravity.google/cli/install.sh` |
| Coexists with | Claude Code at `/home/g2k247/.local/bin/claude` — both remain usable |
| App data | `~/.gemini/antigravity-cli/`, config `~/.gemini/config/` |

`~/.local/bin` was already on `PATH` via both `.bashrc` and `.profile`; the
installer's duplicate exports were removed so `PATH` does not stack.

## 1. Launching

```bash
cd /home/g2k247/loki-bot
agy
```

The repository root is the workspace root, which is what makes `AGENTS.md`,
`GEMINI.md`, and `.agents/` discoverable. **Always launch from the repo root** —
starting elsewhere and adding the directory later does not reliably load
workspace rules.

Useful flags:

| Flag | Use |
|---|---|
| `-p "<prompt>"` | One-shot non-interactive run, prints and exits |
| `-i "<prompt>"` | Start interactive with an opening prompt |
| `-c` | Continue the most recent conversation |
| `--conversation <id>` | Resume a specific conversation |
| `--agent <name>` | Select an agent for the session |
| `--mode plan` | Plan-only; no edits |
| `--effort low\|medium\|high` | Reasoning effort |
| `--sandbox` | Terminal restrictions on |

**Never use `--dangerously-skip-permissions` on dex247.** It auto-approves every
tool permission request, which defeats the ASK tier and the safety hook.

## 2. Normal fresh-session prompt

```
Read AGENTS.md and docs/agent-context/CURRENT_HANDOFF.md.
Confirm what was most recently completed and what the current active objective
is, then wait for my instruction. Do not start work I have not requested.
```

## 3. Resume-current-task prompt

```
Continue the active objective in docs/agent-context/TASK_LEDGER.md.
Restate the DONE condition before you touch anything, read the relevant skill in
.agents/skills/, then continue. Work completion-first: fix defects you hit in
that path and keep going. Do not restart completed investigations.
```

Or simply `agy -c` to continue the previous conversation with its context.

## 4. Session-lost recovery prompt

```
My previous Antigravity session was lost. Rebuild your context from the project
only — do not ask me to re-explain the homelab.

Read in this order:
  1. AGENTS.md
  2. docs/agent-context/CURRENT_HANDOFF.md
  3. docs/agent-context/TASK_LEDGER.md
  4. docs/agent-context/PROJECT_STATE.md
  5. the relevant .agents/skills/<name>/SKILL.md

Then tell me: what was completed last, what is active, what DONE means for it,
which investigations are closed, and what you propose as the next action.
Verify anything time-sensitive against live state (systemctl, journalctl,
read-only DB queries) before trusting a document.
```

## 5. Using the Loki Builder agent

```bash
agy --agent loki-builder
```

Defined at `.agents/agents/loki-builder/agent.md`. Use it for implementation
work on Loki: it reads the handoff first, works completion-first, owns one
active task, preserves the security architecture, and does not push git.

If `--agent loki-builder` is not resolved by your build, the same behaviour is
available by opening a normal session and saying:

```
Adopt the role defined in .agents/agents/loki-builder/agent.md for this session.
```

The agent file is plain markdown, so this fallback is exact, not approximate.
`agy agents` lists agents available to your account and requires sign-in.

## 6. How context, rules and skills load

| Type | Location | Loading |
|---|---|---|
| Rules | `AGENTS.md`, `GEMINI.md` (any directory, walked up to repo root) | Always active for that directory scope. No frontmatter. Deduplicated |
| Rules | `.agents/rules/*.md` | Always active |
| Skills | `.agents/skills/<name>/SKILL.md` | **On-demand.** The agent reads the frontmatter `description` and pulls the body only when relevant |
| Agents | `.agents/agents/<name>/agent.md` | Selected via `--agent` |
| Hooks | `.agents/hooks.json` | Lifecycle events; can `allow`/`ask`/`deny` a tool call |
| MCP | `mcp_config.json` | Not used by this project |

Skills use **progressive disclosure**: keep `SKILL.md` concise, put bulk in
`references/`. That is why the skills here are short and point at real source
files rather than restating the whole project.

The customization system itself is documented offline in the builtin
`agy-customizations` skill (`~/.gemini/antigravity-cli/builtin/skills/`).

## 7. Authentication

Antigravity signs in with a **Google OAuth flow**, launched by running `agy`
with no arguments. Tokens are stored in the system keyring with a file fallback
under `~/.gemini/` — they are never written into this repository, and must never
be pasted into a prompt, a document, a log, or a command argument.

Verify sign-in with:

```bash
agy models        # errors with "Please sign in" when unauthenticated
```

On a headless or SSH session the CLI prints a URL to open manually. `agy -p`
will trigger an interactive OAuth if the session is not authenticated, so
authenticate first before scripting anything.

## 8. Permission policy on dex247

Enforced by `.agents/hooks.json` → `.agents/scripts/permission-guard.sh`, and
documented in `.agents/rules/production-safety.md`.

- **ALLOW** — repository reads and edits, `git status`/`diff`/`log`, focused
  tests, read-only service and log diagnostics.
- **ASK** — service restarts, container mutation, privileged NAS deployment,
  destructive cleanup, anything writing outside the repo.
- **DENY** — `git push`, unrestricted `sudo`, root SSH, `.ssh` modification,
  reading `.env`/secrets, `rm -rf`-style destruction, Docker prune or volume
  deletion.

Antigravity is **not** configured as an unrestricted autonomous root agent, and
must not be reconfigured as one.

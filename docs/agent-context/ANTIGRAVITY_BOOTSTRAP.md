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

## 5. Using the Loki Builder role

**`--agent loki-builder` does not work on agy 1.1.10.** Verified live: the flag
is accepted silently and the session keeps the default Antigravity persona.
`agy agents` lists only account/server-side agents and returns nothing for
`.agents/agents/`, which is not a discovered customization type in this build
(the shipped `json_configs.md` documents `skills.json` and `plugins.json` only).

The role is therefore reached through a **skill**, which this build does
discover:

```
Use the loki-builder skill.
```

or simply ask for it by name — "work as Loki Builder". Either way the agent
reads `.agents/skills/loki-builder/SKILL.md`, which points at the canonical
definition in `.agents/agents/loki-builder/agent.md` and adopts it.

The explicit fallback, exact rather than approximate, is:

```
Read .agents/agents/loki-builder/agent.md and adopt that role for this session.
```

`agent.md` is retained as the canonical role definition — it is the single
source the skill and the fallback both point at, and it is what a future agy
version (or another Antigravity surface) would consume if `--agent` gains
workspace support.

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

Two layers. The settings file opens the gate; the hook is the actual policy.

### Layer 1 — `~/.gemini/antigravity-cli/settings.json`

User-level, outside the repo, so it is not committed. Without a
`permissions.allow` block, headless (`-p`) runs fail immediately with *"a tool
required the read_file permission that headless mode cannot prompt for"*.

Rule syntax verified empirically against 1.1.10 — the docs do not specify it:

- Globs are used: `read_file(**)`, `command(*)`.
- **`**` does not cross directory separators.** `read_file(**)` matches
  root-level files only; nested paths need their own entries
  (`read_file(docs/**)`, `read_file(docs/agent-context/**)`, `read_file(*/**)`).
  This is the single most confusing failure here — a nested read is denied while
  a root read succeeds under the same config.
- A plain directory rule (`read_file(/home/g2k247/loki-bot)`) does **not** grant
  recursive access despite the shipped note suggesting it does.
- Both workspace-relative and absolute forms are accepted; the installed config
  lists both.

`command(*)` is allowed at this layer deliberately, because the hook below is
where command policy is actually decided.

### Layer 2 — `.agents/hooks.json` → `.agents/scripts/permission-guard.sh`

Tracked in the repo, so the policy travels with the project. Matches
`run_command|read_file|write_file|edit_file`.

- **ALLOW** — repository reads and edits, `git status`/`diff`/`log`, focused
  tests, read-only service and log diagnostics.
- **ASK** — service restarts, container mutation, git state changes, package
  installs, `sudo`, NAS/razr actions, recursive deletes.
- **DENY** — `git push`, Docker prune, volume removal, `.env`/`.ssh`/credential
  reads by *any* tool, root SSH, `sshpass`, disabling host-key verification,
  `chmod 777`, docker-group membership, `rm -rf` of a root or home path.

Unparseable input falls through to agy's own prompting, so a hook bug can never
silently widen permissions. In headless mode an **ASK** cannot be answered and
is auto-denied — it fails closed.

**Never use `--dangerously-skip-permissions` on dex247.** It bypasses layer 1
entirely. Antigravity is not configured as an unrestricted autonomous root
agent and must not be reconfigured as one.

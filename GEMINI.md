# Loki — Gemini / Antigravity Entry Point

The operating rules for this repository live in **[AGENTS.md](./AGENTS.md)**.
Antigravity loads both files and deduplicates rules, so the substance is kept
in one place rather than mirrored here — a divergent copy would be worse than
no copy.

Read, in this order:

1. **[AGENTS.md](./AGENTS.md)** — production safety, git policy, completion-first,
   source-of-truth precedence, validation commands.
2. **[docs/agent-context/CURRENT_HANDOFF.md](./docs/agent-context/CURRENT_HANDOFF.md)**
   — what was just completed, what is active now, what DONE means, what not to
   reopen. Under a minute.
3. The relevant skill in `.agents/skills/<name>/SKILL.md` before touching any
   subsystem.

For implementation work, use the **loki-builder** agent
(`.agents/agents/loki-builder/agent.md`).

Full durable context index: **[docs/agent-context/](./docs/agent-context/)**.

This is a live production service. `loki.service` runs this working tree.

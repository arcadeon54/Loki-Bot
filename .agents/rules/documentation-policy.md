# Documentation Policy — Two Layers

There are two documentation layers, and a substantial homelab task is not done
until both have been considered.

1. **Repository agent context** (`docs/agent-context/` — `CURRENT_HANDOFF.md`,
   `COMPLETED_WORK.md`, `TASK_LEDGER.md`, `DECISIONS.md`, `HOMELAB_INVENTORY.md`,
   `PROJECT_STATE.md`) — optimized for Claude Code, Antigravity, and future AI
   sessions picking up where the last one left off.
2. **Joplin homelab knowledge base** — the Boss's permanent, human-readable
   reference. Primarily the **Homelab** notebook (machines, network, storage,
   services — human-curated) and, for Loki's own internals, **Homelab/L.O.K.I.**
   and **Loki/Documentation**. The skillkit-maintained **Loki Architecture**
   notebook (one note per subsystem, archdoc format) is a separate, machine-owned
   layer — do not hand-edit it; that's skillkit's `archdoc` tool's job.

## When a substantial homelab task reaches DONE

1. Update the repository agent-context files as appropriate (this was already
   the rule; unchanged).
2. Update the **appropriate existing** Joplin homelab note. Read the current
   notebook tree first — do not assume a note's name or content from memory.
3. Create a new Joplin note **only** when no suitable authoritative note
   already exists for that topic.
4. Do not duplicate documentation unnecessarily. If in doubt, append a dated
   update section to the existing note rather than writing a parallel one.
5. Do not put secrets, tokens, or credentials in Joplin.
6. Do not dump debugging transcripts, raw logs, or hundreds of test outputs
   into Joplin. Joplin gets durable information: purpose, topology, important
   paths, dependencies, current state, root cause (when something was fixed),
   the permanent repair, recovery behaviour, operational warnings, and
   approval/security boundaries — written so it's still useful six months from
   now.
7. Document the final verified state, not speculation or an in-progress plan.
8. Documentation happens at task completion, not after every minor edit.

A task should not be reported as fully documented until both layers have been
considered — not necessarily both edited, since not every change needs a
Joplin update, but both weighed.

## Verifying a Joplin write

Loki's `joplin_integration.py` is available directly (`from dotenv import
load_dotenv; load_dotenv()` before importing it, or run inside the live
process). After writing:

- Read the note back (`get_note`) — don't trust the write call's return code.
- Confirm it landed in the intended notebook (`notebook_notes` /
  `get_folder_tree`), not a duplicate elsewhere.
- Check `sync_health()` reports `healthy` before calling it done.

See `.agents/skills/joplin/SKILL.md` for the note-lookup rules (unscoped
search must walk every notebook, never rely on `/search` immediately after a
write) and why the CLI sidecar must stay stopped.

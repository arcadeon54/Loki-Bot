"""
assistant_tools.py — Boss-level tools for the July 2026 personal-AI upgrade.

Registers into the existing tools.py REGISTRY (same permission model, same
audit log, same execute() path), so both Discord and Telegram get them
through LLMHandler.chat_with_tools with zero interface-specific code.

Tools:
    remember          store a fact/preference/recipe/project in long-term memory
    recall_memory     semantic search over stored memories
    note_create       create a Joplin note
    note_search       full-text search all Joplin notes
    note_read         read a note by title/search
    note_append       append to (or create) a note
    list_create       create a checkbox to-do list note
    home_status       Home Assistant entity states matching a query
    home_control      natural-language Home Assistant command
    work_hours        work-session report (the 90-min tracker)

Import this module once at startup (after tools); registration is a side
effect, mirroring how tools.py self-registers.
"""

import logging

from tools import ToolSpec, ToolContext, register

log = logging.getLogger("AssistantTools")

_deps: dict = {}


def bind(**deps):
    """loki_bot injects: llm (for home_control)."""
    _deps.update(deps)


# ─── Memory ──────────────────────────────────────────────────────────────────

async def _remember(args: dict, ctx: ToolContext) -> str:
    import semantic_memory
    text = str(args.get("text", "")).strip()
    kind = str(args.get("kind", "fact")).strip().lower()
    context = str(args.get("context", "")).strip()
    if not text:
        return "Nothing to remember — empty text."
    try:
        res = await semantic_memory.remember(text, kind=kind, context=context)
        verb = "Updated existing memory" if res["action"] == "updated" else "Remembered"
        return f"{verb}: \"{text[:120]}\" (note: {res['title']})"
    except Exception as e:
        log.error(f"remember failed: {e}")
        return "Couldn't write that to memory — Joplin might be syncing. Try again in a minute."


async def _recall_memory(args: dict, ctx: ToolContext) -> str:
    import semantic_memory
    query = str(args.get("query", "")).strip()
    if not query:
        return "Empty memory query."
    hits = await semantic_memory.recall(query, n=6)
    if not hits:
        return f"No stored memories match '{query}'."
    return "Stored memories (closest first):\n" + "\n".join(
        f"- ({h['kind']}) {h['text']}" for h in hits)


# ─── Joplin notes ────────────────────────────────────────────────────────────

async def _note_create(args: dict, ctx: ToolContext) -> str:
    import joplin_integration as jp
    title = str(args.get("title", "")).strip()
    body = str(args.get("body", ""))
    notebook = str(args.get("notebook", "")).strip() or None
    if not title:
        return "A note needs a title."
    note = await jp.create_note(title, body, notebook)
    where = notebook or f"{jp.LOKI_NOTEBOOK}/Inbox"
    return f"Note created: \"{title}\" in {where} (syncs to your devices in ~5 min)."


async def _note_search(args: dict, ctx: ToolContext) -> str:
    import joplin_integration as jp
    query = str(args.get("query", "")).strip()
    if not query:
        return "Empty search."
    hits = await jp.search_notes(query, limit=8)
    if not hits:
        return f"No notes match '{query}'."
    lines = []
    for h in hits:
        snippet = " ".join((h.get("body") or "").split())[:140]
        lines.append(f"- **{h['title']}** — {snippet}")
    return f"Notes matching '{query}':\n" + "\n".join(lines)


async def _note_read(args: dict, ctx: ToolContext) -> str:
    import joplin_integration as jp
    query = str(args.get("title", "")).strip()
    if not query:
        return "Which note?"
    note = await jp.find_note_by_title(query)
    if not note:
        hits = await jp.search_notes(query, limit=1)
        note = hits[0] if hits else None
    if not note:
        return f"Couldn't find a note like '{query}'."
    body = note.get("body", "")
    if len(body) > 3500:
        body = body[:3500] + "\n…(truncated)"
    return f"# {note['title']}\n\n{body}"


async def _note_append(args: dict, ctx: ToolContext) -> str:
    import joplin_integration as jp
    title = str(args.get("title", "")).strip()
    content = str(args.get("content", "")).rstrip()
    notebook = str(args.get("notebook", "")).strip() or f"{jp.LOKI_NOTEBOOK}/Inbox"
    if not title or not content:
        return "Need both a note title and content to append."
    await jp.append_or_create(title, notebook, content)
    return f"Appended to \"{title}\"."


async def _list_create(args: dict, ctx: ToolContext) -> str:
    import joplin_integration as jp
    title = str(args.get("title", "")).strip()
    items = args.get("items") or []
    notebook = str(args.get("notebook", "")).strip() or f"{jp.LOKI_NOTEBOOK}/Lists"
    if not title or not isinstance(items, list) or not items:
        return "Need a list title and at least one item."
    body = "\n".join(f"- [ ] {str(i).strip()}" for i in items if str(i).strip())
    existing = await jp.find_note_by_title(title, notebook)
    if existing:
        await jp.append_to_note(existing["id"], body)
        return f"Added {len(items)} item(s) to the existing \"{title}\" list."
    await jp.create_note(title, body, notebook, tags=["list"])
    return f"List \"{title}\" created with {len(items)} item(s)."


# ─── Home Assistant ──────────────────────────────────────────────────────────

async def _home_status(args: dict, ctx: ToolContext) -> str:
    import ha_integration
    query = str(args.get("query", "")).strip().lower()
    states = await ha_integration.get_all_states()
    if not states:
        return "Couldn't reach Home Assistant."
    words = [w for w in query.split() if len(w) > 2]
    matches = []
    for s in states:
        hay = (s["entity_id"] + " "
               + str(s.get("attributes", {}).get("friendly_name", ""))).lower()
        if not words or all(w in hay for w in words) or any(w in hay for w in words):
            score = sum(w in hay for w in words)
            matches.append((score, s))
    matches.sort(key=lambda x: -x[0])
    top = [m[1] for m in matches[:12]]
    if not top:
        return f"No Home Assistant entities match '{query}'."
    lines = []
    for s in top:
        name = s.get("attributes", {}).get("friendly_name") or s["entity_id"]
        unit = s.get("attributes", {}).get("unit_of_measurement", "")
        lines.append(f"- {name}: {s['state']}{(' ' + unit) if unit else ''}")
    return "\n".join(lines)


async def _home_control(args: dict, ctx: ToolContext) -> str:
    import ha_integration
    request = str(args.get("request", "")).strip()
    if not request:
        return "What should I do with the house?"
    llm = _deps.get("llm")
    if llm is None:
        return "Home control isn't wired up."
    return await ha_integration.ha_control(request, llm)


# ─── Work hours ──────────────────────────────────────────────────────────────

async def _work_hours(args: dict, ctx: ToolContext) -> str:
    import work_tracker
    try:
        days = min(max(int(args.get("days", 14)), 1), 120)
    except (TypeError, ValueError):
        days = 14
    return work_tracker.report(days)


# ─── Registration ────────────────────────────────────────────────────────────

register(ToolSpec(
    name="remember",
    description=("Store something the Boss wants remembered long-term: a fact, "
                 "preference, recipe, project detail, or a conversation summary. "
                 "Use proactively when he states durable personal info "
                 "(e.g. 'my scooter tires stay at 45 PSI')."),
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string",
                     "description": "The memory, self-contained and specific "
                                    "(include the subject: 'scooter tires: 45 PSI', "
                                    "not just '45 PSI')"},
            "kind": {"type": "string",
                     "enum": ["fact", "preference", "recipe", "project",
                              "conversation", "list"]},
            "context": {"type": "string",
                        "description": "Optional extra context (why/when)"},
        },
        "required": ["text"],
    },
    handler=_remember, permission="boss",
))

register(ToolSpec(
    name="recall_memory",
    description=("Search the Boss's long-term memory semantically. Use before "
                 "answering any question about his preferences, settings, "
                 "recipes, projects, or anything personal ('what pressure do "
                 "I use?', 'what was that recipe?')."),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    handler=_recall_memory, permission="boss",
))

register(ToolSpec(
    name="note_create",
    description="Create a new note in the Boss's Joplin. Give it a clear title and markdown body.",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "body": {"type": "string"},
            "notebook": {"type": "string",
                         "description": "Notebook path like 'Loki/Projects' or "
                                        "'Kitchen Corner'. Omit for the Loki inbox."},
        },
        "required": ["title", "body"],
    },
    handler=_note_create, permission="boss",
))

register(ToolSpec(
    name="note_search",
    description="Full-text search across ALL of the Boss's Joplin notes (recipes, homelab docs, journals...).",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    handler=_note_search, permission="boss",
))

register(ToolSpec(
    name="note_read",
    description="Read the full content of one Joplin note by (approximate) title.",
    parameters={
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    },
    handler=_note_read, permission="boss",
))

register(ToolSpec(
    name="note_append",
    description="Append markdown content to a Joplin note (created if missing).",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "content": {"type": "string"},
            "notebook": {"type": "string", "description": "Only used if the note must be created"},
        },
        "required": ["title", "content"],
    },
    handler=_note_append, permission="boss",
))

register(ToolSpec(
    name="list_create",
    description="Create (or extend) a checkbox to-do/shopping list in Joplin.",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "items": {"type": "array", "items": {"type": "string"}},
            "notebook": {"type": "string"},
        },
        "required": ["title", "items"],
    },
    handler=_list_create, permission="boss",
))

register(ToolSpec(
    name="home_status",
    description=("Look up current Home Assistant entity states: temperatures, "
                 "lights, locks, who's home (persons), cameras, sensors. "
                 "Query by rough name, e.g. 'temperature', 'kavaris', 'front door'."),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
    handler=_home_status, permission="boss",
))

register(ToolSpec(
    name="home_control",
    description=("Control the house through Home Assistant with natural language: "
                 "lights, switches, fans, climate, media, run automations, send "
                 "phone notifications. E.g. 'turn off the bedroom lamp'."),
    parameters={
        "type": "object",
        "properties": {"request": {"type": "string"}},
        "required": ["request"],
    },
    handler=_home_control, permission="boss",
))

register(ToolSpec(
    name="work_hours",
    description=("Report the Boss's automatically tracked work sessions and "
                 "totals (the 90-minute rule tracker). Use for 'how many hours "
                 "did I work', 'am I on the clock', etc."),
    parameters={
        "type": "object",
        "properties": {"days": {"type": "integer",
                                "description": "Look-back window in days (default 14)"}},
    },
    handler=_work_hours, permission="boss",
))

log.info("Assistant tools registered (memory, notes, home, work)")

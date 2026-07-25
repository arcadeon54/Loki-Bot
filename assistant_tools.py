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

import json
import logging
import re

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
        return "Couldn't write that to memory — the Joplin note store isn't answering. Try again in a minute."


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
    return f"Note created: \"{title}\" in {where} ({jp.sync_summary()})."


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
    return f"Appended to \"{title}\" ({jp.sync_summary()})."


_CHECKBOX_RE = re.compile(r"^\s*-\s*\[[ xX]?\]\s*")
# For inbound items also tolerate checkbox markup without the dash ("[ ] milk").
_ITEM_PREFIX_RE = re.compile(r"^\s*(?:-\s*)?(?:\[[ xX]?\]\s*)?")

# Retry guard that needs no Joplin round-trip: lists created this process,
# keyed (folder_id, lowercased title) -> note_id. The Joplin /search index
# lags note creation (observed live 2026-07-19), so retries are deduped by
# this cache and by the direct folder listing, never by full-text search.
_recent_lists: dict[tuple[str, str], str] = {}


def _normalize_items(raw) -> list[str]:
    """Accept a list or a comma/newline-separated string; strip bullets and
    checkbox markup the model sometimes includes; dedupe case-insensitively."""
    if isinstance(raw, str):
        parts = re.split(r"[\n;,]+", raw)
    elif isinstance(raw, list):
        parts = [str(i) for i in raw]
    else:
        parts = []
    items: list[str] = []
    seen: set[str] = set()
    for p in parts:
        p = _ITEM_PREFIX_RE.sub("", p.strip().lstrip("•*").strip(), count=1).strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            items.append(p)
    return items


async def _list_create(args: dict, ctx: ToolContext) -> str:
    import joplin_integration as jp

    def fail(error: str, **extra) -> str:
        return json.dumps({"success": False, "error": error, **extra})

    title = str(args.get("title", "")).strip()
    items = _normalize_items(args.get("items"))
    explicit_nb = str(args.get("notebook", "")).strip()
    default_nb = f"{jp.LOKI_NOTEBOOK}/Lists"
    notebook = explicit_nb or default_nb

    if not title:
        return fail("missing_title",
                    fix="Call list_create again with a short title, e.g. 'Grocery List'.")
    if not items:
        return fail("missing_items",
                    fix="Call list_create again with every item in the `items` array, e.g. "
                        '{"title": "Grocery List", "items": ["Milk", "Eggs"]}. You DO have '
                        "Joplin write access — never tell the user you cannot write to Joplin.")
    if not jp.is_configured():
        return fail("joplin_not_configured",
                    fix="JOPLIN_API_TOKEN is not configured for the bot. Tell the user the "
                        "Joplin connection is down; the list was NOT saved.")

    try:
        # Explicitly named notebooks must already exist; only the default
        # Loki/Lists path may be auto-created (existing namespace behavior).
        folder_id = await jp.resolve_notebook_path(notebook, create=not explicit_nb)
        if not folder_id:
            return fail("notebook_not_found", notebook=notebook,
                        fix="That notebook doesn't exist. Retry with one of the Boss's real "
                            "notebooks, or omit `notebook` to use the default list notebook. "
                            "The list was NOT saved yet.")

        # Idempotency: an exact-title list is extended, never duplicated.
        # Deterministic lookups first — the /search full-text index lags new
        # notes, so it must never be what dedupe depends on.
        existing = None
        cached_id = _recent_lists.get((folder_id, title.lower()))
        if cached_id:
            cached = await jp.get_note(cached_id)
            if cached and cached.get("parent_id") == folder_id:
                existing = cached
            else:
                _recent_lists.pop((folder_id, title.lower()), None)
        if existing is None:
            existing = await jp.find_note_in_folder(title, folder_id)
        # Courtesy only, when no notebook was named: an exact-title list living
        # in some other notebook wins over creating a second one (best-effort —
        # search-index lag just means a miss here, never a duplicate in the
        # target notebook).
        if existing is None and not explicit_nb:
            hits = await jp.search_notes(f'title:"{title}"', limit=20)
            exact = [h for h in hits
                     if (h.get("title") or "").strip().lower() == title.lower()]
            if exact:
                existing = exact[0]
                folder_id = existing.get("parent_id") or folder_id
                notebook = await jp.folder_path_of(folder_id) or notebook

        if existing:
            body = ((await jp.get_note(existing["id"])) or {}).get("body", "")
            have = {_CHECKBOX_RE.sub("", l).strip().lower()
                    for l in body.splitlines() if _CHECKBOX_RE.match(l)}
            new_items = [i for i in items if i.lower() not in have]
            if new_items:
                await jp.append_to_note(existing["id"],
                                        "\n".join(f"- [ ] {i}" for i in new_items))
            note_id, added = existing["id"], len(new_items)
            _recent_lists[(folder_id, title.lower())] = note_id
            message = (f"Added {added} item(s) to the existing '{title}' list in Joplin "
                       f"under '{notebook}'." if added else
                       f"'{title}' in Joplin under '{notebook}' already has all of those "
                       "items; nothing was added.")
        else:
            note = await jp.create_note(
                title, "\n".join(f"- [ ] {i}" for i in items), notebook, tags=["list"])
            note_id = (note or {}).get("id")
            if not note_id:
                return fail("create_failed",
                            detail="Joplin did not return a note id — the list was NOT saved.")
            _recent_lists[(folder_id, title.lower())] = note_id
            added = len(items)
            message = f"Created '{title}' in Joplin under '{notebook}' with {added} items."

        sync = jp.sync_health()
        return json.dumps({
            "success": True, "note_id": note_id, "title": title,
            "notebook_id": folder_id, "notebook_title": notebook,
            "item_count": added,
            "message": f"{message} ({jp.sync_summary(sync)}.)",
            "sync_state": sync["state"],
        })
    except jp.JoplinError as e:
        log.error(f"list_create Joplin error: {str(e)[:200]}")
        return fail("joplin_api_error", detail=str(e)[:200],
                    fix="The list was NOT saved. Tell the user honestly and offer to retry.")
    except Exception as e:
        log.error(f"list_create failed: {type(e).__name__}: {str(e)[:200]}")
        return fail("unexpected_error", detail=f"{type(e).__name__}: {str(e)[:120]}",
                    fix="The list was NOT saved. Tell the user honestly.")


async def _joplin_sync_status(args: dict, ctx: ToolContext) -> str:
    import joplin_integration as jp
    h = jp.sync_health()
    api_up = await jp.ping()
    return json.dumps({
        "local_api": "up" if api_up else "DOWN — Loki cannot read/write notes",
        "device_sync": h["state"],
        "last_sync_attempt": h["last_attempt"],
        "detail": h["detail"],
        "meaning": ("Local saves work and reach the Boss's devices."
                    if api_up and h["state"] == "healthy" else
                    "Local saves work, but new notes will NOT show up on the "
                    "Boss's phone/desktop until device sync is repaired."
                    if api_up else
                    "Note reads/writes are failing entirely right now."),
    })


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


def _home_control_prepare(args: dict, ctx: ToolContext):
    """Validate/normalize a house command into a minimal payload + safe summary.
    Home Assistant changes are consequential, so this stages a draft rather than
    acting (see tools.ToolSpec.action_type)."""
    request = str(args.get("request", "")).strip()
    if not request:
        return {}, "", "What should I do with the house?"
    if len(request) > 500:
        return {}, "", "That house command is too long — keep it to one action."
    return {"request": request}, f"Home Assistant command: “{request}”", ""


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
    handler=_remember, permission="boss", redact_log=True,
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
    handler=_recall_memory, permission="boss", redact_log=True,
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
    handler=_note_read, permission="boss", redact_log=True,
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
    description=("FIRST CHOICE whenever the Boss asks to make or save ANY list — "
                 "grocery, shopping, to-do, packing, or a named list. Creates (or "
                 "extends) a real checkbox note in his Joplin; a list that only "
                 "exists in this chat is lost. Pass EVERY item in `items`."),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string",
                      "description": "List name, e.g. 'Grocery List'"},
            "items": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                      "description": "All list entries, one string each — never empty"},
            "notebook": {"type": "string",
                         "description": "Existing notebook path only if the Boss "
                                        "names one; omit for the default list notebook"},
        },
        "required": ["title", "items"],
    },
    handler=_list_create, permission="boss", timeout=45,
))

register(ToolSpec(
    name="joplin_sync_status",
    description=("Check Joplin health: whether Loki's local note storage is up "
                 "AND whether notes are syncing to the Boss's phone/desktop. "
                 "Use when a note or list seems missing on his devices — a "
                 "missing note usually means device sync is failing, not that "
                 "Loki lacks write access. Read-only."),
    parameters={"type": "object", "properties": {}},
    handler=_joplin_sync_status, permission="boss",
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
    # Consequential: house changes are staged as a draft and only run once the
    # Boss approves the exact draft ID (see draft_approval.py).
    action_type="ha_control", approval_ttl=1800,
    prepare=_home_control_prepare, redact_log=True,
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

"""
joplin_integration.py — Joplin as Loki's long-term memory (July 2026 upgrade).

Talks to the Joplin Data API exposed by the `loki-joplin-api` sidecar
container on dex247 (joplin-cli syncing with notes.ivn-group.cc, see
/home/g2k247/docker/joplin-api/). Writes land locally immediately; whether
they reach the Boss's phone/desktop depends on the sidecar's sync loop —
sync_health() reports that separately, and callers must never promise
device visibility unless it says the sync is healthy.

Design notes:
- Notebook paths are slash-delimited ("Loki/Memories"); resolve_notebook_path
  walks the tree, creates missing levels, and caches IDs in-process (1h TTL).
- All network calls retry once on transient failure (the sidecar restarts
  occasionally when Watchtower updates the stack).
- Loki's own namespace is the "Loki/" notebook; it may READ anywhere but
  by default only auto-files new notes under its own namespace unless the
  caller passes an explicit notebook path.
"""

import asyncio
import json
import logging
import os
import re
import time
from urllib.parse import quote

import aiohttp

log = logging.getLogger("Joplin")

JOPLIN_API_URL   = os.getenv("JOPLIN_API_URL", "http://127.0.0.1:41184").rstrip("/")
JOPLIN_API_TOKEN = os.getenv("JOPLIN_API_TOKEN", "")

# The sidecar's own log file (bind-mounted profile, world-readable). Reading
# it is the only credential-free way to observe the sync loop's health.
JOPLIN_SYNC_LOG = os.getenv(
    "JOPLIN_SYNC_LOG", "/home/g2k247/docker/joplin-api/data/log.txt")
# Sync loop runs every ~5 min; older than this and we call the status stale.
JOPLIN_SYNC_STALE_SECS = int(os.getenv("JOPLIN_SYNC_STALE_SECS", "1800"))

# Default namespace for notes Loki creates on its own initiative.
LOKI_NOTEBOOK = os.getenv("LOKI_NOTEBOOK", "Loki")

_CACHE_TTL = 3600  # notebook path → id cache
_folder_cache: dict[str, tuple[str, float]] = {}

# Injected by loki_bot.py so we reuse the bot's shared session; falls back to
# its own session when running standalone (tests, scripts).
_session_factory = None
_own_session: aiohttp.ClientSession | None = None


def bind_session(factory):
    global _session_factory
    _session_factory = factory


async def _session() -> aiohttp.ClientSession:
    global _own_session
    if _session_factory is not None:
        return await _session_factory()
    if _own_session is None or _own_session.closed:
        _own_session = aiohttp.ClientSession()
    return _own_session


class JoplinError(Exception):
    pass


async def _request(method: str, path: str, params: dict | None = None,
                   body: dict | None = None, retries: int = 1):
    """One Data API call with token, JSON handling and a single retry."""
    if not JOPLIN_API_TOKEN:
        raise JoplinError("JOPLIN_API_TOKEN not configured")
    params = dict(params or {})
    params["token"] = JOPLIN_API_TOKEN
    url = f"{JOPLIN_API_URL}{path}"
    last_err = None
    for attempt in range(retries + 1):
        try:
            sess = await _session()
            async with sess.request(
                method, url, params=params, json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as r:
                if r.status == 404:
                    return None
                if r.status >= 400:
                    text = await r.text()
                    raise JoplinError(f"{method} {path} -> {r.status}: {text[:200]}")
                if r.content_type == "application/json" or "json" in (r.content_type or ""):
                    return await r.json()
                return await r.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            last_err = e
            if attempt < retries:
                await asyncio.sleep(1.5)
    raise JoplinError(f"{method} {path} failed after retries: {last_err}")


async def _paginated(path: str, params: dict | None = None) -> list[dict]:
    """Fetch all pages of a Data API list endpoint."""
    items, page = [], 1
    params = dict(params or {})
    while True:
        params["page"] = page
        data = await _request("GET", path, params=params)
        if not data:
            break
        items.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        page += 1
        if page > 200:  # safety
            break
    return items


def is_configured() -> bool:
    return bool(JOPLIN_API_TOKEN)


async def ping() -> bool:
    try:
        sess = await _session()
        async with sess.get(f"{JOPLIN_API_URL}/ping",
                            timeout=aiohttp.ClientTimeout(total=5)) as r:
            return r.status == 200
    except Exception:
        return False


# ─── Sync health (read-only) ──────────────────────────────────────────────────

_SYNC_START_MARK = "Starting synchronisation"
_SYNC_DONE_MARK = "Operations completed"
_LOG_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
# Belt-and-braces: never surface anything credential-shaped from the log.
_CRED_RE = re.compile(
    r"(?i)(token|password|secret|authorization|bearer)\S*[=:]\s*\S+")


def sync_health() -> dict:
    """Health of the sidecar's Joplin-Server sync, from its log tail.

    Purely read-only and credential-free. Returns:
      {"state": "healthy"|"failing"|"stale"|"unknown",
       "detail": short human string, "last_attempt": "YYYY-mm-dd HH:MM:SS"|None}

    Local Data-API writes always succeed independently of this — this only
    says whether those writes are reaching the Boss's other devices.
    """
    try:
        with open(JOPLIN_SYNC_LOG, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 131072))
            tail = f.read().decode("utf-8", "replace")
    except OSError as e:
        return {"state": "unknown", "last_attempt": None,
                "detail": f"sync log not readable ({type(e).__name__})"}

    # Latest attempt that actually finished (start mark followed by the
    # completion mark) — an attempt still in flight is not judged.
    starts = [m.start() for m in re.finditer(_SYNC_START_MARK, tail)]
    segment = start_pos = None
    for pos in reversed(starts):
        seg = tail[pos:]
        nxt = seg.find(_SYNC_START_MARK, 1)
        seg = seg if nxt == -1 else seg[:nxt]
        if _SYNC_DONE_MARK in seg:
            segment, start_pos = seg, pos
            break
    if segment is None:
        return {"state": "unknown", "last_attempt": None,
                "detail": "no completed sync attempt in recent log"}

    line_start = tail.rfind("\n", 0, start_pos) + 1
    ts_match = _LOG_TS_RE.search(tail[line_start:start_pos + len(_SYNC_START_MARK)])
    last_attempt = ts_match.group(1) if ts_match else None

    stale = False
    if last_attempt:
        try:
            attempt_t = time.mktime(time.strptime(last_attempt,
                                                  "%Y-%m-%d %H:%M:%S"))
            stale = (time.time() - attempt_t) > JOPLIN_SYNC_STALE_SECS
        except ValueError:
            pass

    errors = [l for l in segment.splitlines() if "[error]" in l]
    if errors:
        detail = errors[-1].split("[error]", 1)[1].strip()
        detail = _CRED_RE.sub("[REDACTED]", detail)[:160]
        return {"state": "failing", "last_attempt": last_attempt,
                "detail": detail or "sync error"}
    if stale:
        return {"state": "stale", "last_attempt": last_attempt,
                "detail": "last completed sync attempt is old — sync loop "
                          "may be stuck"}
    return {"state": "healthy", "last_attempt": last_attempt,
            "detail": "last sync attempt completed without errors"}


def sync_summary(health: dict | None = None) -> str:
    """One honest parenthetical about device visibility, for tool replies."""
    h = health or sync_health()
    return {
        "healthy": "saved locally; device sync is healthy, so it should reach "
                   "your other devices within a few minutes",
        "failing": "saved locally in Joplin, BUT sync to your devices is "
                   "currently FAILING — it will not appear on your phone or "
                   "desktop until sync is repaired",
        "stale":   "saved locally in Joplin, but the sync loop looks stalled — "
                   "it may not reach your devices until sync catches up",
    }.get(h["state"],
          "saved locally in Joplin; device-sync status could not be confirmed")


# ─── Notebooks ────────────────────────────────────────────────────────────────

async def get_folder_tree() -> list[dict]:
    return await _paginated("/folders", {"fields": "id,title,parent_id"})


async def resolve_notebook_path(path: str, create: bool = True) -> str | None:
    """Walk 'A/B/C' and return C's folder id, creating missing levels.

    Also accepts the arrow/gt forms the Boss types in chat ('A → B', 'A > B')
    by normalizing them to '/' first.

    Positive results cached 1h; creation is idempotent-ish (we re-list on
    every miss so concurrent creators converge)."""
    path = path.replace("→", "/").replace(">", "/")
    path = path.strip().strip("/")
    if not path:
        return None
    cached = _folder_cache.get(path)
    if cached and time.monotonic() - cached[1] < _CACHE_TTL:
        return cached[0]

    folders = await get_folder_tree()
    parent_id = ""
    current: list[str] = []
    for segment in path.split("/"):
        segment = segment.strip()
        if not segment:
            continue
        current.append(segment)
        match = next(
            (f for f in folders
             if f["title"] == segment and (f.get("parent_id") or "") == parent_id),
            None,
        )
        if match:
            parent_id = match["id"]
        elif create:
            created = await _request("POST", "/folders",
                                     body={"title": segment, "parent_id": parent_id})
            parent_id = created["id"]
            folders.append({"id": parent_id, "title": segment,
                            "parent_id": created.get("parent_id", "")})
            log.info(f"Created notebook '{'/'.join(current)}'")
        else:
            return None
        _folder_cache["/".join(current)] = (parent_id, time.monotonic())
    return parent_id


async def folder_path_of(folder_id: str) -> str:
    """Best-effort reverse lookup: folder id → 'A/B/C' path."""
    folders = {f["id"]: f for f in await get_folder_tree()}
    parts, cur, hops = [], folder_id, 0
    while cur and cur in folders and hops < 20:
        parts.append(folders[cur]["title"])
        cur = folders[cur].get("parent_id") or ""
        hops += 1
    return "/".join(reversed(parts))


class NotebookAmbiguous(JoplinError):
    """More than one notebook shares a bare name; never guessed between them."""

    def __init__(self, name: str, paths: list[str]):
        self.name = name
        self.paths = paths
        super().__init__(f"'{name}' matches multiple notebooks: {', '.join(paths)}")


_FOLDER_ID_RE = re.compile(r"^[0-9a-f]{32}$")


async def get_folder(folder_id: str) -> dict | None:
    return await _request("GET", f"/folders/{quote(folder_id)}",
                          {"fields": "id,title,parent_id"})


async def find_folders_by_name(name: str) -> list[dict]:
    """All notebooks whose own title matches `name` exactly (case-insensitive),
    regardless of where they sit in the tree — this is how duplicate-name
    ambiguity is detected."""
    name_l = name.strip().lower()
    return [f for f in await get_folder_tree() if f["title"].strip().lower() == name_l]


async def resolve_notebook_ref(name_or_path: str) -> tuple[str, str]:
    """Resolve a bare notebook name, a slash/arrow path, or a raw folder id to
    exactly one notebook. Returns (folder_id, display_path).

    Raises JoplinError if nothing matches, or NotebookAmbiguous if a bare name
    matches more than one notebook — callers must surface that to the Boss as
    a clarifying question rather than picking one."""
    ref = name_or_path.strip()
    if _FOLDER_ID_RE.match(ref):
        folder = await get_folder(ref)
        if not folder:
            raise JoplinError(f"No notebook with id '{ref}'.")
        return ref, await folder_path_of(ref)
    if "/" in ref or "→" in ref or ">" in ref:
        folder_id = await resolve_notebook_path(ref, create=False)
        if not folder_id:
            raise JoplinError(f"No notebook found at path '{ref}'.")
        return folder_id, await folder_path_of(folder_id)
    matches = await find_folders_by_name(ref)
    if not matches:
        raise JoplinError(f"No notebook named '{ref}'.")
    if len(matches) > 1:
        paths = [await folder_path_of(m["id"]) for m in matches]
        raise NotebookAmbiguous(ref, paths)
    folder_id = matches[0]["id"]
    return folder_id, await folder_path_of(folder_id)


def build_notebook_tree(folders: list[dict]) -> list[dict]:
    """Flat [{id,title,parent_id}] -> nested [{id,title,children:[...]}]."""
    by_id = {f["id"]: {"id": f["id"], "title": f["title"], "children": []}
             for f in folders}
    roots = []
    for f in folders:
        node = by_id[f["id"]]
        parent_id = f.get("parent_id") or ""
        if parent_id and parent_id in by_id:
            by_id[parent_id]["children"].append(node)
        else:
            roots.append(node)
    return roots


async def folder_children(folder_id: str) -> list[dict]:
    """Immediate child notebooks of folder_id."""
    return [f for f in await get_folder_tree()
            if (f.get("parent_id") or "") == folder_id]


async def notes_in_folder(folder_id: str, limit: int | None = None) -> list[dict]:
    """Notes directly in folder_id (not recursive), most recently updated first."""
    items = await _paginated(f"/folders/{folder_id}/notes",
                             {"fields": "id,title,updated_time"})
    items.sort(key=lambda n: n.get("updated_time") or 0, reverse=True)
    return items[:limit] if limit else items


async def move_note(note_id: str, folder_id: str) -> dict:
    """Move an existing note into a different notebook."""
    return await update_note(note_id, parent_id=folder_id)


# ─── Notes ────────────────────────────────────────────────────────────────────

async def create_note(title: str, body: str, notebook: str | None = None,
                      tags: list[str] | None = None) -> dict:
    """Create a note. `notebook` is a path like 'Loki/Memories'; defaults to
    the Loki namespace inbox."""
    folder_id = await resolve_notebook_path(notebook or f"{LOKI_NOTEBOOK}/Inbox")
    note = await _request("POST", "/notes", body={
        "title": title.strip()[:200],
        "body": body,
        "parent_id": folder_id,
    })
    if tags:
        await _apply_tags(note["id"], tags)
    log.info(f"Created note '{title[:60]}' in {notebook or LOKI_NOTEBOOK + '/Inbox'}")
    return note


async def get_note(note_id: str, fields: str = "id,title,body,parent_id,updated_time") -> dict | None:
    return await _request("GET", f"/notes/{note_id}", {"fields": fields})


async def update_note_body(note_id: str, body: str) -> dict:
    return await _request("PUT", f"/notes/{note_id}", body={"body": body})


async def update_note(note_id: str, **fields) -> dict:
    """Update arbitrary note fields (title, body, parent_id...)."""
    allowed = {k: v for k, v in fields.items()
               if k in ("title", "body", "parent_id", "is_todo", "todo_completed")}
    return await _request("PUT", f"/notes/{note_id}", body=allowed)


async def append_to_note(note_id: str, content: str, section: str | None = None) -> dict:
    """Append content to a note; if `section` is given, insert at the end of
    that '## section' block, else append at the end."""
    note = await get_note(note_id)
    if note is None:
        raise JoplinError(f"note {note_id} not found")
    body = note.get("body", "")
    if section:
        lines = body.split("\n")
        heading = section.strip().lower()
        start = next((i for i, l in enumerate(lines)
                      if l.strip().lower().lstrip("# ").strip() == heading
                      and l.lstrip().startswith("#")), None)
        if start is not None:
            end = next((j for j in range(start + 1, len(lines))
                        if lines[j].lstrip().startswith("#")), len(lines))
            # trim trailing blanks inside the section
            while end - 1 > start and not lines[end - 1].strip():
                end -= 1
            lines[end:end] = [content]
            body = "\n".join(lines)
        else:
            body = body.rstrip() + f"\n\n## {section}\n{content}\n"
    else:
        body = body.rstrip() + "\n" + content + "\n"
    return await update_note_body(note_id, body)


async def search_notes(query: str, limit: int = 10) -> list[dict]:
    """Full-text search across all notes."""
    data = await _request("GET", "/search", {
        "query": query, "limit": limit,
        "fields": "id,title,parent_id,updated_time,body",
    })
    items = (data or {}).get("items", [])
    return items[:limit]


async def find_note_in_folder(title: str, folder_id: str) -> dict | None:
    """Exact-title (case-insensitive) lookup via the direct folder listing.
    Immediately consistent, unlike /search which lags the full-text index."""
    items = await _paginated(f"/folders/{folder_id}/notes",
                             {"fields": "id,title,parent_id"})
    t = title.strip().lower()
    return next((n for n in items
                 if (n.get("title") or "").strip().lower() == t), None)


async def _descendant_folder_ids(root_id: str) -> list[str]:
    """root_id plus every folder nested under it, from one folder listing."""
    folders = await get_folder_tree()
    by_parent: dict[str, list[str]] = {}
    for f in folders:
        by_parent.setdefault(f.get("parent_id") or "", []).append(f["id"])
    out, stack = [], [root_id]
    while stack:
        fid = stack.pop()
        out.append(fid)
        stack.extend(by_parent.get(fid, []))
    return out


async def _find_note_anywhere(title: str) -> dict | None:
    """Exact-title lookup across EVERY notebook, immediately consistent.

    One `/notes` listing sees a note the instant it is written, unlike the
    full-text index, and costs a single paginated call regardless of how many
    notebooks exist — cheaper than walking folders one at a time. When several
    notes share a title the most recently updated one wins, so a read that
    follows a write returns what was just written."""
    items = await _paginated("/notes",
                             {"fields": "id,title,parent_id,updated_time"})
    t = title.strip().lower()
    hits = [n for n in items if (n.get("title") or "").strip().lower() == t]
    if not hits:
        return None
    return max(hits, key=lambda n: n.get("updated_time") or 0)


async def _find_note_under_folder(title: str, root_id: str) -> dict | None:
    """Immediately-consistent lookup across root_id and its descendants —
    unlike /search, a direct folder listing sees a note the instant it's
    created, so this is what a read-right-after-write needs."""
    for fid in await _descendant_folder_ids(root_id):
        hit = await find_note_in_folder(title, fid)
        if hit:
            return hit
    return None


async def find_note_by_title(title: str, notebook: str | None = None) -> dict | None:
    """Exact-title lookup, optionally scoped to a notebook path.

    Joplin's /search index lags well behind note creation — observed several
    seconds and up in testing, not a brief race — so a note Loki just wrote
    can be invisible to search_notes() for a while. Direct listings have no
    such lag, so try one first: scoped to `notebook` when given, otherwise
    across every notebook.

    That unscoped lookup used to cover only the Loki namespace, on the
    assumption that Loki reads back only what it filed there. It doesn't: the
    Boss's own notebooks are equally valid targets ("add this to Officer
    Logs"), and a note written to one was invisible to the very next read —
    which made note_append believe the note was missing and create a DUPLICATE
    instead of appending. A single `/notes` listing covers everything.
    """
    if notebook:
        folder_id = await resolve_notebook_path(notebook, create=False)
        if folder_id:
            hit = await find_note_in_folder(title, folder_id)
            if hit:
                # find_note_in_folder only requests id/title/parent_id for
                # speed; callers expect the full note (body included).
                return await get_note(hit["id"]) or hit
    else:
        hit = await _find_note_anywhere(title)
        if hit:
            return await get_note(hit["id"]) or hit

    hits = await search_notes(f'title:"{title}"', limit=20)
    if notebook:
        folder_id = await resolve_notebook_path(notebook, create=False)
        hits = [h for h in hits if h.get("parent_id") == folder_id]
    return next((h for h in hits if h["title"] == title), hits[0] if hits else None)


async def list_notes_in_notebook(notebook: str, limit: int = 50) -> list[dict]:
    folder_id = await resolve_notebook_path(notebook, create=False)
    if not folder_id:
        return []
    items = await _paginated(f"/folders/{folder_id}/notes",
                             {"fields": "id,title,updated_time"})
    return items[:limit]


# ─── Tags ─────────────────────────────────────────────────────────────────────

async def _apply_tags(note_id: str, tags: list[str]):
    existing = {t["title"]: t["id"] for t in await _paginated("/tags", {"fields": "id,title"})}
    for tag in tags:
        tag = tag.strip().lower()
        if not tag:
            continue
        tag_id = existing.get(tag)
        if not tag_id:
            created = await _request("POST", "/tags", body={"title": tag})
            tag_id = created["id"]
            existing[tag] = tag_id
        try:
            await _request("POST", f"/tags/{tag_id}/notes", body={"id": note_id})
        except JoplinError as e:
            log.debug(f"tag apply {tag}: {e}")


# ─── Convenience: upsert-style helpers Loki uses a lot ───────────────────────

async def upsert_note(title: str, body: str, notebook: str,
                      tags: list[str] | None = None) -> dict:
    """Create the note, or fully replace its body if a note with the same
    title already exists in that notebook."""
    existing = await find_note_by_title(title, notebook)
    if existing:
        return await update_note_body(existing["id"], body)
    return await create_note(title, body, notebook, tags)


async def append_or_create(title: str, notebook: str, content: str,
                           header: str | None = None,
                           tags: list[str] | None = None,
                           search_all: bool = False) -> dict:
    """Append a line/block to a note, creating it (with optional header) first
    if missing. The workhorse for logs (work sessions, lists, journals).

    `notebook` is where the note is CREATED if it does not exist. By default
    the lookup is scoped there too, which is what the log writers want: a
    daily journal note must match the one in the journal notebook, never a
    same-titled note elsewhere. `search_all=True` instead looks everywhere —
    for "append to the note called X" where the Boss named no notebook, and
    scoping the search to a default would create a duplicate of a note that
    already exists in one of his own notebooks."""
    existing = await find_note_by_title(title, None if search_all else notebook)
    if existing:
        return await append_to_note(existing["id"], content)
    body = (header.rstrip() + "\n" if header else "") + content + "\n"
    return await create_note(title, body, notebook, tags)

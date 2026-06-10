"""
tools.py — tool registry + native function-calling tools for Loki (Phase 2).

Each tool is a ToolSpec: OpenAI function schema + async handler + permission
level. loki_bot.py attaches the registry's schemas to OpenAI calls and routes
tool_calls back through execute(); backends without tool support simply never
see the schemas.

Permission levels:
    everyone — any user in the server
    crew     — user IDs in CREW_USER_IDS (comma-separated) or the owner
    boss     — OWNER_USER_ID only

Handlers ALWAYS return a short human-readable string (the model sees it and
answers in character). They never raise — errors come back as friendly text,
and full details go to the tool-call log.

Every call is appended to tool_calls.jsonl: timestamp, user, tool, args,
ok/error, duration.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger("Tools")

OWNER_USER_ID = os.getenv("OWNER_USER_ID", "")
CREW_USER_IDS = {u.strip() for u in os.getenv("CREW_USER_IDS", "").split(",") if u.strip()}

SEARXNG_URL      = os.getenv("SEARXNG_URL", "http://192.168.1.247:8083")
JELLYFIN_URL     = os.getenv("JELLYFIN_URL", "http://localhost:8096")
JELLYFIN_API_KEY = os.getenv("JELLYFIN_API_KEY", "")
SEERR_URL        = os.getenv("SEERR_URL", "")
SEERR_API_KEY    = os.getenv("SEERR_API_KEY", "")

TOOL_LOG_PATH = os.getenv("TOOL_LOG_PATH", "tool_calls.jsonl")

PERM_RANK = {"everyone": 0, "crew": 1, "boss": 2}


@dataclass
class ToolContext:
    user_id: str
    user_name: str
    channel_id: str = ""
    guild_id: str = ""


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, ToolContext], Awaitable[str]]
    permission: str = "everyone"

    def openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


REGISTRY: dict[str, ToolSpec] = {}

# Handlers that need bot internals (e.g. the download chain) get injected by
# loki_bot.py at startup via bind() — avoids a circular import.
_bound: dict[str, Callable] = {}


def bind(**handlers):
    _bound.update(handlers)


def register(spec: ToolSpec):
    REGISTRY[spec.name] = spec


def user_level(user_id: str) -> str:
    uid = str(user_id)
    if uid == OWNER_USER_ID:
        return "boss"
    if uid in CREW_USER_IDS:
        return "crew"
    return "everyone"


def schemas_for(user_id: str) -> list[dict]:
    """OpenAI tools array containing only the tools this user may call."""
    rank = PERM_RANK[user_level(user_id)]
    return [
        spec.openai_schema()
        for spec in REGISTRY.values()
        if PERM_RANK[spec.permission] <= rank
    ]


def _log_call(ctx: ToolContext, name: str, args: dict, ok: bool, detail: str, ms: int):
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "user": ctx.user_name, "user_id": ctx.user_id,
        "tool": name, "args": args, "ok": ok,
        "detail": detail[:500], "ms": ms,
    }
    try:
        with open(TOOL_LOG_PATH, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
    log.info(f"tool={name} user={ctx.user_name} ok={ok} ms={ms} args={str(args)[:120]}")


async def execute(name: str, raw_args: str, ctx: ToolContext) -> str:
    """Run a tool call from the model. Returns text for the tool result message."""
    spec = REGISTRY.get(name)
    if spec is None:
        return f"Unknown tool '{name}'."
    if PERM_RANK[spec.permission] > PERM_RANK[user_level(ctx.user_id)]:
        _log_call(ctx, name, {}, False, "permission denied", 0)
        return ("Permission denied — this one isn't available to "
                f"{ctx.user_name}. Don't pretend you did it.")
    try:
        args = json.loads(raw_args) if raw_args else {}
        if not isinstance(args, dict):
            args = {}
    except json.JSONDecodeError:
        _log_call(ctx, name, {}, False, f"bad args: {raw_args[:100]}", 0)
        return "Tool call had malformed arguments — try rephrasing the request."

    start = time.monotonic()
    try:
        result = await asyncio.wait_for(spec.handler(args, ctx), timeout=30)
        _log_call(ctx, name, args, True, result, int((time.monotonic() - start) * 1000))
        return result
    except asyncio.TimeoutError:
        _log_call(ctx, name, args, False, "timeout", int((time.monotonic() - start) * 1000))
        return "That took too long and timed out. The server might be busy."
    except Exception as e:
        log.error(f"Tool {name} failed: {e}", exc_info=True)
        _log_call(ctx, name, args, False, f"{type(e).__name__}: {e}",
                  int((time.monotonic() - start) * 1000))
        return "Something broke on my end running that. The Boss can check my tool log."


async def _session() -> aiohttp.ClientSession:
    if "http_session" in _bound:
        return await _bound["http_session"]()
    # standalone fallback (tests)
    global _own_session
    try:
        return _own_session
    except NameError:
        _own_session = aiohttp.ClientSession()
        return _own_session


# ═════════════════════════════════════════════════════════════════════════════
#  Tool implementations
# ═════════════════════════════════════════════════════════════════════════════

async def _web_search(args: dict, ctx: ToolContext) -> str:
    query = str(args.get("query", "")).strip()[:300]
    if not query:
        return "Empty search query."
    sess = await _session()
    async with sess.get(
        f"{SEARXNG_URL}/search",
        params={"q": query, "format": "json", "categories": "general"},
        timeout=aiohttp.ClientTimeout(total=8),
    ) as resp:
        data = await resp.json(content_type=None)
    results = [r for r in data.get("results", []) if r.get("content")][:5]
    if not results:
        return f"No web results for '{query}'."
    return "\n".join(f"- {r['content'][:250]} ({r.get('url', '')})" for r in results)


async def _media_lookup(args: dict, ctx: ToolContext) -> str:
    title = str(args.get("title", "")).strip()[:120]
    if not title:
        return "No title given."
    if not JELLYFIN_API_KEY:
        return "Jellyfin lookup isn't configured."
    sess = await _session()
    async with sess.get(
        f"{JELLYFIN_URL}/Items",
        params={
            "searchTerm": title, "Recursive": "true",
            "IncludeItemTypes": "Movie,Series", "Limit": "8",
            "fields": "ProductionYear",
        },
        headers={"X-Emby-Token": JELLYFIN_API_KEY},
        timeout=aiohttp.ClientTimeout(total=8),
    ) as resp:
        if resp.status != 200:
            return "Couldn't reach the media library right now."
        data = await resp.json()
    items = data.get("Items", [])
    if not items:
        return f"'{title}' is NOT on the server."
    lines = [
        f"- {i.get('Name')} ({i.get('ProductionYear', '?')}) — "
        f"{'series' if i.get('Type') == 'Series' else 'movie'}"
        for i in items[:8]
    ]
    return f"On the server, matching '{title}':\n" + "\n".join(lines)


async def _media_request(args: dict, ctx: ToolContext) -> str:
    title = str(args.get("title", "")).strip()[:120]
    media_type = str(args.get("media_type", "movie")).lower()
    if media_type not in ("movie", "tv"):
        media_type = "movie"
    if not title:
        return "No title given."
    if not SEERR_URL or not SEERR_API_KEY:
        return ("The request system isn't hooked up right now — tell the Boss "
                "to configure SEERR_URL/SEERR_API_KEY.")
    sess = await _session()
    headers = {"X-Api-Key": SEERR_API_KEY}
    async with sess.get(
        f"{SEERR_URL}/api/v1/search",
        params={"query": title}, headers=headers,
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        if resp.status != 200:
            return "The request system isn't answering right now."
        data = await resp.json()
    match = next(
        (r for r in data.get("results", []) if r.get("mediaType") == media_type),
        None,
    )
    if not match:
        return f"Couldn't find '{title}' ({media_type}) to request."
    payload = {"mediaType": media_type, "mediaId": match["id"]}
    if media_type == "tv":
        payload["seasons"] = "all"
    async with sess.post(
        f"{SEERR_URL}/api/v1/request", json=payload, headers=headers,
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        if resp.status in (200, 201):
            name = match.get("title") or match.get("name") or title
            return f"Requested: {name} ({media_type}). It'll show up once it's grabbed."
        if resp.status == 409:
            return f"'{title}' was already requested or is already available."
        return "The request didn't go through — the request system rejected it."


async def _download_media(args: dict, ctx: ToolContext) -> str:
    url = str(args.get("url", "")).strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or len(url) > 800 \
            or any(c in url for c in " \n\r\t\"'<>"):
        return "That URL doesn't look valid — give me a normal http(s) link."
    if "download" not in _bound:
        return "Downloads aren't wired up in this build."
    # fire-and-forget into the existing download chain; it posts results itself
    asyncio.create_task(_bound["download"](url, ctx.user_name))
    return (f"Download started for {parsed.netloc} — it'll land in the downloads "
            "channel when it's done. Tell them it's on the way; do NOT claim it's finished.")


async def _server_status(args: dict, ctx: ToolContext) -> str:
    async def run(*cmd) -> str:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        return out.decode(errors="replace").strip()

    disk = await run("df", "-h", "--output=target,size,used,avail,pcent", "/", "/home")
    uptime = await run("uptime", "-p")
    load = (await run("cat", "/proc/loadavg")).split()[:3]
    docker = await run("docker", "ps", "--format", "{{.Names}}\t{{.Status}}")
    containers = docker.splitlines() if docker else []
    unhealthy = [c for c in containers if "unhealthy" in c.lower() or "restarting" in c.lower()]
    summary = [
        f"Uptime: {uptime}",
        f"Load: {' '.join(load)}",
        f"Disk:\n{disk}",
        f"Docker: {len(containers)} containers running"
        + (f", PROBLEMS: {'; '.join(unhealthy)}" if unhealthy else ", all healthy"),
    ]
    return "\n".join(summary)


# ═════════════════════════════════════════════════════════════════════════════
#  Registration
# ═════════════════════════════════════════════════════════════════════════════

register(ToolSpec(
    name="web_search",
    description=("Search the live web for current information: news, scores, prices, "
                 "release dates, facts you don't know or that may have changed."),
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "description": "The search query"}},
        "required": ["query"],
    },
    handler=_web_search,
    permission="everyone",
))

register(ToolSpec(
    name="media_lookup",
    description=("Check whether a movie or TV series is available on the server's "
                 "media library (Jellyfin/Plex). Use whenever someone asks if "
                 "something is 'on the server' or available to watch."),
    parameters={
        "type": "object",
        "properties": {"title": {"type": "string", "description": "Movie or series title"}},
        "required": ["title"],
    },
    handler=_media_lookup,
    permission="everyone",
))

register(ToolSpec(
    name="media_request",
    description=("Submit a request to add a movie or TV series to the media server. "
                 "Use when someone asks to add/get/request a title that isn't on the server."),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Title to request"},
            "media_type": {"type": "string", "enum": ["movie", "tv"]},
        },
        "required": ["title", "media_type"],
    },
    handler=_media_request,
    permission="crew",
))

register(ToolSpec(
    name="download_media",
    description=("Download a video/image from a social media or video URL "
                 "(TikTok, Instagram, YouTube, Twitter/X, Reddit, etc.) and post it "
                 "to the downloads channel. Use when someone asks you to download/save/grab a link."),
    parameters={
        "type": "object",
        "properties": {"url": {"type": "string", "description": "The http(s) URL to download"}},
        "required": ["url"],
    },
    handler=_download_media,
    permission="crew",
))

register(ToolSpec(
    name="server_status",
    description=("Report server health: disk space, docker container states, uptime, load. "
                 "Boss-only diagnostics."),
    parameters={"type": "object", "properties": {}},
    handler=_server_status,
    permission="boss",
))

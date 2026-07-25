"""
browser_research.py — Loki's client for the scoped RAZR headless-browser worker.

Loki calls the worker (on RAZR, over Tailscale) ONLY to render JavaScript-heavy
public pages when ordinary fetch/extract can't. It is registered as a
task-supervisor handler ("browser_research") so a browse runs as a durable
background task with progress updates and screenshot delivery — not inline, and
never for every URL.

The dispatcher should route here only when:
  - the user explicitly asks Loki to open / browse / screenshot a specific site, or
  - a Career-Ops extraction reported that the page needs JavaScript
    (see needs_browser_fallback()).

Security is enforced by the worker (Bearer auth, Tailscale-only bind, SSRF guard,
limits). This client adds a light public-URL pre-check and never logs the token.

Env (never committed): BROWSER_WORKER_URL, BROWSER_WORKER_TOKEN.
"""

import asyncio
import base64
import ipaddress
import json
import logging
import os
import tempfile
from urllib.parse import urlparse

from tools import ToolSpec, ToolContext, register, user_level

log = logging.getLogger("BrowserResearch")

WORKER_URL = os.getenv("BROWSER_WORKER_URL", "").rstrip("/")
WORKER_TOKEN = os.getenv("BROWSER_WORKER_TOKEN", "")
enabled = bool(WORKER_URL and WORKER_TOKEN)

# Attachment ceilings per platform (bytes) — mirror career_ops.
DISCORD_LIMIT = 10 * 1024 * 1024
TELEGRAM_LIMIT = 50 * 1024 * 1024

_session_factory = None   # async () -> aiohttp.ClientSession
_send = None              # async (channel_id, text, file_path=None, filename=None)


def bind(session_factory, send):
    global _session_factory, _send
    _session_factory = session_factory
    _send = send


class BrowserError(Exception):
    pass


# ── Career-Ops JS-failure detection (when to fall back to the browser) ──────
_JS_FAIL_SIGNALS = (
    "requires javascript", "enable javascript", "javascript is required",
    "javascript required", "needs javascript", "page requires js",
    "turn on javascript", "without javascript", "please enable js",
    "content did not render", "empty extraction",
)


def needs_browser_fallback(text: str) -> bool:
    """True if a Career-Ops extraction result/error suggests the page needs JS."""
    t = (text or "").lower()
    return any(s in t for s in _JS_FAIL_SIGNALS)


# ── Light client-side public-URL guard (worker is authoritative) ───────────
def is_public_url(raw: str) -> bool:
    try:
        u = urlparse(raw)
    except Exception:
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    host = u.hostname
    if host in ("localhost",) or host.endswith((".local", ".internal", ".lan")):
        return False
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    except ValueError:
        if "." not in host:      # bare hostname isn't public
            return False
    return True


# ── Worker client ──────────────────────────────────────────────────────────
async def _extract(url: str, screenshot: bool, timeout_ms: int | None = None):
    import aiohttp
    if not enabled:
        raise BrowserError("browser worker is not configured")
    if _session_factory is None:
        raise BrowserError("browser client not bound yet")
    payload = {"url": url, "screenshot": bool(screenshot)}
    if timeout_ms:
        payload["timeout_ms"] = int(timeout_ms)
    sess = await _session_factory()
    async with sess.post(
        f"{WORKER_URL}/v1/extract", json=payload,
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
        timeout=aiohttp.ClientTimeout(total=45),
    ) as r:
        data = await r.json(content_type=None)
        if r.status >= 400:
            raise BrowserError(data.get("error", f"HTTP {r.status}"))
        return data


async def _deliver_screenshot(channel_id: str, title: str, b64: str) -> bool:
    if not b64 or _send is None:
        return False
    raw = base64.b64decode(b64)
    limit = TELEGRAM_LIMIT if channel_id.startswith("tg:") else DISCORD_LIMIT
    if len(raw) > limit:
        return False
    fd, tmp = tempfile.mkstemp(prefix="loki_browse_", suffix=".png")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        await _send(channel_id, f"📸 {title}"[:200], file_path=tmp, filename="page.png")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return True


# ── Task-supervisor handler ────────────────────────────────────────────────
async def _handler(h):
    import task_supervisor as ts
    inp = h.input
    url = inp["url"]
    want_shot = bool(inp.get("screenshot"))
    channel_id = h.row.get("channel_id", "")

    # Run the worker call as a cancellable subtask so task_cancel propagates
    # (cancelling the request closes the socket → the worker aborts the browse).
    fut = asyncio.ensure_future(_extract(url, want_shot))
    try:
        while not fut.done():
            if h.cancelled():
                fut.cancel()
                return ts.TaskResult("cancelled", summary="cancelled by user")
            await asyncio.sleep(0.4)
            await h.beat()
        data = fut.result()
    except asyncio.CancelledError:
        return ts.TaskResult("cancelled", summary="cancelled")
    except BrowserError as e:
        return ts.TaskResult("failed", error_category="browser",
                             error_detail=str(e), summary=f"browser fetch failed: {e}")

    title = data.get("title") or url
    final = data.get("final_url") or url
    text = data.get("text") or ""
    shot_sent = False
    if want_shot and data.get("screenshot") and channel_id:
        try:
            shot_sent = await _deliver_screenshot(channel_id, title, data["screenshot"])
        except Exception:
            log.exception("screenshot delivery failed")
    snippet = text[:400].strip()
    summary = f"{title} — {final}" + (f" · {snippet}" if snippet else "")
    if data.get("screenshot_refused"):
        summary += " (screenshot not allowed for this host)"
    elif shot_sent:
        summary += " · screenshot posted"
    return ts.TaskResult("completed", summary=summary[:800])


def _register_task_type():
    try:
        import task_supervisor as ts
    except Exception:
        return False

    def _validate(raw: dict, ctx: ToolContext):
        url = str(raw.get("url", "")).strip()
        if not is_public_url(url):
            return {}, "need a public http(s) URL (loopback/LAN/file/etc are refused)"
        return {"url": url, "screenshot": bool(raw.get("screenshot"))}, ""

    ts.register_type(ts.TaskType(
        name="browser_research", handler=_handler, permission="crew",
        capabilities=frozenset({"browser_worker"}),
        default_timeout=int(os.getenv("BROWSER_TASK_TIMEOUT_SECS", "120")),
        max_retries=1, validate=_validate,
        title=lambda i: f"Browse {urlparse(i.get('url', '')).netloc or 'page'}",
    ))
    return True


# ── Tool (gated: explicit browse requests / JS-required fallback only) ──────
async def _tool_browse(args: dict, ctx: ToolContext) -> str:
    import task_supervisor as ts
    url = str(args.get("url", "")).strip()
    if not is_public_url(url):
        return json.dumps({"ok": False,
                           "error": "need a public http(s) URL (loopback/LAN/file refused)"})
    tt = ts._TYPES.get("browser_research")
    if tt is None:
        return json.dumps({"ok": False, "error": "browser worker not available"})
    clean = {"url": url, "screenshot": bool(args.get("screenshot"))}
    task_id = ts.submit(tt, ctx, clean)
    return json.dumps({"ok": True, "task_id": task_id, "status": "queued",
                       "note": "browsing in the background — I'll post the result "
                               "(and screenshot, if requested) here"})


def _register_tool():
    register(ToolSpec(
        name="browser_research",
        description=(
            "Open a PUBLIC web page in a real headless browser (on RAZR) to read "
            "JavaScript-rendered content or capture a screenshot. Use ONLY when the "
            "user explicitly asks you to open/browse/screenshot a specific site, or "
            "when a Career-Ops extraction failed because the page needs JavaScript. "
            "Do NOT send every link here — normal fetch/search handles static pages. "
            "Runs as a background task; set screenshot=true to also post an image."),
        parameters={"type": "object",
                    "properties": {"url": {"type": "string"},
                                   "screenshot": {"type": "boolean"}},
                    "required": ["url"]},
        handler=_tool_browse, permission="crew", timeout=30,
    ))


if enabled:
    _register_task_type()
    _register_tool()
    log.info("Browser research online — worker configured")
else:
    log.info("Browser research disabled — BROWSER_WORKER_URL/TOKEN not set")

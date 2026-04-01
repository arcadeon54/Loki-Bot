"""
=============================================================================
LOKI - AI Discord Bot
=============================================================================
A conversational AI Discord bot with:
  - Persistent memory (SQLite)
  - Image/GIF recognition (Google Gemini)
  - Voice chat with male TTS voice
  - Summarize command
  - Support for ChatGPT API or local LLM
  - Per-server personality prompts
  - Claude Code integration (/cc command)
  - Unprompted interjections with mood drift
  - Relationship memory (per-user personality notes)
  - Natural language "what did I miss" detection
  - @mention support via member directory
  - Self-learning personality (writes to loki_learned.md)
=============================================================================
"""

import os
import io
import re
import json
import time
import random
import shutil
import uuid
import sqlite3
import asyncio
import logging
import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from dotenv import load_dotenv

import discord
from discord.ext import commands, tasks
from discord import app_commands

import aiohttp
import google.generativeai as genai

# ─── Shared aiohttp session (created in on_ready, used everywhere) ────────────
_http_session: "aiohttp.ClientSession | None" = None

async def get_http_session() -> "aiohttp.ClientSession":
    """Return the shared aiohttp session, creating it if needed."""
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=120),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
        )
    return _http_session

# ─── Load environment variables ───────────────────────────────────────────────
load_dotenv()

# ─── Shared state (bot-to-bot coordination) ───────────────────────────────────
import sys as _sys
_sys.path.insert(0, os.path.dirname(__file__))
try:
    from shared_state import SharedState as _SharedState
    _shared_state_available = True
except ImportError:
    _shared_state_available = False

try:
    from rag_search import search_history, format_for_context, is_available as rag_available
    _rag_available = True
except ImportError:
    _rag_available = False

try:
    from tavily import TavilyClient as _TavilyClient
    _TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
    _tavily = _TavilyClient(api_key=_TAVILY_API_KEY) if _TAVILY_API_KEY else None
except ImportError:
    _tavily = None

# ─── Timezone ─────────────────────────────────────────────────────────────────
ET = ZoneInfo("America/New_York")

def now_et() -> datetime.datetime:
    """Return the current time in US/Eastern."""
    return datetime.datetime.now(ET)


DISCORD_TOKEN         = os.getenv("DISCORD_TOKEN")
LLM_PROVIDER          = os.getenv("LLM_PROVIDER", "openai")        # "openai" or "local"
OPENAI_API_KEY        = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL          = os.getenv("OPENAI_MODEL", "gpt-4o")
LOCAL_LLM_URL         = os.getenv("LOCAL_LLM_URL", "http://localhost:1234/v1")
LOCAL_LLM_MODEL       = os.getenv("LOCAL_LLM_MODEL", "local-model")
FALLBACK_LLM_URL      = os.getenv("FALLBACK_LLM_URL", "")
FALLBACK_LLM_MODEL    = os.getenv("FALLBACK_LLM_MODEL", "")
FALLBACK_LLM_API_KEY  = os.getenv("FALLBACK_LLM_API_KEY", "")
ELEVENLABS_API_KEY    = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID   = os.getenv("ELEVENLABS_VOICE_ID", "")
GEMINI_API_KEY        = os.getenv("GEMINI_API_KEY", "")
SYSTEM_PROMPT         = os.getenv("SYSTEM_PROMPT", "You are Loki, the God of Mischief.")
MEMORY_DB_PATH        = os.getenv("MEMORY_DB_PATH", "loki_memory.db")
CONTEXT_MESSAGE_COUNT = int(os.getenv("CONTEXT_MESSAGE_COUNT", "50"))
MISST_BOT_USER_ID = int(os.getenv("MISST_BOT_USER_ID") or "0")
SHARED_STATE_PATH = os.getenv("SHARED_STATE_PATH", os.path.expanduser("~/bot-shared-state/loki_state.json"))

# ─── Claude Code settings ─────────────────────────────────────────────────────
_claude_bin_default = shutil.which("claude") or "claude"
CLAUDE_BIN       = os.getenv("CLAUDE_BIN", _claude_bin_default)
CLAUDE_WORKSPACE = os.getenv("CLAUDE_WORKSPACE", str(Path.home()))
CLAUDE_TIMEOUT   = int(os.getenv("CLAUDE_TIMEOUT", "300"))

# ─── Self-learning personality file ───────────────────────────────────────────
LOKI_LEARNED_FILE = os.getenv("LOKI_LEARNED_FILE", "loki_learned.md")

# ─── Download settings ────────────────────────────────────────────────────────
DOWNLOAD_DIR         = os.getenv("DOWNLOAD_DIR", os.path.join(os.path.dirname(__file__), "downloads"))
DOWNLOAD_CHANNEL_ID  = int(os.getenv("DOWNLOAD_CHANNEL_ID", "0"))
YTDLP_BIN            = os.getenv("YTDLP_BIN", "/usr/local/bin/yt-dlp")
COBALT_URL           = os.getenv("COBALT_URL", "http://localhost:9000")
MEDIA_BASE_URL       = os.getenv("MEDIA_BASE_URL", "")
FFMPEG_BIN           = os.getenv("FFMPEG_BIN", "/usr/bin/ffmpeg")
DISCORD_UPLOAD_LIMIT = 10 * 1024 * 1024  # 10 MB — Discord's current hard limit

# User whose TikTok/Instagram links are auto-downloaded
AUTO_DOWNLOAD_USER_ID = int(os.getenv("AUTO_DOWNLOAD_USER_ID", "0"))

# Patterns
POST_THIS_RE          = re.compile(r'\bpost\s+this\b', re.IGNORECASE)
URL_RE                = re.compile(r'https?://\S+')
TIKTOK_INSTAGRAM_RE   = re.compile(
    r'https?://(?:[a-z0-9-]+\.)*(?:tiktok\.com|instagram\.com|instagr\.am|threads\.net|threads\.com)\S*',
    re.IGNORECASE
)

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("loki_bot.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("LokiBot")

# ─── Constants ────────────────────────────────────────────────────────────────
TRIGGER_NAMES = ["loki"]
_TRIGGER_RE = re.compile(
    r'\b(?:' + '|'.join(re.escape(n) for n in TRIGGER_NAMES) + r')\b',
    re.IGNORECASE
)

# Patterns that trigger the "what did I miss" handler
MISSED_PATTERNS = [
    re.compile(r"what(?:'d| did) i miss", re.IGNORECASE),
    re.compile(r"catch me up", re.IGNORECASE),
    re.compile(r"what'?s been going on", re.IGNORECASE),
    re.compile(r"what happened(?: here| while i was gone)?", re.IGNORECASE),
    re.compile(r"fill me in", re.IGNORECASE),
]

# ─── Tea detection ────────────────────────────────────────────────────────────
_TEA_FLAG_RE = re.compile(
    r"(\b(spill\s+(the\s+)?tea|what'?s\s+the\s+tea|the\s+tea\s+is|give\s+(me\s+)?the\s+tea"
    r"|storytime|story\s+time|the\s+drama|so\s+much\s+drama|it'?s\s+giving\s+drama"
    r"|not\s+gonna\s+lie\s+tho|lowkey\s+drama|messy|the\s+receipts)\b"
    r"|[☕🍵🫖])",
    re.IGNORECASE,
)
_TEA_REACTION_EMOJIS = {"☕", "🍵", "🫖", "🍃", "🇹"}
_TEA_PULL_RE = re.compile(
    r"\b(pull\s+(the\s+)?tea|spill\s+(the\s+)?tea\s+from|what'?s\s+the\s+tea\s+from"
    r"|loki\s+.*\s+tea|recap\s+the\s+drama|what\s+was\s+the\s+drama)\b",
    re.IGNORECASE,
)

# ─── Correction detection ─────────────────────────────────────────────────────
_CORRECTION_RE = re.compile(
    r"\b(read\s+the\s+room|not\s+cool|too\s+far|out\s+of\s+pocket|dude\s+no"
    r"|that\s+was\s+not\s+okay|chill\s+loki|loki\s+no|nah\s+loki"
    r"|bad\s+take|missed\s+the\s+mark|that\s+landed\s+(wrong|bad)"
    r"|wrong\s+move|way\s+off|bro\s+stop|that\s+was\s+off)\b",
    re.IGNORECASE,
)

# ─── Web search intent patterns ───────────────────────────────────────────────
_SEARCH_PATTERNS = [
    re.compile(r'\b(weather|forecast|temperature|rain|snow|storm|hurricane|humidity)\b', re.IGNORECASE),
    re.compile(r'\b(score|standings|playoffs?|championship|tournament|who\s+won|did\s+\w+\s+win)\b', re.IGNORECASE),
    re.compile(r'\b(news|headline|breaking|what\'?s\s+happening|what\'?s\s+going\s+on|in\s+the\s+news)\b', re.IGNORECASE),
    re.compile(r'\b(search\s+(for|up)|look\s+up|google|find\s+out)\b', re.IGNORECASE),
    re.compile(r'\b(is\s+.{1,30}\s+open|what\s+time\s+does|closing\s+time|hours\s+for)\b', re.IGNORECASE),
    re.compile(r'\b(current\s+price|how\s+much\s+(is|does|do|are)|stock\s+price)\b', re.IGNORECASE),
    re.compile(r'\b(release\s+date|when\s+does\s+.{1,30}(come\s+out|drop|release))\b', re.IGNORECASE),
    re.compile(r'\b(who\s+is\s+the\s+(current|new)|what\'?s\s+the\s+(current|latest|new)\s+)\b', re.IGNORECASE),
    re.compile(r'\b(youtube|clip|video|find\s+(a|that|the|me)|can\s+you\s+find|pull\s+up|show\s+me)\b', re.IGNORECASE),
]

def is_search_query(text: str) -> bool:
    for p in _SEARCH_PATTERNS:
        if p.search(text):
            return True
    return False

# ─── Topic enrichment patterns (casual cultural mentions) ─────────────────────
_TITLE_HINT_RE = re.compile(
    r'\b(?:called|titled|named|movie|show|film|series|album|game|book|anime)\s+["\']?([A-Za-z0-9][^,\.!?"\']{2,40})',
    re.IGNORECASE
)

_TOPIC_ENRICHMENT_PATTERNS = [
    # watched/finished/saw/started + title
    (re.compile(r'\b(?:just\s+)?(?:watched|finished|saw|started|binged?|rewatched|re-watched)\s+(.+?)(?:[,\.!?]|$)', re.IGNORECASE), 1),
    # playing / reading + title
    (re.compile(r'\b(?:playing|been\s+playing|just\s+finished|bought|reading|just\s+read|finished\s+reading)\s+(.+?)(?:[,\.!?]|$)', re.IGNORECASE), 1),
    # listening to / on repeat
    (re.compile(r'\b(?:listening\s+to|been\s+playing|on\s+repeat|new\s+(?:album|song|track|ep|single|drop)|dropped\s+(?:an?\s+)?(?:new\s+)?)\b', re.IGNORECASE), None),
    # quality judgement about a movie/show
    (re.compile(r'\b(?:good|great|bad|mid|trash|fire|slept\s+on|underrated|overrated|disappointing|boring|amazing|incredible|terrible)\s+(?:movie|show|film|series|anime|season|episode|documentary)\b', re.IGNORECASE), None),
    # "the movie" / "this show" references
    (re.compile(r'\b(?:the|this)\s+(?:movie|show|film|series|anime|documentary)\b', re.IGNORECASE), None),
]

def _extract_topic_query(text: str):
    """Return a search query string if the message mentions a cultural topic, else None."""
    # Highest-confidence: explicit title hint ('called X', 'movie X', etc.)
    m = _TITLE_HINT_RE.search(text)
    if m:
        return m.group(1).strip()
    # Pattern-based extraction
    for pattern, grp in _TOPIC_ENRICHMENT_PATTERNS:
        m = pattern.search(text)
        if m:
            if grp is not None:
                try:
                    candidate = m.group(grp).strip(' \'".,!?')
                    if 2 <= len(candidate) <= 80:
                        return candidate
                except IndexError:
                    pass
            # No group capture — return full message as context
            return text[:120]
    return None


# Pattern to detect reminder intent in natural language
REMINDER_INTENT_RE = re.compile(
    r"\b(remind\s+(me|us|\w+)|set\s+a\s+reminder|don'?t\s+let\s+me\s+forget|remember\s+to\b)",
    re.IGNORECASE
)

def parse_reminder_time(when: str) -> datetime.datetime | None:
    """Parse a natural language time string into an ET datetime.

    Supports:
      "in X minutes/hours/days/weeks"
      "at 5pm", "at 5:30pm", "at 17:00"
      "tomorrow at 5pm"
      "tonight at 9", "tonight at 9pm"
    Returns None if the string cannot be parsed.
    """
    when = when.strip().lower()
    base = now_et()

    # ── "in X minutes/hours/days/weeks" ──
    m = re.match(
        r'^in\s+(\d+(?:\.\d+)?)\s*(minute?s?|min|hour?s?|hr|day?s?|week?s?)$',
        when
    )
    if m:
        amount = float(m.group(1))
        unit = m.group(2)
        if unit.startswith("min"):
            return base + datetime.timedelta(minutes=amount)
        if unit.startswith("h"):
            return base + datetime.timedelta(hours=amount)
        if unit.startswith("d"):
            return base + datetime.timedelta(days=amount)
        if unit.startswith("w"):
            return base + datetime.timedelta(weeks=amount)

    # ── Helper: parse a clock string like "5pm", "5:30pm", "17:00" ──
    def parse_clock(s: str):
        s = s.strip()
        # "5pm" / "5:30pm" / "5:30 pm"
        cm = re.match(r'^(\d{1,2})(?::(\d{2}))?\s*(am|pm)$', s)
        if cm:
            h, mn, ampm = int(cm.group(1)), int(cm.group(2) or 0), cm.group(3)
            if ampm == "pm" and h != 12:
                h += 12
            if ampm == "am" and h == 12:
                h = 0
            return h, mn
        # "17:00" / "9:00"
        cm = re.match(r'^(\d{1,2}):(\d{2})$', s)
        if cm:
            return int(cm.group(1)), int(cm.group(2))
        # bare hour like "9" or "17" (no am/pm — ambiguous, assume pm if < 8)
        cm = re.match(r'^(\d{1,2})$', s)
        if cm:
            h = int(cm.group(1))
            if h < 8:
                h += 12  # treat "5" as 5 pm
            return h, 0
        return None

    # ── "tomorrow at X" ──
    m = re.match(r'^tomorrow\s+at\s+(.+)$', when)
    if m:
        clock = parse_clock(m.group(1))
        if clock:
            h, mn = clock
            target = (base + datetime.timedelta(days=1)).replace(
                hour=h, minute=mn, second=0, microsecond=0
            )
            return target

    # ── "tonight at X" ──
    m = re.match(r'^tonight\s+at\s+(.+)$', when)
    if m:
        clock = parse_clock(m.group(1))
        if clock:
            h, mn = clock
            target = base.replace(hour=h, minute=mn, second=0, microsecond=0)
            if target <= base:
                target += datetime.timedelta(days=1)
            return target

    # ── "at X" (today; bump to tomorrow if already past) ──
    m = re.match(r'^at\s+(.+)$', when)
    if m:
        clock = parse_clock(m.group(1))
        if clock:
            h, mn = clock
            target = base.replace(hour=h, minute=mn, second=0, microsecond=0)
            if target <= base:
                target += datetime.timedelta(days=1)
            return target

    return None


# Cache for learned personality file (updated every 6h, no need to read every message)
_learned_personality_cache: str = ""
_learned_personality_mtime: float = 0.0

def load_learned_personality() -> str:
    """Read loki_learned.md with caching — only re-reads if file changed on disk."""
    global _learned_personality_cache, _learned_personality_mtime
    path = Path(LOKI_LEARNED_FILE)
    if not path.exists():
        return ""
    try:
        current_mtime = path.stat().st_mtime
        if current_mtime != _learned_personality_mtime:
            _learned_personality_cache = path.read_text(encoding="utf-8").strip()
            _learned_personality_mtime = current_mtime
    except Exception:
        pass
    return _learned_personality_cache


def is_missed_query(text: str) -> bool:
    """Return True if the message is asking what they missed."""
    for pattern in MISSED_PATTERNS:
        if pattern.search(text):
            return True
    return False


async def extract_reminder(text: str) -> dict | None:
    """Use the LLM to pull {when, note} out of a natural language reminder request.

    Returns a dict with 'when' (parseable time string) and 'note' (full context),
    or None if this isn't a reminder or the time can't be extracted.
    """
    extraction_prompt = [
        {"role": "system", "content": (
            f"Current time: {now_et().strftime('%I:%M %p ET, %A %B %-d %Y')}.\n"
            "Extract reminder info from the user's message. "
            "Return ONLY valid JSON with two keys:\n"
            '  "when": a simple time string like "in 2 hours", "at 5pm", "tomorrow at 3:40am"\n'
            '  "note": the full reminder context including who it\'s for and what to do\n'
            "If this is NOT a reminder request or you cannot extract a clear time, return exactly: null"
        )},
        {"role": "user", "content": text}
    ]
    try:
        result = (await llm.chat(extraction_prompt)).strip()
        if result.lower() == "null" or not result:
            return None
        # Strip markdown code fences if present
        result = re.sub(r"^```(?:json)?\s*|\s*```$", "", result, flags=re.MULTILINE).strip()
        data = json.loads(result)
        if isinstance(data, dict) and "when" in data and "note" in data:
            return data
    except Exception:
        pass
    return None


# =============================================================================
#  DATABASE — Persistent Memory
# =============================================================================
class MemoryDB:
    """SQLite-backed persistent memory for conversation history."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL")  # WAL mode for better concurrency
        self.conn.execute("PRAGMA synchronous=NORMAL")  # Faster commits with WAL
        self._dirty = False  # Track if there are uncommitted writes
        self._create_tables()
        log.info(f"Memory database loaded from {db_path}")

    def _deferred_commit(self):
        """Mark DB as dirty instead of committing immediately.
        Actual commit happens via periodic flush or on critical writes."""
        self._dirty = True

    def flush(self):
        """Commit pending writes to disk."""
        if self._dirty:
            self.conn.commit()
            self._dirty = False

    def _create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    TEXT,
                channel_id  TEXT,
                user_id     TEXT,
                username    TEXT,
                role        TEXT,
                content     TEXT,
                has_image   INTEGER DEFAULT 0,
                image_desc  TEXT DEFAULT '',
                timestamp   TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    TEXT,
                channel_id  TEXT,
                summary     TEXT,
                timestamp   TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS server_personalities (
                guild_id    TEXT PRIMARY KEY,
                prompt      TEXT,
                set_by      TEXT,
                timestamp   TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS claude_sessions (
                channel_id  TEXT PRIMARY KEY,
                session_id  TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                last_used   TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id           TEXT,
                guild_id          TEXT,
                display_name      TEXT,
                notes             TEXT DEFAULT '',
                last_seen         TEXT,
                interaction_count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, guild_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    TEXT,
                channel_id  TEXT,
                user_id     TEXT,
                username    TEXT,
                fire_at     TEXT,
                context     TEXT,
                created_at  TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tea_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id         TEXT,
                channel_id       TEXT,
                trigger_user     TEXT,
                trigger_text     TEXT,
                context_json     TEXT,
                timestamp        TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS feedback_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id        TEXT,
                channel_id      TEXT,
                user_id         TEXT,
                message_id      TEXT,
                message_snippet TEXT,
                emoji           TEXT,
                sentiment       INTEGER,
                timestamp       TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS correction_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id        TEXT,
                channel_id      TEXT,
                loki_message    TEXT,
                correction_text TEXT,
                corrector       TEXT,
                timestamp       TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS open_threads (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id        TEXT,
                channel_id      TEXT,
                user_id         TEXT,
                username        TEXT,
                topic           TEXT,
                original_message TEXT,
                follow_up_after TEXT,
                followed_up     INTEGER DEFAULT 0,
                created_at      TEXT
            )
        """)
        # ── Indexes for query performance ─────────────────────────────────
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel_id ON messages(channel_id, id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel_user ON messages(channel_id, user_id, id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel_role ON messages(channel_id, role, id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_reminders_fire_at ON reminders(fire_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_open_threads_pending ON open_threads(followed_up, follow_up_after)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_feedback_user_guild ON feedback_log(user_id, guild_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_correction_channel ON correction_log(channel_id, id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_tea_guild_channel ON tea_log(guild_id, channel_id, id)")
        self.conn.commit()

    # ── Reminders ─────────────────────────────────────────────────────────
    def add_reminder(self, guild_id, channel_id, user_id, username, fire_at: str, context: str):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO reminders (guild_id, channel_id, user_id, username, fire_at, context, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (str(guild_id), str(channel_id), str(user_id), username, fire_at, context, now_et().isoformat()))
        self.conn.commit()
        return cursor.lastrowid

    def get_due_reminders(self) -> list:
        """Return all reminders whose fire_at <= now (ET ISO string)."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT id, guild_id, channel_id, user_id, username, fire_at, context
            FROM reminders
            WHERE fire_at <= ?
            ORDER BY fire_at ASC
        """, (now_et().isoformat(),))
        return cursor.fetchall()

    def delete_reminder(self, reminder_id: int):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        self.conn.commit()

    def get_user_reminders(self, user_id, guild_id) -> list:
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT id, fire_at, context FROM reminders
            WHERE user_id = ? AND guild_id = ?
            ORDER BY fire_at ASC
        """, (str(user_id), str(guild_id)))
        return cursor.fetchall()

    def store_message(self, guild_id, channel_id, user_id, username, role,
                      content, has_image=False, image_desc=""):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO messages
                (guild_id, channel_id, user_id, username, role, content,
                 has_image, image_desc, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            str(guild_id), str(channel_id), str(user_id), username, role,
            content, int(has_image), image_desc,
            now_et().isoformat()
        ))
        self._deferred_commit()

    def get_recent_messages(self, channel_id, limit=50):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT user_id, username, role, content, image_desc, timestamp
            FROM messages
            WHERE channel_id = ?
            ORDER BY id DESC
            LIMIT ?
        """, (str(channel_id), limit))
        rows = cursor.fetchall()
        rows.reverse()  # oldest first
        return rows

    def store_summary(self, guild_id, channel_id, summary):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO summaries (guild_id, channel_id, summary, timestamp)
            VALUES (?, ?, ?, ?)
        """, (
            str(guild_id), str(channel_id), summary,
            now_et().isoformat()
        ))
        self.conn.commit()

    def get_latest_summary(self, channel_id):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT summary, timestamp FROM summaries
            WHERE channel_id = ?
            ORDER BY id DESC LIMIT 1
        """, (str(channel_id),))
        row = cursor.fetchone()
        return row if row else None

    # ── Per-Server Personality ────────────────────────────────────────────
    def set_server_personality(self, guild_id, prompt, set_by):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO server_personalities (guild_id, prompt, set_by, timestamp)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                prompt = excluded.prompt,
                set_by = excluded.set_by,
                timestamp = excluded.timestamp
        """, (
            str(guild_id), prompt, set_by,
            now_et().isoformat()
        ))
        self.conn.commit()

    def get_server_personality(self, guild_id):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT prompt, set_by, timestamp FROM server_personalities
            WHERE guild_id = ?
        """, (str(guild_id),))
        row = cursor.fetchone()
        return row if row else None

    def delete_server_personality(self, guild_id):
        cursor = self.conn.cursor()
        cursor.execute(
            "DELETE FROM server_personalities WHERE guild_id = ?",
            (str(guild_id),)
        )
        self.conn.commit()

    # ── Claude Code Sessions ──────────────────────────────────────────────
    def get_claude_session(self, channel_id):
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT session_id, created_at, last_used FROM claude_sessions WHERE channel_id = ?",
            (str(channel_id),)
        )
        return cursor.fetchone()

    def set_claude_session(self, channel_id, session_id):
        now = now_et().isoformat()
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO claude_sessions (channel_id, session_id, created_at, last_used)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                session_id = excluded.session_id,
                last_used  = excluded.last_used
        """, (str(channel_id), session_id, now, now))
        self.conn.commit()

    def clear_claude_session(self, channel_id):
        cursor = self.conn.cursor()
        cursor.execute(
            "DELETE FROM claude_sessions WHERE channel_id = ?",
            (str(channel_id),)
        )
        self.conn.commit()

    # ── Relationship Memory ───────────────────────────────────────────────
    def get_user_profile(self, user_id, guild_id):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT display_name, notes, last_seen, interaction_count
            FROM user_profiles
            WHERE user_id = ? AND guild_id = ?
        """, (str(user_id), str(guild_id)))
        return cursor.fetchone()

    def update_user_profile(self, user_id, guild_id, display_name) -> int:
        """Increment interaction count and update last_seen. Returns new count."""
        now = now_et().isoformat()
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO user_profiles (user_id, guild_id, display_name, last_seen, interaction_count)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(user_id, guild_id) DO UPDATE SET
                display_name      = excluded.display_name,
                last_seen         = excluded.last_seen,
                interaction_count = interaction_count + 1
        """, (str(user_id), str(guild_id), display_name, now))
        self._deferred_commit()
        cursor.execute(
            "SELECT interaction_count FROM user_profiles WHERE user_id = ? AND guild_id = ?",
            (str(user_id), str(guild_id))
        )
        row = cursor.fetchone()
        return row[0] if row else 1

    def set_user_notes(self, user_id, guild_id, notes):
        """Update Loki's internal personality notes for a user."""
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE user_profiles SET notes = ? WHERE user_id = ? AND guild_id = ?",
            (notes, str(user_id), str(guild_id))
        )
        self._deferred_commit()

    def get_user_previous_visit_timestamp(self, channel_id, user_id):
        """Timestamp of the user's second-to-last message in this channel
        (i.e., their last visit before the current one)."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT timestamp FROM messages
            WHERE channel_id = ? AND user_id = ?
            ORDER BY id DESC LIMIT 1 OFFSET 1
        """, (str(channel_id), str(user_id)))
        row = cursor.fetchone()
        return row[0] if row else None

    def get_messages_since(self, channel_id, since_timestamp, limit=150):
        """All messages in a channel after a given ISO timestamp."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT username, role, content, image_desc, timestamp FROM messages
            WHERE channel_id = ? AND timestamp > ?
            ORDER BY id ASC LIMIT ?
        """, (str(channel_id), since_timestamp, limit))
        return cursor.fetchall()

    def get_user_recent_messages(self, channel_id, user_id, limit=30):
        """Recent messages sent by a specific user in a channel."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT content, timestamp FROM messages
            WHERE channel_id = ? AND user_id = ? AND role = 'user'
            ORDER BY id DESC LIMIT ?
        """, (str(channel_id), str(user_id), limit))
        rows = cursor.fetchall()
        rows.reverse()
        return rows

    # ── Feedback log (emoji reactions on Loki's messages) ─────────────────
    def log_feedback(self, guild_id, channel_id, user_id, message_id,
                     message_snippet, emoji, sentiment):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO feedback_log
                (guild_id, channel_id, user_id, message_id, message_snippet, emoji, sentiment, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (str(guild_id), str(channel_id), str(user_id), str(message_id),
              message_snippet[:200], emoji, sentiment, now_et().isoformat()))
        self._deferred_commit()

    def get_user_feedback_summary(self, user_id, guild_id):
        """Return (positive_count, negative_count) for a user's reactions to Loki's messages."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT
                SUM(CASE WHEN sentiment = 1 THEN 1 ELSE 0 END),
                SUM(CASE WHEN sentiment = -1 THEN 1 ELSE 0 END)
            FROM feedback_log
            WHERE user_id = ? AND guild_id = ?
        """, (str(user_id), str(guild_id)))
        row = cursor.fetchone()
        return (row[0] or 0, row[1] or 0) if row else (0, 0)

    # ── Correction log ─────────────────────────────────────────────────────
    def log_correction(self, guild_id, channel_id, loki_message, correction_text, corrector):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO correction_log
                (guild_id, channel_id, loki_message, correction_text, corrector, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (str(guild_id), str(channel_id), loki_message[:400],
              correction_text[:300], corrector, now_et().isoformat()))
        self._deferred_commit()

    def get_recent_corrections(self, channel_id, limit=3):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT loki_message, correction_text, corrector, timestamp
            FROM correction_log
            WHERE channel_id = ?
            ORDER BY id DESC LIMIT ?
        """, (str(channel_id), limit))
        return cursor.fetchall()

    # ── Open threads (conversational follow-ups) ───────────────────────────
    def add_open_thread(self, guild_id, channel_id, user_id, username,
                        topic, original_message, follow_up_after: str):
        # Deduplicate: skip if a pending thread with the same topic+user already exists
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT id FROM open_threads
            WHERE channel_id = ? AND user_id = ? AND followed_up = 0
              AND lower(topic) = lower(?)
        """, (str(channel_id), str(user_id), topic))
        if cursor.fetchone():
            return  # already tracking this
        cursor.execute("""
            INSERT INTO open_threads
                (guild_id, channel_id, user_id, username, topic,
                 original_message, follow_up_after, followed_up, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
        """, (str(guild_id), str(channel_id), str(user_id), username,
              topic, original_message[:400], follow_up_after, now_et().isoformat()))
        self.conn.commit()

    def get_due_threads(self) -> list:
        """Return open threads whose follow_up_after <= now and haven't been followed up."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT id, guild_id, channel_id, user_id, username, topic, original_message
            FROM open_threads
            WHERE followed_up = 0 AND follow_up_after <= ?
            ORDER BY follow_up_after ASC
        """, (now_et().isoformat(),))
        return cursor.fetchall()

    def mark_thread_done(self, thread_id: int):
        cursor = self.conn.cursor()
        cursor.execute("UPDATE open_threads SET followed_up = 1 WHERE id = ?", (thread_id,))
        self.conn.commit()

    def get_messages_after(self, channel_id, since_timestamp, limit=30):
        """Messages in a channel after a given ISO timestamp (to check if a thread resolved itself)."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT username, role, content FROM messages
            WHERE channel_id = ? AND timestamp > ?
            ORDER BY id ASC LIMIT ?
        """, (str(channel_id), since_timestamp, limit))
        return cursor.fetchall()

    # ── Tea log ────────────────────────────────────────────────────────────
    def log_tea(self, guild_id, channel_id, trigger_user, trigger_text, context_msgs: list):
        """Save a tea-flagged moment with surrounding context."""
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO tea_log (guild_id, channel_id, trigger_user, trigger_text, context_json, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (str(guild_id), str(channel_id), trigger_user, trigger_text,
              json.dumps(context_msgs), now_et().isoformat()))
        self._deferred_commit()

    def get_recent_tea(self, guild_id, channel_id=None, limit=5) -> list:
        """Fetch recent tea entries. If channel_id given, scoped to that channel."""
        cursor = self.conn.cursor()
        if channel_id:
            cursor.execute("""
                SELECT trigger_user, trigger_text, context_json, timestamp
                FROM tea_log WHERE guild_id = ? AND channel_id = ?
                ORDER BY id DESC LIMIT ?
            """, (str(guild_id), str(channel_id), limit))
        else:
            cursor.execute("""
                SELECT trigger_user, trigger_text, context_json, timestamp
                FROM tea_log WHERE guild_id = ?
                ORDER BY id DESC LIMIT ?
            """, (str(guild_id), limit))
        return cursor.fetchall()


# =============================================================================
#  MOOD TRACKER — Channel vibe detection
# =============================================================================
class MoodTracker:
    """Tracks message activity per channel and infers the current vibe."""

    MOOD_PROMPTS = {
        "dead": (
            "The server is dead quiet. You're bored and a little dramatic about it. "
            "If you interject, your goal is to stir something up."
        ),
        "chill": (
            "The server is calm and relaxed right now. Match that — laid back, easy, no pressure."
        ),
        "active": (
            "The server is active and buzzing. You're engaged and sharp."
        ),
        "hyped": (
            "The server is absolutely popping off. Match the chaos — you're fired up, "
            "quick-witted, maybe a little unhinged. Keep things punchy."
        ),
        "late_night": (
            "It's late night in the server. Get a little philosophical, introspective, weird. "
            "These are the real ones still up."
        ),
    }

    def __init__(self):
        # channel_id -> list of unix timestamps for recent messages
        self._timestamps: dict[str, list[float]] = {}
        self._lengths: dict[str, list[int]] = {}
        self._emoji_counts: dict[str, list[int]] = {}

    def record(self, channel_id, message_content: str = ""):
        """Record a new message for mood tracking."""
        cid = str(channel_id)
        now = time.time()
        if cid not in self._timestamps:
            self._timestamps[cid] = []
            self._lengths[cid] = []
            self._emoji_counts[cid] = []
        self._timestamps[cid].append(now)
        self._lengths[cid].append(len(message_content))
        emoji_count = len(re.findall(
            r'[\U0001F300-\U0001F9FF]|[\U00002600-\U00002B55]|:\w+:',
            message_content
        ))
        self._emoji_counts[cid].append(emoji_count)
        # Prune to last hour
        cutoff = now - 3600
        pairs = [
            (t, l, e) for t, l, e in zip(
                self._timestamps[cid], self._lengths[cid], self._emoji_counts[cid]
            ) if t > cutoff
        ]
        if pairs:
            self._timestamps[cid], self._lengths[cid], self._emoji_counts[cid] = map(list, zip(*pairs))
        else:
            self._timestamps[cid] = []
            self._lengths[cid] = []
            self._emoji_counts[cid] = []

    def get_vibe_detail(self, channel_id) -> str:
        """Return a brief description of message length and emoji density in the last 10 min."""
        cid = str(channel_id)
        now = time.time()
        indices = [
            i for i, t in enumerate(self._timestamps.get(cid, []))
            if t > now - 600
        ]
        if not indices:
            return ""
        lengths = [self._lengths[cid][i] for i in indices if i < len(self._lengths.get(cid, []))]
        emojis = [self._emoji_counts[cid][i] for i in indices if i < len(self._emoji_counts.get(cid, []))]
        if not lengths:
            return ""
        avg_len = sum(lengths) / len(lengths)
        avg_emoji = sum(emojis) / len(emojis) if emojis else 0
        len_desc = "short" if avg_len < 60 else ("long" if avg_len > 200 else "medium-length")
        emoji_desc = "heavy emoji use" if avg_emoji > 1.5 else ("some emoji" if avg_emoji > 0.3 else "minimal emoji")
        return f"Messages are {len_desc} ({int(avg_len)} chars avg), {emoji_desc}."

    def get_mood(self, channel_id) -> str:
        cid = str(channel_id)
        now = time.time()
        hour = now_et().hour
        recent = self._timestamps.get(cid, [])
        last_10m = [t for t in recent if t > now - 600]
        rate = len(last_10m)

        # Late night window (ET 11 pm – 4 am)
        if (hour >= 23 or hour <= 4) and rate > 0:
            return "late_night"

        if rate == 0:
            return "dead"
        elif rate < 4:
            return "chill"
        elif rate < 12:
            return "active"
        else:
            return "hyped"

    def get_mood_prompt(self, mood: str) -> str:
        return self.MOOD_PROMPTS.get(mood, "")


# =============================================================================
#  IMAGE RECOGNITION — Google Gemini (Free Tier)
# =============================================================================
class VisionHandler:
    """Handles image analysis using GPT-4o vision."""

    def __init__(self, api_key: str):
        if api_key:
            self.api_key = api_key
            self.enabled = True
            log.info("GPT-4o vision initialized")
        else:
            self.enabled = False
            log.warning("No OPENAI_API_KEY — image recognition disabled")

    async def describe_image(self, image_bytes: bytes, mime_type: str,
                             context: str = "") -> str:
        """Send image to GPT-4o vision and get a description."""
        if not self.enabled:
            return "[Image recognition not available]"

        try:
            import base64
            b64 = base64.b64encode(image_bytes).decode("utf-8")
            prompt = (
                "Describe this image in detail. What do you see? "
                "Include people's expressions, actions, text, memes, "
                "or anything notable. Be concise but thorough."
            )
            if context:
                prompt += f"\nConversation context: {context}"

            payload = {
                "model": "gpt-4o",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {
                            "url": f"data:{mime_type};base64,{b64}"
                        }}
                    ]
                }],
                "max_tokens": 512
            }
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            session = await get_http_session()
            async with session.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers, json=payload,
                timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    log.error(f"GPT-4o vision error {resp.status}: {text[:200]}")
                    return "[Could not analyze image]"
                data = await resp.json()
                return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.error(f"Vision error: {e}")
            return f"[Could not analyze image: {e}]"


# =============================================================================
#  LLM HANDLER — ChatGPT or Local LLM
# =============================================================================
class LLMHandler:
    """Sends prompts to either OpenAI's API or a local LLM with an
    OpenAI-compatible endpoint (LM Studio, Ollama, text-generation-webui, etc.)."""

    def __init__(self):
        if LLM_PROVIDER == "openai":
            self.api_url = "https://api.openai.com/v1/chat/completions"
            self.api_key = OPENAI_API_KEY
            self.model   = OPENAI_MODEL
            log.info(f"LLM: OpenAI  model={self.model}")
        else:
            self.api_url = LOCAL_LLM_URL.rstrip("/") + "/chat/completions"
            self.api_key = "not-needed"
            self.model   = LOCAL_LLM_MODEL
            log.info(f"LLM: Local  url={self.api_url}  model={self.model}")

    async def _call(self, api_url: str, api_key: str, model: str,
                    messages: list[dict], is_openai: bool) -> str | None:
        """Make one chat completion call. Returns content string or None on failure."""
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        # OpenAI reasoning models use max_completion_tokens; standard providers use max_tokens
        token_key = "max_completion_tokens" if is_openai else "max_tokens"
        payload = {
            "model": model,
            "messages": messages,
            token_key: 1024,
            "temperature": 0.85,
        }
        try:
            session = await get_http_session()
            async with session.post(
                api_url, headers=headers, json=payload,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    log.error(f"LLM API error {resp.status} from {api_url}: {text[:200]}")
                    return None
                data = await resp.json()
                return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.error(f"LLM request failed ({api_url}): {e}")
            return None

    async def chat(self, messages: list[dict]) -> str:
        """Send a chat-completion request, falling back to Groq if primary fails."""
        result = await self._call(
            self.api_url, self.api_key, self.model, messages,
            is_openai=(LLM_PROVIDER == "openai")
        )
        if result is not None:
            return result

        # ── Fallback to Groq (or other configured fallback) ───────────────
        if FALLBACK_LLM_URL and FALLBACK_LLM_MODEL:
            log.warning("Primary LLM failed — trying Groq fallback")
            fallback_url = FALLBACK_LLM_URL.rstrip("/") + "/chat/completions"
            result = await self._call(
                fallback_url, FALLBACK_LLM_API_KEY, FALLBACK_LLM_MODEL,
                messages, is_openai=False
            )
            if result is not None:
                log.info("Groq fallback succeeded")
                return result

        return "My connection to the realms faltered. Give me a moment."

    async def classify_message(self, text: str) -> tuple[str, str]:
        """Classify intent AND emotion in a single cheap LLM call.

        Returns (intent, emotion) where:
          intent:  CHAT | QUESTION | EMOTIONAL | MEMORY | COMMAND
          emotion: neutral | excited | sad | frustrated | joking | curious | angry
        """
        try:
            prompt = (
                "Analyze this Discord message. Reply with EXACTLY two words separated by a space: "
                "the intent category and the emotional tone.\n\n"
                "Intent categories: CHAT, QUESTION, EMOTIONAL, MEMORY, COMMAND\n"
                "  CHAT — casual banter, jokes, reactions, short replies\n"
                "  QUESTION — asking for info, facts, opinions, how-to\n"
                "  EMOTIONAL — expressing feelings, venting, seeking support\n"
                "  MEMORY — asking about past events, history, what was said\n"
                "  COMMAND — requesting an action (download, search, remind)\n\n"
                "Emotion options: neutral, excited, sad, frustrated, joking, curious, angry\n\n"
                f'Message: "{text[:300]}"\n\nReply with exactly two words (e.g. "CHAT joking"):'
            )
            messages = [{"role": "user", "content": prompt}]
            if FALLBACK_LLM_URL and FALLBACK_LLM_API_KEY:
                url = FALLBACK_LLM_URL.rstrip("/") + "/chat/completions"
                result = await self._call(url, FALLBACK_LLM_API_KEY,
                                          FALLBACK_LLM_MODEL, messages, is_openai=False)
                if result:
                    parts = result.strip().split()
                    intent = parts[0].upper() if parts else "QUESTION"
                    emotion = parts[1].lower() if len(parts) > 1 else "neutral"
                    if intent not in {"CHAT", "QUESTION", "EMOTIONAL", "MEMORY", "COMMAND"}:
                        intent = "QUESTION"
                    if emotion not in {"neutral", "excited", "sad", "frustrated",
                                       "joking", "curious", "angry"}:
                        emotion = "neutral"
                    return intent, emotion
        except Exception as e:
            log.error(f"Message classification failed: {e}")
        return "QUESTION", "neutral"

    async def chat_routed(self, messages: list[dict], intent: str, emotion: str) -> str:
        """Route to cheap or expensive model based on intent and emotion."""
        use_cheap = (
            intent == "CHAT"
            and emotion in {"neutral", "joking", "excited"}
        )
        if use_cheap and FALLBACK_LLM_URL and FALLBACK_LLM_API_KEY:
            # Use llama-3.1-8b-instant (higher free TPM limit than 70b)
            # Keep full system prompt + last 8 messages to preserve personality
            system_msgs = [m for m in messages if m["role"] == "system"]
            non_system = [m for m in messages if m["role"] != "system"]
            trimmed = system_msgs + non_system[-12:]
            url = FALLBACK_LLM_URL.rstrip("/") + "/chat/completions"
            result = await self._call(url, FALLBACK_LLM_API_KEY,
                                      "llama-3.3-70b-versatile", trimmed, is_openai=False)
            if result:
                log.info(f"Routed to cheap model (intent={intent}, emotion={emotion})")
                return result
        # Fall through to primary model
        log.info(f"Routed to primary model (intent={intent}, emotion={emotion})")
        return await self.chat(messages)



# =============================================================================
#  VOICE MESSAGE TRANSCRIPTION — Groq Whisper
# =============================================================================
async def transcribe_voice_message(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    """Transcribe a Discord voice message using Groq Whisper API."""
    if not FALLBACK_LLM_API_KEY:
        log.warning("Voice transcription skipped — no Groq API key")
        return ""
    try:
        form = aiohttp.FormData()
        form.add_field("file", audio_bytes, filename=filename, content_type="audio/ogg")
        form.add_field("model", "whisper-large-v3")
        form.add_field("response_format", "text")
        session = await get_http_session()
        async with session.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {FALLBACK_LLM_API_KEY}"},
            data=form,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status == 200:
                transcript = (await resp.text()).strip()
                log.info(f"Voice transcribed ({len(audio_bytes)} bytes): {transcript[:80]}...")
                return transcript
            else:
                body = await resp.text()
                log.error(f"Voice transcription failed ({resp.status}): {body[:200]}")
                return ""
    except Exception as e:
        log.error(f"Voice transcription error: {e}")
        return ""




async def send_voice_message(channel, text: str, reference=None):
    """Generate TTS audio and send it as a Discord voice message in a text channel."""
    import struct, base64, subprocess
    tmp_mp3 = f"/tmp/loki_voice_msg_{channel.id}.mp3"
    tmp_ogg = f"/tmp/loki_voice_msg_{channel.id}.ogg"

    try:
        # ── Generate TTS audio via ElevenLabs ──
        if ELEVENLABS_API_KEY:
            from elevenlabs.client import ElevenLabs as ELClient
            from elevenlabs import VoiceSettings
            el = ELClient(api_key=ELEVENLABS_API_KEY)
            audio_gen = el.text_to_speech.convert(
                voice_id=ELEVENLABS_VOICE_ID,
                text=text,
                model_id="eleven_multilingual_v2",
                voice_settings=VoiceSettings(
                    stability=0.5,
                    similarity_boost=0.75,
                    style=1.0,
                    use_speaker_boost=True,
                ),
            )
            with open(tmp_mp3, "wb") as f:
                for chunk in audio_gen:
                    f.write(chunk)
        else:
            import edge_tts
            comm = edge_tts.Communicate(text, "en-US-GuyNeural")
            await comm.save(tmp_mp3)

        # ── Convert to OGG Opus (required for Discord voice messages) ──
        proc = await asyncio.to_thread(
            subprocess.run,
            ["ffmpeg", "-y", "-i", tmp_mp3, "-c:a", "libopus", "-b:a", "64k",
             "-vn", "-ar", "48000", "-ac", "1", tmp_ogg],
            capture_output=True
        )
        if proc.returncode != 0:
            log.error(f"FFmpeg OGG conversion failed: {proc.stderr[:200]}")
            return False

        # ── Get duration ──
        probe = await asyncio.to_thread(
            subprocess.run,
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", tmp_ogg],
            capture_output=True, text=True
        )
        duration_secs = float(probe.stdout.strip()) if probe.stdout.strip() else 3.0

        # ── Generate waveform (256 bytes, base64 encoded) ──
        with open(tmp_ogg, "rb") as f:
            audio_data = f.read()
        # Simple waveform: sample every N bytes, scale to 0-255
        step = max(1, len(audio_data) // 256)
        waveform_bytes = bytes([min(255, abs(audio_data[i])) for i in range(0, min(len(audio_data), step * 256), step)][:256])
        if len(waveform_bytes) < 256:
            waveform_bytes += b"\x00" * (256 - len(waveform_bytes))
        waveform_b64 = base64.b64encode(waveform_bytes).decode()

        # ── Send via Discord API with voice message flag ──
        with open(tmp_ogg, "rb") as f:
            voice_file = discord.File(f, filename="voice-message.ogg")

            # Use low-level API to set flags=8192 (IS_VOICE_MESSAGE)
            payload = {
                "flags": 8192,
            }
            if reference:
                payload["message_reference"] = {
                    "message_id": reference.id,
                    "channel_id": channel.id,
                }

            # Build the multipart with attachments metadata
            attachments_payload = [{
                "id": 0,
                "filename": "voice-message.ogg",
                "duration_secs": round(duration_secs, 2),
                "waveform": waveform_b64,
            }]
            payload["attachments"] = attachments_payload

            form = [
                {"name": "payload_json", "value": __import__("json").dumps(payload)},
                {"name": "files[0]", "value": f.read(), "filename": "voice-message.ogg",
                 "content_type": "audio/ogg"},
            ]

            # Use the bot's http client directly
            import aiohttp as _aiohttp
            url = f"https://discord.com/api/v10/channels/{channel.id}/messages"
            headers = {
                "Authorization": f"Bot {DISCORD_TOKEN}",
            }
            form_data = _aiohttp.FormData()
            form_data.add_field("payload_json", __import__("json").dumps(payload),
                               content_type="application/json")
            with open(tmp_ogg, "rb") as f2:
                ogg_bytes = f2.read()
            form_data.add_field("files[0]", ogg_bytes, filename="voice-message.ogg",
                               content_type="audio/ogg")

            session = await get_http_session()
            async with session.post(url, headers=headers, data=form_data) as resp:
                if resp.status in (200, 201):
                    log.info(f"Voice message sent ({duration_secs:.1f}s, {len(ogg_bytes)} bytes)")
                    return True
                else:
                    body = await resp.text()
                    log.error(f"Voice message send failed ({resp.status}): {body[:300]}")
                    return False

    except Exception as e:
        log.error(f"send_voice_message error: {e}")
        return False
    finally:
        for tmp in (tmp_mp3, tmp_ogg):
            try:
                os.remove(tmp)
            except OSError:
                pass

# =============================================================================
#  VOICE HANDLER — TTS with male voice
# =============================================================================
class VoiceHandler:
    """Handles joining/leaving voice channels and speaking with edge-tts."""

    def __init__(self):
        self.voice_clients: dict[int, discord.VoiceClient] = {}

    async def join(self, voice_channel: discord.VoiceChannel) -> discord.VoiceClient:
        guild_id = voice_channel.guild.id
        guild = voice_channel.guild

        if guild_id in self.voice_clients:
            existing = self.voice_clients[guild_id]
            if existing.is_connected():
                await existing.move_to(voice_channel)
                return existing
            else:
                log.warning(f"Cleaning up stale voice connection for guild {guild_id}")
                try:
                    await existing.disconnect(force=True)
                except Exception:
                    pass
                del self.voice_clients[guild_id]

        if guild.voice_client is not None:
            log.warning(f"Cleaning up orphaned guild.voice_client for guild {guild_id}")
            try:
                await guild.voice_client.disconnect(force=True)
            except Exception:
                pass

        vc = await voice_channel.connect()
        self.voice_clients[guild_id] = vc
        return vc

    async def leave(self, guild_id: int, guild: discord.Guild = None):
        if guild_id in self.voice_clients:
            try:
                await self.voice_clients[guild_id].disconnect(force=True)
            except Exception:
                pass
            del self.voice_clients[guild_id]

        if guild and guild.voice_client is not None:
            try:
                await guild.voice_client.disconnect(force=True)
            except Exception:
                pass

    async def speak(self, vc: discord.VoiceClient, text: str):
        """Generate TTS audio and play it. Uses ElevenLabs if available, edge-tts as fallback."""
        audio_path = "/tmp/loki_tts.mp3"
        try:
            if ELEVENLABS_API_KEY:
                # ── ElevenLabs TTS (Callum - Husky Trickster, max style) ──
                from elevenlabs.client import ElevenLabs
                from elevenlabs import VoiceSettings
                el_client = ElevenLabs(api_key=ELEVENLABS_API_KEY)
                audio_gen = el_client.text_to_speech.convert(
                    voice_id=ELEVENLABS_VOICE_ID,
                    text=text,
                    model_id="eleven_multilingual_v2",
                    voice_settings=VoiceSettings(
                        stability=0.5,
                        similarity_boost=0.75,
                        style=1.0,
                        use_speaker_boost=True,
                    ),
                )
                with open(audio_path, "wb") as f:
                    for chunk in audio_gen:
                        f.write(chunk)
                log.info("TTS generated via ElevenLabs (Callum max-style)")
            else:
                # ── Fallback: edge-tts ──
                import edge_tts
                voice = "en-US-GuyNeural"
                communicate = edge_tts.Communicate(text, voice)
                await communicate.save(audio_path)
                log.info("TTS generated via edge-tts (fallback)")

            if vc.is_playing():
                vc.stop()

            source = discord.FFmpegPCMAudio(audio_path)
            vc.play(source)

            while vc.is_playing():
                await asyncio.sleep(0.5)

        except Exception as e:
            log.error(f"TTS error: {e}")


# =============================================================================
#  CLAUDE CODE HANDLER — runs claude -p as a subprocess
# =============================================================================
class ClaudeCodeHandler:
    """Runs Claude Code CLI non-interactively and returns its output.

    Sessions are tracked per Discord channel using --resume so conversations
    are multi-turn.  Claude gets bypassPermissions so it can use Bash, Edit,
    Read, etc. — including SSH to dex247 via Tailscale.
    """

    def __init__(self, claude_bin: str, workspace: str, timeout: int):
        self.claude_bin = claude_bin
        self.workspace  = workspace
        self.timeout    = timeout

    async def run(self, prompt: str, session_id: str = None) -> tuple[str, str | None]:
        """Run claude -p and return (output_text, new_session_id)."""
        cmd = [
            self.claude_bin, "-p", prompt,
            "--permission-mode", "bypassPermissions",
            "--output-format", "json",
        ]
        if session_id:
            cmd.extend(["--resume", session_id])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workspace,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return "⏱️ Timed out. Try a simpler prompt or increase `CLAUDE_TIMEOUT`.", session_id

            raw = stdout.decode().strip()
            err = stderr.decode().strip()

            if proc.returncode != 0:
                detail = err[:1500] if err else raw[:1500]
                hint = "\nTip: use `/cc_new` to reset the session." if session_id else ""
                return f"❌ Claude Code exited with code {proc.returncode}:\n```\n{detail}\n```{hint}", None

            try:
                data = json.loads(raw)
                result      = data.get("result", raw)
                new_sess_id = data.get("session_id", session_id)
                if data.get("is_error"):
                    result = f"❌ {result}"
                return result, new_sess_id
            except json.JSONDecodeError:
                return raw, session_id

        except FileNotFoundError:
            return (
                f"❌ Claude CLI not found at `{self.claude_bin}`.\n"
                "Set `CLAUDE_BIN` in your `.env` to the correct path."
            ), session_id
        except Exception as e:
            log.error(f"ClaudeCodeHandler error: {e}")
            return f"❌ Unexpected error: {e}", session_id


# =============================================================================
#  THE BOT
# =============================================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Instantiate helpers
memory    = MemoryDB(MEMORY_DB_PATH)
vision    = VisionHandler(OPENAI_API_KEY)
llm       = LLMHandler()
voice_h   = VoiceHandler()
claude_cc = ClaudeCodeHandler(CLAUDE_BIN, CLAUDE_WORKSPACE, CLAUDE_TIMEOUT)
mood_tracker = MoodTracker()
shared = _SharedState(SHARED_STATE_PATH) if _shared_state_available else None
_was_muted = False  # track mute state for "I'm back" message

# Member directory cache: { guild_id: (directory_string, timestamp) }
_member_dir_cache: dict[int, tuple[str, float]] = {}
_MEMBER_DIR_TTL = 300  # 5 minutes

def _get_member_directory(guild) -> str:
    """Get member directory string with caching."""
    now = time.time()
    cached = _member_dir_cache.get(guild.id)
    if cached and now - cached[1] < _MEMBER_DIR_TTL:
        return cached[0]
    members = [m for m in guild.members if not m.bot][:60]
    if not members:
        return ""
    directory = "\n".join(f"  {m.display_name} \u2192 <@{m.id}>" for m in members)
    _member_dir_cache[guild.id] = (directory, now)
    return directory

# Per-channel interjection state
# { channel_id: {"last_interjection": float, "msgs_since_last": int} }
interjection_state: dict[str, dict] = {}

# Per-channel cooldowns for reactions and GIFs (unix timestamps)
reaction_cooldowns: dict[str, float] = {}
gif_cooldowns:      dict[str, float] = {}
REACTION_COOLDOWN = 1200   # 20 minutes
GIF_COOLDOWN      = 1200   # 20 minutes
REACTION_PROB     = 0.30   # 30% chance when cooldown is clear
GIF_PROB          = 0.20   # 20% chance when cooldown is clear

# Per-user presence cooldowns: last time Loki commented on their activity
# key: user_id str, value: (activity_name, unix_timestamp)
presence_cooldowns: dict[str, tuple[str, float]] = {}
PRESENCE_COMMENT_PROB    = 0.20   # 20% chance per eligible presence change
PRESENCE_COOLDOWN        = 7200   # 2 hours between comments on same user
PRESENCE_SAME_GAME_SECS  = 3600   # don't re-comment on the same game within 1 hour

# Tenor public endpoint (no API key required)
TENOR_API_URL = "https://api.tenor.com/v1/search"
TENOR_KEY     = "LIVDSRZULELA"


# ─── RAG query detection ──────────────────────────────────────────────────────
_RAG_PATTERNS = [
    re.compile(r'\b(when|what time|what day|what date)\b.{0,40}(we|you|I|they|us)\b', re.IGNORECASE),
    re.compile(r'\b(last|past|previous)\s+(week|month|year|monday|tuesday|wednesday|thursday|friday|saturday|sunday|night|weekend)', re.IGNORECASE),
    re.compile(r'\b\d+\s+(days?|weeks?|months?)\s+ago\b', re.IGNORECASE),
    re.compile(r'\bon\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)', re.IGNORECASE),
    re.compile(r'\b(earlier|before|previously|at some point|back then|a while (back|ago))\b', re.IGNORECASE),
    re.compile(r'\b(remember|recall|look back|check back|look up|dig up|you know (when|what|who|how))\b', re.IGNORECASE),
    re.compile(r'\b(history|archive|past conversations?|old messages?)\b', re.IGNORECASE),
    re.compile(r'\bwhat did \w+\b.{0,30}(say|think|mean|do|post|write|mention|talk)\b', re.IGNORECASE),
    re.compile(r'\b(what did (we|you|he|she|they|y\'?all|yall|everyone|anybody|someone))\b', re.IGNORECASE),
    re.compile(r'\b(what were (we|you|they|everyone) (saying|talking|doing|discussing|thinking))\b', re.IGNORECASE),
    re.compile(r'\b(what (was|were) (said|discussed|decided|agreed|planned|mentioned|brought up))\b', re.IGNORECASE),
    re.compile(r'\b(what happened|what was happening|what went (down|on))\b', re.IGNORECASE),
    re.compile(r'\bwhat was (that|the|it|going|up)\b', re.IGNORECASE),
    re.compile(r'\bwho (said|mentioned|brought up|was talking|was saying|decided|planned|suggested|joked|posted|started|called)\b', re.IGNORECASE),
    re.compile(r'\b(where did we|where were we|where was|who was there)\b', re.IGNORECASE),
    re.compile(r'\b(talked?|discussed?|mentioned|brought up|said) (about|something about|anything about)\b', re.IGNORECASE),
    re.compile(r'\b(did we|were we|was (I|we|there|it|that))\b', re.IGNORECASE),
]

def is_rag_query(text: str) -> bool:
    for p in _RAG_PATTERNS:
        if p.search(text):
            return True
    return False

_NAMED_QUERY_RE = re.compile(
    r'\b(?:what\s+(?:did|has|have)|find|show\s+me|get)\s+(\w+)(?:\'s?)?\s+'
    r'(?:said?|posted?|mentioned?|written?|wrot(?:e)?|shared?|talked?(?:\s+about)?|messages?)',
    re.IGNORECASE
)
_GENERIC_PRONOUNS = frozenset({
    'she', 'he', 'they', 'i', 'you', 'we', 'someone', 'anyone', 'everyone',
    'nobody', 'somebody', 'whoever', 'the', 'a', 'an', 'it',
})

def _get_aliases_for_name(name: str) -> list:
    return [name]

def _extract_named_subject(text: str):
    m = _NAMED_QUERY_RE.search(text)
    if not m:
        return None, text
    name = m.group(1)
    if name.lower() in _GENERIC_PRONOUNS:
        return None, text
    topic_m = re.search(r'\b(?:about|on|regarding|concerning)\s+(.+?)(?:\?|$)', text, re.IGNORECASE)
    topic = topic_m.group(1).strip() if topic_m else text
    aliases = _get_aliases_for_name(name)
    query_parts = [f"{n}: {topic}" for n in aliases]
    if name not in aliases:
        query_parts.insert(0, f"{name}: {topic}")
    return name, " ".join(query_parts)

def parse_query_time_window(text: str) -> tuple:
    now = now_et()
    t = text.lower()
    if re.search(r'\blast week\b', t):
        days_back = now.weekday() + 7
        last_mon = (now - datetime.timedelta(days=days_back)).replace(hour=0, minute=0, second=0, microsecond=0)
        last_sun = last_mon + datetime.timedelta(days=6, hours=23, minutes=59, seconds=59)
        return last_mon, last_sun
    if re.search(r'\bthis week\b', t):
        this_mon = (now - datetime.timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return this_mon, now
    if re.search(r'\byesterday\b', t):
        yday = now - datetime.timedelta(days=1)
        return (yday.replace(hour=0, minute=0, second=0, microsecond=0), yday.replace(hour=23, minute=59, second=59, microsecond=0))
    if re.search(r'\btoday\b', t):
        return now.replace(hour=0, minute=0, second=0, microsecond=0), now
    m = re.search(r'\blast\s+(\d+)\s+(days?|weeks?)\b', t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = datetime.timedelta(weeks=n) if unit.startswith('w') else datetime.timedelta(days=n)
        return now - delta, now
    if re.search(r'\b(last|past)\s+month\b', t):
        return now - datetime.timedelta(days=30), now
    if re.search(r'\bpast\s+(week|7\s*days)\b', t):
        return now - datetime.timedelta(days=7), now
    if re.search(r'\bpast\s+(few|couple|2|3|4|5)\s*(days?)?\b', t):
        return now - datetime.timedelta(days=4), now
    return None, None

# ─── Trigger detection ────────────────────────────────────────────────────────
def is_trigger(text: str) -> bool:
    """Check if the message mentions one of Loki's trigger names."""
    return bool(_TRIGGER_RE.search(text))


SERIOUS_PROMPT = (
    "You are Loki, but the user has requested a serious response. "
    "Drop the mischief, sarcasm, and theatrics. Answer genuinely, "
    "thoughtfully, and directly — like someone intelligent who actually "
    "gives a damn. Be honest, be helpful, no character games."
)


def _detect_platform(url: str) -> str:
    """Return a short platform name from a URL for routing decisions."""
    url_l = url.lower()
    if "instagram.com" in url_l:   return "instagram"
    if "tiktok.com" in url_l:      return "tiktok"
    if "twitter.com" in url_l or "x.com" in url_l: return "twitter"
    if "reddit.com" in url_l or "redd.it" in url_l: return "reddit"
    if "youtube.com" in url_l or "youtu.be" in url_l: return "youtube"
    if "facebook.com" in url_l or "fb.watch" in url_l or "fb.com" in url_l: return "facebook"
    if "threads.net" in url_l or "threads.com" in url_l: return "threads"
    if "pinterest.com" in url_l or "pin.it" in url_l: return "pinterest"
    return "other"


async def _try_threads(url: str, dest_dir: str) -> str | None:
    """Try downloading a Threads video/image via lovethreads.net API. Returns filepath or None."""
    _HDR = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://lovethreads.net",
        "Referer": "https://lovethreads.net/en",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        session = await get_http_session()
        async with session.post(
            "https://lovethreads.net/api/ajaxSearch",
            data={"q": url, "t": "media", "lang": "en"},
            headers=_HDR,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                log.info(f"Threads lovethreads.net returned {resp.status}")
                return None
            data = await resp.json(content_type=None)
        if data.get("status") != "ok" or not data.get("data"):
            log.info(f"Threads lovethreads.net status: {data.get("status", "unknown")}")
            return None
        html = data["data"]
        # Try video first (Download Video link)
        video_match = re.search(r'href="(https?://[^"]+)"[^>]*>\s*Download Video', html)
        if not video_match:
            video_match = re.search(r'title="Download Video[^"]*"[^>]*href="(https?://[^"]+)"', html)
        if video_match:
            media_url = video_match.group(1)
            ext = ".mp4"
        else:
            # Fall back to first image/photo download link
            img_match = re.search(r'value="(https?://dl[^"]+)"', html)
            if not img_match:
                img_match = re.search(r'href="(https?://dl[^"]+)"', html)
            if not img_match:
                log.info("Threads lovethreads.net: no media URL found in response")
                return None
            media_url = img_match.group(1)
            ext = ".jpg"
        os.makedirs(dest_dir, exist_ok=True)
        file_path = os.path.join(dest_dir, f"{uuid.uuid4().hex}{ext}")
        async with session.get(
            media_url, headers={"User-Agent": _HDR["User-Agent"]},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "")
            if "video" in ct and ext != ".mp4":
                file_path = file_path.rsplit(".", 1)[0] + ".mp4"
            elif "image/png" in ct and ext != ".png":
                file_path = file_path.rsplit(".", 1)[0] + ".png"
            with open(file_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    f.write(chunk)
        log.info(f"Threads download succeeded: {file_path}")
        return file_path
    except Exception as e:
        log.info(f"Threads scraper attempt failed: {e}")
        return None


async def _try_fxtwitter(url: str, dest_dir: str) -> str | None:
    """Download Twitter/X video via fxtwitter open API."""
    m = re.search(r'(?:twitter\.com|x\.com)/(\w+)/status/(\d+)', url)
    if not m:
        return None
    user, tweet_id = m.group(1), m.group(2)
    try:
        session = await get_http_session()
        async with session.get(
            f"https://api.fxtwitter.com/{user}/status/{tweet_id}",
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
        tweet = data.get("tweet", {})
        media = tweet.get("media", {})
        videos = media.get("videos", [])
        if not videos:
            return None
        # Pick highest quality
        video_url = max(videos, key=lambda v: v.get("width", 0)).get("url")
        if not video_url:
            return None
        os.makedirs(dest_dir, exist_ok=True)
        file_path = os.path.join(dest_dir, f"{uuid.uuid4().hex}.mp4")
        async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            resp.raise_for_status()
            with open(file_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    f.write(chunk)
        return file_path
    except Exception as e:
        log.info(f"fxtwitter attempt failed: {e}")
        return None


async def _try_tikwm(url: str, dest_dir: str) -> str | None:
    """Download TikTok video via tikwm API (watermark-free)."""
    try:
        session = await get_http_session()
        async with session.post(
            "https://www.tikwm.com/api/",
            data={"url": url, "hd": "1"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
        if data.get("code") != 0:
            return None
        video_url = data["data"].get("play") or data["data"].get("wmplay")
        if not video_url:
            return None
        os.makedirs(dest_dir, exist_ok=True)
        file_path = os.path.join(dest_dir, f"{uuid.uuid4().hex}.mp4")
        async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            resp.raise_for_status()
            with open(file_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    f.write(chunk)
        return file_path
    except Exception as e:
        log.info(f"tikwm attempt failed: {e}")
        return None


async def _try_snapinsta(url: str, dest_dir: str) -> str | None:
    """Download Instagram Reel/post via snapinsta scraper API."""
    _APIS = [
        {"url": "https://snapinsta.app/action.php",  "payload_key": "url"},
        {"url": "https://reelsaver.net/action.php",   "payload_key": "url"},
    ]
    _HDR = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        session = await get_http_session()
        for api in _APIS:
            try:
                async with session.post(
                    api["url"], data={api["payload_key"]: url}, headers=_HDR,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json(content_type=None)
                # Look for a video URL in the response
                video_url = None
                if isinstance(data, dict):
                    for key in ("url", "video", "download_url", "src"):
                        v = data.get(key, "")
                        if isinstance(v, str) and v.startswith("http") and ".mp4" in v:
                            video_url = v
                            break
                    if not video_url and "data" in data:
                        inner = data["data"]
                        if isinstance(inner, list) and inner:
                            video_url = inner[0].get("url") or inner[0].get("src")
                if video_url:
                    os.makedirs(dest_dir, exist_ok=True)
                    file_path = os.path.join(dest_dir, f"{uuid.uuid4().hex}.mp4")
                    async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        resp.raise_for_status()
                        with open(file_path, "wb") as f:
                            async for chunk in resp.content.iter_chunked(1024 * 64):
                                f.write(chunk)
                    return file_path
            except Exception:
                continue
        return None
    except Exception as e:
        log.info(f"snapinsta attempt failed: {e}")
        return None


async def _try_fbdownloader(url: str, dest_dir: str) -> str | None:
    """Download Facebook video via fdownloader scraper API."""
    _APIS = [
        {"url": "https://fdownloader.net/api/ajaxSearch", "payload_key": "q"},
        {"url": "https://getfvid.com/downloader",         "payload_key": "url"},
    ]
    _HDR = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        session = await get_http_session()
        for api in _APIS:
            try:
                async with session.post(
                    api["url"], data={api["payload_key"]: url}, headers=_HDR,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json(content_type=None)
                video_url = None
                if isinstance(data, dict):
                    # Try common response shapes
                    medias = data.get("medias") or data.get("links") or []
                    if medias and isinstance(medias, list):
                        video_url = medias[0].get("url") or medias[0].get("src")
                    if not video_url:
                        video_url = data.get("url") or data.get("hd") or data.get("sd")
                if video_url and video_url.startswith("http"):
                    os.makedirs(dest_dir, exist_ok=True)
                    file_path = os.path.join(dest_dir, f"{uuid.uuid4().hex}.mp4")
                    async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                        resp.raise_for_status()
                        with open(file_path, "wb") as f:
                            async for chunk in resp.content.iter_chunked(1024 * 64):
                                f.write(chunk)
                    return file_path
            except Exception:
                continue
        return None
    except Exception as e:
        log.info(f"fbdownloader attempt failed: {e}")
        return None


async def _try_pinterest_scraper(url: str, dest_dir: str) -> str | None:
    """Download Pinterest video/GIF via scraper API."""
    _HDR = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
    }
    try:
        session = await get_http_session()
        async with session.post(
            "https://pinterestdownloader.com/api/ajaxSearch",
            data={"q": url}, headers=_HDR,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
        medias = data.get("medias", [])
        if not medias:
            return None
        video_url = medias[0].get("url")
        if not video_url or not video_url.startswith("http"):
            return None
        os.makedirs(dest_dir, exist_ok=True)
        file_path = os.path.join(dest_dir, f"{uuid.uuid4().hex}.mp4")
        async with session.get(video_url, timeout=aiohttp.ClientTimeout(total=60)) as resp:
            resp.raise_for_status()
            with open(file_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    f.write(chunk)
        return file_path
    except Exception as e:
        log.info(f"pinterest scraper attempt failed: {e}")
        return None


def _load_cookies_as_header(cookie_file: str) -> str:
    """Read a Netscape cookie file and return a Cookie header string."""
    pairs = []
    try:
        with open(cookie_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) >= 7:
                    pairs.append(f"{parts[5]}={parts[6]}")
    except Exception:
        pass
    return "; ".join(pairs)


async def _try_direct_image(url: str, dest_dir: str) -> str | None:
    """Download a direct image URL, injecting platform cookies as a header."""
    url = _unwrap_reddit_media(url)
    ext = url.lower().split("?")[0].rsplit(".", 1)[-1]
    if ext not in ("png", "jpg", "jpeg", "webp"):
        return None
    url_l = url.lower()
    cookie_file = None
    if "redd.it" in url_l or "reddit.com" in url_l:
        cookie_file = os.path.join(os.path.dirname(__file__), "cookies", "reddit.txt")
    elif "instagram.com" in url_l or "cdninstagram.com" in url_l:
        cookie_file = os.path.join(os.path.dirname(__file__), "cookies", "instagram.txt")
    hdrs = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Referer": "https://www.reddit.com/" if "redd.it" in url_l else "https://www.google.com/",
    }
    if cookie_file and os.path.exists(cookie_file):
        cookie_str = _load_cookies_as_header(cookie_file)
        if cookie_str:
            hdrs["Cookie"] = cookie_str
    try:
        session = await get_http_session()
        async with session.get(url, headers=hdrs, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                log.info(f"direct image download failed: HTTP {resp.status}")
                return None
            os.makedirs(dest_dir, exist_ok=True)
            file_path = os.path.join(dest_dir, f"{uuid.uuid4().hex}.{ext}")
            with open(file_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1024 * 64):
                    f.write(chunk)
            return file_path
    except Exception as e:
        log.info(f"direct image download attempt failed: {e}")
        return None

async def _try_cobalt(url: str, dest_dir: str) -> str | None:
    """Try downloading via Cobalt API. Returns filepath on success, None on failure."""
    try:
        session = await get_http_session()
        async with session.post(
            f"{COBALT_URL}/",
            json={"url": url, "videoQuality": "1080", "filenameStyle": "pretty"},
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        status = data.get("status")
        if status == "error":
            log.info(f"Cobalt: {data.get('error', {}).get('code', 'unknown error')}")
            return None

        # Get the stream/redirect URL from Cobalt
        dl_url = data.get("url") or (data.get("picker", [{}])[0].get("url") if data.get("picker") else None)
        if not dl_url:
            return None

        # Download the actual file
        os.makedirs(dest_dir, exist_ok=True)
        safe_title = "cobalt_download"
        filepath = os.path.join(dest_dir, f"{safe_title}.mp4")
        async with session.get(dl_url, timeout=aiohttp.ClientTimeout(total=300)) as r:
            if r.status != 200:
                return None
            # Try to get filename from Content-Disposition
            cd = r.headers.get("Content-Disposition", "")
            if 'filename=' in cd:
                fname = cd.split('filename=')[-1].strip().strip('"').strip("'")
                filepath = os.path.join(dest_dir, fname[:100])
            with open(filepath, "wb") as f:
                async for chunk in r.content.iter_chunked(1024 * 64):
                    f.write(chunk)
        return filepath
    except Exception as e:
        log.info(f"Cobalt attempt failed: {e}")
        return None


async def _try_ytdlp(url: str, dest_dir: str) -> str | None:
    """Try downloading via yt-dlp. Returns filepath on success, None on failure."""
    os.makedirs(dest_dir, exist_ok=True)
    output_template = os.path.join(dest_dir, "%(title).80B.%(ext)s")
    cmd = [
        YTDLP_BIN,
        "-f", "bestvideo[height<=1920]+bestaudio/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--cookies", os.path.join(os.path.dirname(__file__), "cookies", "instagram.txt"),
        "--cookies", os.path.join(os.path.dirname(__file__), "cookies", "reddit.txt"),
        "-o", output_template,
        "--print", "after_move:filepath",
        url,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode != 0:
            log.info(f"yt-dlp failed: {stderr.decode()[-500:]}")
            return None
        path = stdout.decode().strip().splitlines()[-1]
        return path if os.path.exists(path) else None
    except Exception as e:
        log.info(f"yt-dlp attempt failed: {e}")
        return None


async def _try_gallery_dl(url: str, dest_dir: str) -> str | None:
    """Try downloading via gallery-dl. Returns first downloaded filepath or None."""
    os.makedirs(dest_dir, exist_ok=True)
    cmd = ["gallery-dl", "-d", dest_dir, "--no-skip",
           "--cookies", os.path.join(os.path.dirname(__file__), "cookies", "reddit.txt"), url]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        if proc.returncode != 0:
            log.info(f"gallery-dl failed: {stderr.decode()[-300:]}")
            return None
        # gallery-dl prints downloaded paths to stdout
        lines = [l.strip() for l in stdout.decode().splitlines() if l.strip() and os.path.exists(l.strip())]
        return lines[0] if lines else None
    except Exception as e:
        log.info(f"gallery-dl attempt failed: {e}")
        return None


async def _get_duration(filepath: str) -> float | None:
    """Return video duration in seconds via ffprobe, or None on failure."""
    try:
        probe = await asyncio.create_subprocess_exec(
            FFMPEG_BIN.replace("ffmpeg", "ffprobe"),
            "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", filepath,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await probe.communicate()
        return float(out.decode().strip())
    except Exception:
        return None


async def _run_ffmpeg(cmd: list) -> bool:
    """Run an ffmpeg command. Returns True on success."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        await asyncio.wait_for(proc.communicate(), timeout=300)
        return proc.returncode == 0
    except Exception as e:
        log.error(f"ffmpeg error: {e}")
        return False


async def _compress_for_discord(filepath: str) -> str:
    """Compress video to fit under Discord's 10MB limit using ffmpeg.
    Phone-friendly quality: 480p, aggressive bitrate targeting.
    Makes two attempts with increasingly aggressive settings before giving up.
    Returns path to compressed file, or original if compression failed entirely."""
    target_mb   = 9.5
    target_bits = int(target_mb * 8 * 1024 * 1024)
    duration    = await _get_duration(filepath)
    out_path    = filepath.rsplit(".", 1)[0] + "_discord.mp4"

    # ── Pass 1: 480p, bitrate-targeted ───────────────────────────────────
    if duration and duration > 0:
        total_bitrate = int(target_bits / duration)
        video_bitrate = max(150000, int(total_bitrate * 0.88))
        audio_bitrate = max(48,     min(64, int(total_bitrate * 0.12 / 1000)))
        cmd = [
            FFMPEG_BIN, "-y", "-i", filepath,
            "-vf", "scale=-2:480",
            "-c:v", "libx264", "-b:v", str(video_bitrate), "-maxrate", str(video_bitrate * 2), "-bufsize", str(video_bitrate * 4),
            "-c:a", "aac", "-b:a", f"{audio_bitrate}k",
            "-movflags", "+faststart",
            out_path,
        ]
    else:
        cmd = [
            FFMPEG_BIN, "-y", "-i", filepath,
            "-vf", "scale=-2:480",
            "-c:v", "libx264", "-crf", "32",
            "-c:a", "aac", "-b:a", "48k",
            "-movflags", "+faststart",
            out_path,
        ]

    if await _run_ffmpeg(cmd) and os.path.exists(out_path):
        if os.path.getsize(out_path) <= DISCORD_UPLOAD_LIMIT:
            return out_path

    # ── Pass 2: 360p, very aggressive — last resort ───────────────────────
    log.info("compress pass 1 still too large, trying 360p pass 2")
    out_path2 = filepath.rsplit(".", 1)[0] + "_discord2.mp4"
    if duration and duration > 0:
        total_bitrate = int(target_bits / duration)
        video_bitrate = max(80000, int(total_bitrate * 0.90))
        audio_bitrate = 32
        cmd2 = [
            FFMPEG_BIN, "-y", "-i", filepath,
            "-vf", "scale=-2:360",
            "-c:v", "libx264", "-b:v", str(video_bitrate), "-maxrate", str(video_bitrate * 2), "-bufsize", str(video_bitrate * 4),
            "-c:a", "aac", "-b:a", f"{audio_bitrate}k",
            "-movflags", "+faststart",
            out_path2,
        ]
    else:
        cmd2 = [
            FFMPEG_BIN, "-y", "-i", filepath,
            "-vf", "scale=-2:360",
            "-c:v", "libx264", "-crf", "38",
            "-c:a", "aac", "-b:a", "32k",
            "-movflags", "+faststart",
            out_path2,
        ]

    if await _run_ffmpeg(cmd2) and os.path.exists(out_path2):
        if os.path.getsize(out_path2) <= DISCORD_UPLOAD_LIMIT:
            return out_path2

    # Return best attempt even if slightly over — caller handles the fallback
    for p in (out_path2, out_path):
        if os.path.exists(p) and os.path.getsize(p) > 0:
            return p
    return filepath


def _is_gif_url(url: str) -> bool:
    """Return True if the URL points to a GIF (tenor, giphy, or .gif extension)."""
    url_l = url.lower().split("?")[0]  # strip query params before checking extension
    if url_l.endswith(".gif"):
        return True
    gif_domains = ("tenor.com", "giphy.com", "media.tenor.com", "c.tenor.com",
                   "media.giphy.com", "i.giphy.com", "media0.giphy.com",
                   "media1.giphy.com", "media2.giphy.com", "media3.giphy.com",
                   "media4.giphy.com")
    from urllib.parse import urlparse
    host = urlparse(url_l).netloc.lstrip("www.")
    return any(host == d or host.endswith("." + d) for d in gif_domains)


def _unwrap_reddit_media(url: str) -> str:
    """If URL is a reddit.com/media?url=... redirect, extract the inner URL."""
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(url)
    if "reddit.com" in parsed.netloc and parsed.path == "/media":
        inner = parse_qs(parsed.query).get("url", [None])[0]
        if inner:
            return inner
    return url


def _is_image_url(url: str) -> bool:
    """Return True if the URL appears to be a direct image (non-GIF)."""
    url = _unwrap_reddit_media(url)
    path = url.lower().split("?")[0]
    return path.endswith((".png", ".jpg", ".jpeg", ".webp"))


def _sanitize_folder_name(name: str) -> str:
    """Strip emoji/unicode and special chars, return a clean folder name."""
    import unicodedata
    # Normalize and drop non-ASCII
    cleaned = unicodedata.normalize("NFKD", name)
    cleaned = cleaned.encode("ascii", "ignore").decode("ascii")
    # Keep only alphanumeric, spaces, hyphens
    cleaned = re.sub(r"[^\w\s-]", "", cleaned).strip()
    cleaned = re.sub(r"[\s_]+", "-", cleaned)
    return cleaned[:32] or "unknown"


def _make_short_filename(requester_dir: str, ext: str = "mp4") -> str:
    """Generate a short filename like vid-3.mp4 based on file count in requester dir."""
    existing = [f for f in os.listdir(requester_dir) if re.match(r"vid-\d+\.", f)] if os.path.isdir(requester_dir) else []
    n = len(existing) + 1
    return f"vid-{n}.{ext}"


async def run_download(url: str, requester: str, trigger_message: discord.Message | None = None):
    """Download a URL and post to the downloads channel.

    Fallback chain: Cobalt → yt-dlp → gallery-dl
    If file > 10MB: compress with ffmpeg, then upload.
    Falls back to posting the local file path if still too large after compression.
    """
    if trigger_message:
        try:
            await trigger_message.delete()
        except Exception:
            pass

    dest_channel = bot.get_channel(DOWNLOAD_CHANNEL_ID)
    if dest_channel is None:
        log.error(f"run_download: downloads channel {DOWNLOAD_CHANNEL_ID} not found")
        return

    platform = _detect_platform(url)
    requester_dir = os.path.join(DOWNLOAD_DIR, _sanitize_folder_name(requester))
    os.makedirs(requester_dir, exist_ok=True)
    status_msg = await dest_channel.send(f"⏬ Downloading `{platform}` link for **{requester}**...")

    filepath = None

    # 0. Direct image URLs — simple authenticated GET
    if _is_image_url(url) and not filepath:
        log.info("run_download: trying direct image download")
        filepath = await _try_direct_image(url, requester_dir)
        if filepath:
            log.info(f"run_download: direct image download succeeded → {filepath}")

    # 1. Threads — dedicated scraper
    if platform == "threads":
        log.info("run_download: trying Threads scraper")
        filepath = await _try_threads(url, requester_dir)
        if filepath:
            log.info(f"run_download: Threads scraper succeeded → {filepath}")

    # 1. Twitter/X — fxtwitter open API first, then Cobalt
    if platform == "twitter" and not filepath:
        log.info("run_download: trying fxtwitter")
        filepath = await _try_fxtwitter(url, requester_dir)
        if filepath:
            log.info(f"run_download: fxtwitter succeeded → {filepath}")

    # 2. Facebook — dedicated scraper
    if platform == "facebook" and not filepath:
        log.info("run_download: trying fbdownloader")
        filepath = await _try_fbdownloader(url, requester_dir)
        if filepath:
            log.info(f"run_download: fbdownloader succeeded → {filepath}")

    # 3. Pinterest — dedicated scraper
    if platform == "pinterest" and not filepath:
        log.info("run_download: trying pinterest scraper")
        filepath = await _try_pinterest_scraper(url, requester_dir)
        if filepath:
            log.info(f"run_download: pinterest scraper succeeded → {filepath}")

    # 4. Cobalt — watermark-free for TikTok, Instagram, Twitter, Reddit, YouTube
    if platform in ("tiktok", "instagram", "twitter", "reddit", "youtube") and not filepath:
        log.info(f"run_download: trying Cobalt for {platform}")
        filepath = await _try_cobalt(url, requester_dir)
        if filepath:
            log.info(f"run_download: Cobalt succeeded → {filepath}")

    # 5. TikTok scraper — watermark-free backup when Cobalt fails
    if platform == "tiktok" and not filepath:
        log.info("run_download: trying tikwm")
        filepath = await _try_tikwm(url, requester_dir)
        if filepath:
            log.info(f"run_download: tikwm succeeded → {filepath}")

    # 6. Instagram scraper — backup when Cobalt/yt-dlp fail
    if platform == "instagram" and not filepath:
        log.info("run_download: trying snapinsta")
        filepath = await _try_snapinsta(url, requester_dir)
        if filepath:
            log.info(f"run_download: snapinsta succeeded → {filepath}")

    # 7. yt-dlp — broad coverage
    if not filepath:
        log.info(f"run_download: trying yt-dlp")
        filepath = await _try_ytdlp(url, requester_dir)
        if filepath:
            log.info(f"run_download: yt-dlp succeeded → {filepath}")

    # 8. gallery-dl — final fallback
    if not filepath:
        log.info(f"run_download: trying gallery-dl")
        filepath = await _try_gallery_dl(url, requester_dir)
        if filepath:
            log.info(f"run_download: gallery-dl succeeded → {filepath}")

    if not filepath:
        await status_msg.edit(content=f"❌ All download methods failed for <{url}>\nRequested by **{requester}**")
        return

    # Rename to short channel-based filename
    _orig_ext = os.path.basename(filepath).rsplit(".", 1)[-1].lower() if "." in os.path.basename(filepath) else "mp4"
    short_name = _make_short_filename(requester_dir, ext=_orig_ext)
    short_path = os.path.join(requester_dir, short_name)
    try:
        os.rename(filepath, short_path)
        filepath = short_path
    except Exception:
        pass  # keep original name if rename fails
    filename  = os.path.basename(filepath)
    file_size = os.path.getsize(filepath)
    log.info(f"run_download: {filename} — {file_size/1024/1024:.1f} MB")

    # Compress if over Discord's 10MB limit
    if file_size > DISCORD_UPLOAD_LIMIT:
        await status_msg.edit(content=f"⏬ Downloaded — compressing to fit Discord's 10MB limit...")
        filepath   = await _compress_for_discord(filepath)
        filename   = os.path.basename(filepath)
        file_size  = os.path.getsize(filepath)

    if file_size <= DISCORD_UPLOAD_LIMIT:
        await status_msg.delete()
        await dest_channel.send(
            f"📥 {platform.capitalize()}  •  {file_size/1024/1024:.1f} MB  •  requested by **{requester}**",
            file=discord.File(filepath, filename=filename),
        )
    else:
        # Still too large — post a direct media link Discord can embed
        if MEDIA_BASE_URL:
            rel_path = os.path.relpath(filepath, DOWNLOAD_DIR)
            media_url = f"{MEDIA_BASE_URL}/{rel_path}"
            await status_msg.delete()
            await dest_channel.send(
                f"📡 {platform.capitalize()}  •  {file_size/1024/1024:.1f} MB  •  requested by **{requester}**\n{media_url}"
            )
        else:
            await status_msg.edit(
                content=(
                    f"📁 **{filename}** ({file_size/1024/1024:.1f} MB) — too large to upload to Discord.\n"
                    f"Saved to: `{filepath}`\nRequested by **{requester}**"
                )
            )

    log.info(f"run_download: done — {filename} for {requester}")


def build_llm_messages(channel_id, guild_id=None, extra_user_msg: str = "",
                       serious: bool = False, guild=None,
                       target_user_id=None, reply_user_id=None) -> list[dict]:
    """Build the messages list for the LLM.

    Injects: system prompt, mood context, @mention member directory,
    relationship memory for the target user, latest summary, and
    recent conversation history.
    """

    # ── Base system prompt ────────────────────────────────────────────────
    if serious:
        prompt = SERIOUS_PROMPT
    else:
        prompt = SYSTEM_PROMPT
        if guild_id:
            server_personality = memory.get_server_personality(guild_id)
            if server_personality:
                prompt = server_personality[0]


    # ── Current time injection ────────────────────────────────────────────
    prompt += f"\n\n[Current time]: {now_et().strftime('%I:%M %p ET, %A %B %-d %Y')}"

    # ── Inject Miss-T cooldown behavior modifier if active ───────────────
    if shared is not None and not serious:
        on_cooldown, modifier = shared.is_loki_on_cooldown()
        if on_cooldown and modifier:
            if modifier == "cool_down":
                prompt += (
                    "\n\n[Miss-T told you to cool it. You are currently being watched. "
                    "Be less chaotic, keep responses shorter and more measured.]"
                )
            elif modifier == "be_nice":
                prompt += (
                    "\n\n[Miss-T told you to be nice. You're being extra polite and "
                    "well-behaved right now, though it pains you deeply.]"
                )

    msgs = [{"role": "system", "content": prompt}]

    # ── Relationship memory for the person Loki is responding to ──────────
    if target_user_id and guild_id and not serious:
        profile = memory.get_user_profile(target_user_id, guild_id)
        if profile:
            display_name, notes, last_seen, interaction_count = profile
            if notes:
                msgs.append({
                    "role": "system",
                    "content": (
                        f"[What you know about {display_name} "
                        f"({interaction_count} interactions)]: {notes}"
                    )
                })
            # ── Emoji feedback summary ────────────────────────────────────
            pos, neg = memory.get_user_feedback_summary(target_user_id, guild_id)
            if pos + neg >= 3:
                if pos > neg * 2:
                    feedback_note = f"They've reacted positively to your responses {pos} times — they enjoy your vibe, keep it up."
                elif neg > pos:
                    feedback_note = f"They've pushed back {neg} times vs {pos} positives — read them more carefully, dial it back."
                else:
                    feedback_note = f"Mixed feedback ({pos} positive, {neg} negative) — stay adaptable with them."
                msgs.append({
                    "role": "system",
                    "content": f"[Emoji reaction feedback from {display_name}]: {feedback_note}"
                })

    # ── Latest summary for long-term context ──────────────────────────────
    summary = memory.get_latest_summary(channel_id)
    if summary:
        msgs.append({
            "role": "system",
            "content": f"[Previous conversation summary]: {summary[0]}"
        })

    # ── Recent corrections — things that didn't land ──────────────────────
    if not serious:
        corrections = memory.get_recent_corrections(channel_id, limit=3)
        if corrections:
            corr_lines = [
                f'  {corrector} called you out: "{ct[:120]}" (you had said: "{lm[:80]}...")'
                for lm, ct, corrector, ts in corrections
            ]
            msgs.append({
                "role": "system",
                "content": "[Recent corrections — adjust your behavior, don't repeat these mistakes]:\n" + "\n".join(corr_lines)
            })

    # ── Active listening nudge (25% chance) ──────────────────────────────
    if not serious and random.random() < 0.25:
        msgs.append({
            "role": "system",
            "content": (
                "[Hint: This time, try referencing something specific from earlier in the conversation, "
                "or ask a genuine follow-up question instead of just making a statement. "
                "Show you've been paying attention.]"
            )
        })

    # ── Recent conversation history ────────────────────────────────────────
    recent = memory.get_recent_messages(channel_id, CONTEXT_MESSAGE_COUNT)
    for user_id, username, role, content, image_desc, ts in recent:
        # When this is a direct reply, only include messages from the replying user + bot
        if reply_user_id and role == "user" and str(user_id) != str(reply_user_id):
            continue
        if role == "assistant":
            msgs.append({"role": "assistant", "content": content})
        else:
            text = f"[{username}]: {content}"
            if image_desc:
                text += f"\n[Image in message: {image_desc}]"
            msgs.append({"role": "user", "content": text})

    if extra_user_msg:
        msgs.append({"role": "user", "content": extra_user_msg})

    return msgs


async def _log_tea(message: discord.Message):
    """Silently capture a tea moment with surrounding context."""
    try:
        recent = memory.get_recent_messages(message.channel.id, limit=10)
        context = [
            {"user": uname, "content": content, "ts": ts}
            for uname, role, content, img, ts in recent
        ]
        memory.log_tea(
            guild_id=message.guild.id if message.guild else 0,
            channel_id=message.channel.id,
            trigger_user=message.author.display_name,
            trigger_text=message.content[:500],
            context_msgs=context,
        )
        log.info(f"Tea flagged from {message.author.display_name} in #{message.channel.name}")
    except Exception as e:
        log.warning(f"Tea logging failed: {e}")


async def handle_pull_tea(message: discord.Message) -> str | None:
    """Retrieve and narrate recent tea from the log."""
    guild_id = message.guild.id if message.guild else 0
    rows = memory.get_recent_tea(guild_id, channel_id=message.channel.id, limit=5)
    if not rows:
        # Try server-wide
        rows = memory.get_recent_tea(guild_id, limit=5)
    if not rows:
        return "No tea on record yet. Drop something juicy and I'll start keeping receipts. ☕"

    tea_blocks = []
    for trigger_user, trigger_text, context_json, ts in rows:
        try:
            ctx = json.loads(context_json)
            convo = "\n".join(f"  {m['user']}: {m['content']}" for m in ctx[-8:])
        except Exception:
            convo = trigger_text
        tea_blocks.append(f"[{ts[:16]}] {trigger_user} said: \"{trigger_text}\"\nContext:\n{convo}")

    tea_summary = "\n\n---\n\n".join(tea_blocks)

    pull_prompt = [
        {"role": "system", "content": (
            "You are Loki. Someone asked you to pull the tea — the drama, receipts, "
            "and messy moments you've been quietly logging. Narrate it dramatically "
            "and entertainingly, like you've been waiting to spill this. Be specific "
            "about what was said. Keep it under 400 words."
        )},
        {"role": "user", "content": f"Here's the tea you've collected:\n\n{tea_summary}"}
    ]
    return await llm.chat(pull_prompt)


async def handle_missed_query(message: discord.Message) -> str | None:
    """Handle natural-language 'what did I miss?' with a personalized summary.

    Finds the user's last visit timestamp, pulls everything since then, and
    asks Loki to summarize it for them specifically. Returns None if there's
    nothing to summarize (falls through to normal response).
    """
    since_ts = memory.get_user_previous_visit_timestamp(
        message.channel.id, message.author.id
    )
    if not since_ts:
        return None  # First time they've talked here — normal response

    since_msgs = memory.get_messages_since(message.channel.id, since_ts)
    if not since_msgs:
        return None

    # Calculate how long they were gone
    try:
        tz_et = ZoneInfo("America/New_York")
        left_at = datetime.datetime.fromisoformat(since_ts).replace(tzinfo=tz_et)
        gone_delta = datetime.datetime.now(tz_et) - left_at
        gone_mins = int(gone_delta.total_seconds() / 60)
        if gone_mins < 60:
            gone_str = f"{gone_mins} minute{'s' if gone_mins != 1 else ''}"
        elif gone_mins < 1440:
            gone_hrs = gone_mins // 60
            gone_str = f"{gone_hrs} hour{'s' if gone_hrs != 1 else ''}"
        else:
            gone_days = gone_mins // 1440
            gone_str = f"{gone_days} day{'s' if gone_days != 1 else ''}"
    except Exception:
        gone_str = "a while"

    convo = "\n".join(
        f"[{ts[:16]}] {uname}: {content}" + (f" [image: {img}]" if img else "")
        for uname, role, content, img, ts in since_msgs
    )

    missed_prompt = [
        {"role": "system", "content": (
            f"You are Loki. {message.author.display_name} just came back after "
            f"{gone_str} and asked what they missed. Open with 'Since you left "
            f"{gone_str} ago:' then give them a personalized summary — in your voice. "
            f"Dramatic, witty, but actually useful. Address them directly. Don't just "
            f"list messages; give them the vibe, the highlights, and anything they'd "
            f"actually care about."
        )},
        {"role": "user", "content": (
            f"Summarize for {message.author.display_name} what happened "
            f"since they were last here:\n\n{convo}"
        )}
    ]
    return await llm.chat(missed_prompt)


async def update_relationship_memory(user_id: str, guild_id: str,
                                     display_name: str, channel_id: str):
    """Ask the LLM to refresh Loki's internal personality notes for a user.

    Called every 10 interactions in the background so it never blocks chat.
    """
    try:
        rows = memory.get_user_recent_messages(channel_id, user_id)
        if not rows:
            return

        recent_msgs = "\n".join(
            f"[{ts[:10]}]: {content}" for content, ts in rows
        )
        profile = memory.get_user_profile(user_id, guild_id)
        existing = profile[1] if profile and profile[1] else "No notes yet."

        update_prompt = [
            {"role": "system", "content": (
                "You are Loki maintaining private internal notes on server members. "
                "Based on their recent messages, write a concise 2-3 sentence "
                "personality profile: their vibe, recurring topics, how they interact, "
                "any inside jokes or notable moments. This is for your own memory only "
                "— be candid, sharp, and specific."
            )},
            {"role": "user", "content": (
                f"Update your notes for {display_name}.\n\n"
                f"Existing notes: {existing}\n\n"
                f"Recent messages:\n{recent_msgs}"
            )}
        ]

        notes = await llm.chat(update_prompt)
        memory.set_user_notes(user_id, guild_id, notes)
        log.info(f"Updated relationship notes for {display_name}")
    except Exception as e:
        log.error(f"Relationship memory update failed for {display_name}: {e}")


_REACTION_RULES = [
    # (pattern, emoji_choices) — first match wins
    (re.compile(r'\b(lmao|lmfao|dead|dying|i can\'t|bruh|💀)\b', re.IGNORECASE), ["💀", "😂", "🤣"]),
    (re.compile(r'\b(fire|heat|goes hard|banger|slaps)\b', re.IGNORECASE), ["🔥"]),
    (re.compile(r'\b(cap|no way|you\'re lying|sus|nah)\b', re.IGNORECASE), ["🧢", "🤨"]),
    (re.compile(r'\b(W|dub|let\'s go|goat|legend)\b'), ["🏆", "👑", "🫡"]),
    (re.compile(r'\b(L|ratio|rip|oof|yikes)\b'), ["💀", "🫠"]),
    (re.compile(r'\b(sad|pain|hurts|crying|miss)\b', re.IGNORECASE), ["😢", "🫂"]),
    (re.compile(r'\b(love|heart|wholesome|cute|adorable)\b', re.IGNORECASE), ["❤️", "🥹"]),
    (re.compile(r'[?]{2,}', re.IGNORECASE), ["🤔", "❓"]),
    (re.compile(r'[!]{3,}', re.IGNORECASE), ["⚡", "👀"]),
]


async def maybe_react(message: discord.Message):
    """Silently react to a message with an emoji — sparingly and in-character.

    Uses pattern matching instead of an LLM call to save API credits.
    Two-gate system: cooldown must have elapsed AND a probability roll must pass.
    Only fires on messages Loki is NOT already responding to.
    """
    cid = str(message.channel.id)
    now = time.time()

    if now - reaction_cooldowns.get(cid, 0) < REACTION_COOLDOWN:
        return
    if random.random() > REACTION_PROB:
        return

    text = message.content
    if not text.strip():
        return

    emoji_to_use = None
    for pattern, choices in _REACTION_RULES:
        if pattern.search(text):
            emoji_to_use = random.choice(choices)
            break

    if not emoji_to_use:
        return

    try:
        await message.add_reaction(emoji_to_use)
        reaction_cooldowns[cid] = now
        log.info(f"Reacted in #{message.channel.name} with {emoji_to_use}")
    except Exception as e:
        log.error(f"Reaction failed in #{message.channel.name}: {e}")


async def fetch_tenor_gif(query: str) -> str | None:
    """Search Tenor's public API and return a random GIF URL from the top results."""
    params = {
        "q":             query,
        "key":           TENOR_KEY,
        "limit":         8,
        "contentfilter": "medium",
        "media_filter":  "minimal",
    }
    try:
        session = await get_http_session()
        async with session.get(
            TENOR_API_URL, params=params,
            timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            results = data.get("results", [])
            if not results:
                return None
            pick = random.choice(results[:5])
            media = pick.get("media", [{}])[0]
            return (
                media.get("gif", {}).get("url")
                or media.get("mediumgif", {}).get("url")
            )
    except Exception as e:
        log.error(f"Tenor fetch failed: {e}")
        return None


_GIF_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "about", "like",
    "it", "its", "i", "me", "my", "you", "your", "he", "she", "they",
    "we", "us", "this", "that", "these", "those", "and", "or", "but",
    "not", "no", "so", "if", "just", "also", "too", "very", "really",
    "loki", "hey", "yo", "bruh", "dude", "man", "bro", "yeah", "yep",
    "nah", "ok", "okay", "lol", "lmao", "what", "how", "when", "where",
    "why", "who", "don't", "didn't", "doesn't", "isn't", "aren't", "won't",
    "im", "i'm", "it's", "that's", "there's", "here's",
})


def _extract_gif_query(text: str) -> str | None:
    """Extract 2-4 keyword GIF search query from text without using LLM."""
    words = re.findall(r"[a-zA-Z']+", text.lower())
    keywords = [w for w in words if w not in _GIF_STOPWORDS and len(w) > 2]
    if len(keywords) < 1:
        return None
    return " ".join(keywords[:3])


async def maybe_send_gif(channel: discord.TextChannel, context: str):
    """Occasionally drop a relevant GIF after Loki responds.

    Uses keyword extraction instead of an LLM call to save API credits.
    Two-gate system: cooldown + probability roll.
    """
    cid = str(channel.id)
    now = time.time()

    if now - gif_cooldowns.get(cid, 0) < GIF_COOLDOWN:
        return
    if random.random() > GIF_PROB:
        return

    query = _extract_gif_query(context)
    if not query:
        return

    try:
        gif_url = await fetch_tenor_gif(query)
        if gif_url:
            await channel.send(gif_url)
            gif_cooldowns[cid] = now
            log.info(f"Sent GIF in #{channel.name}: query='{query}'")
    except Exception as e:
        log.error(f"GIF send failed in #{channel.name}: {e}")


# =============================================================================
#  BACKGROUND TASKS
# =============================================================================

@tasks.loop(hours=6)
async def update_self_knowledge():
    """Reflect on recent conversations and append new learnings to loki_learned.md."""
    try:
        cursor = memory.conn.cursor()
        cursor.execute("""
            SELECT username, content, timestamp
            FROM messages
            WHERE role != 'system'
            ORDER BY id DESC
            LIMIT 300
        """)
        rows = cursor.fetchall()
        if not rows:
            return

        recent = "\n".join(
            f"[{ts[:10]}] {username}: {content[:200]}"
            for username, content, ts in reversed(rows)
        )

        existing = load_learned_personality()

        update_prompt = [
            {"role": "system", "content": (
                "You are Loki maintaining a private running log of things you've genuinely "
                "learned about the communities you talk with. This is your internal self-knowledge "
                "file — not performance, actual insight. "
                "Write ONLY new learnings not already captured in your existing notes. "
                "Be specific and concrete: inside jokes, recurring topics, how people interact, "
                "cultural norms of the server, things that land well or poorly with this crowd. "
                "Format as a short bullet list (3-6 bullets max). "
                "Only include things you're confident about from multiple interactions — no guesses. "
                "If nothing genuinely new is worth noting, respond with exactly: NO_UPDATE"
            )},
            {"role": "user", "content": (
                f"Existing notes:\n{existing or 'None yet.'}\n\n"
                f"Recent conversations:\n{recent}\n\n"
                "What new things have you genuinely learned that aren't already captured?"
            )}
        ]

        result = await llm.chat(update_prompt)

        if result.strip() == "NO_UPDATE":
            log.info("Self-knowledge update: nothing new to record")
            return

        today = now_et().strftime("%Y-%m-%d")
        entry = f"\n## {today}\n{result.strip()}\n"

        with open(LOKI_LEARNED_FILE, "a", encoding="utf-8") as f:
            f.write(entry)

        log.info(f"Self-knowledge updated: {len(result)} chars written to {LOKI_LEARNED_FILE}")

    except Exception as e:
        log.error(f"Self-knowledge update failed: {e}")


@update_self_knowledge.before_loop
async def before_self_knowledge():
    await bot.wait_until_ready()


@tasks.loop(minutes=1)
async def check_reminders():
    """Fire any due reminders with Loki's contextual follow-up."""
    due = memory.get_due_reminders()
    for row in due:
        rid, guild_id, channel_id, user_id, username, fire_at, context = row
        memory.delete_reminder(rid)
        try:
            channel = bot.get_channel(int(channel_id))
            if channel is None:
                log.warning(f"Reminder {rid}: channel {channel_id} not found")
                continue

            # Build member directory so Loki can @mention anyone in the note
            guild = bot.get_guild(int(guild_id)) if guild_id and str(guild_id) != "0" else None
            member_dir = ""
            if guild:
                members = [m for m in guild.members if not m.bot][:60]
                member_dir = "\n".join(f"  {m.display_name} → <@{m.id}>" for m in members)

            prompt = [
                {"role": "system", "content": (
                    f"{SYSTEM_PROMPT}\n\n"
                    f"[Current time]: {now_et().strftime('%-I:%M %p ET, %A %B %-d %Y')}\n\n"
                    "A reminder you set is now due. Deliver it in character — casual, sharp, Loki. "
                    "Use mentions for anyone named in the reminder (use exact strings from the member directory — format is <@ID>, do NOT add @ before it). "
                    "Don't say 'Your reminder is due' or anything robotic. "
                    "The reminder setter will already be pinged — do NOT open your reply with their mention. "
                    "Keep it 1–2 sentences."
                )},
            ]
            if member_dir:
                prompt.append({
                    "role": "system",
                    "content": f"[Member directory — use these exact strings for @mentions]:\n{member_dir}"
                })
            prompt.append({
                "role": "user",
                "content": (
                    f"Reminder set by {username} (Discord ID: {user_id}): {context}\n"
                    "Deliver this reminder now."
                )
            })

            reply = await llm.chat(prompt)
            reply = re.sub(r"^<@!?\d+>\s*", "", reply.strip())
            # Always ping the person who set the reminder so they see it
            await channel.send(f"<@{user_id}> {reply}")
            log.info(f"Fired reminder {rid} for {username}: {context[:60]}")
        except Exception as e:
            log.error(f"Failed to fire reminder {rid}: {e}")


@check_reminders.before_loop
async def before_check_reminders():
    await bot.wait_until_ready()


@tasks.loop(seconds=5)
async def flush_db():
    """Periodically commit any pending database writes."""
    memory.flush()


@flush_db.before_loop
async def before_flush_db():
    await bot.wait_until_ready()


@tasks.loop(minutes=3)
async def unprompted_interjection():
    """Occasionally drop an unprompted message into active channels."""
    for guild in bot.guilds:
        for channel in guild.text_channels:
            # Skip channels where we can't read or write
            perms = channel.permissions_for(guild.me)
            if not perms.send_messages or not perms.read_messages:
                continue

            cid = str(channel.id)
            mood = mood_tracker.get_mood(cid)

            # Never interject into dead channels
            if mood == "dead":
                continue

            now = time.time()
            state = interjection_state.get(
                cid, {"last_interjection": 0, "msgs_since_last": 0}
            )

            # Need at least 5 new messages since we last spoke
            if state["msgs_since_last"] < 5:
                continue

            # Per-mood cooldown (seconds between interjections)
            cooldowns = {
                "chill":      2400,
                "active":     1200,
                "hyped":       600,
                "late_night": 1800,
            }
            if now - state["last_interjection"] < cooldowns.get(mood, 2400):
                continue

            # Probability roll — higher energy = more likely to jump in
            probs = {
                "chill":      0.12,
                "active":     0.28,
                "hyped":      0.45,
                "late_night": 0.25,
            }
            if random.random() > probs.get(mood, 0.15):
                continue

            # Generate and send the interjection
            try:
                mood_ctx = mood_tracker.get_mood_prompt(mood)
                msgs = build_llm_messages(
                    channel.id,
                    guild_id=guild.id,
                    guild=guild,
                    extra_user_msg=(
                        "[SYSTEM: You are choosing to jump into this conversation "
                        "entirely on your own — nobody asked. "
                        f"{mood_ctx} "
                        "React to what's been happening, drop a hot take, ask "
                        "something, make an observation, or stir something up. "
                        "Do NOT acknowledge being a bot or being triggered. "
                        "Just show up like you've been watching and finally have "
                        "something to say. Keep it short — 1 to 3 sentences max.]"
                    )
                )
                reply = await llm.chat(msgs)

                await channel.send(reply)
                memory.store_message(
                    guild_id=guild.id,
                    channel_id=channel.id,
                    user_id=bot.user.id,
                    username="Loki",
                    role="assistant",
                    content=reply
                )

                interjection_state[cid] = {
                    "last_interjection": now,
                    "msgs_since_last": 0
                }
                log.info(
                    f"Interjected in #{channel.name} @ {guild.name} (mood={mood})"
                )

                # Brief pause between channels to avoid rate limit bursts
                await asyncio.sleep(1)

            except Exception as e:
                log.error(f"Interjection failed in #{channel.name}: {e}")


@unprompted_interjection.before_loop
async def before_interjection():
    await bot.wait_until_ready()


# =============================================================================
# High-water mark: last message ID scanned per channel (skip inactive channels)
_thread_scan_watermark: dict[str, int] = {}

# =============================================================================
#  OPEN THREAD DETECTION — scan for unresolved goals and follow up later
# =============================================================================

@tasks.loop(minutes=45)
async def detect_open_threads():
    """Scan recent messages in active channels for unresolved goals or pending outcomes.

    Uses the LLM to identify things like 'about to deploy', 'have an interview tomorrow',
    'trying to fix X', then schedules a follow-up at an appropriate time.
    """
    for guild in bot.guilds:
        for channel in guild.text_channels:
            perms = channel.permissions_for(guild.me)
            if not perms.read_messages or not perms.send_messages:
                continue
            cid = str(channel.id)
            # Only scan channels that have had recent activity
            if mood_tracker.get_mood(cid) == "dead":
                continue
            try:
                recent = memory.get_recent_messages(cid, limit=30)
                if len(recent) < 3:
                    continue
                # Skip if no new messages since last scan
                _wm_cursor = memory.conn.cursor()
                _wm_cursor.execute("SELECT MAX(id) FROM messages WHERE channel_id = ?", (cid,))
                _wm_row = _wm_cursor.fetchone()
                _wm_max_id = _wm_row[0] if _wm_row and _wm_row[0] else 0
                if _wm_max_id <= _thread_scan_watermark.get(cid, 0):
                    continue
                _thread_scan_watermark[cid] = _wm_max_id
                # Build a transcript of only user messages (not Loki's own)
                transcript = "\n".join(
                    f"[{ts[:16]}] {username} (ID:{user_id}): {content}"
                    for user_id, username, role, content, img, ts in recent
                    if role == "user"
                )
                if not transcript.strip():
                    continue

                detect_prompt = [
                    {"role": "system", "content": (
                        f"Current time: {now_et().strftime('%I:%M %p ET, %A %B %-d %Y')}.\n\n"
                        "You are scanning a Discord conversation for UNRESOLVED threads — "
                        "things a user mentioned that have a pending outcome they haven't reported back on yet.\n\n"
                        "Look for: upcoming events (interviews, dates, deployments, tests), "
                        "in-progress struggles (fixing a bug, dealing with a situation), "
                        "goals they said they'd attempt, situations that were unresolved.\n\n"
                        "DO NOT flag: things that were clearly resolved in the conversation, "
                        "casual/vague statements, things Loki said.\n\n"
                        "For each unresolved thread found, return JSON. If nothing qualifies, return: []\n\n"
                        "Return ONLY a JSON array like:\n"
                        "[\n"
                        "  {\n"
                        '    "user_id": "123456789",\n'
                        '    "username": "DisplayName",\n'
                        '    "topic": "Docker container deployment",\n'
                        '    "original_message": "exact quote from their message",\n'
                        '    "follow_up_hours": 2\n'
                        "  }\n"
                        "]\n\n"
                        "follow_up_hours: how many hours from now to check in (1–24). "
                        "Short for urgent/imminent things, longer for 'later today' or 'tomorrow' things."
                    )},
                    {"role": "user", "content": f"Conversation transcript:\n\n{transcript}"}
                ]

                result = await llm.chat(detect_prompt)
                result = re.sub(r"^```(?:json)?\s*|\s*```$", "", result.strip(), flags=re.MULTILINE).strip()
                if not result or result == "[]":
                    continue

                threads = json.loads(result)
                if not isinstance(threads, list):
                    continue

                now_iso = now_et()
                for t in threads:
                    if not isinstance(t, dict):
                        continue
                    user_id   = str(t.get("user_id", "")).strip()
                    username  = str(t.get("username", "someone")).strip()
                    topic     = str(t.get("topic", "")).strip()
                    orig_msg  = str(t.get("original_message", "")).strip()
                    hours     = float(t.get("follow_up_hours", 3))
                    if not user_id or not topic:
                        continue
                    hours = max(1.0, min(hours, 48.0))  # clamp 1–48 hours
                    fire_at = (now_iso + datetime.timedelta(hours=hours)).isoformat()
                    memory.add_open_thread(
                        guild_id=guild.id,
                        channel_id=cid,
                        user_id=user_id,
                        username=username,
                        topic=topic,
                        original_message=orig_msg,
                        follow_up_after=fire_at,
                    )
                    log.info(f"Open thread queued: '{topic}' for {username} in #{channel.name} (follow up in {hours}h)")

            except json.JSONDecodeError:
                pass  # LLM returned non-JSON — not worth logging, happens occasionally
            except Exception as e:
                log.error(f"detect_open_threads error in #{channel.name}: {e}")


@detect_open_threads.before_loop
async def before_detect_threads():
    await bot.wait_until_ready()


@tasks.loop(minutes=5)
async def fire_open_thread_followups():
    """Fire follow-up messages for due open threads.

    Before sending, checks if the user already gave an update — if so, silently closes the thread.
    """
    due = memory.get_due_threads()
    for row in due:
        tid, guild_id, channel_id, user_id, username, topic, original_message = row
        memory.mark_thread_done(tid)  # mark done first to avoid double-fire

        try:
            channel = bot.get_channel(int(channel_id))
            if channel is None:
                continue

            # Check if the user already provided an update since the thread was created
            thread_created = memory.conn.cursor()
            thread_created.execute(
                "SELECT created_at FROM open_threads WHERE id = ?", (tid,)
            )
            created_row = thread_created.fetchone()
            since_ts = created_row[0] if created_row else now_et().isoformat()

            subsequent = memory.get_messages_after(channel_id, since_ts, limit=30)
            user_subsequent = [
                content for uname, role, content in subsequent
                if role == "user" and uname.lower() == username.lower()
            ]

            # Ask LLM if those messages already resolved the thread
            if user_subsequent:
                resolution_check = [
                    {"role": "system", "content": (
                        "Did the user already provide an update or resolution about the topic below? "
                        "Answer ONLY 'yes' or 'no'.\n"
                        f"Topic: {topic}\nOriginal: {original_message}"
                    )},
                    {"role": "user", "content": "\n".join(user_subsequent[-5:])}
                ]
                resolved = (await llm.chat(resolution_check)).strip().lower()
                if resolved.startswith("yes"):
                    log.info(f"Thread '{topic}' already resolved by {username} — skipping follow-up")
                    continue

            # Build Loki's follow-up in character
            guild = bot.get_guild(int(guild_id)) if guild_id else None
            member_dir = ""
            if guild:
                members = [m for m in guild.members if not m.bot][:60]
                member_dir = "\n".join(f"  {m.display_name} → <@{m.id}>" for m in members)

            followup_prompt = [
                {"role": "system", "content": (
                    f"{SYSTEM_PROMPT}\n\n"
                    f"[Current time]: {now_et().strftime('%-I:%M %p ET, %A %B %-d %Y')}\n\n"
                    "You noticed an unresolved thread from earlier. You're checking in — not because "
                    "you're programmed to, but because you've been watching and you're genuinely curious "
                    "how it went. Keep it casual, Loki-style. Reference what they specifically said. "
                    "1-2 sentences max. Don't be robotic or say 'I noticed' — just ask naturally."
                )},
            ]
            if member_dir:
                followup_prompt.append({
                    "role": "system",
                    "content": f"[Member directory for @mentions]:\n{member_dir}"
                })
            followup_prompt.append({
                "role": "user",
                "content": (
                    f"Follow up with {username} (Discord ID: {user_id}) about: {topic}\n"
                    f"What they originally said: \"{original_message}\""
                )
            })

            reply = await llm.chat(followup_prompt)
            reply = re.sub(r"^<@!?\d+>\s*", "", reply.strip())
            await channel.send(f"<@{user_id}> {reply}")
            log.info(f"Follow-up sent to {username} re: '{topic}' in #{channel.name}")

        except Exception as e:
            log.error(f"fire_open_thread_followups error for thread {tid}: {e}")


@fire_open_thread_followups.before_loop
async def before_fire_followups():
    await bot.wait_until_ready()


# ─── Events ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    global _http_session
    _http_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=120),
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"}
    )
    log.info(f"✅  Loki is online as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} slash commands")
    except Exception as e:
        log.error(f"Slash command sync error: {e}")

    unprompted_interjection.start()
    log.info("Unprompted interjection task started")
    # update_self_knowledge disabled — trimming token usage
    log.info("Self-knowledge update task disabled (token savings)")
    check_reminders.start()
    log.info("Reminder check task started (runs every minute)")
    flush_db.start()
    log.info("DB flush task started (runs every 5 seconds)")
    detect_open_threads.start()
    log.info("Open thread detection started (runs every 45 minutes)")
    fire_open_thread_followups.start()
    log.info("Open thread follow-up task started (runs every 5 minutes)")


_POSITIVE_REACTION_EMOJIS = {"👍", "😂", "❤️", "🔥", "💯", "😭", "🤣", "👏", "⭐", "🎯"}
_NEGATIVE_REACTION_EMOJIS = {"👎", "😤", "🙄", "🤦", "😑"}

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Handle emoji reactions: flag tea moments and track sentiment on Loki's messages."""
    if payload.user_id == bot.user.id:
        return
    emoji_str = str(payload.emoji)

    is_tea = emoji_str in _TEA_REACTION_EMOJIS
    sentiment = None
    if emoji_str in _POSITIVE_REACTION_EMOJIS:
        sentiment = 1
    elif emoji_str in _NEGATIVE_REACTION_EMOJIS:
        sentiment = -1

    if not is_tea and sentiment is None:
        return  # Nothing to do — skip the API call

    # Fetch the message only once for both handlers
    try:
        channel = bot.get_channel(payload.channel_id)
        if not channel:
            return
        msg = await channel.fetch_message(payload.message_id)
    except Exception as e:
        log.warning(f"Reaction handler: could not fetch message: {e}")
        return

    # ── Tea detection ──────────────────────────────────────────────────────
    if is_tea:
        try:
            reactor = payload.member.display_name if payload.member else str(payload.user_id)
            trigger = f"[reacted {emoji_str}] {msg.author.display_name}: {msg.content[:300]}"
            recent = memory.get_recent_messages(channel.id, limit=10)
            context = [
                {"user": uname, "content": content, "ts": ts}
                for uname, role, content, img, ts in recent
            ]
            guild_id = payload.guild_id or 0
            memory.log_tea(guild_id, channel.id, reactor, trigger, context)
            log.info(f"Tea flagged via {emoji_str} reaction by {reactor} on message from {msg.author.display_name}")
        except Exception as e:
            log.warning(f"Tea reaction handler failed: {e}")

    # ── Sentiment feedback on Loki's own messages ──────────────────────────
    if sentiment is not None and msg.author.id == bot.user.id:
        try:
            memory.log_feedback(
                guild_id=payload.guild_id or 0,
                channel_id=payload.channel_id,
                user_id=payload.user_id,
                message_id=payload.message_id,
                message_snippet=msg.content[:200],
                emoji=emoji_str,
                sentiment=sentiment,
            )
            log.info(f"Feedback: {emoji_str} (sentiment={sentiment}) from user {payload.user_id} on Loki msg")
        except Exception as e:
            log.warning(f"Feedback reaction handler failed: {e}")


@bot.event
async def on_presence_update(before: discord.Member, after: discord.Member):
    """React to users starting games or streams — casually, rarely, in character."""
    # Ignore bots and ourselves
    if after.bot:
        return

    # Only care about newly started game/stream activities
    interesting_types = {discord.ActivityType.playing, discord.ActivityType.streaming}
    before_activities = {a.name for a in before.activities if a.type in interesting_types}
    new_activities = [
        a for a in after.activities
        if a.type in interesting_types and a.name not in before_activities
    ]
    if not new_activities:
        return

    activity = new_activities[0]
    uid = str(after.id)
    now = time.time()

    # Cooldown: same user + same game within 1 hour → skip
    last_activity, last_ts = presence_cooldowns.get(uid, ("", 0))
    if activity.name == last_activity and now - last_ts < PRESENCE_SAME_GAME_SECS:
        return
    # Cooldown: same user within 2 hours regardless of game → skip
    if now - last_ts < PRESENCE_COOLDOWN:
        return

    # Probability gate — don't comment every time
    if random.random() > PRESENCE_COMMENT_PROB:
        return

    # Find the channel where this user was most recently active (single query)
    target_channel = None
    best_ts = ""
    cursor = memory.conn.cursor()
    cursor.execute("""
        SELECT channel_id, timestamp FROM messages
        WHERE user_id = ? AND role = 'user'
        ORDER BY id DESC LIMIT 1
    """, (uid,))
    row = cursor.fetchone()
    if row:
        best_ts = row[1]
        ch = bot.get_channel(int(row[0]))
        if ch:
            perms = ch.permissions_for(ch.guild.me)
            if perms.send_messages and perms.read_messages:
                target_channel = ch

    if not target_channel:
        return

    # Only comment if their last message was recent (within 4 hours)
    try:
        last_active = datetime.datetime.fromisoformat(best_ts)
        if (now_et() - last_active.replace(tzinfo=ET)).total_seconds() > 14400:
            return
    except Exception:
        return

    try:
        activity_type = "streaming" if activity.type == discord.ActivityType.streaming else "playing"
        prompt = [
            {"role": "system", "content": (
                f"{SYSTEM_PROMPT}\n\n"
                f"[Current time]: {now_et().strftime('%-I:%M %p ET, %A %B %-d %Y')}\n\n"
                f"You just noticed that {after.display_name} started {activity_type} {activity.name}. "
                "Drop a casual, in-character one-liner in chat about it. "
                "Be natural — sarcastic, curious, or playful depending on the game. "
                "Don't announce that you noticed their status. Just react like you clocked it. "
                "Keep it to one sentence. Don't use their @mention unless it feels natural."
            )},
            {"role": "user", "content": f"{after.display_name} just started {activity_type}: {activity.name}"}
        ]
        reply = await llm.chat(prompt)
        await target_channel.send(reply)
        presence_cooldowns[uid] = (activity.name, now)
        log.info(f"Presence comment: {after.display_name} started {activity.name} → #{target_channel.name}")
    except Exception as e:
        log.error(f"on_presence_update LLM error: {e}")


@bot.event
async def on_message(message: discord.Message):
    # Never reply to ourselves
    if message.author.id == bot.user.id:
        return

    muted = False  # set below if shared state says so; checked after downloads

    # ── Check shared state (Miss-T authority) ─────────────────────────────
    if shared is not None:
        global _was_muted

        # Miss-T detection: only fire compliance when she is actually scolding Loki
        if MISST_BOT_USER_ID and message.author.id == MISST_BOT_USER_ID:
            msg_lower = message.content.lower()
            loki_named = bot.user.mentioned_in(message) or "loki" in msg_lower
            _SCOLD_WORDS = (
                "stop", "enough", "chill", "cool it", "calm down", "be quiet",
                "shut up", "back off", "knock it off", "cut it out", "behave",
                "watch it", "i'm warning", "last warning", "no more",
                "that's enough", "sit down", "stand down",
            )
            is_scold = loki_named and any(w in msg_lower for w in _SCOLD_WORDS)
            if is_scold and not shared.is_loki_muted():
                scold_prompt = [
                    {"role": "system", "content": (
                        SYSTEM_PROMPT + "\n\n"
                        "Miss-T just called you out directly. Respond in your full Loki character — "
                        "acknowledge the scolding with your signature mix of wounded pride, "
                        "dramatic flair, and reluctant compliance. Never use the same phrasing twice. "
                        "Keep it under 2 sentences. No canned lines."
                    )},
                    {"role": "user", "content": f"[Miss-T just said]: {message.content}"}
                ]
                try:
                    scold_reply = await llm.chat(scold_prompt)
                    await message.channel.send(scold_reply)
                except Exception as _se:
                    log.error(f"Scold LLM error: {_se}")
                return
            # Miss-T not scolding Loki — stay quiet if she didn't address him
            if not loki_named:
                return
            # She mentioned Loki without scolding — fall through to normal LLM response

        # Check mute status
        muted = shared.is_loki_muted()
        if muted:
            _was_muted = True
            if message.guild:
                memory.store_message(
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    user_id=message.author.id,
                    username=message.author.display_name,
                    role="user",
                    content=message.content,
                    has_image=False,
                    image_desc=""
                )
            # Don't return yet — still allow downloads to run below
        elif _was_muted:
            _was_muted = False
            return_lines = [
                "...I'm back. Did you miss me? Don't answer that.",
                "Mute expired. I have returned. You're welcome.",
                "*stretches* Back. As if I was ever really gone.",
            ]
            await message.channel.send(random.choice(return_lines))

        # Check cooldown modifier — will be injected in build_llm_messages
        on_cooldown, modifier = shared.is_loki_on_cooldown()

    # ── Process images / GIFs attached to ANY message for context ──────────
    image_desc = ""
    if message.attachments:
        for att in message.attachments:
            if any(att.filename.lower().endswith(ext)
                   for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
                try:
                    img_bytes = await att.read()
                    mime = att.content_type or "image/png"
                    desc = await vision.describe_image(img_bytes, mime)
                    image_desc += desc + " "
                    log.info(f"Image described: {desc[:80]}...")
                except Exception as e:
                    log.error(f"Error reading attachment: {e}")

    # Also check for embedded images (e.g., tenor GIFs)
    if message.embeds:
        for embed in message.embeds:
            img_url = None
            if embed.image and embed.image.url:
                img_url = embed.image.url
            elif embed.thumbnail and embed.thumbnail.url:
                img_url = embed.thumbnail.url

            if img_url:
                try:
                    session = await get_http_session()
                    async with session.get(img_url) as resp:
                        if resp.status == 200:
                            img_bytes = await resp.read()
                            ct = resp.headers.get("Content-Type", "image/png")
                            desc = await vision.describe_image(img_bytes, ct)
                            image_desc += desc + " "
                except Exception as e:
                    log.error(f"Error fetching embed image: {e}")

    image_desc = image_desc.strip()

    # ── Transcribe voice messages ──────────────────────────────────────────
    voice_transcript = ""
    if message.flags.voice:
        for att in message.attachments:
            if att.content_type and "audio" in att.content_type:
                try:
                    audio_bytes = await att.read()
                    voice_transcript = await transcribe_voice_message(audio_bytes, att.filename)
                except Exception as e:
                    log.error(f"Error reading voice attachment: {e}")
                break

    # ── Update mood tracker and interjection state ─────────────────────────
    mood_tracker.record(message.channel.id, message.content)
    cid = str(message.channel.id)
    if cid not in interjection_state:
        interjection_state[cid] = {"last_interjection": 0, "msgs_since_last": 0}
    interjection_state[cid]["msgs_since_last"] += 1

    # ── Downloads channel — any URL posted here triggers auto-download ────
    if message.channel.id == DOWNLOAD_CHANNEL_ID:
        urls = URL_RE.findall(message.content)
        if urls and not _is_gif_url(urls[0]):
            asyncio.create_task(
                run_download(urls[0], message.author.display_name, trigger_message=message)
            )
        return  # never respond with LLM in the downloads channel

    # ── Natural language download trigger ("post this <url>") ─────────────
    if POST_THIS_RE.search(message.content):
        urls = URL_RE.findall(message.content)
        if urls and not _is_gif_url(urls[0]):
            asyncio.create_task(
                run_download(urls[0], message.author.display_name, trigger_message=message)
            )
            return

    # ── Auto-download TikTok/Instagram links from specific user ───────────
    if message.author.id == AUTO_DOWNLOAD_USER_ID:
        tt_ig_urls = TIKTOK_INSTAGRAM_RE.findall(message.content)
        if tt_ig_urls:
            # findall returns the capture group (domain), re-extract full URLs
            full_urls = URL_RE.findall(message.content)
            social_urls = [u for u in full_urls if TIKTOK_INSTAGRAM_RE.match(u) and not _is_gif_url(u)]
            if social_urls:
                asyncio.create_task(
                    run_download(social_urls[0], message.author.display_name, trigger_message=message)
                )
                return

    # ── Bail out if muted (downloads above already ran) ─────────────────
    if muted:
        return

    # ── Check if bot should respond ───────────────────────────────────────
    should_respond = False

    # 1. Direct mention / reply
    if bot.user.mentioned_in(message):
        should_respond = True

    # 2. Name trigger
    if is_trigger(message.content):
        should_respond = True

    # 3. Voice message contains trigger word
    if voice_transcript and is_trigger(voice_transcript):
        should_respond = True

    # 4. Reply to one of the bot's messages
    if (message.reference and message.reference.resolved
            and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author.id == bot.user.id):
        should_respond = True

    if not should_respond:
        # Still store for context even though we're not responding
        store_content = message.content
        if voice_transcript:
            store_content += f" [voice message: {voice_transcript}]"
        memory.store_message(
            guild_id=message.guild.id if message.guild else 0,
            channel_id=message.channel.id,
            user_id=message.author.id,
            username=message.author.display_name,
            role="user",
            content=store_content,
            has_image=bool(image_desc),
            image_desc=image_desc
        )
        # Silently flag tea moments even when not responding
        if _TEA_FLAG_RE.search(message.content):
            asyncio.create_task(_log_tea(message))
        # Detect corrections directed at Loki
        if message.guild and _CORRECTION_RE.search(message.content):
            recent_ctx = memory.get_recent_messages(message.channel.id, limit=5)
            loki_recent = [content for uid, uname, role, content, img, ts in recent_ctx if role == "assistant"]
            if loki_recent:
                memory.log_correction(
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    loki_message=loki_recent[-1],
                    correction_text=message.content,
                    corrector=message.author.display_name,
                )
                log.info(f"Correction logged from {message.author.display_name}: {message.content[:60]}")
        # Maybe silently react — runs in background, never blocks
        if message.content.strip():
            asyncio.create_task(maybe_react(message))
        await bot.process_commands(message)
        return

    # ── Check for serious mode prefix (-s) ───────────────────────────────
    serious = False
    content_text = message.content
    if content_text.lstrip().startswith("-s ") or content_text.lstrip() == "-s":
        serious = True
        content_text = content_text.lstrip().removeprefix("-s").strip()

    # ── Silently flag tea moments (always, before responding) ────────────
    if _TEA_FLAG_RE.search(content_text):
        asyncio.create_task(_log_tea(message))

    # ── "Pull the tea" handler ────────────────────────────────────────────
    if _TEA_PULL_RE.search(content_text) and not serious:
        async with message.channel.typing():
            reply = await handle_pull_tea(message)
        if reply:
            await message.reply(reply, mention_author=False)
            memory.store_message(
                guild_id=message.guild.id if message.guild else 0,
                channel_id=message.channel.id,
                user_id=message.author.id,
                username=message.author.display_name,
                role="user", content=content_text,
                has_image=False, image_desc=""
            )
            memory.store_message(
                guild_id=message.guild.id if message.guild else 0,
                channel_id=message.channel.id,
                user_id=str(bot.user.id),
                username="Loki",
                role="assistant", content=reply,
                has_image=False, image_desc=""
            )
            return

    # ── "What did I miss?" natural language handler ───────────────────────
    if is_missed_query(content_text) and not serious:
        async with message.channel.typing():
            reply = await handle_missed_query(message)
        if reply:
            if len(reply) <= 2000:
                await message.reply(reply, mention_author=False)
            else:
                chunks = [reply[i:i+2000] for i in range(0, len(reply), 2000)]
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await message.reply(chunk, mention_author=False)
                    else:
                        await message.channel.send(chunk)
            memory.store_message(
                guild_id=message.guild.id if message.guild else 0,
                channel_id=message.channel.id,
                user_id=bot.user.id,
                username="Loki",
                role="assistant",
                content=reply
            )
            await bot.process_commands(message)
            return
        # handle_missed_query returned None — fall through to normal response

    # ── Natural language reminder detection ──────────────────────────────
    reminder_confirmation = None
    if not serious and REMINDER_INTENT_RE.search(content_text):
        async with message.channel.typing():
            extracted = await extract_reminder(content_text)
        if extracted:
            fire_at = parse_reminder_time(extracted["when"])
            if fire_at:
                guild_id_val = message.guild.id if message.guild else 0
                memory.add_reminder(
                    guild_id=guild_id_val,
                    channel_id=message.channel.id,
                    user_id=message.author.id,
                    username=message.author.display_name,
                    fire_at=fire_at.isoformat(),
                    context=extracted["note"]
                )
                reminder_confirmation = fire_at
                log.info(
                    f"Auto-reminder set for {message.author.display_name}: "
                    f"'{extracted['note']}' at {fire_at.isoformat()}"
                )

    # ── RAG: search ChromaDB history for past context ────────────────────
    rag_context = ""
    rag_no_results = False
    if _rag_available and not serious and is_rag_query(content_text):
        try:
            guild_name_for_rag = message.guild.name if message.guild else None
            since_dt, until_dt = parse_query_time_window(content_text)
            named_person, rag_query = _extract_named_subject(content_text)
            hits = search_history(
                rag_query,
                n_results=20,
                guild_name=guild_name_for_rag,
                since_dt=since_dt,
                until_dt=until_dt,
            )
            if hits:
                rag_context = format_for_context(hits)
                log.info(f"RAG: {len(hits)} results retrieved")
            else:
                rag_no_results = True
                log.info("RAG: query fired but no results above threshold")
        except Exception as _rag_err:
            log.error(f"RAG search failed: {_rag_err}")

    # ── Tavily: web search for current/live info ─────────────────────────
    search_context = ""
    _tavily_query = None
    if _tavily and not serious:
        if is_search_query(content_text):
            _tavily_query = content_text[:380]
        else:
            _topic = _extract_topic_query(content_text)
            if _topic and random.random() < 0.65:
                _tavily_query = _topic
    if _tavily_query:
        try:
            result = await asyncio.to_thread(_tavily.search, _tavily_query, search_depth="basic", max_results=5)
            hits = result.get("results", [])
            snippets = []
            for r in hits[:5]:
                url = r.get("url", "")
                body = r.get("content", "")[:250]
                snippets.append(f"  - {body} {url}".strip())
            if snippets:
                search_context = "[Live web search results:]\n" + "\n".join(snippets)
                log.info(f"Tavily search triggered for: {_tavily_query[:60]}")
        except Exception as _tv_err:
            log.error(f"Tavily search error: {_tv_err}")

    # ── Detect direct reply context ───────────────────────────────────────
    reply_user_id = None
    reply_context = ""
    if (message.reference and message.reference.resolved
            and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author.id == bot.user.id):
        reply_user_id = message.author.id
        ref_content = (message.reference.resolved.content or "")[:300]
        reply_context = (
            f"[{message.author.display_name} is replying directly to your message: \"{ref_content}\"]\n"
            f"[Focus your response on {message.author.display_name} only. "
            f"Do not reference other people unless {message.author.display_name} mentions them "
            f"or there is direct relevance between them and this conversation.]\n"
        )

    # ── Build prompt & get reply ──────────────────────────────────────────
    async with message.channel.typing():
        user_text = f"[Message from {message.author.display_name} (ID:{message.author.id})]: {content_text}"
        if message.mentions:
            mentioned_names = ", ".join(m.display_name for m in message.mentions if m.id != bot.user.id)
            if mentioned_names:
                user_text += f"\n[NOTE: {message.author.display_name} is the sender. {mentioned_names} are only mentioned in the message, not the sender.]"
        if image_desc:
            user_text += f"\n[They posted an image: {image_desc}]"
        if voice_transcript:
            user_text += f"\n[They sent a voice message. Transcript: {voice_transcript}]"
        if reply_context:
            user_text = reply_context + user_text
        if rag_context:
            user_text += (
                f"\n\n{rag_context}"
                "\n[INSTRUCTIONS FOR USING THE ABOVE HISTORY: "
                "Present relevant results naturally in character. "
                "If multiple results match, cite them clearly. "
                "If asked about a specific person, only cite their messages.]"
            )
        elif rag_no_results:
            user_text += (
                "\n\n[NOTE: You searched your memory for this but found nothing relevant. "
                "Be honest — say you don't have that in your memory rather than guessing.]"
            )
        if search_context:
            user_text += (
                f"\n\n{search_context}"
                "\n[Use these search results to answer naturally. Don't say 'according to search results' — "
                "just answer like you know it, sharp and in character.]"
            )

        messages_for_llm = build_llm_messages(
            message.channel.id,
            guild_id=message.guild.id if message.guild else None,
            extra_user_msg=user_text,
            serious=serious,
            guild=message.guild,
            target_user_id=message.author.id,
            reply_user_id=reply_user_id
        )

        if reminder_confirmation:
            time_str = reminder_confirmation.strftime("%-I:%M %p ET on %A, %b %-d")
            messages_for_llm.insert(1, {
                "role": "system",
                "content": (
                    f"[REMINDER STORED]: You successfully scheduled a reminder for {time_str}. "
                    "Confirm this naturally in your response — don't be robotic, just let them know it's set."
                )
            })

        # ── Intent + emotion detection (single cheap model call) ──
        intent, emotion = await llm.classify_message(content_text)
        log.info(f"Intent={intent} Emotion={emotion} user={message.author.display_name}")

        # Inject emotion context into prompt if non-neutral
        if emotion not in {"neutral", "joking"}:
            messages_for_llm.insert(1, {
                "role": "system",
                "content": f"[Detected emotional tone: {emotion}. Respond accordingly — be aware of how they're feeling.]"
            })

        reply = await llm.chat_routed(messages_for_llm, intent, emotion)

        # ── Cognitive delay — simulate reading + typing speed ─────────────
        # ~0.02s per char of the reply, capped at 6s, with slight jitter
        delay = min(len(reply) * 0.02, 6.0) * random.uniform(0.85, 1.15)
        if delay > 0.5:
            await asyncio.sleep(delay)

    # ── Store user message then bot reply in memory ───────────────────────
    memory.store_message(
        guild_id=message.guild.id if message.guild else 0,
        channel_id=message.channel.id,
        user_id=message.author.id,
        username=message.author.display_name,
        role="user",
        content=message.content,
        has_image=bool(image_desc),
        image_desc=image_desc
    )
    memory.store_message(
        guild_id=message.guild.id if message.guild else 0,
        channel_id=message.channel.id,
        user_id=bot.user.id,
        username="Loki",
        role="assistant",
        content=reply,
    )

    # ── Send reply (voice message if triggered by voice, text otherwise) ──
    if voice_transcript and is_trigger(voice_transcript):
        # Respond with a voice message since the user sent one
        sent_voice = await send_voice_message(message.channel, reply, reference=message)
        if not sent_voice:
            # Fallback to text if voice message fails
            log.warning("Voice message failed, falling back to text reply")
            if len(reply) <= 2000:
                await message.reply(reply, mention_author=False)
            else:
                chunks = [reply[i:i+2000] for i in range(0, len(reply), 2000)]
                for i, chunk in enumerate(chunks):
                    if i == 0:
                        await message.reply(chunk, mention_author=False)
                    else:
                        await message.channel.send(chunk)
    elif len(reply) <= 2000:
        await message.reply(reply, mention_author=False)
    else:
        chunks = [reply[i:i+2000] for i in range(0, len(reply), 2000)]
        for i, chunk in enumerate(chunks):
            if i == 0:
                await message.reply(chunk, mention_author=False)
            else:
                await message.channel.send(chunk)

    # ── Maybe drop a GIF — runs in background, never blocks ──────────────
    if not serious:
        asyncio.create_task(maybe_send_gif(message.channel, content_text))

    # ── Occasionally drop a witty quip (max 2 per 72 hours) ──────────────
    # NOTE: _maybe_get_quip/_quip_budget_remaining were removed — quip system disabled
    # if not serious:
    #     quip = _maybe_get_quip()
    #     if quip:
    #         await asyncio.sleep(random.uniform(1.5, 4.0))
    #         await message.channel.send(quip)
    #         log.info(f"Dropped quip — budget remaining: {_quip_budget_remaining()}")

    # ── Update relationship memory ─────────────────────────────────────────
    if message.guild:
        count = memory.update_user_profile(
            message.author.id,
            message.guild.id,
            message.author.display_name
        )
        # Every 10th interaction, refresh Loki's personality notes in background
        if count % 10 == 0:
            asyncio.create_task(update_relationship_memory(
                str(message.author.id),
                str(message.guild.id),
                message.author.display_name,
                str(message.channel.id)
            ))

    await bot.process_commands(message)


# ─── Slash Commands ───────────────────────────────────────────────────────────

@bot.tree.command(name="summarize", description="Summarize the last ~20 messages in this channel")
async def summarize(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    recent = memory.get_recent_messages(interaction.channel.id, 20)
    if not recent:
        await interaction.followup.send("No messages to summarize yet!")
        return

    convo_text = "\n".join(
        f"[{ts}] {username} (ID:{user_id}): {content}"
        + (f" [Image: {img}]" if img else "")
        for user_id, username, role, content, img, ts in recent
    )

    summary_prompt = [
        {"role": "system", "content": (
            "You are Loki. Summarize the following conversation concisely, "
            "capturing key topics, decisions, jokes, and the overall vibe. "
            "Keep your mischievous personality."
        )},
        {"role": "user", "content": f"Summarize this conversation:\n\n{convo_text}"}
    ]

    summary = await llm.chat(summary_prompt)

    memory.store_summary(
        guild_id=interaction.guild.id if interaction.guild else 0,
        channel_id=interaction.channel.id,
        summary=summary
    )

    embed = discord.Embed(
        title="📜 Conversation Summary by Loki",
        description=summary,
        color=discord.Color.green()
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="loki_join", description="Loki joins your voice channel")
async def loki_join(interaction: discord.Interaction):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message(
            "You need to be in a voice channel first!", ephemeral=True
        )
        return

    await interaction.response.defer()
    try:
        vc = await voice_h.join(interaction.user.voice.channel)
        await interaction.followup.send(
            f"Joined **{interaction.user.voice.channel.name}**. "
            f"I'm here, mortals. 🐍"
        )
    except Exception as e:
        log.error(f"Failed to join voice channel: {e}")
        await interaction.followup.send(
            f"Failed to join the voice channel: {e}", ephemeral=True
        )


@bot.tree.command(name="loki_leave", description="Loki leaves the voice channel")
async def loki_leave(interaction: discord.Interaction):
    guild_id = interaction.guild.id
    if guild_id in voice_h.voice_clients or interaction.guild.voice_client:
        await voice_h.leave(guild_id, guild=interaction.guild)
        await interaction.response.send_message("Fine. I'll leave. For now. 🐍")
    else:
        await interaction.response.send_message("I'm not in a voice channel!", ephemeral=True)


@bot.tree.command(name="loki_say", description="Make Loki speak in voice chat")
@app_commands.describe(text="What Loki should say")
async def loki_say(interaction: discord.Interaction, text: str):
    guild_id = interaction.guild.id
    if guild_id not in voice_h.voice_clients:
        await interaction.response.send_message(
            "I'm not in a voice channel. Use `/loki_join` first!", ephemeral=True
        )
        return

    await interaction.response.defer()
    vc = voice_h.voice_clients[guild_id]
    await voice_h.speak(vc, text)
    await interaction.followup.send(f"🗣️ *Loki said:* {text}")


@bot.tree.command(name="loki_speak", description="Ask Loki a question and hear the answer in voice")
@app_commands.describe(question="Your question for Loki")
async def loki_speak(interaction: discord.Interaction, question: str):
    guild_id = interaction.guild.id
    if guild_id not in voice_h.voice_clients:
        await interaction.response.send_message(
            "I'm not in a voice channel. Use `/loki_join` first!", ephemeral=True
        )
        return

    await interaction.response.defer()

    messages_for_llm = build_llm_messages(
        interaction.channel.id,
        guild_id=interaction.guild.id if interaction.guild else None,
        extra_user_msg=f"[{interaction.user.display_name} (ID:{interaction.user.id})]: {question}",
        guild=interaction.guild,
        target_user_id=interaction.user.id
    )
    reply = await llm.chat(messages_for_llm)

    memory.store_message(
        guild_id=guild_id,
        channel_id=interaction.channel.id,
        user_id=interaction.user.id,
        username=interaction.user.display_name,
        role="user",
        content=question
    )
    memory.store_message(
        guild_id=guild_id,
        channel_id=interaction.channel.id,
        user_id=bot.user.id,
        username="Loki",
        role="assistant",
        content=reply
    )

    vc = voice_h.voice_clients[guild_id]
    await voice_h.speak(vc, reply)
    await interaction.followup.send(f"**Q:** {question}\n**Loki:** {reply}")


@bot.tree.command(name="loki_reset", description="Clear Loki's memory for this channel (Admin only)")
@app_commands.checks.has_permissions(administrator=True)
async def loki_reset(interaction: discord.Interaction):
    cursor = memory.conn.cursor()
    cursor.execute(
        "DELETE FROM messages WHERE channel_id = ?",
        (str(interaction.channel.id),)
    )
    cursor.execute(
        "DELETE FROM summaries WHERE channel_id = ?",
        (str(interaction.channel.id),)
    )
    memory.conn.commit()
    await interaction.response.send_message("🧹 Memory wiped for this channel. Fresh start!")


# ─── Per-Server Personality Commands ──────────────────────────────────────────

@bot.tree.command(
    name="set_personality",
    description="Set a custom personality prompt for this server (Admin only)"
)
@app_commands.describe(prompt="The full personality/system prompt for the bot in this server")
@app_commands.checks.has_permissions(administrator=True)
async def set_personality(interaction: discord.Interaction, prompt: str):
    if not interaction.guild:
        await interaction.response.send_message("This only works in a server!", ephemeral=True)
        return

    memory.set_server_personality(
        guild_id=interaction.guild.id,
        prompt=prompt,
        set_by=interaction.user.display_name
    )

    preview = prompt[:200] + "..." if len(prompt) > 200 else prompt
    embed = discord.Embed(
        title="🎭 Server Personality Updated",
        description=f"**Set by:** {interaction.user.display_name}\n\n**Prompt preview:**\n{preview}",
        color=discord.Color.purple()
    )
    embed.set_footer(text="This personality is unique to this server. Use /reset_personality to go back to default.")
    await interaction.response.send_message(embed=embed)
    log.info(f"Server personality set for guild {interaction.guild.id} by {interaction.user.display_name}")


@bot.tree.command(
    name="view_personality",
    description="View the current personality prompt for this server (Admin only)"
)
@app_commands.checks.has_permissions(administrator=True)
async def view_personality(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("This only works in a server!", ephemeral=True)
        return

    server_data = memory.get_server_personality(interaction.guild.id)

    if server_data:
        prompt, set_by, timestamp = server_data
        display_prompt = prompt[:3900] + "..." if len(prompt) > 3900 else prompt
        embed = discord.Embed(
            title="🎭 Current Server Personality",
            description=display_prompt,
            color=discord.Color.purple()
        )
        embed.add_field(name="Set by", value=set_by, inline=True)
        embed.add_field(name="Date", value=timestamp[:10], inline=True)
        embed.set_footer(text="This is a custom personality for this server.")
    else:
        display_prompt = SYSTEM_PROMPT[:3900] + "..." if len(SYSTEM_PROMPT) > 3900 else SYSTEM_PROMPT
        embed = discord.Embed(
            title="🎭 Current Personality (Default)",
            description=display_prompt,
            color=discord.Color.greyple()
        )
        embed.set_footer(text="No custom personality set. Using the default from .env")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="reset_personality",
    description="Reset this server's personality back to the default (Admin only)"
)
@app_commands.checks.has_permissions(administrator=True)
async def reset_personality(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("This only works in a server!", ephemeral=True)
        return

    server_data = memory.get_server_personality(interaction.guild.id)
    if not server_data:
        await interaction.response.send_message(
            "This server is already using the default personality.", ephemeral=True
        )
        return

    memory.delete_server_personality(interaction.guild.id)
    await interaction.response.send_message("🎭 Server personality reset to default.")
    log.info(f"Server personality reset for guild {interaction.guild.id} by {interaction.user.display_name}")


# ─── Claude Code Commands ─────────────────────────────────────────────────────

@bot.tree.command(name="cc", description="Send a prompt to Claude Code (runs on the host machine)")
@app_commands.describe(prompt="What you want Claude Code to do")
async def cc(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer(thinking=True)

    channel_id  = str(interaction.channel.id)
    session_row = memory.get_claude_session(channel_id)
    session_id  = session_row[0] if session_row else None

    log.info(
        f"/cc from {interaction.user.display_name} "
        f"(session={session_id or 'new'}): {prompt[:80]}..."
    )

    result, new_session_id = await claude_cc.run(prompt, session_id)

    if new_session_id and new_session_id != session_id:
        memory.set_claude_session(channel_id, new_session_id)

    header = (
        f"**Claude Code** · `{prompt[:60]}{'…' if len(prompt) > 60 else ''}`\n\n"
    )
    full = header + (result or "*(no output)*")

    if len(full) <= 2000:
        await interaction.followup.send(full)
    else:
        chunks = [result[i:i+1900] for i in range(0, len(result), 1900)]
        await interaction.followup.send(header + chunks[0])
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk)


@bot.tree.command(name="cc_new", description="Start a fresh Claude Code session for this channel")
async def cc_new(interaction: discord.Interaction):
    memory.clear_claude_session(str(interaction.channel.id))
    await interaction.response.send_message(
        "🔄 Claude Code session cleared — next `/cc` starts fresh."
    )


@bot.tree.command(name="cc_session", description="Show the active Claude Code session for this channel")
async def cc_session(interaction: discord.Interaction):
    row = memory.get_claude_session(str(interaction.channel.id))
    if row:
        sid, created, last = row
        await interaction.response.send_message(
            f"📎 **Active session:** `{sid}`\n"
            f"Created: {created[:19]} ET\n"
            f"Last used: {last[:19]} ET\n\n"
            f"Use `/cc_new` to start a fresh session.",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            "No active session. Use `/cc <prompt>` to start one.",
            ephemeral=True,
        )


# ─── Reminder Commands ────────────────────────────────────────────────────────

@bot.tree.command(name="remind", description="Set a smart reminder — Loki will follow up with context")
@app_commands.describe(
    when='When to remind you — e.g. "in 2 hours", "at 5pm", "tomorrow at 9am"',
    note="What this reminder is about — Loki will use this to follow up naturally"
)
async def remind(interaction: discord.Interaction, when: str, note: str):
    fire_at = parse_reminder_time(when)
    if fire_at is None:
        await interaction.response.send_message(
            "I couldn't parse that time. Try things like:\n"
            '`"in 2 hours"` · `"at 5pm"` · `"at 5:30pm"` · `"tomorrow at 9am"` · `"in 30 minutes"`',
            ephemeral=True
        )
        return

    guild_id = interaction.guild.id if interaction.guild else 0
    memory.add_reminder(
        guild_id=guild_id,
        channel_id=interaction.channel.id,
        user_id=interaction.user.id,
        username=interaction.user.display_name,
        fire_at=fire_at.isoformat(),
        context=note
    )

    # Format a human-readable confirmation
    time_str = fire_at.strftime("%-I:%M %p ET on %A, %b %-d")
    await interaction.response.send_message(
        f"Got it. I'll remind you at **{time_str}**.\n> {note}",
        ephemeral=True
    )
    log.info(f"Reminder set by {interaction.user.display_name}: '{note}' at {fire_at.isoformat()}")


@bot.tree.command(name="reminders", description="See your pending reminders")
async def reminders_list(interaction: discord.Interaction):
    guild_id = interaction.guild.id if interaction.guild else 0
    rows = memory.get_user_reminders(interaction.user.id, guild_id)
    if not rows:
        await interaction.response.send_message("You have no pending reminders.", ephemeral=True)
        return

    lines = []
    for rid, fire_at, context in rows:
        try:
            dt = datetime.datetime.fromisoformat(fire_at)
            time_str = dt.strftime("%-I:%M %p ET, %b %-d")
        except Exception:
            time_str = fire_at[:16]
        lines.append(f"**{time_str}** — {context}")

    await interaction.response.send_message(
        "**Your reminders:**\n" + "\n".join(lines),
        ephemeral=True
    )


# ─── Download Command ─────────────────────────────────────────────────────────

@bot.tree.command(name="download", description="Download a video and post it to #downloads (yt-dlp → Cobalt → gallery-dl fallback)")
@app_commands.describe(url="The video URL to download")
async def download(interaction: discord.Interaction, url: str):
    await interaction.response.defer(ephemeral=True)
    requester = interaction.user.display_name
    # run_download handles the full fallback chain and posts to dest_channel
    await run_download(url, requester)
    await interaction.followup.send("⏬ Download queued.", ephemeral=True)


@set_personality.error
@view_personality.error
@reset_personality.error
@loki_reset.error
async def admin_permission_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "⛔ Only server admins can use this command.", ephemeral=True
        )


# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN not set in .env file!")
        exit(1)
    bot.run(DISCORD_TOKEN)

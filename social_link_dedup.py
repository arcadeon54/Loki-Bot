"""Cross-channel duplicate detection for supported social-media links.

Canonicalizes platform links (YouTube, TikTok, Instagram, Twitter/X,
Facebook, Reddit) down to a content identifier, tracks first-seen state in
SQLite, and reports duplicates. Pure logic + sqlite only (no discord.py
dependency) so it can be unit tested without a live bot.
"""
from __future__ import annotations

import datetime
import hashlib
import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

URL_RE = re.compile(r'https?://\S+')

_WRAP_LEADING = '<'
_WRAP_TRAILING = '>)]}.,!?\'"*_~`|'

# Query params that identify the same content but vary per share/click.
TRACKING_PARAMS = {
    'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content',
    'utm_id', 'utm_name', 'utm_reader', 'utm_social', 'utm_cid',
    'fbclid', 'gclid', 'igshid', 'igsh', 'share_app_id', 'is_from_webapp',
    'sender_device', 'sender_web_id', 'si', '_r', '_d', 'spm_id_from',
    'refer', 'ref', 'ref_src', 'ref_url', 'source', 'traffic_source',
}

PLATFORM_HOSTS = {
    'instagram': ('instagram.com',),
    'tiktok': ('tiktok.com',),
    'youtube': ('youtube.com', 'youtu.be'),
    'twitter': ('twitter.com', 'x.com'),
    'facebook': ('facebook.com', 'fb.watch'),
    'reddit': ('reddit.com', 'redd.it'),
}

_HOST_STRIP_PREFIXES = ('www.', 'm.', 'mobile.', 'vm.', 'vt.')


def strip_wrapping(raw: str) -> str:
    """Strip Discord formatting characters that can surround a bare URL
    (e.g. <url> to suppress embeds, or trailing punctuation/markdown)."""
    url = raw.strip()
    if url.startswith(_WRAP_LEADING):
        url = url[1:]
    url = url.rstrip(_WRAP_TRAILING)
    return url


def _bare_host(url: str) -> str:
    host = (urlparse(url).hostname or '').lower()
    changed = True
    while changed:
        changed = False
        for prefix in _HOST_STRIP_PREFIXES:
            if host.startswith(prefix):
                host = host[len(prefix):]
                changed = True
    return host


def detect_platform(url: str) -> str | None:
    host = _bare_host(url)
    for platform, hosts in PLATFORM_HOSTS.items():
        if any(host == h or host.endswith('.' + h) for h in hosts):
            return platform
    return None


def extract_supported_links(text: str) -> list[tuple[str, str]]:
    """Return [(url, platform), ...] for supported-platform links found in text.
    Preserves the raw (wrapped) substrings actually present so callers can
    strip them back out of the original text."""
    out = []
    for raw in URL_RE.findall(text or ''):
        url = strip_wrapping(raw)
        platform = detect_platform(url)
        if platform:
            out.append((raw, platform))
    return out


def _extract_content_id(platform: str, url: str) -> str | None:
    parsed = urlparse(url)
    path = parsed.path
    host = _bare_host(url)
    qs = parse_qs(parsed.query)

    if platform == 'youtube':
        if host == 'youtu.be' or host.endswith('.youtu.be'):
            seg = path.strip('/').split('/')
            return seg[0] if seg and seg[0] else None
        if qs.get('v'):
            return qs['v'][0]
        m = re.search(r'/(?:shorts|embed|live)/([A-Za-z0-9_-]{6,})', path)
        return m.group(1) if m else None

    if platform == 'tiktok':
        m = re.search(r'/video/(\d+)', path)
        return m.group(1) if m else None

    if platform == 'instagram':
        m = re.search(r'/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', path)
        return m.group(1) if m else None

    if platform == 'twitter':
        m = re.search(r'/status(?:es)?/(\d+)', path)
        return m.group(1) if m else None

    if platform == 'facebook':
        if qs.get('v'):
            return qs['v'][0]
        m = re.search(r'/videos/(\d+)', path)
        if m:
            return m.group(1)
        if host == 'fb.watch':
            seg = path.strip('/').split('/')
            return seg[0] if seg and seg[0] else None
        return None

    if platform == 'reddit':
        m = re.search(r'/comments/([a-z0-9]+)', path)
        if m:
            return m.group(1)
        if host == 'redd.it':
            seg = path.strip('/').split('/')
            return seg[0] if seg and seg[0] else None
        return None

    return None


def normalize_url(url: str) -> str:
    """Fallback normalization for links we can't reduce to a content ID:
    lowercase host (minus www.), https, no fragment, no trailing slash,
    tracking params stripped, remaining params sorted."""
    parsed = urlparse(url)
    host = _bare_host(url)
    path = parsed.path.rstrip('/') or '/'
    params = parse_qs(parsed.query, keep_blank_values=False)
    filtered = sorted(
        (k, v) for k, values in params.items()
        if k.lower() not in TRACKING_PARAMS
        for v in values
    )
    query = '&'.join(f'{k}={v}' for k, v in filtered)
    return f'https://{host}{path}' + (f'?{query}' if query else '')


def canonicalize(url: str, platform: str) -> str:
    """Canonical content identifier for a supported-platform URL.
    Prefers a platform content ID; falls back to a normalized URL so distinct
    posts on the same platform never collapse into one identifier."""
    content_id = _extract_content_id(platform, url)
    if content_id:
        return f'{platform}:{content_id}'
    return f'{platform}:url:{normalize_url(url)}'


def canonical_hash(url: str, platform: str) -> str:
    return hashlib.sha256(canonicalize(url, platform).encode('utf-8')).hexdigest()


def is_primarily_links(content: str, raw_urls: list[str], max_caption_chars: int) -> bool:
    """True if, once all extracted URLs are removed, what remains is short
    enough to be 'a caption' rather than substantial unrelated conversation."""
    remainder = content or ''
    for u in raw_urls:
        remainder = remainder.replace(u, '')
    remainder = re.sub(r'\s+', ' ', remainder).strip(' <>')
    return len(remainder) <= max_caption_chars


# ─── Persistence ───────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS social_link_dupes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id TEXT NOT NULL,
    link_hash TEXT NOT NULL,
    platform TEXT NOT NULL,
    first_message_id TEXT NOT NULL,
    first_channel_id TEXT NOT NULL,
    first_user_id TEXT NOT NULL,
    first_seen_ts TEXT NOT NULL,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    last_duplicate_ts TEXT,
    last_duplicate_user_id TEXT,
    UNIQUE(guild_id, link_hash)
);
CREATE TABLE IF NOT EXISTS social_link_user_warnings (
    guild_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    level INTEGER NOT NULL DEFAULT 0,
    last_warned_ts TEXT,
    PRIMARY KEY (guild_id, user_id)
);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@dataclass
class ClaimResult:
    is_new: bool
    duplicate_count: int


def claim_or_record_duplicate(
    conn: sqlite3.Connection, *, guild_id: str, link_hash: str, platform: str,
    message_id: str, channel_id: str, user_id: str, now_iso: str,
) -> ClaimResult:
    """Atomically claim a canonical link as the original, or record a
    duplicate hit against the existing original. No awaits happen between
    the INSERT and the rowcount check, so within this process concurrent
    on_message handlers can't both win the race."""
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO social_link_dupes "
        "(guild_id, link_hash, platform, first_message_id, first_channel_id, "
        "first_user_id, first_seen_ts, duplicate_count) VALUES (?, ?, ?, ?, ?, ?, ?, 0)",
        (guild_id, link_hash, platform, message_id, channel_id, user_id, now_iso),
    )
    if cur.rowcount == 1:
        conn.commit()
        return ClaimResult(is_new=True, duplicate_count=0)

    cur.execute(
        "UPDATE social_link_dupes SET duplicate_count = duplicate_count + 1, "
        "last_duplicate_ts = ?, last_duplicate_user_id = ? "
        "WHERE guild_id = ? AND link_hash = ?",
        (now_iso, user_id, guild_id, link_hash),
    )
    cur.execute(
        "SELECT duplicate_count FROM social_link_dupes WHERE guild_id = ? AND link_hash = ?",
        (guild_id, link_hash),
    )
    row = cur.fetchone()
    conn.commit()
    return ClaimResult(is_new=False, duplicate_count=row[0] if row else 1)


def _within_window(last_iso: str, now_iso: str, window_days: int) -> bool:
    last = datetime.datetime.fromisoformat(last_iso)
    now = datetime.datetime.fromisoformat(now_iso)
    return (now - last) <= datetime.timedelta(days=window_days)


def bump_user_warning_level(
    conn: sqlite3.Connection, *, guild_id: str, user_id: str, now_iso: str, window_days: int,
) -> int:
    """Escalate (or reset, if outside the window) this user's duplicate-warning
    level. Purely cosmetic — never used for moderation."""
    cur = conn.cursor()
    cur.execute(
        "SELECT level, last_warned_ts FROM social_link_user_warnings WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    row = cur.fetchone()
    level = 1
    if row:
        prev_level, last_ts = row
        if last_ts and _within_window(last_ts, now_iso, window_days):
            level = prev_level + 1
    cur.execute(
        "INSERT INTO social_link_user_warnings (guild_id, user_id, level, last_warned_ts) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(guild_id, user_id) DO UPDATE SET level = excluded.level, last_warned_ts = excluded.last_warned_ts",
        (guild_id, user_id, level, now_iso),
    )
    conn.commit()
    return level


def purge_expired(conn: sqlite3.Connection, *, retention_days: int, now_iso: str) -> int:
    """Delete original-link records older than retention_days. retention_days
    <= 0 means retain indefinitely (default) — no-op."""
    if retention_days <= 0:
        return 0
    cutoff = (datetime.datetime.fromisoformat(now_iso) - datetime.timedelta(days=retention_days)).isoformat()
    cur = conn.cursor()
    cur.execute("DELETE FROM social_link_dupes WHERE first_seen_ts < ?", (cutoff,))
    n = cur.rowcount
    conn.commit()
    return n


# ─── Warning tone pool (fallback when the LLM call is unavailable) ─────────

WARNING_POOL = {
    1: [
        "Easy there, rerun champion — that one already made the rounds.",
        "Ah, a rerun! Bold choice. Already been shared, though.",
        "That link's already had its moment in the sun. No re-runs.",
    ],
    2: [
        "That's number two. I'm beginning to suspect you enjoy making me clean up after you.",
        "Twice now. Are we doing a bit, or is this just your thing?",
        "Second time's not the charm — that one's already out there.",
    ],
    3: [
        "I warned you already. I've got my eye on you now — don't make me call the imaginary link police.",
        "This is officially a pattern. The imaginary link police have been notified (they are not real, but the vibes are).",
        "At this point I'm just going to assume every link you post is a rerun until proven otherwise.",
    ],
}


def pick_fallback_warning(level: int) -> str:
    import random
    tier = min(max(level, 1), 3)
    return random.choice(WARNING_POOL[tier])

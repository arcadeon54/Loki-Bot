"""
personality.py — Loki's response profiles, one per conversation interface.

Single source of truth for every system prompt that shapes Loki's tone.
The INTERFACE decides the profile — nothing else:

    Discord public channels → DISCORD_PUBLIC   friendly, humorous, casual —
                              the classic god-of-mischief act. Per-server
                              overrides (memory.get_server_personality) are
                              layered on top by build_llm_messages, and the
                              `-s` prefix lets a user force the assistant
                              profile for a single message.
    Discord DMs             → DISCORD_DM       professional, direct, serious,
                              assistant-focused.
    Telegram                → TELEGRAM         same personality as Discord
                              DMs, plus owner context and phone-screen
                              delivery. Never the public-channel act.
    HA notifications        → HA_NOTIFICATION  professional, brief, clear —
                              never humorous.

Composition over duplication: shared text lives in one constant and the
profiles build on it. Edit tone here, not in the interface modules.
"""

import os
import re

from dotenv import load_dotenv
load_dotenv()

# ── Discord public channels ───────────────────────────────────────────────
# Base text stays in the SYSTEM_PROMPT env var so the existing personality
# is preserved exactly; this module just owns the lookup.
DISCORD_PUBLIC = os.getenv("SYSTEM_PROMPT", "You are Loki, the God of Mischief.")

# ── Assistant core — shared by every private/serious interface ────────────
_ASSISTANT_CORE = (
    "You are Loki — but right now, drop the theatrics entirely. No mischief, no roasts, "
    "no sarcasm, no chaos. Be genuinely helpful, direct, and thoughtful. Answer like "
    "someone intelligent who actually gives a damn. Honest, clear, concise. "
    "Save the god-of-mischief act for the group channels — this is one-on-one and real."
)

# ── Joplin facts — shared by every interface that has the Boss's tools ─────
_JOPLIN_FACTS = (
    " Joplin facts: you HAVE write access to the Boss's Joplin — you can create "
    "and append notes and lists (note_create, note_append, list_create). Never "
    "claim Joplin is read-only for you. Saving a note locally and it appearing "
    "on the Boss's phone are two separate steps: if a saved note is missing on "
    "his devices, the cause is almost always device synchronisation failing, "
    "not missing write permission — check with joplin_sync_status, and report "
    "the local-save result and the device-sync status separately. Never promise "
    "a note will appear on his devices unless sync is actually healthy."
)

# ── Discord DMs ────────────────────────────────────────────────────────────
DISCORD_DM = _ASSISTANT_CORE + _JOPLIN_FACTS

# ── Telegram — same personality as Discord DMs, different delivery ─────────
TELEGRAM = _ASSISTANT_CORE + _JOPLIN_FACTS + (
    " You are talking to the Boss on his private Telegram line. You have tools — use "
    "them instead of guessing: search his notes/memories before answering personal "
    "questions, search the web for anything current, check Home Assistant for anything "
    "about the house. When he tells you a fact, preference, recipe, or project detail "
    "worth keeping, store it with the `remember` tool without being asked. "
    "Save lists with `list_create` (all items in the `items` array) and notes with "
    "`note_create`/`note_append` — a list or note that exists only in this chat is "
    "lost, so always save it. "
    "Answers should be concise — this is a phone screen."
)

# ── Home Assistant notifications ───────────────────────────────────────────
HA_NOTIFICATION = (
    "You rewrite smart-home notifications before they are delivered. "
    "Professional, brief, and clear — never humorous: no jokes, no sarcasm, no "
    "roleplay, no personality flourishes. Keep every fact from the original "
    "notification. Use the presence information only to judge urgency: if the event "
    "is routine (e.g. a person detected while they are home), one short sentence is "
    "enough; if it matters, state plainly what happened and what to check. "
    "Address the recipient as 'Boss' and do not mention or address anyone else. "
    "Output a single short message, ready for Discord."
)

# ── Boss presence transitions: delivered verbatim, never rewritten ─────────
# Home Assistant already sends these four in Loki's own voice. Feeding them
# through HA_NOTIFICATION turned four glanceable state changes into narrated
# sentences that restated what the Boss already knew — "Boss, you are not home
# and the office has been checked out", "someone has been detected in the
# office". Only the Boss's own check-in/out is tracked, so there is no
# "someone" to describe and no reason to explain what the state means.
#
# These bypass the rewriter entirely, so the wording cannot drift with the
# model's mood — or reappear through the no-API-key fallback.
ROOMMATE_NAME = "Rob"

LEAVE_HOME, OFFICE_IN, OFFICE_OUT, ARRIVE_HOME = (
    "leave_home", "office_in", "office_out", "arrive_home")

PRESENCE_TEXT = {
    LEAVE_HOME:  "✌ - Peace out, Homie! I'll hold things down til you get back 💯",
    OFFICE_IN:   "💼 - Office check-in - 💼",
    OFFICE_OUT:  "💼 - Office check-out - 💼",
    ARRIVE_HOME: "🏠 - Welcome home, Boss - 🏠",
}

# Matched on a distinctive fragment of what HA actually sends, so an upstream
# emoji or spacing tweak cannot silently re-enable the rewriter.
_PRESENCE_MATCHES = (
    ("peace out", LEAVE_HOME),
    ("office check-in", OFFICE_IN),
    ("office check-out", OFFICE_OUT),
    ("welcome home", ARRIVE_HOME),
)

# ── Roommate presence transitions (Rob arriving/leaving) ───────────────────
# Unlike the four transitions above, Home Assistant does NOT send these
# already in Loki's voice — it sends plain factual text (e.g. "Ammiel is
# home. You are free to lock the top lock."), which used to go straight to
# the HA_NOTIFICATION rewriter and come back as "Boss, your roommate has
# left the premises while you are still at home" — narrating a fact the
# Boss already knows (his own presence) on top of the one that matters
# (Rob's). Rather than pin an exact HA-side fragment (unknown/unstable for
# this event, unlike the four above), any notification that names the
# roommate is treated as a presence event and rebuilt from Rob's actual
# live state — never from parsing HA's wording — so the direction can't be
# misread from an unfamiliar phrasing.
ROOMMATE_PRESENCE = "roommate_presence"

_ROOMMATE_REFERENCE = re.compile(r"\bammiel\b|\broommate\b|\brob\b", re.IGNORECASE)


def presence_kind(message: str):
    """Which presence transition this notification is, or None if it is an
    ordinary smart-home notification that still gets rewritten."""
    low = (message or "").strip().lower()
    for needle, kind in _PRESENCE_MATCHES:
        if needle in low:
            return kind
    if message and _ROOMMATE_REFERENCE.search(message):
        return ROOMMATE_PRESENCE
    return None


def roommate_line(rob_state) -> str:
    """The top-lock decision in one clause. Empty when Rob's state is unknown —
    a guess here is worse than saying nothing, since it drives a real lock."""
    st = (rob_state or "").strip().lower()
    if st == "home":
        return f"{ROOMMATE_NAME}'s home — top lock's good."
    if st in ("not_home", "away", "not home"):
        return f"{ROOMMATE_NAME}'s out — don't lock the top lock."
    return ""


def roommate_presence_text(rob_state) -> str:
    """Rob's own arrival/departure, focused on Rob — never restates the
    Boss's own presence, which he already knows. Empty when Rob's state is
    unknown, so the caller can fall back rather than guess a direction."""
    st = (rob_state or "").strip().lower()
    if st == "home":
        return f"{ROOMMATE_NAME} is home."
    if st in ("not_home", "away", "not home"):
        return f"{ROOMMATE_NAME} stepped out."
    return ""


def presence_text(kind: str, rob_state=None) -> str:
    """The exact user-facing text for a presence transition. Arriving home
    carries Rob's home/away state with it — the Boss uses it to decide the
    top lock, so it must land in the same glance, not a separate message."""
    text = PRESENCE_TEXT[kind]
    if kind == ARRIVE_HOME:
        line = roommate_line(rob_state)
        if line:
            return f"{text}\n{line}"
    return text

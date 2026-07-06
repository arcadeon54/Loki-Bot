"""
work_tracker.py — automatic work-hours tracking (July 2026 upgrade).

THE RULE (from the Boss, verbatim intent):
  If the Boss stays at any non-home location for at least 90 consecutive
  minutes, that location is WORK. The session starts at ARRIVAL time — the
  90 minutes only confirms it was work, it is not a grace period.
  Arrive 6:00 AM, confirmed 7:30 AM, leave 1:00 PM  →  7 hours, not 5.5.

State machine (fed by a 5-min poll of person.kavaris in loki_bot.py):

    IDLE ──arrive non-home──▶ CANDIDATE(anchor, arrived_at)
    CANDIDATE ──moved >250m / went home before 90 min──▶ IDLE (no session)
    CANDIDATE ──dwell ≥ 90 min──▶ CONFIRMED (start = arrived_at)
    CONFIRMED ──2 consecutive departed polls or zone=home──▶ CLOSED
    CLOSED: SQLite row + Joplin work-log row + Discord announce
            + Google Sheets append (legacy behavior, kept)

Departure needs two consecutive "gone" polls (GPS jitter protection) unless
HA says the person is literally home. State persists to JSON so restarts
mid-shift don't lose the arrival time.

Every completed session lands in Joplin: `Loki/Work Log/Work Log — YYYY-MM`
with Date | Location | Start | End | Duration | Running Total.
"""

import asyncio
import datetime
import json
import logging
import os
import sqlite3
from zoneinfo import ZoneInfo

log = logging.getLogger("WorkTracker")

ET = ZoneInfo("America/New_York")

DB_PATH    = os.path.join(os.path.dirname(__file__), "jobsite.db")
STATE_PATH = os.path.join(os.path.dirname(__file__), "work_tracker_state.json")

WORK_CONFIRM_MINUTES = int(os.getenv("WORK_CONFIRM_MINUTES", "90"))
MOVE_THRESHOLD_M     = int(os.getenv("WORK_MOVE_THRESHOLD_M", "250"))
WORK_HOURLY_RATE     = float(os.getenv("WORK_HOURLY_RATE", "16"))
PAY_PERIOD_ANCHOR    = datetime.date(2026, 5, 16)          # 14-day cycles
SHEETS_CONFIG_ENTRY  = os.getenv("GOOGLE_SHEETS_CONFIG_ENTRY", "01KV6AEYDJ6DNXE6GVXCGT0CRR")
SHEETS_WORKSHEET     = os.getenv("GOOGLE_SHEETS_WORKSHEET", "Sheet1")

WORKLOG_NOTEBOOK = os.getenv("LOKI_WORKLOG_NOTEBOOK", "Loki/Work Log")

# Injected by loki_bot.py at startup.
_hooks: dict = {}


def bind(**hooks):
    """Expected hooks:
        ha_get_state(entity_id) -> dict|None          (async)
        ha_call_service(domain, service, extra) ...   (async, for sheets)
        announce(text) -> None                        (async, Discord channel)
        nearby_site(lat, lon) -> dict|None            (sync, jobsite_db)
        reverse_geocode(lat, lon) -> str|None         (async)
    """
    _hooks.update(hooks)


# ─── SQLite ───────────────────────────────────────────────────────────────────

def init_db():
    with sqlite3.connect(DB_PATH) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS work_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location TEXT NOT NULL,
            lat REAL, lon REAL,
            arrived_at TEXT NOT NULL,      -- ISO, ET
            departed_at TEXT,
            duration_minutes REAL,
            joplin_ok INTEGER DEFAULT 0,
            sheets_ok INTEGER DEFAULT 0
        )""")


def _insert_session(loc, lat, lon, arrived, departed, minutes) -> int:
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(
            "INSERT INTO work_sessions (location,lat,lon,arrived_at,departed_at,duration_minutes)"
            " VALUES (?,?,?,?,?,?)",
            (loc, lat, lon, arrived.isoformat(), departed.isoformat(), minutes))
        return cur.lastrowid


def _mark(session_id: int, column: str):
    with sqlite3.connect(DB_PATH) as c:
        c.execute(f"UPDATE work_sessions SET {column}=1 WHERE id=?", (session_id,))


def total_minutes(before_id: int | None = None) -> float:
    """All-time recorded minutes (optionally up to and including a session)."""
    with sqlite3.connect(DB_PATH) as c:
        if before_id is not None:
            row = c.execute("SELECT COALESCE(SUM(duration_minutes),0) FROM work_sessions"
                            " WHERE id<=?", (before_id,)).fetchone()
        else:
            row = c.execute("SELECT COALESCE(SUM(duration_minutes),0) FROM work_sessions").fetchone()
        return row[0] or 0.0


def recent_sessions(limit: int = 10) -> list[dict]:
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT location,arrived_at,departed_at,duration_minutes FROM work_sessions"
            " ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"location": r[0], "arrived_at": r[1], "departed_at": r[2],
             "duration_minutes": r[3]} for r in rows]


# ─── Persistent poll state ────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        with open(STATE_PATH) as f:
            s = json.load(f)
        if s.get("arrived_at"):
            s["arrived_at"] = datetime.datetime.fromisoformat(s["arrived_at"])
        return s
    except Exception:
        return {}


def _save_state(s: dict):
    out = dict(s)
    if isinstance(out.get("arrived_at"), datetime.datetime):
        out["arrived_at"] = out["arrived_at"].isoformat()
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(out, f)
    except Exception as e:
        log.error(f"state save failed: {e}")


_state = _load_state()
# keys: phase (idle|candidate|confirmed), lat, lon, arrived_at,
#       location, away_strikes


def _dist_m(lat1, lon1, lat2, lon2):
    import math
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _fmt_dur(minutes: float) -> str:
    h, m = divmod(int(round(minutes)), 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def pay_period(day: datetime.date) -> str:
    days = (day - PAY_PERIOD_ANCHOR).days
    cycle = days // 14
    start = PAY_PERIOD_ANCHOR + datetime.timedelta(days=cycle * 14)
    end = start + datetime.timedelta(days=13)
    return f"{start.strftime('%b %d')} - {end.strftime('%b %d')}"


async def _location_name(lat: float, lon: float, zone_state: str) -> str:
    """Zone name > saved job site > reverse geocode > coordinates."""
    if zone_state and zone_state not in ("home", "not_home", "unknown", "unavailable"):
        return zone_state  # HA zone friendly name, e.g. "Kroger Tucker"
    site = None
    if "nearby_site" in _hooks:
        try:
            site = _hooks["nearby_site"](lat, lon)
        except Exception:
            site = None
    if site:
        return site["name"]
    if "reverse_geocode" in _hooks:
        addr = await _hooks["reverse_geocode"](lat, lon)
        if addr:
            return addr
    return f"{lat:.5f}, {lon:.5f}"


# ─── Session close-out ────────────────────────────────────────────────────────

async def _write_joplin_row(session_id: int, loc: str,
                            arrived: datetime.datetime,
                            departed: datetime.datetime, minutes: float):
    try:
        import joplin_integration as jp
        if not jp.is_configured():
            return
        running = total_minutes(before_id=session_id) / 60.0
        title = f"Work Log — {arrived.strftime('%Y-%m')}"
        header = (
            f"# {title}\n\nAutomatic work-session log (Loki). "
            f"Rule: ≥{WORK_CONFIRM_MINUTES} min at a non-home location = work; "
            "session starts at arrival.\n\n"
            "| Date | Location | Start | End | Duration | Running Total |\n"
            "|------|----------|-------|-----|----------|---------------|"
        )
        row = (f"| {arrived.strftime('%Y-%m-%d (%a)')} | {loc} "
               f"| {arrived.strftime('%-I:%M %p')} | {departed.strftime('%-I:%M %p')} "
               f"| {_fmt_dur(minutes)} | {running:.2f} h |")
        await jp.append_or_create(title, WORKLOG_NOTEBOOK, row, header=header,
                                  tags=["work-log"])
        _mark(session_id, "joplin_ok")
        log.info(f"Work session written to Joplin: {loc} {_fmt_dur(minutes)}")
    except Exception as e:
        log.error(f"Joplin work-log write failed: {e}")


async def _write_sheets_row(session_id: int, loc: str,
                            arrived: datetime.datetime,
                            departed: datetime.datetime, minutes: float):
    """Legacy Google Sheets export via HA google_sheets.append_sheet."""
    if not SHEETS_CONFIG_ENTRY or "ha_call_service" not in _hooks:
        return
    try:
        hours = minutes / 60.0
        payload = {
            "config_entry": SHEETS_CONFIG_ENTRY,
            "worksheet": SHEETS_WORKSHEET,
            "data": {
                "Pay Period": pay_period(arrived.date()),
                "Date": arrived.strftime("%Y-%m-%d"),
                "Job Location": loc,
                "Arrival": arrived.strftime("%-I:%M %p"),
                "Departure": departed.strftime("%-I:%M %p"),
                "Hours Worked": f"{hours:.2f}",
                "Total Earned": f"${hours * WORK_HOURLY_RATE:.2f}",
            },
        }
        ok = await _hooks["ha_call_service"]("google_sheets", "append_sheet",
                                             extra=payload)
        if ok:
            _mark(session_id, "sheets_ok")
        else:
            log.warning("Sheets append returned not-ok")
    except Exception as e:
        log.error(f"Sheets append failed: {e}")


async def _close_session(departed: datetime.datetime):
    arrived: datetime.datetime = _state["arrived_at"]
    loc = _state.get("location") or "Unknown site"
    lat, lon = _state.get("lat"), _state.get("lon")
    minutes = (departed - arrived).total_seconds() / 60.0

    session_id = _insert_session(loc, lat, lon, arrived, departed, minutes)
    running_h = total_minutes(before_id=session_id) / 60.0
    log.info(f"Work session closed: {loc} {arrived:%H:%M}–{departed:%H:%M} "
             f"({_fmt_dur(minutes)}), running total {running_h:.2f} h")

    if "announce" in _hooks:
        try:
            await _hooks["announce"](
                f"🧾 **Work session logged** — **{loc}**\n"
                f"{arrived.strftime('%-I:%M %p')} → {departed.strftime('%-I:%M %p')} "
                f"= **{_fmt_dur(minutes)}**  (running total {running_h:.1f} h)"
            )
        except Exception as e:
            log.error(f"announce failed: {e}")

    await _write_joplin_row(session_id, loc, arrived, departed, minutes)
    await _write_sheets_row(session_id, loc, arrived, departed, minutes)


# ─── The poll ────────────────────────────────────────────────────────────────

async def poll(person_entity: str = "person.kavaris"):
    """Advance the state machine one tick. Called every ~5 min by loki_bot."""
    if "ha_get_state" not in _hooks:
        return
    st = await _hooks["ha_get_state"](person_entity)
    if not st:
        return
    zone = (st.get("state") or "").lower()
    attrs = st.get("attributes", {})
    lat, lon = attrs.get("latitude"), attrs.get("longitude")
    now = datetime.datetime.now(ET)

    phase = _state.get("phase", "idle")

    # No GPS → can't reason about movement; only a hard "home" ends things.
    if lat is None or lon is None:
        if zone == "home" and phase == "confirmed":
            await _close_session(now)
            _state.clear()
            _save_state(_state)
        return

    if phase == "idle":
        if zone != "home":
            zone_label = st.get("state") if zone not in ("not_home",) else ""
            _state.update({"phase": "candidate", "lat": lat, "lon": lon,
                           "arrived_at": now, "zone_label": zone_label,
                           "away_strikes": 0})
            _save_state(_state)
            log.info(f"Arrival candidate at {lat:.5f},{lon:.5f} ({zone})")
        return

    anchor_lat, anchor_lon = _state["lat"], _state["lon"]
    moved = _dist_m(lat, lon, anchor_lat, anchor_lon) > MOVE_THRESHOLD_M
    went_home = zone == "home"

    if phase == "candidate":
        if went_home or moved:
            # Left before the 90-min confirmation → not work.
            log.info("Candidate abandoned (left before confirmation window)")
            if moved and not went_home:
                # Immediately re-anchor at the new spot.
                _state.update({"phase": "candidate", "lat": lat, "lon": lon,
                               "arrived_at": now, "away_strikes": 0})
            else:
                _state.clear()
            _save_state(_state)
            return
        dwell = (now - _state["arrived_at"]).total_seconds() / 60.0
        if dwell >= WORK_CONFIRM_MINUTES:
            loc = await _location_name(anchor_lat, anchor_lon,
                                       _state.get("zone_label") or zone)
            _state.update({"phase": "confirmed", "location": loc,
                           "away_strikes": 0})
            _save_state(_state)
            log.info(f"Work session CONFIRMED at '{loc}' "
                     f"(started {_state['arrived_at']:%H:%M})")
            if "announce" in _hooks:
                try:
                    await _hooks["announce"](
                        f"📍 Looks like you're working at **{loc}** — on the clock "
                        f"since **{_state['arrived_at'].strftime('%-I:%M %p')}**. "
                        f"I'll log the session when you leave.")
                except Exception:
                    pass
        return

    if phase == "confirmed":
        if went_home:
            await _close_session(now)
            _state.clear()
            _save_state(_state)
            return
        if moved:
            strikes = _state.get("away_strikes", 0) + 1
            _state["away_strikes"] = strikes
            if strikes >= 2:  # two consecutive polls away → real departure
                await _close_session(now)
                _state.clear()
                if zone != "home":
                    _state.update({"phase": "candidate", "lat": lat, "lon": lon,
                                   "arrived_at": now, "away_strikes": 0})
            _save_state(_state)
        else:
            if _state.get("away_strikes"):
                _state["away_strikes"] = 0
                _save_state(_state)


# ─── Reporting (used by the `work_hours_report` LLM tool) ────────────────────

def report(days: int = 14) -> str:
    init_db()
    cutoff = (datetime.datetime.now(ET) - datetime.timedelta(days=days)).isoformat()
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            "SELECT location,arrived_at,departed_at,duration_minutes FROM work_sessions"
            " WHERE arrived_at>=? ORDER BY arrived_at", (cutoff,)).fetchall()
    if not rows:
        base = f"No work sessions recorded in the last {days} days."
    else:
        lines, subtotal = [], 0.0
        for loc, a, d, m in rows:
            a_dt = datetime.datetime.fromisoformat(a)
            d_dt = datetime.datetime.fromisoformat(d) if d else None
            subtotal += m or 0
            lines.append(f"- {a_dt.strftime('%a %b %-d')}: {loc} "
                         f"{a_dt.strftime('%-I:%M %p')}–"
                         f"{d_dt.strftime('%-I:%M %p') if d_dt else '…'} "
                         f"({_fmt_dur(m or 0)})")
        base = (f"Work sessions, last {days} days ({_fmt_dur(subtotal)} total, "
                f"~${subtotal / 60 * WORK_HOURLY_RATE:.2f}):\n" + "\n".join(lines))
    if _state.get("phase") == "confirmed":
        base += (f"\nCurrently ON THE CLOCK at {_state.get('location')} "
                 f"since {_state['arrived_at'].strftime('%-I:%M %p')}.")
    all_time = total_minutes() / 60.0
    return base + f"\nAll-time running total: {all_time:.1f} h."

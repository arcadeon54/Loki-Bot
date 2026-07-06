"""
presence_monitor.py — lockout-prevention notifications (July 2026 upgrade).

Residents (Home Assistant person entities):
    person.kavaris  = Boss (Kavaris)     → notify.mobile_app_nokia_e23
    person.ammiel   = Rob  (roommate)    → notify.mobile_app_nothing_phone

Rules (only fire when EXACTLY ONE resident is home):
    Boss arrives home while Rob is away  → notify Boss:
        "Rob is not home. Do not lock him out."
    Rob arrives home while Boss is away  → notify Rob:
        "Boss is not home. Do not lock him out."

Arrival = state transition to 'home' observed between two polls. State
persists to JSON so a bot restart doesn't fake an "arrival" for someone
who has been home all along. A small cooldown stops double-fires from
GPS flapping at the edge of the home zone.
"""

import datetime
import json
import logging
import os

log = logging.getLogger("Presence")

STATE_PATH = os.path.join(os.path.dirname(__file__), "presence_state.json")

BOSS_PERSON  = os.getenv("PRESENCE_BOSS_ENTITY", "person.kavaris")
ROB_PERSON   = os.getenv("PRESENCE_ROOMMATE_ENTITY", "person.ammiel")
BOSS_NOTIFY  = os.getenv("PRESENCE_BOSS_NOTIFY", "mobile_app_nokia_e23")
ROB_NOTIFY   = os.getenv("PRESENCE_ROOMMATE_NOTIFY", "mobile_app_nothing_phone")
COOLDOWN_MIN = int(os.getenv("PRESENCE_COOLDOWN_MINUTES", "20"))

_hooks: dict = {}


def bind(**hooks):
    """Expected hooks:
        ha_get_state(entity_id) -> dict|None                       (async)
        ha_call_service(domain, service, extra=payload) -> bool    (async)
        dm_boss(text) -> None                                      (async, Discord DM, optional)
    """
    _hooks.update(hooks)


def _load() -> dict:
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(s: dict):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(s, f)
    except Exception as e:
        log.error(f"presence state save failed: {e}")


_state = _load()   # {"boss": "home", "rob": "not_home", "last_fire": iso}


async def _notify_phone(service: str, title: str, message: str) -> bool:
    if "ha_call_service" not in _hooks:
        return False
    return await _hooks["ha_call_service"](
        "notify", service, extra={"title": title, "message": message})


def _cooldown_active(now: datetime.datetime) -> bool:
    last = _state.get("last_fire")
    if not last:
        return False
    try:
        return (now - datetime.datetime.fromisoformat(last)).total_seconds() \
               < COOLDOWN_MIN * 60
    except Exception:
        return False


async def poll():
    """Called every ~2 min by loki_bot. Detects arrivals and fires the rules."""
    if "ha_get_state" not in _hooks:
        return
    boss_st = await _hooks["ha_get_state"](BOSS_PERSON)
    rob_st = await _hooks["ha_get_state"](ROB_PERSON)
    if not boss_st or not rob_st:
        return

    boss = (boss_st.get("state") or "unknown").lower()
    rob = (rob_st.get("state") or "unknown").lower()
    prev_boss = _state.get("boss")
    prev_rob = _state.get("rob")
    now = datetime.datetime.now()

    boss_arrived = boss == "home" and prev_boss is not None and prev_boss != "home"
    rob_arrived = rob == "home" and prev_rob is not None and prev_rob != "home"

    # Exactly one resident home is the only condition that warrants a warning.
    exactly_one_home = (boss == "home") != (rob == "home")

    if exactly_one_home and not _cooldown_active(now):
        if boss_arrived and rob != "home":
            msg = "Rob is not home. Do not lock him out."
            ok = await _notify_phone(BOSS_NOTIFY, "🚪 Heads up, Boss", msg)
            if "dm_boss" in _hooks:
                try:
                    await _hooks["dm_boss"](f"🚪 {msg}")
                except Exception:
                    pass
            _state["last_fire"] = now.isoformat()
            log.info(f"Lockout warning → Boss (phone={'ok' if ok else 'FAILED'})")
        elif rob_arrived and boss != "home":
            msg = "Boss is not home. Do not lock him out."
            ok = await _notify_phone(ROB_NOTIFY, "🚪 Heads up, Rob", msg)
            _state["last_fire"] = now.isoformat()
            log.info(f"Lockout warning → Rob (phone={'ok' if ok else 'FAILED'})")

    if boss != prev_boss or rob != prev_rob:
        log.info(f"Presence: boss={boss} rob={rob}")
    _state["boss"] = boss
    _state["rob"] = rob
    _save(_state)


def status() -> str:
    return (f"Boss: {_state.get('boss', '?')}, Rob: {_state.get('rob', '?')}"
            f" (last warning: {_state.get('last_fire', 'never')})")

"""
shared_state.py — Shared state management for Loki/Miss-T bot dynamic.
Both bots read and write this file to coordinate behavior.
Place in ~/bot-shared-modules/ and import from both bots.
"""

import json
import os
import fcntl
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

DEFAULT_STATE = {
    "loki_muted": False,
    "mute_reason": "",
    "mute_until": None,
    "cooldown_active": False,
    "cooldown_until": None,
    "behavior_modifier": None,   # "cool_down" | "shut_up" | "be_nice" | "topic_change" | None
    "last_warning": None,
    "warning_count": 0,
    "last_updated_by": None,
    "last_updated_at": None,
}


class SharedState:
    def __init__(self, state_path: str):
        self.state_path = Path(state_path)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_path.exists():
            self._write(DEFAULT_STATE.copy())

    def _now(self) -> str:
        return datetime.datetime.now(ET).isoformat()

    def _read(self) -> dict:
        try:
            with open(self.state_path, "r") as f:
                fcntl.flock(f, fcntl.LOCK_SH)
                data = json.load(f)
                fcntl.flock(f, fcntl.LOCK_UN)
            return {**DEFAULT_STATE, **data}
        except (json.JSONDecodeError, FileNotFoundError):
            return DEFAULT_STATE.copy()

    def _write(self, state: dict):
        state["last_updated_at"] = self._now()
        tmp_path = str(self.state_path) + ".tmp"
        with open(tmp_path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(state, f, indent=2)
            fcntl.flock(f, fcntl.LOCK_UN)
        os.replace(tmp_path, self.state_path)

    def get(self) -> dict:
        return self._read()

    def is_loki_muted(self) -> bool:
        state = self._read()
        if not state["loki_muted"]:
            return False
        if state["mute_until"] is None:
            return True
        mute_until = datetime.datetime.fromisoformat(state["mute_until"])
        if datetime.datetime.now(ET) >= mute_until:
            self.clear_mute()
            return False
        return True

    def is_loki_on_cooldown(self) -> tuple:
        """Returns (is_on_cooldown: bool, behavior_modifier: str | None)"""
        state = self._read()
        if not state["cooldown_active"]:
            return False, None
        if state["cooldown_until"] is None:
            return True, state["behavior_modifier"]
        cooldown_until = datetime.datetime.fromisoformat(state["cooldown_until"])
        if datetime.datetime.now(ET) >= cooldown_until:
            self.clear_cooldown()
            return False, None
        return True, state["behavior_modifier"]

    def mute_loki(self, duration_seconds: int, reason: str = "", updated_by: str = "misst"):
        state = self._read()
        mute_until = datetime.datetime.now(ET) + datetime.timedelta(seconds=duration_seconds)
        state["loki_muted"] = True
        state["mute_reason"] = reason
        state["mute_until"] = mute_until.isoformat()
        state["last_warning"] = self._now()
        state["warning_count"] = state.get("warning_count", 0) + 1
        state["last_updated_by"] = updated_by
        self._write(state)

    def set_cooldown(self, duration_seconds: int, modifier: str, updated_by: str = "misst"):
        state = self._read()
        cooldown_until = datetime.datetime.now(ET) + datetime.timedelta(seconds=duration_seconds)
        state["cooldown_active"] = True
        state["cooldown_until"] = cooldown_until.isoformat()
        state["behavior_modifier"] = modifier
        state["last_updated_by"] = updated_by
        self._write(state)

    def clear_mute(self, updated_by: str = "system"):
        state = self._read()
        state["loki_muted"] = False
        state["mute_reason"] = ""
        state["mute_until"] = None
        state["last_updated_by"] = updated_by
        self._write(state)

    def clear_cooldown(self, updated_by: str = "system"):
        state = self._read()
        state["cooldown_active"] = False
        state["cooldown_until"] = None
        state["behavior_modifier"] = None
        state["last_updated_by"] = updated_by
        self._write(state)

    def reset(self, updated_by: str = "system"):
        state = DEFAULT_STATE.copy()
        state["last_updated_by"] = updated_by
        self._write(state)

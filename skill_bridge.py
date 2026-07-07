"""
skill_bridge.py — mirrors every skillkit skill into Loki's tool registry.

Generic on purpose: no skill names appear in this file or anywhere else in
Loki. Skills live in the standalone framework at SKILLKIT_HOME
(/home/g2k247/skillkit); dropping a new file into its skills.d/ makes the
skill available to Loki on next restart with zero Loki changes. Loki is
just one caller of the framework (identity "loki"), exactly like the
skillkit CLI and MCP adapters used by Claude Code / Gemini / Antigravity.

Imported for side effect (same pattern as assistant_tools): registration
happens at import time, before on_ready logs the tool roster.
"""

import logging
import os
import sys

log = logging.getLogger("SkillBridge")

SKILLKIT_HOME = os.getenv("SKILLKIT_HOME", "/home/g2k247/skillkit")
if SKILLKIT_HOME not in sys.path:
    sys.path.insert(0, SKILLKIT_HOME)

import tools as loki_tools
from skillkit import openai_adapter
from skillkit import registry as sk_registry

CALLER = "loki"

# skillkit permission levels → Loki's Discord-user levels. skillkit's own
# check runs at caller level ("loki" = owner); this mapping is what gates
# which Discord users see each mirrored tool.
_LEVEL_MAP = {"public": "everyone", "user": "crew", "admin": "boss", "owner": "boss"}


def _make_handler(skill_name: str):
    async def handler(args: dict, ctx: loki_tools.ToolContext) -> str:
        # Returns the SkillResult JSON — the LLM reads ok/data/error directly.
        return await openai_adapter.execute_raw(skill_name, args, CALLER)
    return handler


sk_registry.load_skills()
_names = []
for _s in sk_registry.visible_to(CALLER):
    loki_tools.register(loki_tools.ToolSpec(
        name=f"skill_{_s.name}",           # prefix avoids native-tool collisions
        description=_s.description,
        parameters=_s.input_schema(),
        handler=_make_handler(_s.name),
        permission=_LEVEL_MAP[_s.permission],
        timeout=max(30, _s.timeout_hint),  # solve's planner loop needs minutes
    ))
    _names.append(_s.name)

log.info(f"Skill bridge online — {len(_names)} skillkit skills mirrored: "
         + ", ".join(_names))

"""
telegram_interface.py — Telegram as a second brain-stem for Loki (July 2026).

Same brain, different mouth: messages route through the exact same LLMHandler
tool loop (chat_with_tools) and tool registry that Discord uses, so anything
Loki can do in Discord chat (memory, notes, HA, web search, work reports)
works from Telegram too.

Implementation notes:
- Raw Bot API long-polling via the bot's shared aiohttp session. No new
  dependency: getUpdates / sendMessage / sendChatAction is all we need,
  and the polling loop retries forever with backoff (network blips at
  home are a fact of life).
- Token discovery order:  TELEGRAM_BOT_TOKEN env  →  Joplin note search.
  The spec: auto-configure if a token exists in Joplin; if none is found
  (or it's revoked), log it and stay dormant — never crash the bot.
- Single-user by design: only the Boss talks to Loki on Telegram.
  TELEGRAM_OWNER_ID pins the account; if unset, the first human to message
  the bot gets paired (id is persisted + announced in the Discord relay so
  a hijack is visible immediately).
"""

import asyncio
import datetime
import json
import logging
import os
import re
from collections import deque

import aiohttp

log = logging.getLogger("Telegram")

STATE_PATH = os.path.join(os.path.dirname(__file__), "telegram_state.json")
TOKEN_RE = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")

HISTORY_LEN = int(os.getenv("TELEGRAM_HISTORY_LEN", "24"))

TELEGRAM_SYSTEM_PROMPT = (
    "You are Loki — the Boss's personal AI assistant, talking to him 1-on-1 "
    "on Telegram. Keep the mischievous wit, but this is his private channel: "
    "be genuinely useful first, funny second. You have tools — use them "
    "instead of guessing: search his notes/memories before answering personal "
    "questions, search the web for anything current, check Home Assistant for "
    "anything about the house. When he tells you a fact, preference, recipe, "
    "or project detail worth keeping, store it with the `remember` tool "
    "without being asked. Answers should be concise — this is a phone screen."
)


class TelegramInterface:
    def __init__(self, llm, tool_ctx_factory, session_factory,
                 memory_recall=None, on_paired=None):
        """
        llm              — LLMHandler (needs .chat_with_tools / .chat)
        tool_ctx_factory — (user_id, user_name, chat_id) -> ToolContext
        session_factory  — async () -> aiohttp.ClientSession
        memory_recall    — async (query, n) -> list[{text, kind}] (optional)
        on_paired        — async (info_text) -> None (optional, Discord announce)
        """
        self.llm = llm
        self.tool_ctx_factory = tool_ctx_factory
        self.session_factory = session_factory
        self.memory_recall = memory_recall
        self.on_paired = on_paired

        self.token: str | None = None
        self.bot_username = ""
        self.owner_id: int = int(os.getenv("TELEGRAM_OWNER_ID") or "0")
        self._history: dict[int, deque] = {}
        self._offset = 0
        self._task: asyncio.Task | None = None
        self._state = self._load_state()
        if not self.owner_id:
            self.owner_id = int(self._state.get("owner_id") or 0)

    # ── state ────────────────────────────────────────────────────────────
    def _load_state(self) -> dict:
        try:
            with open(STATE_PATH) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump(self._state, f)
        except Exception as e:
            log.error(f"telegram state save failed: {e}")

    # ── token discovery ──────────────────────────────────────────────────
    async def _discover_token(self) -> str | None:
        env_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        candidates = [env_token] if env_token else []

        # Joplin: any note mentioning telegram that contains a token pattern.
        try:
            import joplin_integration as jp
            if jp.is_configured() and await jp.ping():
                for hit in await jp.search_notes("telegram", limit=20):
                    for m in TOKEN_RE.findall(hit.get("body", "") or ""):
                        if m not in candidates:
                            candidates.append(m)
        except Exception as e:
            log.debug(f"Joplin token search failed: {e}")

        for token in candidates:
            me = await self._api("getMe", token=token)
            if me:
                self.bot_username = me.get("username", "")
                log.info(f"Telegram token valid → @{self.bot_username}")
                return token
            log.warning(f"Telegram token ending …{token[-6:]} is invalid/revoked")
        return None

    # ── low-level API ────────────────────────────────────────────────────
    async def _api(self, method: str, token: str | None = None,
                   timeout: int = 15, **params):
        token = token or self.token
        if not token:
            return None
        try:
            sess = await self.session_factory()
            async with sess.post(
                f"https://api.telegram.org/bot{token}/{method}",
                json=params, timeout=aiohttp.ClientTimeout(total=timeout),
            ) as r:
                data = await r.json(content_type=None)
                if data.get("ok"):
                    return data.get("result")
                if r.status == 401:
                    return None
                log.warning(f"Telegram {method}: {str(data)[:150]}")
                return None
        except Exception as e:
            log.debug(f"Telegram {method} failed: {e}")
            return None

    async def send(self, chat_id: int, text: str):
        # Telegram hard limit 4096; split on paragraph edges.
        chunks, cur = [], ""
        for para in text.split("\n"):
            if len(cur) + len(para) + 1 > 3900:
                chunks.append(cur)
                cur = para
            else:
                cur = f"{cur}\n{para}" if cur else para
        chunks.append(cur)
        for chunk in chunks:
            if not chunk.strip():
                continue
            ok = await self._api("sendMessage", chat_id=chat_id, text=chunk,
                                 parse_mode="Markdown")
            if ok is None:  # markdown parse failure → resend plain
                await self._api("sendMessage", chat_id=chat_id, text=chunk)

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> bool:
        self.token = await self._discover_token()
        if not self.token:
            log.warning("Telegram interface dormant — no valid bot token found "
                        "(set TELEGRAM_BOT_TOKEN in .env or drop the token in a "
                        "Joplin note mentioning 'telegram').")
            return False
        self._task = asyncio.create_task(self._poll_loop(), name="telegram-poll")
        log.info(f"Telegram interface online as @{self.bot_username} "
                 f"(owner={'#' + str(self.owner_id) if self.owner_id else 'unpaired'})")
        return True

    async def _poll_loop(self):
        backoff = 2
        while True:
            try:
                updates = None
                sess = await self.session_factory()
                async with sess.post(
                    f"https://api.telegram.org/bot{self.token}/getUpdates",
                    json={"offset": self._offset, "timeout": 50,
                          "allowed_updates": ["message"]},
                    timeout=aiohttp.ClientTimeout(total=65),
                ) as r:
                    data = await r.json(content_type=None)
                    if data.get("ok"):
                        updates = data["result"]
                backoff = 2
                for u in updates or []:
                    self._offset = max(self._offset, u["update_id"] + 1)
                    msg = u.get("message")
                    if msg:
                        asyncio.create_task(self._safe_handle(msg))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning(f"Telegram poll error: {e} — retrying in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def _safe_handle(self, msg: dict):
        try:
            await self._handle(msg)
        except Exception as e:
            log.error(f"Telegram handler error: {e}", exc_info=True)
            try:
                await self.send(msg["chat"]["id"],
                                "Something broke on my end handling that one.")
            except Exception:
                pass

    # ── message handling ─────────────────────────────────────────────────
    async def _handle(self, msg: dict):
        chat_id = msg["chat"]["id"]
        user = msg.get("from", {})
        user_id = user.get("id", 0)
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text or user.get("is_bot"):
            return

        # Pairing / auth
        if not self.owner_id:
            self.owner_id = user_id
            self._state["owner_id"] = user_id
            self._state["owner_name"] = user.get("username") or user.get("first_name", "")
            self._save_state()
            info = (f"Telegram paired to @{self._state['owner_name']} "
                    f"(id {user_id}) — if this isn't the Boss, remove "
                    f"telegram_state.json and set TELEGRAM_OWNER_ID.")
            log.warning(info)
            if self.on_paired:
                try:
                    await self.on_paired(info)
                except Exception:
                    pass
        elif user_id != self.owner_id:
            await self.send(chat_id, "This is a private line. Ask the Boss.")
            log.warning(f"Telegram: rejected stranger id={user_id} "
                        f"(@{user.get('username')})")
            return

        if text in ("/start", "/help"):
            await self.send(chat_id,
                "Loki here. Talk to me like you do on Discord — I can "
                "remember things, search your notes, run the house, check "
                "your work hours, and search the web.\n"
                "`-s` prefix = serious mode.")
            return

        await self._api("sendChatAction", chat_id=chat_id, action="typing")

        serious = text.startswith("-s")
        if serious:
            text = text[2:].strip()

        history = self._history.setdefault(chat_id, deque(maxlen=HISTORY_LEN))

        sys_prompt = TELEGRAM_SYSTEM_PROMPT
        if serious:
            sys_prompt += "\n[Serious mode: no jokes, straight answer.]"
        sys_prompt += ("\n[Current time]: "
                       + datetime.datetime.now().astimezone().strftime(
                           "%I:%M %p, %A %B %d %Y"))

        # Semantic memory recall — the notes-before-knowledge step.
        if self.memory_recall:
            try:
                memories = await self.memory_recall(text, 4)
                if memories:
                    mem_lines = "\n".join(f"- ({m['kind']}) {m['text']}"
                                          for m in memories)
                    sys_prompt += ("\n\n[FROM THE BOSS'S NOTES — relevant "
                                   f"memories]\n{mem_lines}")
            except Exception as e:
                log.debug(f"memory recall failed: {e}")

        messages = [{"role": "system", "content": sys_prompt}]
        messages += list(history)
        messages.append({"role": "user", "content": text})

        ctx = self.tool_ctx_factory(user_id, user.get("first_name", "Boss"),
                                    chat_id)
        reply = await self.llm.chat_with_tools(messages, ctx)

        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply})
        await self.send(chat_id, reply)

    async def stop(self):
        if self._task:
            self._task.cancel()

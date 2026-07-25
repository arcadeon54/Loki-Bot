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
import tempfile
from collections import deque

import aiohttp

log = logging.getLogger("Telegram")

STATE_PATH = os.path.join(os.path.dirname(__file__), "telegram_state.json")
TOKEN_RE = re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")

HISTORY_LEN = int(os.getenv("TELEGRAM_HISTORY_LEN", "24"))

# ── Media limits (env-tunable; Bot API refuses downloads >20 MB anyway) ──
IMAGE_MAX_BYTES = int(os.getenv("TELEGRAM_IMAGE_MAX_BYTES", str(10 * 1024 * 1024)))
AUDIO_MAX_BYTES = int(os.getenv("TELEGRAM_AUDIO_MAX_BYTES", str(20 * 1024 * 1024)))
AUDIO_MAX_SECONDS = int(os.getenv("TELEGRAM_AUDIO_MAX_SECONDS", "600"))

# Formats the existing Discord pipelines already accept.
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
AUDIO_MIMES = {"audio/ogg", "audio/oga", "audio/opus", "audio/mpeg", "audio/mp3",
               "audio/mp4", "audio/x-m4a", "audio/m4a", "audio/aac",
               "audio/wav", "audio/x-wav", "audio/flac", "audio/webm"}

_AUDIO_EXT = {"audio/ogg": ".ogg", "audio/oga": ".ogg", "audio/opus": ".ogg",
              "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a",
              "audio/x-m4a": ".m4a", "audio/m4a": ".m4a", "audio/aac": ".m4a",
              "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/flac": ".flac",
              "audio/webm": ".webm"}


def sniff_image_mime(data: bytes) -> str | None:
    """Magic-byte check that the payload really is a supported image."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return None

# Telegram is always the serious 1-on-1 assistant — same personality as
# Discord DMs. Tone lives in personality.py; never define prompt text here.
from personality import TELEGRAM as TELEGRAM_SYSTEM_PROMPT


class TelegramInterface:
    def __init__(self, llm, tool_ctx_factory, session_factory,
                 memory_recall=None, on_paired=None,
                 describe_image=None, transcribe_audio=None):
        """
        llm              — LLMHandler (needs .chat_with_tools / .chat)
        tool_ctx_factory — (user_id, user_name, chat_id) -> ToolContext
        session_factory  — async () -> aiohttp.ClientSession
        memory_recall    — async (query, n) -> list[{text, kind}] (optional)
        on_paired        — async (info_text) -> None (optional, Discord announce)
        describe_image   — async (bytes, mime) -> str (Discord vision pipeline)
        transcribe_audio — async (bytes, filename) -> str (Discord Whisper path)
        """
        self.llm = llm
        self.tool_ctx_factory = tool_ctx_factory
        self.session_factory = session_factory
        self.memory_recall = memory_recall
        self.on_paired = on_paired
        self.describe_image = describe_image
        self.transcribe_audio = transcribe_audio
        # Telegram re-delivers updates it thinks were missed; never process
        # the same message twice (a voice note would re-run tools).
        self._seen_msgs: deque = deque(maxlen=500)

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

    async def send_document(self, chat_id: int, file_path: str,
                            caption: str = "", filename: str | None = None):
        """Upload a document (multipart — _api is JSON-only). Token stays in
        the URL only; errors are logged by status/type, never the URL."""
        if not self.token:
            return None
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if caption:
            form.add_field("caption", caption[:1024])
        with open(file_path, "rb") as f:
            form.add_field("document", f,
                           filename=filename or os.path.basename(file_path))
            try:
                sess = await self.session_factory()
                async with sess.post(
                    f"https://api.telegram.org/bot{self.token}/sendDocument",
                    data=form, timeout=aiohttp.ClientTimeout(total=120),
                ) as r:
                    data = await r.json(content_type=None)
                    if not data.get("ok"):
                        log.warning(f"sendDocument failed: HTTP {r.status}")
                        return None
                    return data.get("result")
            except Exception as e:
                log.warning(f"sendDocument failed: {type(e).__name__}")
                return None

    def note_outbound(self, chat_id: int, text: str):
        """Record an out-of-band message (task completion notification,
        Career-Ops update, …) into the chat history the LLM sees.

        Without this, the model's last visible turn is still its own "I'll
        post the result here" promise, so on the NEXT user message — however
        unrelated — it re-fetches the finished task and pastes the stale
        result into the reply (the browser-task / "BLACK-BOXX has no
        internet" pollution). With the delivery in history the promise reads
        as kept and old task content stays out of new conversations."""
        history = self._history.setdefault(chat_id, deque(maxlen=HISTORY_LEN))
        history.append({"role": "assistant",
                        "content": f"[delivered as a separate notification]\n{text}"})

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

    # ── media (photos / voice) ───────────────────────────────────────────
    def _extract_media(self, msg: dict) -> dict | None:
        """Map a Telegram message to a media descriptor, or None for pure text.

        Returns {"kind": "photo"|"audio", "file_id", "mime", "name"} or
        {"kind": "error", "reason": user-facing text} for media we must
        honestly refuse (too large, unsupported format, too long).
        """
        doc = msg.get("document") or {}
        doc_mime = (doc.get("mime_type") or "").lower()

        if msg.get("photo"):
            # Bot API lists variants smallest→largest; take the largest one
            # that fits the size limit (missing file_size → let the download
            # cap enforce it).
            fits = [p for p in msg["photo"]
                    if p.get("file_id")
                    and (p.get("file_size") or 0) <= IMAGE_MAX_BYTES]
            if not fits:
                return {"kind": "error",
                        "reason": "That image is too large for me — keep it "
                                  f"under {IMAGE_MAX_BYTES // (1024*1024)} MB."}
            best = max(fits, key=lambda p: (p.get("width") or 0) * (p.get("height") or 0))
            return {"kind": "photo", "file_id": best["file_id"],
                    "mime": "image/jpeg", "name": "photo.jpg"}

        if doc_mime.startswith("image/"):
            if doc_mime not in IMAGE_MIMES:
                return {"kind": "error",
                        "reason": "I can't read that image format — send "
                                  "JPEG, PNG, WebP, or GIF."}
            if (doc.get("file_size") or 0) > IMAGE_MAX_BYTES:
                return {"kind": "error",
                        "reason": "That image is too large for me — keep it "
                                  f"under {IMAGE_MAX_BYTES // (1024*1024)} MB."}
            return {"kind": "photo", "file_id": doc["file_id"],
                    "mime": doc_mime,
                    "name": doc.get("file_name") or "image"}

        audio = msg.get("voice") or msg.get("audio") \
            or (doc if doc_mime.startswith("audio/") else None)
        if audio:
            mime = (audio.get("mime_type") or "audio/ogg").lower()
            if mime not in AUDIO_MIMES:
                return {"kind": "error",
                        "reason": "I can't process that audio format — voice "
                                  "notes, OGG, MP3, M4A, or WAV work."}
            if (audio.get("duration") or 0) > AUDIO_MAX_SECONDS:
                return {"kind": "error",
                        "reason": "That recording is too long for me — keep "
                                  f"it under {AUDIO_MAX_SECONDS // 60} minutes."}
            if (audio.get("file_size") or 0) > AUDIO_MAX_BYTES:
                return {"kind": "error",
                        "reason": "That audio file is too large for me — keep "
                                  f"it under {AUDIO_MAX_BYTES // (1024*1024)} MB."}
            name = audio.get("file_name") or ("voice" + _AUDIO_EXT.get(mime, ".ogg"))
            return {"kind": "audio", "file_id": audio["file_id"],
                    "mime": mime, "name": name}

        return None

    async def _download_media(self, file_id: str, max_bytes: int):
        """getFile + download. Returns (bytes, "") or (None, error_code).

        The download URL embeds the bot token — it must never be logged, so
        errors are reported by exception type only.
        """
        info = await self._api("getFile", file_id=file_id)
        if not info or not info.get("file_path"):
            return None, "download_failed"
        if (info.get("file_size") or 0) > max_bytes:
            return None, "too_large"
        try:
            sess = await self.session_factory()
            async with sess.get(
                f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}",
                timeout=aiohttp.ClientTimeout(total=60),
            ) as r:
                if r.status != 200:
                    log.warning(f"Telegram file download failed: HTTP {r.status}")
                    return None, "download_failed"
                data = await r.read()
        except Exception as e:
            log.warning(f"Telegram file download failed: {type(e).__name__}")
            return None, "download_failed"
        if len(data) > max_bytes:
            return None, "too_large"
        return data, ""

    @staticmethod
    def _spool_to_temp(data: bytes, suffix: str) -> str:
        """Write media to a private (0600) temp file; caller unlinks in finally."""
        fd, path = tempfile.mkstemp(prefix="loki_tg_", suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path

    async def _process_photo(self, media: dict) -> tuple[str | None, str]:
        """Download → validate → existing vision pipeline. (description, err)."""
        data, err = await self._download_media(media["file_id"], IMAGE_MAX_BYTES)
        if data is None:
            return None, ("That image is too large for me to pull down."
                          if err == "too_large" else
                          "I couldn't download that image from Telegram — try again.")
        mime = sniff_image_mime(data)
        if mime is None:
            return None, ("That file doesn't look like an image I can read — "
                          "JPEG, PNG, WebP, or GIF only.")
        tmp = None
        try:
            tmp = self._spool_to_temp(data, ".img")
            with open(tmp, "rb") as f:
                img_bytes = f.read()
            log.info(f"Telegram photo → vision ({len(img_bytes)} bytes, {mime})")
            desc = (await self.describe_image(img_bytes, mime) or "").strip()
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if not desc or desc.startswith(("[Could not analyze",
                                        "[Image recognition")):
            return None, ("I got the image but couldn't analyze it — vision "
                          "processing failed on my end.")
        return desc, ""

    async def _process_audio(self, media: dict) -> tuple[str | None, str]:
        """Download → validate → existing Whisper pipeline. (transcript, err)."""
        data, err = await self._download_media(media["file_id"], AUDIO_MAX_BYTES)
        if data is None:
            return None, ("That audio file is too large for me to pull down."
                          if err == "too_large" else
                          "I couldn't download that voice message from "
                          "Telegram — try again.")
        tmp = None
        try:
            tmp = self._spool_to_temp(data, ".audio")
            with open(tmp, "rb") as f:
                audio_bytes = f.read()
            log.info(f"Telegram audio → transcription ({len(audio_bytes)} bytes)")
            transcript = (await self.transcribe_audio(audio_bytes,
                                                      media["name"]) or "").strip()
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        if not transcript:
            return None, ("I couldn't make out that voice message — the "
                          "transcription came back empty. Mind typing it?")
        return transcript, ""

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
        media = self._extract_media(msg)
        if user.get("is_bot") or (not text and media is None):
            return

        # Drop re-delivered updates (auth and media handling must run at
        # most once per message).
        seen_key = (chat_id, msg.get("message_id"))
        if msg.get("message_id") is not None:
            if seen_key in self._seen_msgs:
                return
            self._seen_msgs.append(seen_key)

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
                "Loki here. This is your private line — I can remember "
                "things, search your notes, run the house, check your work "
                "hours, and search the web.")
            return

        await self._api("sendChatAction", chat_id=chat_id, action="typing")

        # Always serious on Telegram; strip a leading -s so muscle memory
        # from Discord doesn't leak into the message text.
        if text.startswith("-s"):
            text = text[2:].strip()

        # Media rides the same brain path as Discord attachments: photos are
        # described by the shared vision pipeline and injected as context;
        # voice notes are transcribed and become the user's message.
        if media and media["kind"] == "error":
            await self.send(chat_id, media["reason"])
            return
        if media and media["kind"] == "photo":
            if not self.describe_image:
                await self.send(chat_id, "I can't see images on this line — "
                                         "vision isn't available right now.")
                return
            desc, err = await self._process_photo(media)
            if desc is None:
                await self.send(chat_id, err)
                return
            if not text:
                text = "Take a look at this image and tell me what you make of it."
            text += f"\n[They sent an image. What you see in it: {desc}]"
        elif media and media["kind"] == "audio":
            if not self.transcribe_audio:
                await self.send(chat_id, "I can't listen to voice messages on "
                                         "this line — transcription isn't "
                                         "available right now.")
                return
            transcript, err = await self._process_audio(media)
            if transcript is None:
                await self.send(chat_id, err)
                return
            text = f"{text}\n{transcript}".strip() if text else transcript

        history = self._history.setdefault(chat_id, deque(maxlen=HISTORY_LEN))

        sys_prompt = TELEGRAM_SYSTEM_PROMPT
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
        # Correlate any background task this turn spawns with the exact
        # originating message (persisted by the task supervisor).
        try:
            ctx.message_id = str(msg.get("message_id") or "")
        except Exception:
            pass
        reply = await self.llm.chat_with_tools(messages, ctx)

        history.append({"role": "user", "content": text})
        history.append({"role": "assistant", "content": reply})
        await self.send(chat_id, reply)

    async def stop(self):
        if self._task:
            self._task.cancel()

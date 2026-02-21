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
=============================================================================
"""

import os
import io
import re
import json
import time
import sqlite3
import asyncio
import logging
import datetime
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv

import discord
from discord.ext import commands
from discord import app_commands

import aiohttp
import google.generativeai as genai

# ─── Load environment variables ───────────────────────────────────────────────
load_dotenv()

DISCORD_TOKEN       = os.getenv("DISCORD_TOKEN")
LLM_PROVIDER        = os.getenv("LLM_PROVIDER", "openai")        # "openai" or "local"
OPENAI_API_KEY       = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL         = os.getenv("OPENAI_MODEL", "gpt-4o")
LOCAL_LLM_URL        = os.getenv("LOCAL_LLM_URL", "http://localhost:1234/v1")
LOCAL_LLM_MODEL      = os.getenv("LOCAL_LLM_MODEL", "local-model")
GEMINI_API_KEY       = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL         = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
SYSTEM_PROMPT        = os.getenv("SYSTEM_PROMPT", "You are Loki, the God of Mischief.")
MEMORY_DB_PATH       = os.getenv("MEMORY_DB_PATH", "loki_memory.db")
CONTEXT_MESSAGE_COUNT = int(os.getenv("CONTEXT_MESSAGE_COUNT", "50"))

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
# Names the bot responds to (case-insensitive matching is done in code)
TRIGGER_NAMES = ["loki", "asshole"]

# Special user & emoji response
SPECIAL_USER_ID = 444718168225611776
SPECIAL_EMOJI   = "👀"
SPECIAL_RESPONSE = (
    "<:emoji_10:1429477074913071174> Sheridan, please!!!"
    "<:emoji_10:1429477074913071174> Those eyes are HELLA DELULU "
    "<:emoji_10:1429477074913071174> It seems you just love pushing "
    "those buttons, huh? <:emoji_10:1429477074913071174>"
)


# =============================================================================
#  DATABASE — Persistent Memory
# =============================================================================
class MemoryDB:
    """SQLite-backed persistent memory for conversation history."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self._create_tables()
        log.info(f"Memory database loaded from {db_path}")

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
            CREATE TABLE IF NOT EXISTS personality_additions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    TEXT,
                addition    TEXT,
                added_by    TEXT,
                timestamp   TEXT
            )
        """)
        self.conn.commit()

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
            datetime.datetime.utcnow().isoformat()
        ))
        self.conn.commit()

    def get_recent_messages(self, channel_id, limit=50):
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT username, role, content, image_desc, timestamp
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
            datetime.datetime.utcnow().isoformat()
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
            datetime.datetime.utcnow().isoformat()
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

    # ── Personality Additions (append without replacing) ──────────────────
    def add_personality_addition(self, guild_id, addition, added_by):
        cursor = self.conn.cursor()
        cursor.execute("""
            INSERT INTO personality_additions
                (guild_id, addition, added_by, timestamp)
            VALUES (?, ?, ?, ?)
        """, (
            str(guild_id), addition, added_by,
            datetime.datetime.utcnow().isoformat()
        ))
        self.conn.commit()
        return cursor.lastrowid

    def get_personality_additions(self, guild_id):
        """Returns list of (id, addition, added_by, timestamp)."""
        cursor = self.conn.cursor()
        cursor.execute("""
            SELECT id, addition, added_by, timestamp
            FROM personality_additions
            WHERE guild_id = ?
            ORDER BY id ASC
        """, (str(guild_id),))
        return cursor.fetchall()

    def remove_personality_addition(self, addition_id, guild_id):
        """Remove a specific addition by ID. Returns True if something was deleted."""
        cursor = self.conn.cursor()
        cursor.execute(
            "DELETE FROM personality_additions WHERE id = ? AND guild_id = ?",
            (addition_id, str(guild_id))
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def clear_personality_additions(self, guild_id):
        cursor = self.conn.cursor()
        cursor.execute(
            "DELETE FROM personality_additions WHERE guild_id = ?",
            (str(guild_id),)
        )
        self.conn.commit()


# =============================================================================
#  IMAGE RECOGNITION — Google Gemini (Free Tier)
# =============================================================================
class VisionHandler:
    """Handles image/GIF analysis using Google Gemini's free API.
    Includes rate limiting to avoid burning through the free tier quota."""

    def __init__(self, api_key: str):
        if api_key:
            genai.configure(api_key=api_key)
            # Use 1.5 Flash by default — more generous free tier (15 RPM vs 10 RPM)
            # Configurable via GEMINI_MODEL in .env
            self.model = genai.GenerativeModel(GEMINI_MODEL)
            self.enabled = True
            log.info(f"Gemini vision initialized ({GEMINI_MODEL})")
        else:
            self.enabled = False
            log.warning("No GEMINI_API_KEY — image recognition disabled")

        # Rate limiting: max 8 requests/minute, 200/hour to stay safe
        self._minute_requests: list[float] = []
        self._hour_requests: list[float] = []
        self.MAX_PER_MINUTE = 8
        self.MAX_PER_HOUR = 200

    def _rate_limit_ok(self) -> bool:
        """Check if we're within rate limits. Returns True if safe to proceed."""
        now = time.time()
        # Clean old entries
        self._minute_requests = [t for t in self._minute_requests if now - t < 60]
        self._hour_requests = [t for t in self._hour_requests if now - t < 3600]

        if len(self._minute_requests) >= self.MAX_PER_MINUTE:
            log.warning("Gemini rate limit: per-minute cap reached, skipping image")
            return False
        if len(self._hour_requests) >= self.MAX_PER_HOUR:
            log.warning("Gemini rate limit: per-hour cap reached, skipping image")
            return False

        self._minute_requests.append(now)
        self._hour_requests.append(now)
        return True

    async def describe_image(self, image_bytes: bytes, mime_type: str,
                             context: str = "") -> str:
        """Send image to Gemini and get a description."""
        if not self.enabled:
            return "[Image recognition not available]"

        if not self._rate_limit_ok():
            return "[Image skipped — rate limit reached, will resume shortly]"

        try:
            prompt = (
                "Describe this image in detail. What do you see? "
                "Include people's expressions, actions, text, memes, "
                "or anything notable. Be concise but thorough."
            )
            if context:
                prompt += f"\nConversation context: {context}"

            image_part = {
                "mime_type": mime_type,
                "data": image_bytes
            }

            # Run the blocking Gemini call in a thread so we don't block the
            # Discord event loop
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self.model.generate_content([prompt, image_part])
            )
            return response.text.strip()
        except Exception as e:
            log.error(f"Gemini vision error: {e}")
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

    async def chat(self, messages: list[dict]) -> str:
        """Send a chat-completion request and return the assistant reply."""
        headers = {
            "Content-Type": "application/json",
        }
        if LLM_PROVIDER == "openai":
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": 1024,
            "temperature": 0.85,
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.api_url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=120)
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        log.error(f"LLM API error {resp.status}: {text}")
                        return "Hmm, my silver tongue seems tied at the moment. Try again shortly."
                    data = await resp.json()
                    return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.error(f"LLM request failed: {e}")
            return "My connection to the realms faltered. Give me a moment."


# =============================================================================
#  VOICE HANDLER — TTS with male voice
# =============================================================================
class VoiceHandler:
    """Handles joining/leaving voice channels and speaking with edge-tts."""

    def __init__(self):
        self.voice_clients: dict[int, discord.VoiceClient] = {}

    async def join(self, voice_channel: discord.VoiceChannel) -> discord.VoiceClient:
        guild_id = voice_channel.guild.id

        # Clean up stale connection if one exists
        if guild_id in self.voice_clients:
            old_vc = self.voice_clients[guild_id]
            if old_vc.is_connected():
                # Already connected — just move to the new channel
                await old_vc.move_to(voice_channel)
                return old_vc
            else:
                # Stale/dead connection — clean it up
                try:
                    await old_vc.disconnect(force=True)
                except Exception:
                    pass
                del self.voice_clients[guild_id]

        # Also check if the bot is somehow connected via guild.voice_client
        # (this catches connections that survived a restart or weren't tracked)
        if voice_channel.guild.voice_client:
            try:
                await voice_channel.guild.voice_client.disconnect(force=True)
            except Exception:
                pass

        try:
            vc = await voice_channel.connect(timeout=30.0, reconnect=True)
            self.voice_clients[guild_id] = vc
            return vc
        except discord.ClientException as e:
            # "Already connected" edge case — force disconnect and retry
            log.warning(f"Voice connect ClientException: {e} — forcing reconnect")
            if voice_channel.guild.voice_client:
                await voice_channel.guild.voice_client.disconnect(force=True)
            vc = await voice_channel.connect(timeout=30.0, reconnect=True)
            self.voice_clients[guild_id] = vc
            return vc

    async def leave(self, guild_id: int):
        if guild_id in self.voice_clients:
            try:
                await self.voice_clients[guild_id].disconnect(force=True)
            except Exception:
                pass
            del self.voice_clients[guild_id]

    async def speak(self, vc: discord.VoiceClient, text: str):
        """Generate TTS audio with a male voice and play it."""
        try:
            import edge_tts

            # Male voice options (pick one):
            #   en-US-GuyNeural      — American male
            #   en-GB-RyanNeural     — British male
            #   en-US-ChristopherNeural — deeper American male
            voice = "en-US-GuyNeural"

            communicate = edge_tts.Communicate(text, voice)
            audio_path = "/tmp/loki_tts.mp3"
            await communicate.save(audio_path)

            if not vc.is_connected():
                log.error("Voice client disconnected before TTS could play")
                return

            if vc.is_playing():
                vc.stop()

            source = discord.FFmpegPCMAudio(audio_path)
            vc.play(source)

            # Wait for audio to finish
            while vc.is_playing():
                await asyncio.sleep(0.5)

        except ImportError:
            log.error("edge-tts not installed — voice TTS unavailable")
        except Exception as e:
            log.error(f"TTS error: {e}")


# =============================================================================
#  THE BOT
# =============================================================================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Instantiate helpers
memory  = MemoryDB(MEMORY_DB_PATH)
vision  = VisionHandler(GEMINI_API_KEY)
llm     = LLMHandler()
voice_h = VoiceHandler()


def is_trigger(text: str) -> bool:
    """Check if the message mentions one of Loki's trigger names."""
    lower = text.lower()
    for name in TRIGGER_NAMES:
        # Match the name as a whole word (not part of another word)
        if re.search(rf'\b{re.escape(name)}\b', lower):
            return True
    return False


def build_llm_messages(channel_id, guild_id=None, extra_user_msg: str = "") -> list[dict]:
    """Build the messages list for the LLM, including system prompt,
    recent memory, and any extra user message.
    Uses per-server personality if one is set, otherwise falls back to
    the default SYSTEM_PROMPT from .env."""

    # Check for a server-specific personality first
    prompt = SYSTEM_PROMPT
    if guild_id:
        server_personality = memory.get_server_personality(guild_id)
        if server_personality:
            prompt = server_personality[0]  # (prompt, set_by, timestamp)

    # Append any personality additions for this server
    if guild_id:
        additions = memory.get_personality_additions(guild_id)
        if additions:
            extra_traits = "\n\nAdditional personality traits and instructions:\n"
            extra_traits += "\n".join(f"- {a[1]}" for a in additions)
            prompt += extra_traits

    msgs = [{"role": "system", "content": prompt}]

    # Add the latest summary for long-term context
    summary = memory.get_latest_summary(channel_id)
    if summary:
        msgs.append({
            "role": "system",
            "content": f"[Previous conversation summary]: {summary[0]}"
        })

    # Add recent conversation history
    recent = memory.get_recent_messages(channel_id, CONTEXT_MESSAGE_COUNT)
    for username, role, content, image_desc, ts in recent:
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


# ─── Events ───────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    log.info(f"✅  Loki is online as {bot.user} (ID: {bot.user.id})")
    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} slash commands")
    except Exception as e:
        log.error(f"Slash command sync error: {e}")


@bot.event
async def on_message(message: discord.Message):
    # Never reply to ourselves
    if message.author.id == bot.user.id:
        return

    # ── Special Emoji Response for Sheridan ────────────────────────────────
    if (message.author.id == SPECIAL_USER_ID
            and SPECIAL_EMOJI in message.content):
        await message.channel.send(SPECIAL_RESPONSE)
        # Don't return — still process the rest in case she also said "Loki"

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
                    async with aiohttp.ClientSession() as session:
                        async with session.get(img_url) as resp:
                            if resp.status == 200:
                                img_bytes = await resp.read()
                                ct = resp.headers.get("Content-Type", "image/png")
                                desc = await vision.describe_image(img_bytes, ct)
                                image_desc += desc + " "
                except Exception as e:
                    log.error(f"Error fetching embed image: {e}")

    image_desc = image_desc.strip()

    # ── Check if bot should respond ───────────────────────────────────────
    should_respond = False

    # 1. Direct mention / reply
    if bot.user.mentioned_in(message):
        should_respond = True

    # 2. Name trigger
    if is_trigger(message.content):
        should_respond = True

    # 3. Reply to one of the bot's messages
    if (message.reference and message.reference.resolved
            and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author.id == bot.user.id):
        should_respond = True

    # ── Store every message in memory (for context) ───────────────────────
    # NOTE: We store AFTER the trigger check but BEFORE calling the LLM,
    # so it's in the DB when build_llm_messages pulls recent history.
    # We do NOT pass it again as extra_user_msg to avoid duplication.
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

    if not should_respond:
        await bot.process_commands(message)
        return

    # ── Build prompt & get reply ──────────────────────────────────────────
    async with message.channel.typing():
        # The user's message is already in the DB from the store above,
        # so build_llm_messages will pick it up — no extra_user_msg needed.
        messages_for_llm = build_llm_messages(
            message.channel.id,
            guild_id=message.guild.id if message.guild else None,
        )
        reply = await llm.chat(messages_for_llm)

    # Store the bot's reply in memory
    memory.store_message(
        guild_id=message.guild.id if message.guild else 0,
        channel_id=message.channel.id,
        user_id=bot.user.id,
        username="Loki",
        role="assistant",
        content=reply,
    )

    # Discord has a 2000 char limit — split if needed
    if len(reply) <= 2000:
        await message.reply(reply, mention_author=False)
    else:
        chunks = [reply[i:i+2000] for i in range(0, len(reply), 2000)]
        for i, chunk in enumerate(chunks):
            if i == 0:
                await message.reply(chunk, mention_author=False)
            else:
                await message.channel.send(chunk)

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
        f"[{ts}] {username}: {content}"
        + (f" [Image: {img}]" if img else "")
        for username, role, content, img, ts in recent
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

    # Store summary for future context
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
            f"Couldn't join the voice channel: `{e}`\n"
            "Make sure I have **Connect** and **Speak** permissions.",
            ephemeral=True
        )


@bot.tree.command(name="loki_leave", description="Loki leaves the voice channel")
async def loki_leave(interaction: discord.Interaction):
    guild_id = interaction.guild.id

    # Check both our tracked dict AND Discord's own voice_client
    if guild_id in voice_h.voice_clients or interaction.guild.voice_client:
        await voice_h.leave(guild_id)
        # Fallback: disconnect guild voice_client if it wasn't tracked
        if interaction.guild.voice_client:
            try:
                await interaction.guild.voice_client.disconnect(force=True)
            except Exception:
                pass
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

    # Get Loki's reply
    messages_for_llm = build_llm_messages(
        interaction.channel.id,
        guild_id=interaction.guild.id if interaction.guild else None,
        extra_user_msg=f"[{interaction.user.display_name}]: {question}"
    )
    reply = await llm.chat(messages_for_llm)

    # Store in memory
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

    # Speak it
    vc = voice_h.voice_clients[guild_id]
    await voice_h.speak(vc, reply)
    await interaction.followup.send(f"**Q:** {question}\n**Loki:** {reply}")


@bot.tree.command(name="loki_reset", description="Clear Loki's memory for this channel")
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

    # Show a preview (truncated if long)
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
    description="View the current personality prompt for this server"
)
async def view_personality(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("This only works in a server!", ephemeral=True)
        return

    server_data = memory.get_server_personality(interaction.guild.id)

    if server_data:
        prompt, set_by, timestamp = server_data
        # Truncate for display if needed (Discord embed limit)
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
        # Show the default
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


@set_personality.error
@reset_personality.error
async def personality_permission_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "⛔ Only server admins can change the personality.", ephemeral=True
        )


# ─── Personality Addition Commands ────────────────────────────────────────────

@bot.tree.command(
    name="add_trait",
    description="Add a new trait/instruction to the personality without replacing it (Admin only)"
)
@app_commands.describe(trait="The new trait, behavior, or instruction to add")
@app_commands.checks.has_permissions(administrator=True)
async def add_trait(interaction: discord.Interaction, trait: str):
    if not interaction.guild:
        await interaction.response.send_message("This only works in a server!", ephemeral=True)
        return

    row_id = memory.add_personality_addition(
        guild_id=interaction.guild.id,
        addition=trait,
        added_by=interaction.user.display_name
    )

    # Count total additions
    all_additions = memory.get_personality_additions(interaction.guild.id)
    count = len(all_additions)

    embed = discord.Embed(
        title="✅ Personality Trait Added",
        description=f"**#{count}:** {trait}",
        color=discord.Color.green()
    )
    embed.set_footer(text=f"Added by {interaction.user.display_name} • Use /view_traits to see all • /remove_trait to remove")
    await interaction.response.send_message(embed=embed)
    log.info(f"Personality trait added for guild {interaction.guild.id}: {trait[:80]}...")


@bot.tree.command(
    name="view_traits",
    description="View all personality additions for this server (Admin only)"
)
@app_commands.checks.has_permissions(administrator=True)
async def view_traits(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("This only works in a server!", ephemeral=True)
        return

    additions = memory.get_personality_additions(interaction.guild.id)

    if not additions:
        await interaction.response.send_message(
            "No personality additions yet. Use `/add_trait` to add some!",
            ephemeral=True
        )
        return

    lines = []
    for i, (row_id, addition, added_by, timestamp) in enumerate(additions, 1):
        date = timestamp[:10] if timestamp else "?"
        # Truncate long additions for the list view
        preview = addition[:150] + "..." if len(addition) > 150 else addition
        lines.append(f"**{i}.** (ID: {row_id}) {preview}\n   *— added by {added_by} on {date}*")

    description = "\n\n".join(lines)
    # Truncate if it exceeds embed limits
    if len(description) > 3900:
        description = description[:3900] + "\n\n*...truncated. Too many additions to display.*"

    embed = discord.Embed(
        title=f"🎭 Personality Additions ({len(additions)} total)",
        description=description,
        color=discord.Color.purple()
    )
    embed.set_footer(text="Use /remove_trait <ID number> to remove one, or /clear_traits to remove all")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="remove_trait",
    description="Remove a specific personality addition by its ID (Admin only)"
)
@app_commands.describe(trait_id="The ID number shown in /view_traits (the number after 'ID:')")
@app_commands.checks.has_permissions(administrator=True)
async def remove_trait(interaction: discord.Interaction, trait_id: int):
    if not interaction.guild:
        await interaction.response.send_message("This only works in a server!", ephemeral=True)
        return

    removed = memory.remove_personality_addition(trait_id, interaction.guild.id)

    if removed:
        await interaction.response.send_message(f"🗑️ Trait #{trait_id} removed.")
    else:
        await interaction.response.send_message(
            f"Couldn't find trait with ID {trait_id} in this server. "
            f"Use `/view_traits` to see current IDs.",
            ephemeral=True
        )


@bot.tree.command(
    name="clear_traits",
    description="Remove ALL personality additions for this server (Admin only)"
)
@app_commands.checks.has_permissions(administrator=True)
async def clear_traits(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("This only works in a server!", ephemeral=True)
        return

    additions = memory.get_personality_additions(interaction.guild.id)
    if not additions:
        await interaction.response.send_message(
            "No personality additions to clear.", ephemeral=True
        )
        return

    memory.clear_personality_additions(interaction.guild.id)
    await interaction.response.send_message(
        f"🧹 Cleared {len(additions)} personality addition(s). Back to base prompt only."
    )


@add_trait.error
@view_traits.error
@remove_trait.error
@clear_traits.error
async def trait_permission_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "⛔ Only server admins can modify personality traits.", ephemeral=True
        )


# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN not set in .env file!")
        exit(1)
    bot.run(DISCORD_TOKEN)

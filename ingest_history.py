"""
ingest_history.py — Discord history ingestion into ChromaDB as conversation chunks.

Run from the bot directory:
    venv/bin/python ingest_history.py [--model NAME] [--collection NAME] [--full]

v2 (Phase 1.2): instead of one embedding per message, consecutive messages in a
channel are grouped into "exchange" chunks. A chunk closes when:
  - the gap to the next message exceeds GAP_MINUTES, or
  - it reaches MAX_MESSAGES messages, or
  - it reaches ~MAX_CHARS characters.

Supports incremental updates — re-running only ingests messages after the last
chunk boundary recorded per channel in PROGRESS_FILE.
"""

import os
import sys
import json
import argparse
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("Ingest")

import discord
import chromadb
from sentence_transformers import SentenceTransformer

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHROMADB_HOST = os.getenv("CHROMADB_HOST", "localhost")
CHROMADB_PORT = int(os.getenv("CHROMADB_PORT", "8100"))

DEFAULT_MODEL      = os.getenv("RAG_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
DEFAULT_COLLECTION = os.getenv("RAG_COLLECTION", "discord_chunks")
PROGRESS_FILE      = "ingest_progress.json"   # last ingested message ID per channel, per collection

GAP_MINUTES  = 25     # silence longer than this starts a new chunk
MAX_MESSAGES = 15     # max messages per chunk
MAX_CHARS    = 1500   # soft cap on chunk text length
EMBED_BATCH  = 64     # chunks embedded + added to ChromaDB per flush


def load_progress() -> dict:
    if Path(PROGRESS_FILE).exists():
        with open(PROGRESS_FILE) as f:
            data = json.load(f)
        # v1 progress files mapped channel_id -> message_id at the top level.
        # v2 namespaces by collection so different models can coexist.
        if data and all(isinstance(v, str) for v in data.values()):
            return {}
        return data
    return {}


def save_progress(progress: dict):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


class ChunkBuilder:
    """Accumulates messages and yields closed chunks."""

    def __init__(self, guild, channel):
        self.guild = guild
        self.channel = channel
        self.messages = []

    def _should_close(self, message) -> bool:
        if not self.messages:
            return False
        gap = (message.created_at - self.messages[-1].created_at).total_seconds()
        if gap > GAP_MINUTES * 60:
            return True
        if len(self.messages) >= MAX_MESSAGES:
            return True
        if sum(len(m.content) for m in self.messages) >= MAX_CHARS:
            return True
        return False

    def add(self, message):
        """Add a message; returns a closed chunk dict if one was completed, else None."""
        closed = None
        if self._should_close(message):
            closed = self.close()
        self.messages.append(message)
        return closed

    def close(self):
        """Close and return the current chunk (or None if empty)."""
        if not self.messages:
            return None
        msgs = self.messages
        self.messages = []

        text = "\n".join(f"{m.author.display_name}: {m.content}" for m in msgs)
        authors  = list(dict.fromkeys(m.author.display_name for m in msgs))
        user_ids = list(dict.fromkeys(str(m.author.id) for m in msgs))
        first, last = msgs[0], msgs[-1]

        return {
            "id": f"chunk-{first.id}",
            "text": text,
            "metadata": {
                "guild":            self.guild.name,
                "guild_id":         str(self.guild.id),
                "channel":          self.channel.name,
                "channel_id":       str(self.channel.id),
                "authors":          ", ".join(authors),
                "user_ids":         ",".join(user_ids),
                "message_ids":      ",".join(str(m.id) for m in msgs),
                "first_message_id": str(first.id),
                "last_message_id":  str(last.id),
                "ts_start":         first.created_at.isoformat(),
                "ts_end":           last.created_at.isoformat(),
                "ts_start_unix":    first.created_at.timestamp(),
                "ts_end_unix":      last.created_at.timestamp(),
                "n_messages":       len(msgs),
            },
        }


async def ingest(model_name: str, collection_name: str, full: bool):
    log.info(f"Loading embedding model {model_name}...")
    model = SentenceTransformer(model_name)
    log.info("Model loaded.")

    log.info(f"Connecting to ChromaDB at {CHROMADB_HOST}:{CHROMADB_PORT}...")
    chroma = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
    collection = chroma.get_or_create_collection(
        collection_name, metadata={"hnsw:space": "cosine", "embed_model": model_name}
    )
    log.info(f"Collection '{collection_name}' ready. Currently {collection.count()} chunks.")

    progress = {} if full else load_progress()
    coll_progress = progress.setdefault(collection_name, {})

    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.messages = True
    client = discord.Client(intents=intents)

    def flush(batch):
        if not batch:
            return
        embeddings = model.encode(
            [c["text"] for c in batch], normalize_embeddings=True, show_progress_bar=False
        ).tolist()
        # upsert: re-runs re-read the previous run's trailing chunk (same
        # deterministic id) and replace it with the grown version
        collection.upsert(
            ids=[c["id"] for c in batch],
            documents=[c["text"] for c in batch],
            embeddings=embeddings,
            metadatas=[c["metadata"] for c in batch],
        )

    @client.event
    async def on_ready():
        log.info(f"Logged in as {client.user}")
        total_chunks = 0

        try:
            for guild in client.guilds:
                log.info(f"Processing guild: {guild.name}")

                for channel in guild.text_channels:
                    perms = channel.permissions_for(guild.me)
                    if not perms.read_messages or not perms.read_message_history:
                        log.info(f"  Skipping #{channel.name} (no permission)")
                        continue

                    channel_key = str(channel.id)
                    after_msg = None
                    after_id = coll_progress.get(channel_key)
                    if after_id:
                        after_msg = discord.Object(id=int(after_id))
                        log.info(f"  #{channel.name} — resuming after message {after_id}")
                    else:
                        log.info(f"  #{channel.name} — starting full ingest")

                    builder = ChunkBuilder(guild, channel)
                    batch = []
                    channel_chunks = 0

                    async for message in channel.history(
                        limit=None, oldest_first=True, after=after_msg
                    ):
                        if not message.content or message.author.bot:
                            continue
                        closed = builder.add(message)
                        if closed:
                            batch.append(closed)
                        if len(batch) >= EMBED_BATCH:
                            flush(batch)
                            channel_chunks += len(batch)
                            # progress = last message of last *closed* chunk; the
                            # open builder's messages get re-read next run
                            coll_progress[channel_key] = batch[-1]["metadata"]["last_message_id"]
                            save_progress(progress)
                            log.info(f"    {channel_chunks} chunks — collection total {collection.count()}")
                            batch = []

                    # Flush remaining closed chunks, then the trailing (still-open)
                    # chunk so current conversations are searchable. Progress only
                    # advances past CLOSED chunks — the tail's messages get re-read
                    # next run and the grown chunk replaces it via upsert.
                    if batch:
                        coll_progress[channel_key] = batch[-1]["metadata"]["last_message_id"]
                    tail = builder.close()
                    if tail:
                        batch.append(tail)
                    if batch:
                        flush(batch)
                        channel_chunks += len(batch)
                        save_progress(progress)

                    if channel_chunks:
                        total_chunks += channel_chunks
                        log.info(f"  #{channel.name} — {channel_chunks} chunks indexed")

            log.info(f"✅ Done. {total_chunks} new chunks. Collection total: {collection.count()}")
        finally:
            await client.close()

    await client.start(DISCORD_TOKEN)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--collection", default=DEFAULT_COLLECTION)
    ap.add_argument("--full", action="store_true", help="ignore saved progress, re-read all history")
    args = ap.parse_args()
    asyncio.run(ingest(args.model, args.collection, args.full))

"""
ingest_history.py — One-time (and incremental) Discord history ingestion into ChromaDB.

Run manually from ~/misst-bot/:
    source venv/bin/activate
    python ingest_history.py

Reads all messages from all accessible channels and indexes them into ChromaDB
so Miss-T (and Loki) can search through full server history.

Supports incremental updates — re-running only ingests new messages.
"""

import os
import sys
import json
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("Ingest")

# ── Add shared modules to path ────────────────────────────────────────────────
sys.path.insert(0, os.path.expanduser("~/bot-shared-modules"))

import discord
import chromadb
from sentence_transformers import SentenceTransformer

DISCORD_TOKEN   = os.getenv("DISCORD_TOKEN")
CHROMADB_HOST   = os.getenv("CHROMADB_HOST", "localhost")
CHROMADB_PORT   = int(os.getenv("CHROMADB_PORT", "8100"))
COLLECTION_NAME = "discord_history"
PROGRESS_FILE   = "ingest_progress.json"   # tracks last ingested message ID per channel
BATCH_SIZE      = 500                       # messages per progress save

# ── Load progress (for incremental updates) ───────────────────────────────────
def load_progress() -> dict:
    if Path(PROGRESS_FILE).exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {}

def save_progress(progress: dict):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)

# ── Main ingestion ─────────────────────────────────────────────────────────────
async def ingest():
    log.info("Loading embedding model...")
    model = SentenceTransformer("all-MiniLM-L6-v2")
    log.info("Model loaded.")

    log.info(f"Connecting to ChromaDB at {CHROMADB_HOST}:{CHROMADB_PORT}...")
    chroma = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
    collection = chroma.get_or_create_collection(COLLECTION_NAME)
    log.info(f"Collection ready. Currently {collection.count()} messages indexed.")

    progress = load_progress()

    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    intents.messages = True

    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        log.info(f"Logged in as {client.user}")
        total_ingested = 0

        for guild in client.guilds:
            log.info(f"Processing guild: {guild.name}")

            for channel in guild.text_channels:
                # Check permissions
                perms = channel.permissions_for(guild.me)
                if not perms.read_messages or not perms.read_message_history:
                    log.info(f"  Skipping #{channel.name} (no permission)")
                    continue

                channel_key = str(channel.id)
                after_id = progress.get(channel_key)
                after_msg = None

                if after_id:
                    # Fetch the message object to use as 'after' anchor
                    try:
                        after_msg = await channel.fetch_message(int(after_id))
                        log.info(f"  #{channel.name} — resuming after message {after_id}")
                    except Exception:
                        log.info(f"  #{channel.name} — couldn't fetch anchor, starting fresh")
                        after_msg = None
                else:
                    log.info(f"  #{channel.name} — starting full ingest")

                channel_count = 0
                batch_docs = []
                batch_embeddings = []
                batch_metadatas = []
                batch_ids = []

                async for message in channel.history(
                    limit=None,
                    oldest_first=True,
                    after=after_msg
                ):
                    # Skip empty messages, bot messages, and system messages
                    if not message.content or message.author.bot:
                        continue

                    text = f"{message.author.display_name}: {message.content}"
                    msg_id = str(message.id)

                    # Skip if already indexed
                    existing = collection.get(ids=[msg_id])
                    if existing["ids"]:
                        continue

                    embedding = model.encode(text).tolist()

                    batch_docs.append(text)
                    batch_embeddings.append(embedding)
                    batch_metadatas.append({
                        "author": message.author.display_name,
                        "author_id": str(message.author.id),
                        "timestamp": message.created_at.isoformat(),
                        "timestamp_unix": message.created_at.timestamp(),
                        "channel": channel.name,
                        "guild": guild.name,
                        "guild_id": str(guild.id),
                        "channel_id": str(channel.id),
                        "message_id": msg_id,
                    })
                    batch_ids.append(msg_id)
                    channel_count += 1
                    total_ingested += 1

                    # Flush batch
                    if len(batch_ids) >= BATCH_SIZE:
                        collection.add(
                            documents=batch_docs,
                            embeddings=batch_embeddings,
                            metadatas=batch_metadatas,
                            ids=batch_ids
                        )
                        progress[channel_key] = batch_ids[-1]
                        save_progress(progress)
                        log.info(f"    Saved batch of {len(batch_ids)} — total {collection.count()}")
                        batch_docs, batch_embeddings, batch_metadatas, batch_ids = [], [], [], []

                # Flush remaining
                if batch_ids:
                    collection.add(
                        documents=batch_docs,
                        embeddings=batch_embeddings,
                        metadatas=batch_metadatas,
                        ids=batch_ids
                    )
                    progress[channel_key] = batch_ids[-1]
                    save_progress(progress)

                if channel_count > 0:
                    log.info(f"  #{channel.name} — indexed {channel_count} messages")

        log.info(f"\n✅ Done! Ingested {total_ingested} new messages.")
        log.info(f"   Total in ChromaDB: {collection.count()}")
        await client.close()

    await client.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(ingest())

"""
rag_search.py — RAG (Retrieval-Augmented Generation) module for Discord history search.
Place in ~/bot-shared-modules/ and import from both bots.

Uses ChromaDB (HTTP) for vector storage and sentence-transformers for local embeddings.
"""

import logging
import os

log = logging.getLogger("RAGSearch")

CHROMADB_HOST = os.getenv("CHROMADB_HOST", "localhost")
CHROMADB_PORT = int(os.getenv("CHROMADB_PORT", "8100"))
COLLECTION_NAME = "discord_history"

# Distance threshold: ChromaDB uses L2 distance with normalized embeddings.
# 0.0 = identical, ~1.0 = unrelated.
MAX_DISTANCE = 1.0           # default (no time window) — semantic must be reasonably close
MAX_DISTANCE_WINDOWED = 1.5  # when a time window is active — temporal filter does the heavy work,
                              # so we allow looser semantic matches (casual phrases like "pulled up",
                              # "on my way", "running late" won't embed close to "who came to work")

# Lazy-loaded singletons
_model = None
_client = None
_collection = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        log.info("Loading sentence-transformers model (first use)...")
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        log.info("Embedding model loaded.")
    return _model


def _get_collection():
    global _client, _collection
    if _collection is None:
        import chromadb
        _client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
        _collection = _client.get_or_create_collection(COLLECTION_NAME)
    return _collection


def is_available() -> bool:
    """Check if ChromaDB is reachable."""
    try:
        _get_collection()
        return True
    except Exception:
        return False


def get_message_count() -> int:
    """Return how many messages are indexed."""
    try:
        return _get_collection().count()
    except Exception:
        return 0


def _to_unix(dt) -> float:
    """Convert a timezone-aware datetime to a Unix timestamp (float) for numeric filtering."""
    import datetime as _dt
    return dt.astimezone(_dt.timezone.utc).timestamp()


def search_history(query: str, n_results: int = 20, guild_name: str = None,
                   since_dt=None, until_dt=None) -> list:
    """Search Discord history for messages relevant to the query.

    Args:
        query:       The search query text.
        n_results:   Max results to return.
        guild_name:  Restrict to this guild only (prevents cross-server leaks).
        since_dt:    Timezone-aware datetime — pre-filter to messages at or after this.
        until_dt:    Timezone-aware datetime — pre-filter to messages at or before this.

    When a time window is provided, temporal filtering happens FIRST inside ChromaDB
    via the where clause — so semantic ranking only operates over messages in that
    window. This is critical for recency-sensitive queries ("who showed up last week")
    where casual presence phrases ("pulled up", "on my way", "running late") need to
    be found within the right date range rather than competing against older messages.
    The distance threshold is also relaxed for windowed searches since the temporal
    filter already provides the primary signal.

    Returns empty list if no results pass all filters.
    """
    try:
        model = _get_model()
        collection = _get_collection()

        count = collection.count()
        if count == 0:
            return []

        query_embedding = model.encode(query).tolist()

        # ── Build ChromaDB where clause ──────────────────────────────────────
        # Temporal pre-filtering via timestamp_unix (float) — ChromaDB only
        # supports $gte/$lte on numeric fields, not strings.
        conditions = []
        if guild_name:
            conditions.append({"guild": {"$eq": guild_name}})
        if since_dt:
            conditions.append({"timestamp_unix": {"$gte": _to_unix(since_dt)}})
        if until_dt:
            conditions.append({"timestamp_unix": {"$lte": _to_unix(until_dt)}})

        if len(conditions) == 0:
            where = None
        elif len(conditions) == 1:
            where = conditions[0]
        else:
            where = {"$and": conditions}

        # Use a looser distance threshold when temporal context does the filtering
        dist_threshold = MAX_DISTANCE_WINDOWED if (since_dt or until_dt) else MAX_DISTANCE

        query_kwargs = dict(
            query_embeddings=[query_embedding],
            n_results=min(n_results, count),
            include=["documents", "metadatas", "distances"]
        )
        if where:
            query_kwargs["where"] = where

        results = collection.query(**query_kwargs)

        if not results["documents"] or not results["documents"][0]:
            return []

        hits = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0]
        ):
            if dist > dist_threshold:
                continue

            # Build Discord jump link
            guild_id   = meta.get("guild_id", "")
            channel_id = meta.get("channel_id", "")
            message_id = meta.get("message_id", "")
            jump_link  = (
                f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
                if guild_id and channel_id and message_id else ""
            )
            hits.append({
                "text":      doc,
                "author":    meta.get("author", "Unknown"),
                "timestamp": meta.get("timestamp", ""),
                "channel":   meta.get("channel", ""),
                "guild":     meta.get("guild", ""),
                "jump_link": jump_link,
                "distance":  dist
            })

        hits.sort(key=lambda x: x["timestamp"])
        return hits

    except Exception as e:
        log.error(f"RAG search error: {e}")
        return []


def format_for_context(hits: list) -> str:
    """Format search results into a readable context block for the LLM.

    Each entry is formatted as a clearly labelled block so the LLM copies
    the exact jump link rather than constructing its own partial URL.
    """
    if not hits:
        return ""
    import datetime
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")

    blocks = ["[Relevant past Discord messages from server history — cite these when answering:]"]
    for h in hits:
        # Parse timestamp → human-readable ET date string
        try:
            dt = datetime.datetime.fromisoformat(h["timestamp"])
            dt_et = dt.astimezone(ET)
            ts_human = dt_et.strftime("%b %-d %Y at %-I:%M %p ET")
        except Exception:
            ts_human = h["timestamp"][:16].replace("T", " ") if h["timestamp"] else "unknown time"

        # Strip duplicate "Author: " prefix that ingest stored in the document text
        text = h["text"]
        author_prefix = h["author"] + ": "
        if text.startswith(author_prefix):
            text = text[len(author_prefix):]

        block = f'  Author: {h["author"]}\n  Date: {ts_human}\n  Channel: #{h["channel"]}\n  Message: "{text}"'
        if h.get("jump_link"):
            block += f'\n  Jump link (copy this EXACTLY — do not shorten it): {h["jump_link"]}'
        blocks.append(block)

    return "\n\n".join(blocks)

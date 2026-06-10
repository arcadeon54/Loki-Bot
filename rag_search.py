"""
rag_search.py — RAG (Retrieval-Augmented Generation) module for Discord history search.

v2 (Phase 1.2): searches conversation *chunks* (multi-message exchanges) instead of
single messages. Chunks are produced by ingest_history.py and stored in ChromaDB
with cosine distance. Each chunk carries full metadata: participants, user IDs,
message IDs, channel/guild IDs, and a start/end timestamp range.

Uses ChromaDB (HTTP) for vector storage and sentence-transformers for local embeddings.
"""

import logging
import os

log = logging.getLogger("RAGSearch")

CHROMADB_HOST   = os.getenv("CHROMADB_HOST", "localhost")
CHROMADB_PORT   = int(os.getenv("CHROMADB_PORT", "8100"))
COLLECTION_NAME = os.getenv("RAG_COLLECTION", "discord_chunks")

# Embedding model. Must match what ingest_history.py used for the collection.
# bge models want a query-side instruction prefix; passage side needs none.
EMBED_MODEL  = os.getenv("RAG_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
QUERY_PREFIX = (
    "Represent this sentence for searching relevant passages: "
    if "bge" in EMBED_MODEL else ""
)

# Cosine distance thresholds: 0.0 = identical, 1.0 = orthogonal.
# Tuned with eval_rag.py — see that script's distance report before changing.
MAX_DISTANCE          = float(os.getenv("RAG_MAX_DISTANCE", "0.55"))
# When a time window is active the temporal filter does the heavy lifting,
# so semantic matching can be looser (casual phrases like "pulled up" won't
# embed close to "who came to work").
MAX_DISTANCE_WINDOWED = float(os.getenv("RAG_MAX_DISTANCE_WINDOWED", "0.70"))

# Lazy-loaded singletons
_model = None
_client = None
_collection = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        log.info(f"Loading embedding model {EMBED_MODEL} (first use)...")
        _model = SentenceTransformer(EMBED_MODEL)
        log.info("Embedding model loaded.")
    return _model


def _get_collection():
    global _client, _collection
    if _collection is None:
        import chromadb
        _client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
        _collection = _client.get_collection(COLLECTION_NAME)
    return _collection


def is_available() -> bool:
    """Check if ChromaDB is reachable and the collection exists."""
    try:
        _get_collection()
        return True
    except Exception:
        return False


def get_chunk_count() -> int:
    """Return how many conversation chunks are indexed."""
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
    """Search Discord history for conversation chunks relevant to the query.

    Args:
        query:       The search query text.
        n_results:   Max chunks to return.
        guild_name:  Restrict to this guild only (prevents cross-server leaks).
        since_dt:    Timezone-aware datetime — only chunks ending at/after this.
        until_dt:    Timezone-aware datetime — only chunks starting at/before this.

    Temporal filtering happens FIRST inside ChromaDB via the where clause, so
    semantic ranking only operates over chunks overlapping the window. A chunk
    overlaps [since, until] when ts_end_unix >= since AND ts_start_unix <= until.

    Returns a list of hit dicts sorted oldest-first; empty list if nothing
    passes the distance threshold.
    """
    try:
        model = _get_model()
        collection = _get_collection()

        count = collection.count()
        if count == 0:
            return []

        query_embedding = model.encode(QUERY_PREFIX + query, normalize_embeddings=True).tolist()

        conditions = []
        if guild_name:
            conditions.append({"guild": {"$eq": guild_name}})
        if since_dt:
            conditions.append({"ts_end_unix": {"$gte": _to_unix(since_dt)}})
        if until_dt:
            conditions.append({"ts_start_unix": {"$lte": _to_unix(until_dt)}})

        if len(conditions) == 0:
            where = None
        elif len(conditions) == 1:
            where = conditions[0]
        else:
            where = {"$and": conditions}

        dist_threshold = MAX_DISTANCE_WINDOWED if (since_dt or until_dt) else MAX_DISTANCE

        query_kwargs = dict(
            query_embeddings=[query_embedding],
            n_results=min(n_results, count),
            include=["documents", "metadatas", "distances"],
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
            results["distances"][0],
        ):
            if dist > dist_threshold:
                continue

            guild_id   = meta.get("guild_id", "")
            channel_id = meta.get("channel_id", "")
            first_id   = meta.get("first_message_id", "")
            jump_link  = (
                f"https://discord.com/channels/{guild_id}/{channel_id}/{first_id}"
                if guild_id and channel_id and first_id else ""
            )
            hits.append({
                "text":       doc,
                "authors":    meta.get("authors", "Unknown"),
                "ts_start":   meta.get("ts_start", ""),
                "ts_end":     meta.get("ts_end", ""),
                # kept for callers that sort on "timestamp" like the old API
                "timestamp":  meta.get("ts_start", ""),
                "channel":    meta.get("channel", ""),
                "guild":      meta.get("guild", ""),
                "n_messages": meta.get("n_messages", 0),
                "jump_link":  jump_link,
                "distance":   dist,
            })

        hits.sort(key=lambda x: x["ts_start"])
        return hits

    except Exception as e:
        log.error(f"RAG search error: {e}")
        return []


def format_for_context(hits: list) -> str:
    """Format chunk hits into a readable context block for the LLM.

    Each chunk is a short conversation excerpt with a clearly labelled header
    so the LLM copies the exact jump link rather than constructing its own.
    """
    if not hits:
        return ""
    import datetime
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")

    def _human(ts: str) -> str:
        try:
            dt = datetime.datetime.fromisoformat(ts)
            return dt.astimezone(ET).strftime("%b %-d %Y at %-I:%M %p ET")
        except Exception:
            return ts[:16].replace("T", " ") if ts else "unknown time"

    blocks = ["[Relevant past Discord conversations from server history — cite these when answering:]"]
    for h in hits:
        when = _human(h["ts_start"])
        end  = _human(h.get("ts_end", ""))
        span = when if end == when else f"{when} → {end}"
        block = (
            f'  Channel: #{h["channel"]}\n'
            f'  When: {span}\n'
            f'  Participants: {h["authors"]}\n'
            f'  Conversation:\n{_indent(h["text"])}'
        )
        if h.get("jump_link"):
            block += f'\n  Jump link (copy this EXACTLY — do not shorten it): {h["jump_link"]}'
        blocks.append(block)

    return "\n\n".join(blocks)


def _indent(text: str, pad: str = "    ") -> str:
    return "\n".join(pad + line for line in text.splitlines())

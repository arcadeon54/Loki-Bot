"""
user_memory.py — per-user learned facts in ChromaDB (Phase 3.1).

Discrete facts about server members ("works night shifts at Trader Joe's",
"has a pug named Waffles") extracted in the background after conversations and
recalled per-speaker at reply time. Complements the existing user_profiles
vibe-notes (SQLite) — facts are retrievable individually and user-deletable
via /forget.

Embeddings share rag_search's model singleton so the bot only loads one
sentence-transformers model.
"""

import hashlib
import logging
import os

log = logging.getLogger("UserMemory")

CHROMADB_HOST   = os.getenv("CHROMADB_HOST", "localhost")
CHROMADB_PORT   = int(os.getenv("CHROMADB_PORT", "8100"))
COLLECTION_NAME = os.getenv("USER_FACTS_COLLECTION", "user_facts")

# A new fact whose nearest existing fact (same user) is closer than this is a
# duplicate and gets skipped.
DEDUPE_DISTANCE = float(os.getenv("USER_FACTS_DEDUPE_DISTANCE", "0.15"))
# Facts further than this from the current message aren't relevant enough to inject.
RECALL_DISTANCE = float(os.getenv("USER_FACTS_RECALL_DISTANCE", "0.60"))

_client = None
_collection = None


def _get_model():
    # rag_search lazily loads the same model; sharing the singleton avoids a
    # second ~400MB load.
    import rag_search
    return rag_search._get_model()


def _get_collection():
    global _client, _collection
    if _collection is None:
        import chromadb
        _client = chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)
        _collection = _client.get_or_create_collection(
            COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )
    return _collection


def is_available() -> bool:
    try:
        _get_collection()
        return True
    except Exception:
        return False


def fact_count() -> int:
    try:
        return _get_collection().count()
    except Exception:
        return 0


def store_facts(user_id: str, user_name: str, guild_id: str, facts: list[str]) -> int:
    """Store new facts for a user, skipping near-duplicates. Returns #stored."""
    try:
        coll = _get_collection()
        model = _get_model()
        stored = 0
        for fact in facts:
            fact = fact.strip()
            if not 10 <= len(fact) <= 300:
                continue
            emb = model.encode(fact, normalize_embeddings=True).tolist()
            try:
                near = coll.query(
                    query_embeddings=[emb], n_results=1,
                    where={"user_id": {"$eq": str(user_id)}},
                    include=["distances"],
                )
                dists = near.get("distances", [[]])[0]
                if dists and dists[0] < DEDUPE_DISTANCE:
                    continue
            except Exception:
                pass
            fid = f"{user_id}-{hashlib.sha1(fact.lower().encode()).hexdigest()[:16]}"
            from time import strftime
            coll.upsert(
                ids=[fid], documents=[fact], embeddings=[emb],
                metadatas=[{
                    "user_id": str(user_id), "user_name": user_name,
                    "guild_id": str(guild_id), "ts": strftime("%Y-%m-%dT%H:%M:%S"),
                }],
            )
            stored += 1
        return stored
    except Exception as e:
        log.error(f"store_facts failed: {e}")
        return 0


def recall_facts(user_id: str, query: str, k: int = 4) -> list[str]:
    """Facts about this user semantically relevant to the current message."""
    try:
        coll = _get_collection()
        if coll.count() == 0:
            return []
        model = _get_model()
        emb = model.encode(query[:400], normalize_embeddings=True).tolist()
        res = coll.query(
            query_embeddings=[emb], n_results=k,
            where={"user_id": {"$eq": str(user_id)}},
            include=["documents", "distances"],
        )
        docs = res.get("documents", [[]])[0]
        dists = res.get("distances", [[]])[0]
        return [d for d, dist in zip(docs, dists) if dist <= RECALL_DISTANCE]
    except Exception as e:
        log.error(f"recall_facts failed: {e}")
        return []


def forget_user(user_id: str) -> int:
    """Delete ALL stored facts for a user (their /forget right). Returns #deleted."""
    try:
        coll = _get_collection()
        existing = coll.get(where={"user_id": {"$eq": str(user_id)}}, include=[])
        ids = existing.get("ids", [])
        if ids:
            coll.delete(ids=ids)
        return len(ids)
    except Exception as e:
        log.error(f"forget_user failed: {e}")
        return 0


# ── Fact extraction prompt (run on the cheap fallback model) ─────────────────

EXTRACTION_SYSTEM = (
    "You extract durable facts about a person from their Discord messages. "
    "Durable = still true next month: job, schedule, pets, family, hobbies, "
    "preferences, projects, where they live, recurring plans. "
    "NOT durable: one-off moods, what they ate today, current conversation topics. "
    "Reply with ONLY a JSON array of short third-person fact strings using their "
    "name, e.g. [\"Shirley sometimes works night shifts at Trader Joe's\"]. "
    "Max 5 facts. If nothing durable, reply []."
)


def parse_facts(raw: str) -> list[str]:
    """Parse the extraction model's reply into a list of fact strings."""
    import json
    raw = raw.strip()
    start, end = raw.find("["), raw.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        arr = json.loads(raw[start:end + 1])
        return [f for f in arr if isinstance(f, str)][:5]
    except json.JSONDecodeError:
        return []

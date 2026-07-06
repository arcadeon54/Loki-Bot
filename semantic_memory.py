"""
semantic_memory.py — the Boss's long-term semantic memory (July 2026 upgrade).

Two-layer design:
  1. Joplin is the SOURCE OF TRUTH — every memory is a note under
     `Loki/Memories`, human-readable and editable from any Joplin client.
  2. ChromaDB (`boss_memory` collection, same server as RAG) is the semantic
     INDEX — embeddings let "what pressure do I use?" find the scooter-tire
     note without keyword overlap.

If the Boss edits or deletes memory notes in Joplin, `reindex()` (run at
startup and daily) makes Chroma follow. Losing Chroma loses nothing: it
rebuilds from Joplin.

Embeddings share rag_search's sentence-transformers singleton — no extra
model load.
"""

import asyncio
import datetime
import hashlib
import logging
import os
import re

log = logging.getLogger("SemanticMemory")

CHROMADB_HOST   = os.getenv("CHROMADB_HOST", "localhost")
CHROMADB_PORT   = int(os.getenv("CHROMADB_PORT", "8100"))
COLLECTION_NAME = os.getenv("BOSS_MEMORY_COLLECTION", "boss_memory")

MEMORY_NOTEBOOK  = os.getenv("LOKI_MEMORY_NOTEBOOK", "Loki/Memories")
ARCHIVE_NOTEBOOK = MEMORY_NOTEBOOK + "/Archived"

# Recall filter: results with cosine distance above this aren't relevant.
RECALL_DISTANCE = float(os.getenv("BOSS_MEMORY_RECALL_DISTANCE", "0.62"))
# A new memory whose nearest neighbor is closer than this updates that note
# instead of creating a near-duplicate.
DEDUPE_DISTANCE = float(os.getenv("BOSS_MEMORY_DEDUPE_DISTANCE", "0.12"))

VALID_KINDS = ("fact", "preference", "recipe", "project", "conversation", "list")

_client = None
_collection = None


def _get_model():
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


def _slug_title(text: str, kind: str) -> str:
    words = re.sub(r"[^\w\s-]", "", text).split()
    slug = " ".join(words[:8])
    return f"{kind.capitalize()}: {slug}" if slug else f"{kind.capitalize()} memory"


def _embed(text: str) -> list[float]:
    return _get_model().encode(text, normalize_embeddings=True).tolist()


# ─── Core operations ─────────────────────────────────────────────────────────

async def remember(text: str, kind: str = "fact", context: str = "") -> dict:
    """Store a memory: Joplin note first (source of truth), then index.

    Returns {"action": "created"|"updated", "title": ..., "note_id": ...}.
    """
    import joplin_integration as jp

    text = text.strip()
    if not text:
        raise ValueError("empty memory")
    if kind not in VALID_KINDS:
        kind = "fact"

    today = datetime.date.today().isoformat()
    emb = await asyncio.to_thread(_embed, text)

    # Near-duplicate? Update the existing note instead of piling up variants.
    coll = _get_collection()
    near = await asyncio.to_thread(
        lambda: coll.query(query_embeddings=[emb], n_results=1,
                           include=["metadatas", "distances", "documents"])
        if coll.count() else None
    )
    if near and near["ids"] and near["ids"][0]:
        dist = near["distances"][0][0]
        if dist < DEDUPE_DISTANCE:
            note_id = near["ids"][0][0]
            note = await jp.get_note(note_id)
            if note:
                await jp.append_to_note(
                    note_id, f"\n**Update {today}:** {text}", section="History")
                await asyncio.to_thread(
                    coll.update, ids=[note_id], documents=[text], embeddings=[emb],
                    metadatas=[{"kind": kind, "updated": today}])
                log.info(f"Memory updated (near-dup d={dist:.3f}): {text[:60]}")
                return {"action": "updated", "title": note["title"], "note_id": note_id}

    title = _slug_title(text, kind)
    body = (
        f"{text}\n\n"
        f"---\n"
        f"**Kind:** {kind}\n"
        f"**Recorded:** {today}\n"
        + (f"**Context:** {context}\n" if context else "")
        + "\n## History\n"
        f"| {today} | recorded |\n"
    )
    note = await jp.create_note(title, body, MEMORY_NOTEBOOK,
                                tags=["loki-memory", kind])
    await asyncio.to_thread(
        _get_collection().upsert,
        ids=[note["id"]], documents=[text], embeddings=[emb],
        metadatas=[{"kind": kind, "updated": today}])
    log.info(f"Memory stored ({kind}): {text[:70]}")
    return {"action": "created", "title": title, "note_id": note["id"]}


async def recall(query: str, n: int = 5,
                 max_distance: float | None = None) -> list[dict]:
    """Semantic search over memories. Returns [{text, kind, note_id, distance}]."""
    max_distance = RECALL_DISTANCE if max_distance is None else max_distance
    coll = _get_collection()
    if not await asyncio.to_thread(coll.count):
        return []
    emb = await asyncio.to_thread(_embed, query)
    res = await asyncio.to_thread(
        lambda: coll.query(query_embeddings=[emb], n_results=min(n, 10),
                           include=["documents", "metadatas", "distances"]))
    out = []
    for nid, doc, meta, dist in zip(res["ids"][0], res["documents"][0],
                                    res["metadatas"][0], res["distances"][0]):
        if dist <= max_distance:
            out.append({"text": doc, "kind": (meta or {}).get("kind", "fact"),
                        "note_id": nid, "distance": round(dist, 3)})
    return out


async def forget(note_id: str) -> bool:
    """Archive a memory: move the Joplin note to Archived, drop the embedding."""
    import joplin_integration as jp
    try:
        archive_id = await jp.resolve_notebook_path(ARCHIVE_NOTEBOOK)
        await jp.update_note(note_id, parent_id=archive_id)
        await asyncio.to_thread(_get_collection().delete, ids=[note_id])
        return True
    except Exception as e:
        log.error(f"forget({note_id}) failed: {e}")
        return False


async def reindex() -> int:
    """Rebuild the Chroma index from the Joplin Memories notebook.

    Handles: notes edited in Joplin (re-embed), notes deleted/moved out
    (drop), notes added by hand (index). Returns number of indexed memories.
    """
    import joplin_integration as jp

    notes = await jp.list_notes_in_notebook(MEMORY_NOTEBOOK, limit=2000)
    coll = _get_collection()

    docs, ids, metas = [], [], []
    for meta_note in notes:
        note = await jp.get_note(meta_note["id"])
        if not note:
            continue
        # First paragraph (before ---) is the memory text.
        text = note.get("body", "").split("\n---\n")[0].strip()
        if not text:
            continue
        kind_m = re.search(r"\*\*Kind:\*\*\s*(\w+)", note.get("body", ""))
        docs.append(text[:1500])
        ids.append(note["id"])
        metas.append({"kind": kind_m.group(1) if kind_m else "fact",
                      "updated": datetime.date.today().isoformat()})

    existing = set()
    try:
        existing = set((await asyncio.to_thread(coll.get, ))["ids"])
    except Exception:
        pass
    stale = existing - set(ids)
    if stale:
        await asyncio.to_thread(coll.delete, ids=list(stale))
    if ids:
        embs = await asyncio.to_thread(
            lambda: _get_model().encode(docs, normalize_embeddings=True).tolist())
        await asyncio.to_thread(coll.upsert, ids=ids, documents=docs,
                                embeddings=embs, metadatas=metas)
    log.info(f"Memory reindex: {len(ids)} memories indexed, {len(stale)} stale removed")
    return len(ids)


def memory_count() -> int:
    try:
        return _get_collection().count()
    except Exception:
        return 0

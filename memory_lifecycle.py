"""
memory_lifecycle.py — lifecycle metadata + consolidation for Loki's memory.

Adapts selected boop-agent memory-lifecycle patterns (ARCHITECTURE.md memory /
consolidation, server/memory/*, server/consolidation.ts) WITHOUT replacing
Loki's existing system:

  - Joplin `Loki/Memories` stays the source of truth; ChromaDB `boss_memory`
    stays the semantic index. Neither is rewritten or blindly reindexed.
  - All new metadata (tier, segment, importance, timestamps, access counts,
    lifecycle, supersedes, source, confidence) lives in a DURABLE SQLite sidecar
    (loki_memory_meta.db) keyed by the memory's existing Joplin/Chroma note ID.
    This survives semantic_memory.reindex(), never edits or deletes a Joplin
    note, and never touches credentials.

Conservative by design:
  - short memories decay ~5%/day, long ~2%/day, permanent never decays.
  - frequently recalled memories get modest reinforcement.
  - low-scoring memories are ARCHIVED (recoverable), never auto-deleted;
    permanent memories are never automatically removed.
  - consolidation is a bounded proposer -> judge flow over small batches; only
    judge-approved proposals apply; every run keeps an audit + rollback record;
    it uses Loki's economical model (not Opus); one run at a time.

Nothing here mutates memory automatically on import. Migration, access-count
flushing, and consolidation are explicit calls (wired conservatively from
loki_bot: weekly, first run dry-run/report-only).
"""

import asyncio
import json
import logging
import math
import os
import sqlite3
import time
import uuid
from typing import Awaitable, Callable, Optional

log = logging.getLogger("MemoryLifecycle")

# ── Model ──────────────────────────────────────────────────────────────────
TIERS = ("short", "long", "permanent")
SEGMENTS = ("identity", "preference", "relationship", "project", "knowledge", "context")
LIFECYCLES = ("active", "archived", "superseded")   # none of these is deletion

DAY = 86400.0

# Per-tier daily decay fraction (configurable, conservative defaults).
TIER_DECAY = {
    "short": float(os.getenv("MEM_DECAY_SHORT", "0.05")),
    "long": float(os.getenv("MEM_DECAY_LONG", "0.02")),
    "permanent": 0.0,
}
REINFORCE_K = float(os.getenv("MEM_REINFORCE_K", "0.10"))   # modest
ARCHIVE_THRESHOLD = float(os.getenv("MEM_ARCHIVE_THRESHOLD", "0.15"))

# segment -> (tier, importance, decay_rate)  — decay_rate mirrors the tier rate.
SEGMENT_DEFAULTS = {
    "identity": ("permanent", 0.90, 0.0),
    "relationship": ("long", 0.75, TIER_DECAY["long"]),
    "preference": ("long", 0.70, TIER_DECAY["long"]),
    "project": ("long", 0.65, TIER_DECAY["long"]),
    "knowledge": ("long", 0.60, TIER_DECAY["long"]),
    "context": ("short", 0.40, TIER_DECAY["short"]),
}

# Loki's existing memory "kinds" -> lifecycle segments.
KIND_TO_SEGMENT = {
    "fact": "knowledge", "preference": "preference", "recipe": "knowledge",
    "project": "project", "conversation": "context", "list": "knowledge",
}

# Consolidation bounds (token/budget + runtime limits).
BATCH_SIZE = min(int(os.getenv("MEM_CONSOLIDATE_BATCH", "40")), 100)
MAX_PAYLOAD_CHARS = int(os.getenv("MEM_CONSOLIDATE_MAX_CHARS", "12000"))
LLM_TIMEOUT = int(os.getenv("MEM_CONSOLIDATE_LLM_TIMEOUT", "60"))
RUN_STALE_SECS = int(os.getenv("MEM_CONSOLIDATE_STALE_SECS", "1800"))  # reclaim crashed lock

DB_PATH = os.getenv("MEMORY_META_DB_PATH",
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "loki_memory_meta.db"))


def assign_defaults(kind_or_segment: str) -> "tuple[str, str, float, float]":
    """Return (segment, tier, importance, decay_rate) for a kind or segment."""
    key = (kind_or_segment or "").strip().lower()
    seg = key if key in SEGMENT_DEFAULTS else KIND_TO_SEGMENT.get(key, "knowledge")
    tier, importance, decay = SEGMENT_DEFAULTS[seg]
    return seg, tier, importance, decay


# ── Injected economical LLM (bound from loki_bot; None in tests) ────────────
_llm: "Optional[Callable[[str, str], Awaitable[str]]]" = None


def bind(llm: "Callable[[str, str], Awaitable[str]]"):
    """Install the economical (non-Opus) completion callable: (system, user) -> text."""
    global _llm
    _llm = llm


# ── Store ──────────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_meta (
    memory_id        TEXT PRIMARY KEY,
    tier             TEXT NOT NULL DEFAULT 'long',
    segment          TEXT NOT NULL DEFAULT 'knowledge',
    importance       REAL NOT NULL DEFAULT 0.6,
    decay_rate       REAL NOT NULL DEFAULT 0.02,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    last_accessed_at REAL NOT NULL,
    access_count     INTEGER NOT NULL DEFAULT 0,
    lifecycle        TEXT NOT NULL DEFAULT 'active',
    supersedes       TEXT NOT NULL DEFAULT '[]',
    source           TEXT NOT NULL DEFAULT 'unknown',
    confidence       REAL NOT NULL DEFAULT 0.8,
    review_flag      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_meta_lifecycle ON memory_meta(lifecycle);
CREATE TABLE IF NOT EXISTS consolidation_runs (
    run_id      TEXT PRIMARY KEY,
    trigger     TEXT NOT NULL DEFAULT 'manual',
    dry_run     INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'running',
    started_at  REAL NOT NULL,
    finished_at REAL,
    scanned     INTEGER NOT NULL DEFAULT 0,
    proposed    INTEGER NOT NULL DEFAULT 0,
    approved    INTEGER NOT NULL DEFAULT 0,
    applied     INTEGER NOT NULL DEFAULT 0,
    report      TEXT NOT NULL DEFAULT '{}',
    rollback    TEXT NOT NULL DEFAULT '[]'
);
"""

_META_COLS = {"tier", "segment", "importance", "decay_rate", "created_at",
              "updated_at", "last_accessed_at", "access_count", "lifecycle",
              "supersedes", "source", "confidence", "review_flag"}

_conn: Optional[sqlite3.Connection] = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(_SCHEMA)
        _conn.commit()
    return _conn


def get_meta(memory_id: str) -> Optional[dict]:
    r = _db().execute("SELECT * FROM memory_meta WHERE memory_id=?",
                      (memory_id,)).fetchone()
    return dict(r) if r else None


def all_meta(lifecycle: Optional[str] = None) -> list[dict]:
    if lifecycle:
        rows = _db().execute("SELECT * FROM memory_meta WHERE lifecycle=?",
                             (lifecycle,)).fetchall()
    else:
        rows = _db().execute("SELECT * FROM memory_meta").fetchall()
    return [dict(r) for r in rows]


def upsert_meta(memory_id: str, **fields) -> dict:
    now = time.time()
    existing = get_meta(memory_id)
    if existing is None:
        seg, tier, importance, decay = assign_defaults(fields.get("segment", ""))
        row = {
            "memory_id": memory_id, "tier": fields.get("tier", tier),
            "segment": fields.get("segment", seg),
            "importance": fields.get("importance", importance),
            "decay_rate": fields.get("decay_rate", decay),
            "created_at": fields.get("created_at", now),
            "updated_at": now, "last_accessed_at": fields.get("last_accessed_at", now),
            "access_count": fields.get("access_count", 0),
            "lifecycle": fields.get("lifecycle", "active"),
            "supersedes": json.dumps(fields.get("supersedes", [])),
            "source": fields.get("source", "unknown"),
            "confidence": fields.get("confidence", 0.8),
            "review_flag": fields.get("review_flag", ""),
        }
        cols = list(row)
        _db().execute(
            f"INSERT INTO memory_meta ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [row[c] for c in cols])
        _db().commit()
        return row
    upd = {k: (json.dumps(v) if k == "supersedes" else v)
           for k, v in fields.items() if k in _META_COLS}
    upd["updated_at"] = now
    sets = ", ".join(f"{k}=?" for k in upd)
    _db().execute(f"UPDATE memory_meta SET {sets} WHERE memory_id=?",
                  [*upd.values(), memory_id])
    _db().commit()
    return get_meta(memory_id)


# ── Backup + migration ─────────────────────────────────────────────────────
def backup_metadata(path: Optional[str] = None) -> str:
    """Dump the current metadata table to JSON before any migration/mutation."""
    path = path or os.path.join(
        os.path.dirname(DB_PATH),
        f"loki_memory_meta_backup_{time.strftime('%Y%m%d_%H%M%S')}.json")
    rows = all_meta()
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"backed_up_at": time.time(), "rows": rows}, f, indent=1)
    os.replace(tmp, path)
    log.info("memory metadata backed up (%d rows) -> %s", len(rows), path)
    return path


def migrate(existing: list[dict]) -> dict:
    """Idempotently add metadata for existing memories. `existing` is a list of
    {id, kind|segment, created_at?} from Chroma/Joplin. Backs up first. Never
    overwrites an already-migrated row (preserves counts/lifecycle)."""
    backup_metadata()
    added = skipped = 0
    for m in existing:
        mid = m.get("id") or m.get("memory_id")
        if not mid or get_meta(mid):
            skipped += 1
            continue
        seg, tier, importance, decay = assign_defaults(
            m.get("segment") or m.get("kind") or "")
        upsert_meta(mid, tier=tier, segment=seg, importance=importance,
                    decay_rate=decay, created_at=m.get("created_at", time.time()),
                    source="migration", confidence=0.8)
        added += 1
    log.info("memory metadata migration: %d added, %d already present", added, skipped)
    return {"added": added, "skipped": skipped}


# ── Retrieval: batched access accounting (no write per vector read) ─────────
_access_buf: dict[str, int] = {}
_access_last: dict[str, float] = {}
FLUSH_AT = int(os.getenv("MEM_ACCESS_FLUSH_AT", "20"))


def record_access(memory_ids):
    """Buffer access bumps; flush in one transaction when the buffer fills.
    Avoids a disk write on every recall."""
    now = time.time()
    for mid in memory_ids:
        _access_buf[mid] = _access_buf.get(mid, 0) + 1
        _access_last[mid] = now
    if sum(_access_buf.values()) >= FLUSH_AT:
        flush_access()


def flush_access() -> int:
    if not _access_buf:
        return 0
    conn = _db()
    n = 0
    for mid, delta in _access_buf.items():
        cur = get_meta(mid)
        if cur is None:
            continue
        conn.execute(
            "UPDATE memory_meta SET access_count=access_count+?, last_accessed_at=? "
            "WHERE memory_id=?", (delta, _access_last.get(mid, time.time()), mid))
        n += 1
    conn.commit()
    _access_buf.clear()
    _access_last.clear()
    return n


# ── Decay + reinforcement ──────────────────────────────────────────────────
def effective_score(meta: dict, now: Optional[float] = None) -> float:
    """importance, decayed by tier rate over days since access, then modestly
    reinforced by recall frequency. Permanent memories never decay."""
    now = now or time.time()
    rate = TIER_DECAY.get(meta["tier"], meta.get("decay_rate", 0.02))
    days = max(0.0, (now - meta["last_accessed_at"]) / DAY)
    retention = 1.0 if rate <= 0 else (1.0 - rate) ** days
    base = meta["importance"] * retention
    reinforcement = 1.0 + REINFORCE_K * math.log1p(meta.get("access_count", 0))
    return max(0.0, min(1.0, base * reinforcement))


def is_protected(meta: dict) -> bool:
    return meta["tier"] == "permanent"


# ── Archive / restore (recoverable; never deletes) ─────────────────────────
def archive(memory_id: str, reason: str = "manual") -> bool:
    meta = get_meta(memory_id)
    if not meta:
        return False
    if is_protected(meta):
        log.info("refused to archive permanent memory %s", memory_id)
        return False
    upsert_meta(memory_id, lifecycle="archived", review_flag=f"archived:{reason}")
    return True


def restore(memory_id: str) -> bool:
    meta = get_meta(memory_id)
    if not meta or meta["lifecycle"] == "active":
        return False
    upsert_meta(memory_id, lifecycle="active", review_flag="")
    return True


def archived_list(limit: int = 50) -> list[dict]:
    rows = all_meta(lifecycle="archived") + all_meta(lifecycle="superseded")
    rows.sort(key=lambda r: r["updated_at"], reverse=True)
    return rows[:limit]


# ── Write-time classification (extraction quality gate) ────────────────────
def classify_write(text: str, nearest: "Optional[tuple[str, float]]" = None,
                   dedupe_distance: float = 0.12,
                   contradiction_band: float = 0.28) -> str:
    """Judge a would-be new memory. Returns one of:
      trivial       — too short/low-value; reject.
      duplicate     — a near-identical memory already exists; update instead.
      contradiction — semantically adjacent but likely conflicting; flag for review.
      novel         — store it.
    `nearest` is (existing_text, cosine_distance) of the closest memory, if any."""
    t = (text or "").strip()
    if len(t) < 12 or t.lower() in {"ok", "yes", "no", "thanks", "lol", "sure"}:
        return "trivial"
    if nearest is not None:
        _, dist = nearest
        if dist < dedupe_distance:
            return "duplicate"
        if dist < contradiction_band and _looks_contradictory(t, nearest[0]):
            return "contradiction"
    return "novel"


def _looks_contradictory(a: str, b: str) -> bool:
    # Cheap heuristic: same subject span, but a negation/number differs. The LLM
    # extraction path can override; this only flags for human review, never
    # overwrites.
    neg = ("not", "no longer", "never", "isn't", "doesn't", "stopped")
    la, lb = a.lower(), b.lower()
    if any(n in la for n in neg) != any(n in lb for n in neg):
        return True
    na = set(_nums(la))
    nb = set(_nums(lb))
    return bool(na and nb and na != nb)


def _nums(s: str) -> list[str]:
    import re
    return re.findall(r"\d+(?:\.\d+)?", s)


# ── Consolidation: bounded proposer -> judge, one at a time ────────────────
def _active_run() -> Optional[dict]:
    r = _db().execute(
        "SELECT * FROM consolidation_runs WHERE status='running' "
        "ORDER BY started_at DESC LIMIT 1").fetchone()
    if not r:
        return None
    run = dict(r)
    # Restart safety: reclaim a crashed run whose lock went stale.
    if time.time() - run["started_at"] > RUN_STALE_SECS:
        _db().execute("UPDATE consolidation_runs SET status='failed', "
                      "finished_at=? WHERE run_id=?", (time.time(), run["run_id"]))
        _db().commit()
        return None
    return run


def _default_candidates(batch_size: int) -> list[dict]:
    """Load a bounded batch of active memories + their content for the LLM.
    Prioritizes the lowest-scoring active memories (best consolidation targets)."""
    rows = [m for m in all_meta(lifecycle="active")]
    rows.sort(key=lambda m: effective_score(m))
    rows = rows[:batch_size]
    if not rows:
        return []
    try:
        import semantic_memory
        coll = semantic_memory._get_collection()
        got = coll.get(ids=[m["memory_id"] for m in rows], include=["documents"])
        docs = dict(zip(got.get("ids", []), got.get("documents", [])))
    except Exception as e:
        log.warning("candidate content load failed: %s", e)
        docs = {}
    out = []
    for m in rows:
        m = dict(m)
        m["content"] = (docs.get(m["memory_id"]) or "")[:400]
        out.append(m)
    return out


def _build_payload(cands: list[dict]) -> str:
    lines = []
    for m in cands:
        age = int((time.time() - m["created_at"]) / DAY)
        lines.append(f"- [{m['memory_id']}] ({m['tier']}/{m['segment']} "
                     f"i={m['importance']:.2f} age={age}d) {m.get('content', '')}")
    payload = "\n".join(lines)
    return payload[:MAX_PAYLOAD_CHARS]


_PROPOSER_SYSTEM = (
    "You consolidate a user's long-term memories. Given a list (each tagged "
    "id/tier/segment), propose ONLY well-justified changes. Be conservative: "
    "distinct facts stay separate; permanent/identity memories are precious.\n"
    "Return STRICT JSON only:\n"
    '{"proposals":[\n'
    '  {"type":"merge","keep":"<id>","absorb":["<id>"],"rewrite":"<one sentence>"},\n'
    '  {"type":"supersede","newer":"<id>","older":["<id>"]},\n'
    '  {"type":"archive","memory_id":"<id>","reason":"<why low-value/stale>"}\n'
    "]}\n"
    'No changes -> {"proposals":[]}. JSON only.')

_JUDGE_SYSTEM = (
    "You judge memory-consolidation proposals. Reject anything that would blur "
    "distinct facts, lose specificity, or touch identity/permanent memories "
    "without clear redundancy. Return STRICT JSON only:\n"
    '{"decisions":[{"index":0,"approve":true,"rationale":"..."}]}\nJSON only.')


def _parse_json_obj(raw: str) -> Optional[dict]:
    import re
    m = re.search(r"\{[\s\S]*\}", raw or "")
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


async def _llm_proposer(payload: str) -> list[dict]:
    if _llm is None:
        return []
    raw = await asyncio.wait_for(_llm(_PROPOSER_SYSTEM, payload), timeout=LLM_TIMEOUT)
    obj = _parse_json_obj(raw) or {}
    return obj.get("proposals", []) if isinstance(obj, dict) else []


async def _llm_judge(proposals: list[dict], payload: str) -> set:
    if _llm is None or not proposals:
        return set()
    listing = "\n".join(f"#{i}: {json.dumps(p)}" for i, p in enumerate(proposals))
    user = f"Proposals:\n{listing}\n\nMemories:\n{payload}"
    raw = await asyncio.wait_for(_llm(_JUDGE_SYSTEM, user), timeout=LLM_TIMEOUT)
    obj = _parse_json_obj(raw) or {}
    approved = set()
    for d in (obj.get("decisions", []) if isinstance(obj, dict) else []):
        if d.get("approve"):
            approved.add(d.get("index"))
    return approved


def _ids_of(p: dict) -> list[str]:
    ids = []
    if p.get("type") == "merge":
        ids += [i for i in p.get("absorb", []) if i != p.get("keep")]
    elif p.get("type") == "supersede":
        ids += list(p.get("older", []))
    elif p.get("type") == "archive" and p.get("memory_id"):
        ids.append(p["memory_id"])
    return ids


async def consolidate(dry_run: bool = True, trigger: str = "manual",
                      batch_size: Optional[int] = None,
                      candidates: Optional[list[dict]] = None,
                      proposer=None, judge=None) -> dict:
    """One bounded proposer->judge pass. Applies only judge-approved proposals
    (skipped entirely when dry_run). Never runs concurrently; always writes an
    audit + rollback record."""
    if _active_run() is not None:
        return {"ok": False, "error": "a consolidation run is already in progress"}

    batch_size = min(batch_size or BATCH_SIZE, 100)
    run_id = "cons_" + uuid.uuid4().hex[:12]
    now = time.time()
    _db().execute(
        "INSERT INTO consolidation_runs (run_id, trigger, dry_run, status, started_at) "
        "VALUES (?,?,?,?,?)", (run_id, trigger, 1 if dry_run else 0, "running", now))
    _db().commit()

    report = {"proposals": [], "decisions": [], "applied": [], "skipped_protected": []}
    rollback: list[dict] = []
    approved_count = applied_count = 0
    try:
        cands = candidates if candidates is not None else _default_candidates(batch_size)
        cands = cands[:batch_size]                       # hard bound
        payload = _build_payload(cands)
        by_id = {c["memory_id"]: c for c in cands}

        proposals = await (proposer or _llm_proposer)(payload)
        proposals = proposals[:batch_size]               # bound proposal count too
        report["proposals"] = proposals

        approved = await (judge or _llm_judge)(proposals, payload)
        approved_count = len(approved)
        report["decisions"] = [{"index": i, "approve": i in approved}
                               for i in range(len(proposals))]

        for i, p in enumerate(proposals):
            if i not in approved:
                continue
            targets = _ids_of(p)
            # Never auto-touch a permanent/identity memory.
            protected = [t for t in targets
                         if (get_meta(t) or {}).get("tier") == "permanent"]
            if protected:
                report["skipped_protected"].extend(protected)
                targets = [t for t in targets if t not in protected]
            if not targets:
                continue
            if dry_run:
                report["applied"].append({"index": i, "type": p.get("type"),
                                          "would_affect": targets})
                continue
            for t in targets:
                m = get_meta(t)
                if not m:
                    continue
                rollback.append({"memory_id": t, "prev_lifecycle": m["lifecycle"],
                                 "prev_supersedes": m["supersedes"]})
                archive(t, reason=f"{p.get('type')}:{run_id}")
            keep = p.get("keep") or p.get("newer")
            if keep and get_meta(keep):
                cur = json.loads(get_meta(keep)["supersedes"])
                upsert_meta(keep, supersedes=sorted(set(cur) | set(targets)))
            report["applied"].append({"index": i, "type": p.get("type"),
                                      "affected": targets, "rewrite": p.get("rewrite")})
            applied_count += len(targets)

        _db().execute(
            "UPDATE consolidation_runs SET status=?, finished_at=?, scanned=?, "
            "proposed=?, approved=?, applied=?, report=?, rollback=? WHERE run_id=?",
            ("completed", time.time(), len(cands), len(proposals), approved_count,
             applied_count, json.dumps(report), json.dumps(rollback), run_id))
        _db().commit()
    except Exception as e:
        _db().execute("UPDATE consolidation_runs SET status='failed', finished_at=?, "
                      "report=? WHERE run_id=?",
                      (time.time(), json.dumps({"error": str(e)}), run_id))
        _db().commit()
        log.exception("consolidation %s failed", run_id)
        return {"ok": False, "error": str(e), "run_id": run_id}

    log.info("consolidation %s (%s): scanned=%d proposed=%d approved=%d applied=%d",
             run_id, "dry-run" if dry_run else "apply", len(cands),
             len(proposals), approved_count, applied_count)
    return {"ok": True, "run_id": run_id, "dry_run": dry_run,
            "scanned": len(cands), "proposed": len(proposals),
            "approved": approved_count, "applied": applied_count,
            "protected_skipped": len(report["skipped_protected"]),
            "report": report}


def rollback_run(run_id: str) -> dict:
    """Undo an applied run using its stored rollback record."""
    r = _db().execute("SELECT rollback FROM consolidation_runs WHERE run_id=?",
                      (run_id,)).fetchone()
    if not r:
        return {"ok": False, "error": "no such run"}
    restored = 0
    for item in json.loads(r["rollback"]):
        upsert_meta(item["memory_id"], lifecycle=item["prev_lifecycle"],
                    supersedes=json.loads(item["prev_supersedes"]))
        restored += 1
    return {"ok": True, "restored": restored}


# ── Health report (read-only) ──────────────────────────────────────────────
def health() -> dict:
    rows = all_meta()
    now = time.time()
    by_tier: dict = {}
    by_segment: dict = {}
    by_lifecycle: dict = {}
    low = 0
    review = 0
    for m in rows:
        by_tier[m["tier"]] = by_tier.get(m["tier"], 0) + 1
        by_segment[m["segment"]] = by_segment.get(m["segment"], 0) + 1
        by_lifecycle[m["lifecycle"]] = by_lifecycle.get(m["lifecycle"], 0) + 1
        if m["lifecycle"] == "active" and not is_protected(m) \
                and effective_score(m, now) < ARCHIVE_THRESHOLD:
            low += 1
        if m["review_flag"].startswith("contradiction"):
            review += 1
    return {"total": len(rows), "by_tier": by_tier, "by_segment": by_segment,
            "by_lifecycle": by_lifecycle,
            "archive_candidates": low, "flagged_for_review": review,
            "archive_threshold": ARCHIVE_THRESHOLD}


# ── Loki tools (Boss only) ─────────────────────────────────────────────────
async def _tool_health(args, ctx) -> str:
    h = health()
    return json.dumps({"ok": True, "health": h})


async def _tool_consolidate(args, ctx) -> str:
    dry = args.get("dry_run", True)
    if isinstance(dry, str):
        dry = dry.strip().lower() not in ("false", "0", "no")
    res = await consolidate(dry_run=bool(dry), trigger="manual")
    return json.dumps(res)


async def _tool_archive_list(args, ctx) -> str:
    rows = archived_list(limit=int(args.get("limit", 30)) if str(
        args.get("limit", "")).isdigit() else 30)
    out = [{"memory_id": r["memory_id"], "segment": r["segment"],
            "lifecycle": r["lifecycle"], "note": r["review_flag"]} for r in rows]
    return json.dumps({"ok": True, "archived": out or ["none archived"]})


async def _tool_restore(args, ctx) -> str:
    mid = str(args.get("memory_id", "")).strip()
    ok = restore(mid)
    return json.dumps({"ok": ok,
                       "message": f"restored {mid}" if ok
                       else "not archived / no such memory"})


def _register_tools():
    from tools import ToolSpec, register

    def _p(props, required):
        return {"type": "object", "properties": props, "required": required}

    register(ToolSpec(
        name="memory_health",
        description=("Read-only report on the Boss's long-term memory: counts by "
                     "tier/segment/lifecycle, how many are decayed enough to be "
                     "archive candidates, and how many are flagged for review."),
        parameters=_p({}, []),
        handler=_tool_health, permission="boss", timeout=20,
    ))
    register(ToolSpec(
        name="memory_consolidate",
        description=("Run a bounded memory consolidation pass (proposer→judge). "
                     "Defaults to dry_run=true (report only, nothing changes). "
                     "Pass dry_run=false to actually apply judge-approved "
                     "archives/merges (reversible)."),
        parameters=_p({"dry_run": {"type": "boolean"}}, []),
        handler=_tool_consolidate, permission="boss", timeout=180,
    ))
    register(ToolSpec(
        name="memory_archive_list",
        description="List archived/superseded memories (recoverable) by recency.",
        parameters=_p({"limit": {"type": "integer"}}, []),
        handler=_tool_archive_list, permission="boss", timeout=15,
    ))
    register(ToolSpec(
        name="memory_restore",
        description="Restore an archived memory to active by its memory_id.",
        parameters=_p({"memory_id": {"type": "string"}}, ["memory_id"]),
        handler=_tool_restore, permission="boss", timeout=15,
    ))


try:
    _register_tools()
except Exception as _e:  # tools registry unavailable (e.g. isolated import)
    log.warning("memory lifecycle tools not registered: %s", _e)

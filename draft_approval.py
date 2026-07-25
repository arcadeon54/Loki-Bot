"""
draft_approval.py — durable draft-and-approval gate for consequential actions.

Inspired by boop-agent's draft-before-send pattern (ARCHITECTURE.md §5,
server/draft-tools.ts), rebuilt for Loki's Python + SQLite world and tightened:

  - Loki may PREPARE a consequential action but must never execute it until the
    authorized user approves the exact draft by ID.
  - Which tools are consequential is declared in the tool REGISTRY
    (ToolSpec.action_type) — not inferred from tool names. When such a tool is
    called, tools.execute() routes here: we validate/normalize the parameters,
    persist a durable draft, and return a concise summary. Nothing runs.
  - Approval verifies the stored payload hash (tamper detection) and expiry,
    then executes THROUGH the durable task supervisor (task type "draft_exec"),
    which re-verifies and runs the real handler via tools.run_approved().
  - Only the originating authorized user or the Boss may approve; roommate can
    never approve a Boss draft.
  - Read-only tools and harmless note/list creation are never gated.

Persistence: loki_drafts.db (TASKS/DRAFTS dir = repo dir, alongside the other
Loki SQLite files). Payloads are the minimum needed to execute; the tool log is
redacted via the existing ToolSpec.redact_log / _scrub protections.
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Optional

import tools
from tools import PERM_RANK, ToolContext, ToolSpec, register, user_level

log = logging.getLogger("DraftApproval")

DB_PATH = os.getenv("DRAFTS_DB_PATH",
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "loki_drafts.db"))
DEFAULT_TTL = int(os.getenv("DRAFT_TTL_SECS", "3600"))

PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"
EXPIRED, EXECUTED, FAILED = "expired", "executed", "failed"
DECIDED = {APPROVED, REJECTED, EXPIRED, EXECUTED, FAILED}

_ALLOWED_COLS = {
    "action_type", "tool_name", "summary", "requester_id", "requester_name",
    "interface", "channel_id", "payload_json", "payload_hash", "created_at",
    "expires_at", "status", "decided_at", "executing_task_id",
}

# ── Store ──────────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    draft_id          TEXT PRIMARY KEY,
    action_type       TEXT NOT NULL,
    tool_name         TEXT NOT NULL,
    summary           TEXT NOT NULL DEFAULT '',
    requester_id      TEXT NOT NULL DEFAULT '',
    requester_name    TEXT NOT NULL DEFAULT '',
    interface         TEXT NOT NULL DEFAULT '',
    channel_id        TEXT NOT NULL DEFAULT '',
    payload_json      TEXT NOT NULL DEFAULT '{}',
    payload_hash      TEXT NOT NULL DEFAULT '',
    created_at        REAL NOT NULL,
    expires_at        REAL NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending',
    decided_at        REAL,
    executing_task_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
"""

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


def _insert(row: dict):
    cols = list(row)
    _db().execute(
        f"INSERT INTO drafts ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [row[c] for c in cols])
    _db().commit()


def _update(draft_id: str, **fields):
    fields = {k: v for k, v in fields.items() if k in _ALLOWED_COLS}
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    _db().execute(f"UPDATE drafts SET {sets} WHERE draft_id=?",
                  [*fields.values(), draft_id])
    _db().commit()


def get_draft(draft_id: str) -> Optional[dict]:
    r = _db().execute("SELECT * FROM drafts WHERE draft_id=?", (draft_id,)).fetchone()
    return dict(r) if r else None


def _list(where: str = "", params=()) -> list[dict]:
    sql = "SELECT * FROM drafts"
    if where:
        sql += " WHERE " + where
    sql += " ORDER BY created_at DESC"
    return [dict(r) for r in _db().execute(sql, params).fetchall()]


# ── Hashing / validation ───────────────────────────────────────────────────
def _hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _payload_intact(draft: dict) -> bool:
    """Recompute the hash from the stored payload; any drift means the row was
    modified after creation and must not execute."""
    try:
        payload = json.loads(draft["payload_json"])
    except ValueError:
        return False
    return _hash(payload) == draft["payload_hash"]


def _expired(draft: dict, now: Optional[float] = None) -> bool:
    return (now or time.time()) >= draft["expires_at"]


def _maybe_expire(draft: dict) -> dict:
    if draft["status"] == PENDING and _expired(draft):
        _update(draft["draft_id"], status=EXPIRED, decided_at=time.time())
        return get_draft(draft["draft_id"])
    return draft


def _can_decide(ctx: ToolContext, draft: dict) -> bool:
    """Boss decides anything; anyone else only their own drafts. Roommate can
    never approve a Boss draft."""
    if user_level(ctx.user_id) == "boss":
        return True
    return draft.get("requester_id") == str(ctx.user_id)


# ── Draft creation (installed as tools._approval_intercept) ─────────────────
def _default_prepare(spec: ToolSpec, args: dict, ctx: ToolContext):
    # A consequential tool with no custom prepare: pass args through as the
    # payload and build a generic summary. Values are not echoed (may be
    # sensitive) — only the field names.
    fields = ", ".join(sorted(args)) or "no parameters"
    return dict(args), f"{spec.action_type}: {spec.name} ({fields})", ""


async def create_draft(spec: ToolSpec, args: dict, ctx: ToolContext) -> str:
    prepare = spec.prepare
    if prepare is not None:
        payload, summary, err = prepare(args, ctx)
    else:
        payload, summary, err = _default_prepare(spec, args, ctx)
    if err:
        return err
    if not isinstance(payload, dict):
        payload = {}
    now = time.time()
    ttl = spec.approval_ttl or DEFAULT_TTL
    draft_id = "dr_" + uuid.uuid4().hex[:12]
    interface = "telegram" if ctx.channel_id.startswith("tg:") else "discord"
    _insert({
        "draft_id": draft_id, "action_type": spec.action_type,
        "tool_name": spec.name, "summary": summary[:400],
        "requester_id": str(ctx.user_id), "requester_name": ctx.user_name,
        "interface": interface, "channel_id": ctx.channel_id,
        "payload_json": json.dumps(payload), "payload_hash": _hash(payload),
        "created_at": now, "expires_at": now + ttl, "status": PENDING,
    })
    log.info("draft %s staged: %s by %s", draft_id, spec.name, ctx.user_name)
    mins = max(1, int(ttl // 60))
    return (f"📝 Draft prepared — {spec.action_type}\n"
            f"{summary}\n"
            f"Draft ID: {draft_id}\n"
            f"Nothing has happened yet. To go ahead, approve this exact ID: "
            f"`draft_approve {draft_id}`. To discard it: `draft_reject {draft_id}`.\n"
            f"(Expires in ~{mins} min if not approved.)")


# ── Execution via the durable task supervisor ──────────────────────────────
async def _draft_exec_handler(h):
    """Runs as an internal 'draft_exec' supervised task. Reloads the draft,
    re-verifies hash + expiry + status, then executes the real tool handler."""
    draft_id = h.input.get("draft_id", "")
    draft = get_draft(draft_id)
    if not draft:
        return _TR("failed", "draft vanished", "missing")
    if draft["status"] != APPROVED:
        # Already executed/failed/rejected — never run twice.
        return _TR("failed", f"draft not approved (is {draft['status']})",
                   "not_approved")
    if not _payload_intact(draft):
        _update(draft_id, status=FAILED)
        return _TR("failed", "payload hash mismatch — refusing to execute", "tamper")
    if _expired(draft):
        _update(draft_id, status=EXPIRED, decided_at=time.time())
        return _TR("failed", "draft expired before execution", "expired")
    ctx = ToolContext(user_id=draft["requester_id"],
                      user_name=draft["requester_name"],
                      channel_id=draft["channel_id"])
    payload = json.loads(draft["payload_json"])
    try:
        result = await tools.run_approved(draft["tool_name"], payload, ctx)
    except Exception as e:
        _update(draft_id, status=FAILED)
        log.warning("draft %s execution failed: %s", draft_id, type(e).__name__)
        return _TR("failed", "action failed during execution", "exec_error",
                   detail=f"{type(e).__name__}: {e}")
    _update(draft_id, status=EXECUTED)
    log.info("draft %s executed (%s)", draft_id, draft["tool_name"])
    return _TR("completed", (result or "done")[:400])


def _TR(status, summary, category="", detail=""):
    # Build a task_supervisor.TaskResult lazily so importing this module never
    # hard-requires the supervisor.
    import task_supervisor
    return task_supervisor.TaskResult(status=status, summary=summary,
                                      error_category=category, error_detail=detail)


def _register_exec_task_type():
    try:
        import task_supervisor
    except Exception:
        log.warning("task supervisor unavailable — draft execution disabled")
        return False
    task_supervisor.register_type(task_supervisor.TaskType(
        name="draft_exec", handler=_draft_exec_handler, permission="crew",
        capabilities=frozenset(), default_timeout=120, max_retries=0,
        internal=True,
        title=lambda i: f"Execute draft {i.get('draft_id', '?')}",
    ))
    return True


# ── Tools ──────────────────────────────────────────────────────────────────
def _fmt(draft: dict) -> str:
    bits = [f"{draft['draft_id']} [{draft['action_type']}] {draft['status']} — {draft['summary']}"]
    if draft["status"] == PENDING:
        left = int(draft["expires_at"] - time.time())
        bits.append(f"expires in {max(0, left) // 60}m")
    if draft.get("executing_task_id"):
        bits.append(f"task {draft['executing_task_id']}")
    return " | ".join(bits)


async def _tool_list(args: dict, ctx: ToolContext) -> str:
    rows = [_maybe_expire(d) for d in _list("status=?", (PENDING,))]
    visible = [d for d in rows if d["status"] == PENDING and _can_decide(ctx, d)]
    out = [_fmt(d) for d in visible[:20]]
    return json.dumps({"ok": True, "drafts": out or ["no pending drafts"]})


async def _tool_view(args: dict, ctx: ToolContext) -> str:
    draft = get_draft(str(args.get("draft_id", "")).strip())
    if not draft:
        return json.dumps({"ok": False, "error": "no such draft"})
    if not _can_decide(ctx, draft):
        return json.dumps({"ok": False, "error": "that draft is not yours to view"})
    draft = _maybe_expire(draft)
    return json.dumps({"ok": True, "draft": _fmt(draft),
                       "requested_by": draft["requester_name"],
                       "via": draft["interface"]})


async def _tool_approve(args: dict, ctx: ToolContext) -> str:
    draft_id = str(args.get("draft_id", "")).strip()
    draft = get_draft(draft_id)
    if not draft:
        return json.dumps({"ok": False, "error": "no such draft"})
    if not _can_decide(ctx, draft):
        return json.dumps({"ok": False,
                           "error": "you are not allowed to approve this draft"})
    draft = _maybe_expire(draft)
    if draft["status"] != PENDING:
        return json.dumps({"ok": False,
                           "error": f"draft is {draft['status']}, not pending"})
    if not _payload_intact(draft):
        _update(draft_id, status=FAILED, decided_at=time.time())
        return json.dumps({"ok": False,
                           "error": "draft was modified since it was prepared — refusing "
                                    "to execute. Re-issue the request."})
    try:
        import task_supervisor
    except Exception:
        return json.dumps({"ok": False, "error": "execution backend unavailable"})
    now = time.time()
    _update(draft_id, status=APPROVED, decided_at=now)
    tt = task_supervisor._TYPES.get("draft_exec")
    if tt is None:
        _update(draft_id, status=FAILED)
        return json.dumps({"ok": False, "error": "draft executor not registered"})
    task_id = task_supervisor.submit(tt, ctx, {"draft_id": draft_id})
    _update(draft_id, executing_task_id=task_id)
    log.info("draft %s approved by %s -> task %s", draft_id, ctx.user_name, task_id)
    return json.dumps({"ok": True, "draft_id": draft_id, "status": APPROVED,
                       "task_id": task_id,
                       "note": "approved — executing now; I'll report the result here"})


async def _tool_reject(args: dict, ctx: ToolContext) -> str:
    draft_id = str(args.get("draft_id", "")).strip()
    draft = get_draft(draft_id)
    if not draft:
        return json.dumps({"ok": False, "error": "no such draft"})
    if not _can_decide(ctx, draft):
        return json.dumps({"ok": False,
                           "error": "you are not allowed to reject this draft"})
    draft = _maybe_expire(draft)
    if draft["status"] != PENDING:
        return json.dumps({"ok": False,
                           "error": f"draft is {draft['status']}, not pending"})
    _update(draft_id, status=REJECTED, decided_at=time.time())
    return json.dumps({"ok": True, "draft_id": draft_id, "status": REJECTED})


def _p(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required}


def _register_tools():
    register(ToolSpec(
        name="draft_list",
        description=("List your pending action drafts awaiting approval "
                     "(consequential actions Loki prepared but did not run)."),
        parameters=_p({}, []),
        handler=_tool_list, permission="crew", timeout=15,
    ))
    register(ToolSpec(
        name="draft_view",
        description="Show one pending action draft by draft_id (e.g. dr_ab12…).",
        parameters=_p({"draft_id": {"type": "string"}}, ["draft_id"]),
        handler=_tool_view, permission="crew", timeout=15,
    ))
    register(ToolSpec(
        name="draft_approve",
        description=("Approve a prepared action by its exact draft_id and execute "
                     "it. Only the person who requested it (or the Boss) may "
                     "approve. Use ONLY on explicit user confirmation."),
        parameters=_p({"draft_id": {"type": "string"}}, ["draft_id"]),
        handler=_tool_approve, permission="crew", timeout=30,
    ))
    register(ToolSpec(
        name="draft_reject",
        description="Reject/discard a prepared action draft by its exact draft_id.",
        parameters=_p({"draft_id": {"type": "string"}}, ["draft_id"]),
        handler=_tool_reject, permission="crew", timeout=15,
    ))


# ── Install ────────────────────────────────────────────────────────────────
_db()
_exec_ready = _register_exec_task_type()
_register_tools()
tools.set_approval_intercept(create_draft)

_consequential = [n for n, s in tools.REGISTRY.items() if s.action_type]
log.info("draft approval gate installed — %d consequential tool(s): %s; "
         "executor=%s", len(_consequential), ", ".join(sorted(_consequential)) or "none",
         "ready" if _exec_ready else "UNAVAILABLE")

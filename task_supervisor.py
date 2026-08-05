"""
task_supervisor.py — durable background task supervisor for Loki.

Inspired by boop-agent's dispatcher / execution-agent / heartbeat split, adapted
to Loki's Python + SQLite world. Ordinary chat stays fast: quick tools run
inline, long work is submitted here and executes in the background with durable
state, lifecycle supervision, restart recovery, and concise one-shot updates.

Hard boundaries (mirror the boop dispatcher rule, tightened for Loki):
  - A task is ALWAYS {task_type -> registered handler}. Callers never pass
    Python, shell commands, or filesystem paths — only a narrowly-scoped input
    dict that the task type's own validate() re-checks.
  - Each handler receives only its declared capabilities. The supervisor grants
    nothing generic.
  - Authorization is checked when creating a task AND when acting on one. A crew
    user (roommate) can never see or touch a Boss task.
  - Sensitive input never reaches the tool log — the dispatcher tool is
    redact_log=True and the supervisor logs task_id/type/status only, never
    input or results.

State model:
  queued -> running -> {completed | failed | cancelled}
  running -> {paused_auth | paused_quota}   (parked; cleared by retry)

Truth lives in SQLite (TASKS_DB_PATH, default loki_tasks.db in the repo dir),
alongside loki_memory.db / jobsite.db. last_announced is persisted BEFORE a
notification is sent, so a Loki restart can lose at most one message but can
never announce the same transition twice.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from tools import PERM_RANK, ToolContext, ToolSpec, register, user_level

try:
    import maintenance_notify as mn
except Exception:      # standalone/test import of the supervisor alone
    mn = None

log = logging.getLogger("TaskSupervisor")

# ── Tunables (env-overridable) ─────────────────────────────────────────────
DB_PATH = os.getenv("TASKS_DB_PATH",
                    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "loki_tasks.db"))
MAX_CONCURRENCY = int(os.getenv("TASK_MAX_CONCURRENCY", "1"))
HEARTBEAT_SECS = int(os.getenv("TASK_HEARTBEAT_SECS", "30"))
DEFAULT_TIMEOUT = int(os.getenv("TASK_DEFAULT_TIMEOUT_SECS", "900"))  # 15 min
ORPHAN_GRACE = int(os.getenv("TASK_ORPHAN_GRACE_SECS", "90"))
CO_POLL_SECS = int(os.getenv("TASK_CAREER_POLL_SECS", "30"))

TERMINAL = {"completed", "failed", "cancelled"}
PAUSED = {"paused_auth", "paused_quota"}
ACTIVE = {"queued", "running"}
OPEN = ACTIVE | PAUSED                       # not finished
ANNOUNCE = {"running", "completed", "failed", "cancelled",
            "paused_auth", "paused_quota"}

_ALLOWED_COLS = {
    "task_type", "title", "requester_id", "requester_name", "requester_level",
    "channel_id", "profile", "status", "created_at", "started_at", "updated_at",
    "heartbeat_at", "completed_at", "retry_count", "max_retries", "timeout_secs",
    "input_json", "result_summary", "error_category", "error_detail",
    "artifacts_json", "last_announced", "external_ref", "request_id",
    "origin_message_id", "conversation_id", "priority",
}

enabled = True  # the supervisor always works — it needs only SQLite

# Injected by loki_bot.py at startup (same pattern as career_ops.bind).
_session_factory = None
_send = None                                 # async (channel_id, text, ...)


def bind(session_factory, send):
    global _session_factory, _send
    _session_factory = session_factory
    _send = send


# ── Task-type registry (the dispatcher's whitelist) ────────────────────────
@dataclass
class TaskResult:
    status: str
    summary: str = ""
    error_category: str = ""
    error_detail: str = ""
    artifacts: list = field(default_factory=list)   # [{"name","size"}] metadata


@dataclass
class TaskType:
    name: str
    handler: Callable[["TaskHandle"], Awaitable[TaskResult]]
    permission: str = "crew"
    capabilities: frozenset = frozenset()
    default_timeout: int = DEFAULT_TIMEOUT
    max_retries: int = 1
    # Higher-priority queued tasks start first: a fresh incident diagnosis must
    # never wait behind an old informational task (e.g. a queued browse).
    priority: int = 0
    # Handler re-attaches to external work via external_ref instead of
    # re-submitting, so it is safe to re-run after a restart (no duplicates).
    reattach: bool = False
    # Internal types are driven by other subsystems (e.g. the draft executor),
    # never started directly by the model via task_start.
    internal: bool = False
    validate: Optional[Callable[[dict, ToolContext], "tuple[dict, str]"]] = None
    on_cancel: Optional[Callable[[dict], Awaitable[None]]] = None
    title: Optional[Callable[[dict], str]] = None


_TYPES: dict[str, TaskType] = {}


def register_type(tt: TaskType):
    _TYPES[tt.name] = tt


# ── Durable store ──────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id         TEXT PRIMARY KEY,
    task_type       TEXT NOT NULL,
    title           TEXT NOT NULL DEFAULT '',
    requester_id    TEXT NOT NULL DEFAULT '',
    requester_name  TEXT NOT NULL DEFAULT '',
    requester_level TEXT NOT NULL DEFAULT '',
    channel_id      TEXT NOT NULL DEFAULT '',
    profile         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'queued',
    created_at      REAL NOT NULL,
    started_at      REAL,
    updated_at      REAL NOT NULL,
    heartbeat_at    REAL,
    completed_at    REAL,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    max_retries     INTEGER NOT NULL DEFAULT 1,
    timeout_secs    INTEGER NOT NULL DEFAULT 900,
    input_json      TEXT NOT NULL DEFAULT '{}',
    result_summary  TEXT NOT NULL DEFAULT '',
    error_category  TEXT NOT NULL DEFAULT '',
    error_detail    TEXT NOT NULL DEFAULT '',
    artifacts_json  TEXT NOT NULL DEFAULT '[]',
    last_announced  TEXT,
    external_ref    TEXT,
    request_id      TEXT,
    origin_message_id TEXT NOT NULL DEFAULT '',
    conversation_id   TEXT NOT NULL DEFAULT '',
    priority          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at);
"""

# Columns added after the first release — applied to pre-existing DBs.
_MIGRATIONS = (
    "ALTER TABLE tasks ADD COLUMN origin_message_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN conversation_id   TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE tasks ADD COLUMN priority          INTEGER NOT NULL DEFAULT 0",
)

def _readdress_autonomous_tasks(conn) -> int:
    """Idempotent data repair. Autonomous maintenance tasks created before the
    ops feed existed were addressed to the Boss's Telegram DM. They are
    long-lived — a paused_quota row never finishes — so every restart
    re-announced their lifecycle there. Re-address them to the feed.

    Only rows the maintenance stack itself submitted are touched; a task a
    person asked for keeps the conversation it came from."""
    if mn is None:
        return 0
    cur = conn.execute(
        "UPDATE tasks SET channel_id=? WHERE requester_name=? AND channel_id<>?",
        (mn.OPS_CHANNEL, mn.AUTONOMOUS_REQUESTER, mn.OPS_CHANNEL))
    return cur.rowcount or 0

_conn: Optional[sqlite3.Connection] = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(_SCHEMA)
        for mig in _MIGRATIONS:
            try:
                _conn.execute(mig)
            except sqlite3.OperationalError:
                pass  # column already exists
        try:
            moved = _readdress_autonomous_tasks(_conn)
            if moved:
                log.info("re-addressed %d autonomous maintenance task(s) from a "
                         "chat channel to the ops feed", moved)
        except sqlite3.OperationalError:
            log.debug("autonomous task re-addressing skipped", exc_info=True)
        _conn.commit()
    return _conn


def _insert(row: dict):
    cols = list(row)
    _db().execute(
        f"INSERT INTO tasks ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [row[c] for c in cols])
    _db().commit()


def _update(task_id: str, **fields):
    fields = {k: v for k, v in fields.items() if k in _ALLOWED_COLS}
    if not fields:
        return
    fields.setdefault("updated_at", time.time())
    sets = ", ".join(f"{k}=?" for k in fields)
    _db().execute(f"UPDATE tasks SET {sets} WHERE task_id=?",
                  [*fields.values(), task_id])
    _db().commit()


def get_task(task_id: str) -> Optional[dict]:
    r = _db().execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    return dict(r) if r else None


def _query(where: str = "", params=()) -> list[dict]:
    sql = "SELECT * FROM tasks"
    if where:
        sql += " WHERE " + where
    sql += " ORDER BY created_at ASC"
    return [dict(r) for r in _db().execute(sql, params).fetchall()]


# ── Worker pool ────────────────────────────────────────────────────────────
@dataclass
class _Run:
    task: asyncio.Task
    handle: "TaskHandle"


_running: dict[str, _Run] = {}
_started = False
_loops: list[asyncio.Task] = []


class TaskHandle:
    """Everything a handler is allowed to touch. No generic exec surface."""

    def __init__(self, task_id: str, caps: frozenset):
        self.task_id = task_id
        self._caps = caps
        self.cancel_event = asyncio.Event()

    @property
    def row(self) -> dict:
        return get_task(self.task_id) or {}

    @property
    def input(self) -> dict:
        try:
            return json.loads(self.row.get("input_json") or "{}")
        except ValueError:
            return {}

    @property
    def external_ref(self) -> Optional[str]:
        return self.row.get("external_ref")

    def set_external_ref(self, ref: str):
        _update(self.task_id, external_ref=ref)

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def require_capability(self, cap: str):
        if cap not in self._caps:
            raise PermissionError(f"task lacks capability '{cap}'")

    async def beat(self):
        _update(self.task_id, heartbeat_at=time.time())


def _pump():
    """Start queued tasks up to the concurrency limit. Sync + single-threaded.
    Highest priority first (incidents jump ahead of informational tasks),
    oldest first within a priority."""
    if not _started:
        return
    while len(_running) < MAX_CONCURRENCY:
        queued = sorted(_query("status='queued'"),
                        key=lambda t: (-(t.get("priority") or 0), t["created_at"]))
        nxt = next((t for t in queued if t["task_id"] not in _running), None)
        if nxt is None:
            return
        _spawn(nxt["task_id"])


def _spawn(task_id: str):
    row = get_task(task_id)
    tt = _TYPES.get(row["task_type"])
    if tt is None:
        _finalize_sync(task_id, TaskResult(
            "failed", error_category="unknown_type",
            summary=f"no handler for task_type '{row['task_type']}'"))
        return
    now = time.time()
    # Claim synchronously so a second _pump tick can't double-start this row.
    _update(task_id, status="running",
            started_at=row.get("started_at") or now, heartbeat_at=now)
    handle = TaskHandle(task_id, tt.capabilities)
    t = asyncio.create_task(_run(task_id, tt, handle))
    _running[task_id] = _Run(t, handle)


async def _run(task_id: str, tt: TaskType, handle: TaskHandle):
    await _maybe_announce(task_id)          # queued -> running
    try:
        result = await tt.handler(handle)
    except asyncio.CancelledError:
        # Hard cancel (timeout sweep). Status was already written by the sweep.
        _running.pop(task_id, None)
        raise
    except Exception as e:
        log.exception("task %s handler crashed", task_id)
        result = TaskResult("failed", error_category="exception",
                            error_detail=f"{type(e).__name__}: {e}",
                            summary="handler error")
    # A concurrent cancel/timeout may already have finalized this row.
    cur = get_task(task_id)
    if cur and cur["status"] in TERMINAL:
        _running.pop(task_id, None)
        _pump()
        return
    await _finalize(task_id, result)
    _running.pop(task_id, None)
    _pump()


def _finalize_sync(task_id: str, result: TaskResult):
    now = time.time()
    fields = dict(status=result.status,
                  result_summary=(result.summary or "")[:800],
                  error_category=result.error_category or "",
                  error_detail=(result.error_detail or "")[:800],
                  artifacts_json=json.dumps(result.artifacts or []))
    if result.status in TERMINAL:
        fields["completed_at"] = now
    _update(task_id, **fields)


async def _finalize(task_id: str, result: TaskResult):
    _finalize_sync(task_id, result)
    await _maybe_announce(task_id)


# ── Notifications (persist-before-send → never a duplicate) ────────────────
def _short(task_id: str) -> str:
    return task_id.split("_")[-1][:8]


def _announce_text(row: dict) -> str:
    s = row["status"]
    tag = f"Task {_short(row['task_id'])} — {row['title']}"
    summ = row.get("result_summary") or ""
    if s == "running":
        return f"▶️ {tag}: started."
    if s == "completed":
        return f"✅ {tag}: done." + (f" {summ}" if summ else "")
    if s == "failed":
        cat = row.get("error_category") or "error"
        return f"❌ {tag}: failed ({cat})." + (f" {summ}" if summ else "")
    if s == "cancelled":
        return f"🚫 {tag}: cancelled."
    if s == "paused_auth":
        return f"⏸️ {tag}: paused — needs (re)authentication. Retry once resolved."
    if s == "paused_quota":
        return f"⏸️ {tag}: paused — quota exhausted. Retry later."
    return f"{tag}: {s}"


async def _maybe_announce(task_id: str):
    row = get_task(task_id)
    if not row or row["status"] not in ANNOUNCE:
        return
    if row.get("last_announced") == row["status"]:
        return
    # Persist the intent first. If the send fails we drop one message rather
    # than risk announcing the same transition twice after a restart.
    _update(task_id, last_announced=row["status"])
    # An AUTONOMOUS maintenance task is not a conversation: the feed carries
    # what the job FOUND, and the router drops the queued/started/claimed/
    # paused/retry chatter entirely. Matched on requester identity, not the
    # stored channel — legacy rows still carry the Boss's Telegram chat.
    # Tasks a person started from a chat are untouched.
    if mn is not None and mn.is_autonomous_task(row):
        try:
            await mn.announce_task(row)
        except Exception:
            log.exception("task %s ops announce failed (%s)", task_id, row["status"])
        return
    if _send is None or not row.get("channel_id"):
        return
    try:
        await _send(row["channel_id"], _announce_text(row))
    except Exception:
        log.exception("task %s announce failed (%s)", task_id, row["status"])


# ── Lifecycle supervision ──────────────────────────────────────────────────
async def sweep_stale():
    """One heartbeat tick: time out live tasks, reconcile orphans."""
    now = time.time()
    for row in _query("status='running'"):
        tid = row["task_id"]
        beat = row.get("heartbeat_at")
        if beat is None:
            beat = row.get("started_at")
        if beat is None:
            beat = now
        age = now - beat
        run = _running.get(tid)
        if run is not None:
            if age <= row["timeout_secs"]:
                continue
            run.handle.cancel_event.set()
            run.task.cancel()
            _running.pop(tid, None)
            await _finalize(tid, TaskResult(
                "failed", error_category="timeout",
                summary=f"no heartbeat for {int(age)}s (timeout {row['timeout_secs']}s)"))
        else:
            # Orphan: a 'running' row with no live worker in THIS process.
            if age <= ORPHAN_GRACE:
                continue
            await _recover_one(row)
    _pump()


async def _recover_one(row: dict):
    tid = row["task_id"]
    tt = _TYPES.get(row["task_type"])
    if tt and tt.reattach and row.get("external_ref"):
        # Re-attach: the handler polls the existing external job by
        # external_ref, so nothing is re-submitted (no duplicate jobs).
        log.info("task %s reattaching to external work after restart", tid)
        _update(tid, status="queued")
        _pump()
    else:
        log.info("task %s had no live worker — marking interrupted", tid)
        await _finalize(tid, TaskResult(
            "failed", error_category="interrupted",
            summary="Interrupted by a Loki restart; no live worker to resume."))


async def _heartbeat_loop():
    log.info("task heartbeat online (every %ss, timeout %ss, %d worker(s))",
             HEARTBEAT_SECS, DEFAULT_TIMEOUT, MAX_CONCURRENCY)
    while True:
        try:
            await sweep_stale()
        except Exception:
            log.exception("heartbeat sweep failed")
        await asyncio.sleep(HEARTBEAT_SECS)


def recover():
    """At startup, adopt rows left 'running' by the previous process."""
    for row in _query("status='running'"):
        if row["task_id"] in _running:
            continue
        asyncio.create_task(_recover_one(row))


def start():
    """Launch the worker pump + heartbeat. Call from on_ready (needs a loop)."""
    global _started
    if _started:
        return
    _db()
    _started = True
    recover()
    _pump()
    _loops.append(asyncio.create_task(_heartbeat_loop()))
    log.info("task supervisor started (%d types: %s)",
             len(_TYPES), ", ".join(sorted(_TYPES)))


# ── Authorization + submission ─────────────────────────────────────────────
def _can_access(ctx: ToolContext, row: dict) -> bool:
    """Boss sees everything; anyone else only their own tasks."""
    if user_level(ctx.user_id) == "boss":
        return True
    return row.get("requester_id") == str(ctx.user_id)


def submit(tt: TaskType, ctx: ToolContext, clean_input: dict) -> str:
    now = time.time()
    task_id = "lt_" + uuid.uuid4().hex[:12]
    title = (tt.title(clean_input) if tt.title else tt.name)[:120]
    profile = str(clean_input.get("profile") or user_level(ctx.user_id))
    _insert({
        "task_id": task_id, "task_type": tt.name, "title": title,
        "requester_id": str(ctx.user_id), "requester_name": ctx.user_name,
        "requester_level": user_level(ctx.user_id),
        "channel_id": ctx.channel_id, "profile": profile,
        "status": "queued", "created_at": now, "updated_at": now,
        "retry_count": 0, "max_retries": tt.max_retries,
        "timeout_secs": tt.default_timeout,
        "input_json": json.dumps(clean_input),
        "request_id": uuid.uuid4().hex,
        # Correlation: the task stays tied to the message + conversation that
        # created it. Internal only — never surfaced in normal replies.
        "origin_message_id": getattr(ctx, "message_id", "") or "",
        "conversation_id": ctx.channel_id,
        "priority": tt.priority,
    })
    log.info("task %s submitted type=%s by=%s", task_id, tt.name, ctx.user_name)
    _pump()
    return task_id


def _fmt_task(row: dict, explicit: bool = False) -> str:
    bits = [f"{row['task_id']} [{row['task_type']}] {row['status']} — {row['title']}"]
    if row["status"] == "completed" and row.get("result_summary"):
        # Once the completion notification has been delivered to the chat, a
        # LIST view must not carry the payload again — an old result would leak
        # into whatever unrelated message the user sends next. The full result
        # stays available via task_status (explicit=True).
        if not explicit and row.get("last_announced") == "completed":
            bits.append("[result already delivered in this chat — do NOT repeat "
                        "it; it is unrelated to any new message]")
        else:
            bits.append(row["result_summary"])
    elif row["status"] in ("failed", "cancelled") or row["status"] in PAUSED:
        note = row.get("error_detail") or row.get("result_summary") or ""
        cat = row.get("error_category")
        if cat:
            bits.append(f"({cat})")
        if note:
            bits.append(note)
    if row.get("retry_count"):
        bits.append(f"retry {row['retry_count']}/{row['max_retries']}")
    return " | ".join(bits)


# ── Loki tools ─────────────────────────────────────────────────────────────
async def _tool_start(args: dict, ctx: ToolContext) -> str:
    ttype = str(args.get("task_type") or "").strip()
    tt = _TYPES.get(ttype)
    startable = sorted(n for n, t in _TYPES.items() if not t.internal)
    if tt is None or tt.internal:
        return json.dumps({"ok": False,
                           "error": f"unknown task_type — choose one of {startable}"})
    if PERM_RANK[user_level(ctx.user_id)] < PERM_RANK[tt.permission]:
        return json.dumps({"ok": False,
                           "error": "you are not authorized to start this task type"})
    raw = args.get("input")
    if not isinstance(raw, dict):
        raw = {}
    clean, err = (tt.validate(raw, ctx) if tt.validate else (raw, ""))
    if err:
        return json.dumps({"ok": False, "error": err})
    task_id = submit(tt, ctx, clean)
    row = get_task(task_id)
    return json.dumps({"ok": True, "task_id": task_id, "status": row["status"],
                       "title": row["title"],
                       "note": "queued — I'll post updates here as it progresses"})


async def _tool_status(args: dict, ctx: ToolContext) -> str:
    row = get_task(str(args.get("task_id") or "").strip())
    if not row:
        return json.dumps({"ok": False, "error": "no such task"})
    if not _can_access(ctx, row):
        return json.dumps({"ok": False, "error": "that task is not yours to view"})
    # Explicitly requested by id → the user is asking about THIS task, so the
    # stored result may be repeated.
    return json.dumps({"ok": True, "task": _fmt_task(row, explicit=True)})


async def _tool_list(args: dict, ctx: ToolContext) -> str:
    active_only = args.get("active_only", True)
    rows = [r for r in _query() if _can_access(ctx, r)]
    if active_only:
        rows = [r for r in rows if r["status"] in OPEN]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    out = [_fmt_task(r) for r in rows[:20]]
    return json.dumps({"ok": True, "tasks": out or ["no tasks"]})


async def _tool_cancel(args: dict, ctx: ToolContext) -> str:
    task_id = str(args.get("task_id") or "").strip()
    row = get_task(task_id)
    if not row:
        return json.dumps({"ok": False, "error": "no such task"})
    if not _can_access(ctx, row):
        return json.dumps({"ok": False, "error": "that task is not yours to cancel"})
    if row["status"] in TERMINAL:
        return json.dumps({"ok": False, "error": f"already {row['status']}"})
    run = _running.get(task_id)
    if run is not None:
        # Cooperative: the handler notices and propagates to its backend.
        run.handle.cancel_event.set()
        return json.dumps({"ok": True, "task_id": task_id,
                           "note": "cancelling — the worker is stopping it now"})
    # Not live (queued or orphaned): propagate to backend if the type supports
    # it, then mark cancelled ourselves.
    tt = _TYPES.get(row["task_type"])
    if tt and tt.on_cancel:
        try:
            await tt.on_cancel(row)
        except Exception:
            log.exception("task %s on_cancel failed", task_id)
    await _finalize(task_id, TaskResult("cancelled", summary="cancelled"))
    return json.dumps({"ok": True, "task_id": task_id, "status": "cancelled"})


async def _tool_retry(args: dict, ctx: ToolContext) -> str:
    task_id = str(args.get("task_id") or "").strip()
    row = get_task(task_id)
    if not row:
        return json.dumps({"ok": False, "error": "no such task"})
    if not _can_access(ctx, row):
        return json.dumps({"ok": False, "error": "that task is not yours to retry"})
    if row["status"] not in (TERMINAL - {"completed"}) | PAUSED:
        return json.dumps({"ok": False,
                           "error": f"only failed/cancelled/paused tasks can be retried "
                                    f"(this one is {row['status']})"})
    if row["retry_count"] >= row["max_retries"]:
        return json.dumps({"ok": False,
                           "error": f"retry limit reached ({row['max_retries']})"})
    now = time.time()
    # Re-run with the ORIGINAL validated input. A fresh request_id + cleared
    # external_ref make this a genuinely new attempt at the backend.
    _update(task_id, status="queued", retry_count=row["retry_count"] + 1,
            started_at=None, completed_at=None, heartbeat_at=now,
            result_summary="", error_category="", error_detail="",
            external_ref=None, request_id=uuid.uuid4().hex, last_announced=None)
    _pump()
    return json.dumps({"ok": True, "task_id": task_id, "status": "queued",
                       "note": f"retry {row['retry_count'] + 1}/{row['max_retries']} queued"})


def _p(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required}


def _register_tools():
    type_names = sorted(n for n, t in _TYPES.items() if not t.internal)
    register(ToolSpec(
        name="task_start",
        description=(
            "Start a long-running background task so the conversation stays "
            "responsive. Only for real background work (Career-Ops evaluations, "
            "diagnostics, research, self-tests) — NOT for quick answers. "
            "task_type must be one of the registered types; input is a small "
            "object that the task type validates."),
        parameters=_p({
            "task_type": {"type": "string", "enum": type_names or ["selftest"]},
            "input": {"type": "object",
                      "description": "task-specific parameters (validated per type)"},
        }, ["task_type"]),
        handler=_tool_start, permission="crew", timeout=30, redact_log=True,
    ))
    register(ToolSpec(
        name="task_status",
        description="Status of one background task by task_id (e.g. lt_ab12…).",
        parameters=_p({"task_id": {"type": "string"}}, ["task_id"]),
        handler=_tool_status, permission="crew", timeout=15,
    ))
    register(ToolSpec(
        name="task_list",
        description=("List background tasks. active_only (default true) hides "
                     "finished ones."),
        parameters=_p({"active_only": {"type": "boolean"}}, []),
        handler=_tool_list, permission="crew", timeout=15,
    ))
    register(ToolSpec(
        name="task_cancel",
        description="Cancel a queued or running background task by task_id.",
        parameters=_p({"task_id": {"type": "string"}}, ["task_id"]),
        handler=_tool_cancel, permission="crew", timeout=35,
    ))
    register(ToolSpec(
        name="task_retry",
        description=("Retry a failed, cancelled, or paused background task using "
                     "its original input."),
        parameters=_p({"task_id": {"type": "string"}}, ["task_id"]),
        handler=_tool_retry, permission="crew", timeout=15,
    ))


# ── Built-in task type: selftest (harmless, for wiring verification) ────────
async def _selftest_handler(h: TaskHandle) -> TaskResult:
    inp = h.input
    secs = int(inp.get("seconds", 3))
    for _ in range(secs):
        if h.cancelled():
            return TaskResult("cancelled", summary="cancelled mid-run")
        await asyncio.sleep(1)
        await h.beat()
    note = str(inp.get("note", "")).strip()
    return TaskResult("completed",
                      summary=f"self-test ok ({secs}s)" + (f" — {note}" if note else ""))


def _selftest_validate(raw: dict, ctx: ToolContext):
    secs = raw.get("seconds", 3)
    try:
        secs = max(1, min(10, int(secs)))
    except (TypeError, ValueError):
        return {}, "seconds must be an integer 1–10"
    note = str(raw.get("note", ""))[:200]
    return {"seconds": secs, "note": note}, ""


register_type(TaskType(
    name="selftest", handler=_selftest_handler, permission="crew",
    capabilities=frozenset(), default_timeout=60, max_retries=3,
    validate=_selftest_validate,
    title=lambda i: "self-test",
))


# ── First external adapter: Career-Ops (reuses career_ops bridge client) ────
async def _career_handler(h: TaskHandle) -> TaskResult:
    h.require_capability("career_ops_bridge")
    import career_ops
    inp = h.input
    ext = h.external_ref
    if not ext:
        # Submit once. The bridge dedups on request_id, so even a retried
        # submit can never create a second job for the same attempt.
        data = await career_ops._api("POST", "/v1/jobs", {
            "url": inp["url"], "profile": inp["profile"],
            "source": inp.get("source", "discord"),
            "requester": inp.get("requester", "boss"),
            "request_id": h.row.get("request_id") or uuid.uuid4().hex,
        })
        ext = data["job"]["id"]
        h.set_external_ref(ext)
        log.info("task %s -> career-ops job %s", h.task_id, ext)
    while True:
        if h.cancelled():
            try:
                await career_ops._api("POST", f"/v1/jobs/{ext}/cancel")
            except career_ops.BridgeError:
                pass
            return TaskResult("cancelled", summary="cancelled at bridge")
        try:
            job = (await career_ops._api("GET", f"/v1/jobs/{ext}"))["job"]
        except career_ops.BridgeError as e:
            return TaskResult("failed", error_category="bridge_unreachable",
                              error_detail=str(e), summary="bridge error")
        await h.beat()
        s = job["state"]
        if s == "completed":
            arts = [{"name": a["name"], "size": a["size"]}
                    for a in job.get("artifacts", [])]
            return TaskResult("completed", summary=career_ops._fmt_job(job),
                              artifacts=arts)
        if s == "failed":
            return TaskResult("failed", error_category="bridge_failed",
                              error_detail=job.get("error", ""),
                              summary=job.get("error", "evaluation failed"))
        if s == "cancelled":
            return TaskResult("cancelled", summary="cancelled at bridge")
        if s == "paused_auth":
            return TaskResult("paused_auth",
                              summary="Antigravity needs (re)auth on razr")
        if s == "paused_quota":
            return TaskResult("paused_quota",
                              summary="Antigravity model quota exhausted")
        await asyncio.sleep(CO_POLL_SECS)


async def _career_on_cancel(row: dict):
    import career_ops
    ext = row.get("external_ref")
    if ext:
        try:
            await career_ops._api("POST", f"/v1/jobs/{ext}/cancel")
        except Exception:
            pass


def _career_validate(raw: dict, ctx: ToolContext):
    import career_ops
    profile, err = career_ops._resolve_profile(ctx, raw.get("profile"))
    if err:
        return {}, err
    url = str(raw.get("url", "")).strip()
    if not url.lower().startswith(("http://", "https://")) or len(url) > 2048:
        return {}, "need a public http(s) job-posting URL"
    return {"url": url, "profile": profile,
            "source": "telegram" if ctx.channel_id.startswith("tg:") else "discord",
            "requester": user_level(ctx.user_id)}, ""


def _maybe_register_career_ops():
    try:
        import career_ops
    except Exception:
        return
    if not getattr(career_ops, "enabled", False):
        return
    register_type(TaskType(
        name="career_ops_eval", handler=_career_handler, permission="crew",
        capabilities=frozenset({"career_ops_bridge"}),
        default_timeout=int(os.getenv("CAREER_OPS_TASK_TIMEOUT_SECS", "3000")),
        max_retries=2, reattach=True,
        validate=_career_validate, on_cancel=_career_on_cancel,
        title=lambda i: f"Career-Ops eval ({i.get('profile', '?')})",
    ))
    log.info("Career-Ops registered as a supervised task type")


_maybe_register_career_ops()
_register_tools()
log.info("task supervisor module loaded (%d task types)", len(_TYPES))

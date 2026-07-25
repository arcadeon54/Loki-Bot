"""
career_ops.py — Loki as liaison to the Career-Ops installation on razr.

Talks to the career-ops-bridge JSON API (Bearer token, one queue, two
isolated profiles: boss / roommate). Loki never evaluates jobs itself and
never falls back to another runtime — razr's Antigravity does the work; if
it is quota-limited or needs auth the bridge parks the job as paused_* and
we just relay that honestly.

Authorization model (mirrors tools.user_level):
  boss  → may submit/read either profile
  crew  → roommate profile only, and only roommate jobs are visible
Registered with permission="crew" so nobody below crew sees the tools.

Env (never committed): CAREER_OPS_API_URL, CAREER_OPS_API_TOKEN,
CAREER_OPS_DEFAULT_PROFILE (default boss).

A small monitor polls active jobs ~once a minute and announces meaningful
state changes back to the conversation that submitted the job. Announced
states are persisted to career_ops_state.json (gitignored) BEFORE sending,
so a Loki restart cannot double-announce.
"""

import asyncio
import json
import logging
import os
import tempfile
import time
import uuid

import aiohttp

from tools import ToolSpec, ToolContext, register, user_level

log = logging.getLogger("CareerOps")

API_URL = os.getenv("CAREER_OPS_API_URL", "").rstrip("/")
API_TOKEN = os.getenv("CAREER_OPS_API_TOKEN", "")
DEFAULT_PROFILE = os.getenv("CAREER_OPS_DEFAULT_PROFILE", "boss")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "career_ops_state.json")
POLL_SECS = 60
TERMINAL = {"completed", "failed", "cancelled"}
PAUSED = {"paused_auth", "paused_quota"}
# Attachment ceilings per platform (bytes)
DISCORD_LIMIT = 10 * 1024 * 1024
TELEGRAM_LIMIT = 50 * 1024 * 1024

enabled = bool(API_URL and API_TOKEN)

_session_factory = None   # async () -> aiohttp.ClientSession
_send = None              # async (channel_id, text, file_path=None, filename=None)


def bind(session_factory, send):
    global _session_factory, _send
    _session_factory = session_factory
    _send = send


# ── Bridge client (token lives only in the header, never in logs) ──────────
class BridgeError(Exception):
    pass


async def _api(method: str, path: str, payload: dict | None = None,
               raw: bool = False):
    if not enabled:
        raise BridgeError("Career-Ops bridge is not configured")
    if _session_factory is None:
        raise BridgeError("Career-Ops bridge not bound yet")
    sess = await _session_factory()
    try:
        async with sess.request(
            method, f"{API_URL}{path}", json=payload,
            headers={"Authorization": f"Bearer {API_TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as r:
            if raw:
                if r.status != 200:
                    raise BridgeError(f"artifact download failed (HTTP {r.status})")
                return await r.read()
            data = await r.json(content_type=None)
            if r.status >= 400:
                raise BridgeError(data.get("error", f"HTTP {r.status}"))
            return data
    except aiohttp.ClientError as e:
        raise BridgeError(f"bridge unreachable ({type(e).__name__})") from e


# ── Per-caller profile authorization ───────────────────────────────────────
def _resolve_profile(ctx: ToolContext, requested: str | None) -> tuple[str | None, str]:
    """Returns (profile, err). Boss: any. Crew: roommate only."""
    level = user_level(ctx.user_id)
    want = (requested or "").strip().lower() or (
        DEFAULT_PROFILE if level == "boss" else "roommate")
    if want not in ("boss", "roommate"):
        return None, "profile must be 'boss' or 'roommate'"
    if level != "boss" and want != "roommate":
        return None, "you can only use the roommate profile"
    return want, ""


def _can_see(ctx: ToolContext, job: dict) -> bool:
    return user_level(ctx.user_id) == "boss" or job.get("profile") == "roommate"


# ── Durable announce-state (written before sending → no dup notifications) ─
def _load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"jobs": {}}


def _save_state(state: dict):
    tmp = STATE_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


def _track(job_id: str, channel_id: str, profile: str, state: str):
    st = _load_state()
    st["jobs"][job_id] = {"channel_id": channel_id, "profile": profile,
                          "last_announced": state, "ts": time.time()}
    _save_state(st)


# ── Formatting (concise; no report bodies, no CV content) ──────────────────
def _fmt_job(job: dict) -> str:
    bits = [f"{job['id']} [{job['profile']}] {job['state']}"]
    if job.get("error"):
        bits.append(f"({job['error']})")
    s = job.get("summary") or {}
    if job["state"] == "completed":
        for label, key in (("company", "company"), ("role", "role"),
                           ("fit", "score"), ("rec", "recommendation")):
            if s.get(key):
                bits.append(f"{label}: {s[key]}")
        if s.get("report_path"):
            bits.append(f"report: {s['report_path']}")
        if s.get("pdf_path"):
            bits.append(f"pdf: {s['pdf_path']}")
    return " | ".join(bits)


def _announce_text(job: dict) -> str:
    state = job["state"]
    tag = f"Career-Ops job {job['id']} ({job['profile']})"
    if state == "running":
        return f"▶️ {tag} started."
    if state == "completed":
        s = job.get("summary") or {}
        lines = [f"✅ {tag} completed."]
        if s.get("company") or s.get("role"):
            lines.append(f"{s.get('company') or '?'} — {s.get('role') or '?'}")
        if s.get("score"):
            lines.append(f"Fit score: {s['score']}")
        if s.get("recommendation"):
            lines.append(f"Recommendation: {s['recommendation']}")
        if s.get("report_path"):
            lines.append(f"Report: {s['report_path']}")
        if s.get("pdf_path"):
            lines.append(f"Tailored CV: {s['pdf_path']} — ask me to send it.")
        return "\n".join(lines)
    if state == "failed":
        return f"❌ {tag} failed: {job.get('error') or 'unknown error'}"
    if state == "cancelled":
        return f"🚫 {tag} cancelled."
    if state == "paused_quota":
        return (f"⏸️ {tag} is paused — Antigravity's model quota is exhausted. "
                "It will need a manual retry once quota resets.")
    if state == "paused_auth":
        return (f"⏸️ {tag} is paused — Antigravity needs (re)authentication "
                "on razr before it can run.")
    return f"{tag}: {state}"


# ── Monitor ────────────────────────────────────────────────────────────────
async def monitor_loop():
    if not enabled:
        return
    log.info("Career-Ops monitor online (poll every %ss)", POLL_SECS)
    while True:
        try:
            await _monitor_tick()
        except BridgeError as e:
            log.warning("Career-Ops monitor: %s", e)
        except Exception:
            log.exception("Career-Ops monitor tick failed")
        await asyncio.sleep(POLL_SECS)


async def _monitor_tick():
    st = _load_state()
    tracked = {jid: t for jid, t in st["jobs"].items()
               if t.get("last_announced") not in TERMINAL}
    if not tracked:
        return
    for jid, t in tracked.items():
        try:
            job = (await _api("GET", f"/v1/jobs/{jid}"))["job"]
        except BridgeError as e:
            if "not found" in str(e):
                st["jobs"].pop(jid, None)
                _save_state(st)
            continue
        if job["state"] == t["last_announced"]:
            continue
        # persist first — a crash between save and send loses one message
        # but can never duplicate one
        prev = t["last_announced"]
        t["last_announced"] = job["state"]
        st["jobs"][jid] = t
        _save_state(st)
        if _send is not None:
            try:
                await _send(t["channel_id"], _announce_text(job))
            except Exception:
                log.exception("Career-Ops announce failed for %s", jid)
                t["last_announced"] = prev
                st["jobs"][jid] = t
                _save_state(st)
                continue
        if job["state"] in TERMINAL:
            log.info("Career-Ops job %s reached %s — monitoring stopped",
                     jid, job["state"])


# ── Tool handlers ──────────────────────────────────────────────────────────
async def _submit(args: dict, ctx: ToolContext) -> str:
    profile, err = _resolve_profile(ctx, args.get("profile"))
    if err:
        return json.dumps({"ok": False, "error": err})
    url = (args.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")) or len(url) > 2048:
        return json.dumps({"ok": False,
                           "error": "need a public http(s) job posting URL"})
    try:
        data = await _api("POST", "/v1/jobs", {
            "url": url, "profile": profile,
            "source": "telegram" if ctx.channel_id.startswith("tg:") else "discord",
            "requester": user_level(ctx.user_id),
            "request_id": uuid.uuid4().hex,
        })
    except BridgeError as e:
        return json.dumps({"ok": False, "error": str(e)})
    job = data["job"]
    _track(job["id"], ctx.channel_id, profile, job["state"])
    log.info("Career-Ops submit %s (%s) by %s", job["id"], profile, ctx.user_name)
    return json.dumps({"ok": True, "job_id": job["id"], "state": job["state"],
                       "profile": profile,
                       "note": "queued — I'll post updates here as it progresses"})


async def _status(args: dict, ctx: ToolContext) -> str:
    jid = (args.get("job_id") or "").strip()
    try:
        job = (await _api("GET", f"/v1/jobs/{jid}"))["job"]
    except BridgeError as e:
        return json.dumps({"ok": False, "error": str(e)})
    if not _can_see(ctx, job):
        return json.dumps({"ok": False, "error": "that job is not yours to view"})
    return json.dumps({"ok": True, "job": _fmt_job(job)})


async def _list(args: dict, ctx: ToolContext) -> str:
    level = user_level(ctx.user_id)
    profile = (args.get("profile") or "").strip().lower() or None
    if level != "boss":
        profile = "roommate"
    q = []
    if profile:
        q.append(f"profile={profile}")
    if args.get("active_only"):
        q.append("active=1")
    try:
        data = await _api("GET", "/v1/jobs" + ("?" + "&".join(q) if q else ""))
    except BridgeError as e:
        return json.dumps({"ok": False, "error": str(e)})
    rows = [_fmt_job(j) for j in data["jobs"] if _can_see(ctx, j)][:20]
    return json.dumps({"ok": True, "jobs": rows or ["no jobs"]})


async def _cancel(args: dict, ctx: ToolContext) -> str:
    jid = (args.get("job_id") or "").strip()
    try:
        job = (await _api("GET", f"/v1/jobs/{jid}"))["job"]
        if not _can_see(ctx, job):
            return json.dumps({"ok": False, "error": "that job is not yours to cancel"})
        job = (await _api("POST", f"/v1/jobs/{jid}/cancel"))["job"]
    except BridgeError as e:
        return json.dumps({"ok": False, "error": str(e)})
    return json.dumps({"ok": True, "job": _fmt_job(job)})


async def _artifacts(args: dict, ctx: ToolContext) -> str:
    jid = (args.get("job_id") or "").strip()
    try:
        job = (await _api("GET", f"/v1/jobs/{jid}"))["job"]
        if not _can_see(ctx, job):
            return json.dumps({"ok": False, "error": "that job is not yours to view"})
        data = await _api("GET", f"/v1/jobs/{jid}/artifacts")
    except BridgeError as e:
        return json.dumps({"ok": False, "error": str(e)})
    arts = [f"{a['name']} ({a['size'] / 1024:.0f} KB)" for a in data["artifacts"]]
    return json.dumps({"ok": True, "artifacts": arts or ["none recorded"]})


async def _send_artifact(args: dict, ctx: ToolContext) -> str:
    jid = (args.get("job_id") or "").strip()
    name = (args.get("artifact_name") or "").strip()
    try:
        job = (await _api("GET", f"/v1/jobs/{jid}"))["job"]
        if not _can_see(ctx, job):
            return json.dumps({"ok": False, "error": "that job is not yours"})
        rec = next((a for a in job.get("artifacts", []) if a["name"] == name), None)
        if rec is None:
            return json.dumps({"ok": False,
                               "error": "unknown artifact — use career_job_artifacts first"})
        limit = TELEGRAM_LIMIT if ctx.channel_id.startswith("tg:") else DISCORD_LIMIT
        if rec["size"] > limit:
            return json.dumps({"ok": False, "error":
                               f"{name} is {rec['size'] / 1024 / 1024:.1f} MB — over this "
                               f"platform's {limit // 1024 // 1024} MB limit"})
        blob = await _api("GET", f"/v1/jobs/{jid}/artifacts/{name}", raw=True)
    except BridgeError as e:
        return json.dumps({"ok": False, "error": str(e)})
    if _send is None:
        return json.dumps({"ok": False, "error": "attachment sender not available"})
    fd, tmp = tempfile.mkstemp(prefix="loki_co_", suffix=os.path.splitext(name)[1] or ".bin")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(blob)
        await _send(ctx.channel_id, f"📎 {name} (Career-Ops job {jid})",
                    file_path=tmp, filename=name)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    log.info("Career-Ops artifact sent: job %s, %d bytes", jid, len(blob))
    return json.dumps({"ok": True, "sent": name})


# ── Registration (import side effect, same pattern as assistant_tools) ─────
def _p(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


if enabled:
    register(ToolSpec(
        name="career_job_submit",
        description=(
            "Queue a public job-posting URL for Career-Ops evaluation on razr "
            "(fit score, report, tailored CV PDF; it never submits applications). "
            "ONLY use when explicitly asked to evaluate/run a job through "
            "Career-Ops — never for random links. profile 'boss' (default) or "
            "'roommate'."),
        parameters=_p({"url": {"type": "string"},
                       "profile": {"type": "string", "enum": ["boss", "roommate"]}},
                      ["url"]),
        handler=_submit, permission="crew", timeout=35,
    ))
    register(ToolSpec(
        name="career_job_status",
        description="Status of one Career-Ops job by job_id (e.g. cj_ab12…).",
        parameters=_p({"job_id": {"type": "string"}}, ["job_id"]),
        handler=_status, permission="crew", timeout=35,
    ))
    register(ToolSpec(
        name="career_job_list",
        description=("List Career-Ops jobs. Optional profile filter "
                     "(boss/roommate) and active_only."),
        parameters=_p({"profile": {"type": "string"},
                       "active_only": {"type": "boolean"}}, []),
        handler=_list, permission="crew", timeout=35,
    ))
    register(ToolSpec(
        name="career_job_cancel",
        description="Cancel a queued or running Career-Ops job.",
        parameters=_p({"job_id": {"type": "string"}}, ["job_id"]),
        handler=_cancel, permission="crew", timeout=35,
    ))
    register(ToolSpec(
        name="career_job_artifacts",
        description="List the report/PDF files a completed Career-Ops job produced.",
        parameters=_p({"job_id": {"type": "string"}}, ["job_id"]),
        handler=_artifacts, permission="crew", timeout=35,
    ))
    register(ToolSpec(
        name="career_job_send_artifact",
        description=("Fetch one artifact (report/PDF) from a Career-Ops job and "
                     "send it as an attachment in this conversation."),
        parameters=_p({"job_id": {"type": "string"},
                       "artifact_name": {"type": "string"}},
                      ["job_id", "artifact_name"]),
        handler=_send_artifact, permission="crew", timeout=120,
    ))
    log.info("Career-Ops tools registered (bridge: configured)")
else:
    log.info("Career-Ops disabled — CAREER_OPS_API_URL/TOKEN not set")

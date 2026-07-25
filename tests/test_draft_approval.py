"""
Focused tests for the draft-and-approval gate (draft_approval.py).

No network, no real consequential action: a synthetic 'test_action' tool stands
in for a gated tool so we control execution success/failure, plus one test that
the REAL home_control tool is gated (staged, never run). Execution is driven
through the durable task supervisor's draft_exec task, run by hand.

Covers: draft creation, no execution before approval, valid approval, rejection,
expiration, payload-tamper detection, authorization boundaries, duplicate
approval, execution failure, and restart persistence.

Run:  venv/bin/python -m unittest tests.test_draft_approval -v
"""

import asyncio
import json
import os
import re
import tempfile
import unittest

BOSS_ID = "111111111111111111"
CREW_ID = "222222222222222222"

os.environ["OWNER_USER_ID"] = BOSS_ID
os.environ["CREW_USER_IDS"] = CREW_ID
os.environ["DRAFTS_DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["TASKS_DB_PATH"] = tempfile.mktemp(suffix=".db")

import tools
tools.OWNER_USER_ID = BOSS_ID
tools.CREW_USER_IDS = {CREW_ID}

import assistant_tools   # registers the real (gated) home_control tool
import task_supervisor as ts
import draft_approval as da   # installs the intercept + draft_exec task type

# A synthetic consequential tool we fully control (never touches production).
EXEC: list = []


async def _exec_handler(args, ctx):
    if args.get("boom"):
        raise RuntimeError("exec boom")
    EXEC.append((args.get("what"), ctx.user_id))
    return "did: " + str(args.get("what", ""))


def _prep(args, ctx):
    what = str(args.get("what", "")).strip()
    if not what:
        return {}, "", "need 'what'"
    return {"what": what, "boom": bool(args.get("boom"))}, f"do {what}", ""


tools.register(tools.ToolSpec(
    name="test_action", description="synthetic gated action",
    parameters={"type": "object", "properties": {"what": {"type": "string"}}},
    handler=_exec_handler, permission="crew",
    action_type="test", approval_ttl=60, prepare=_prep, redact_log=True,
))


def ctx(user_id, channel="tg:424242"):
    return tools.ToolContext(user_id=user_id, user_name="u" + user_id[-1],
                             channel_id=channel)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


DRAFT_RE = re.compile(r"dr_[0-9a-f]{12}")


class Base(unittest.TestCase):
    def setUp(self):
        da.DB_PATH = tempfile.mktemp(suffix=".db")
        da._conn = None
        da._db()
        ts.DB_PATH = tempfile.mktemp(suffix=".db")
        ts._conn = None
        ts._running.clear()
        ts._started = False
        ts.MAX_CONCURRENCY = 1
        ts._send = None
        ts._db()
        EXEC.clear()

    # Stage a draft through the REAL tools.execute() path (the gate).
    def stage(self, what="turn on lamp", user=BOSS_ID, channel="tg:1", boom=False):
        msg = run(tools.execute(
            "test_action",
            json.dumps({"what": what, "boom": boom}), ctx(user, channel)))
        m = DRAFT_RE.search(msg)
        return (m.group(0) if m else None), msg

    # Run the approved draft's executor task to completion.
    def drive(self, task_id):
        tt = ts._TYPES["draft_exec"]
        handle = ts.TaskHandle(task_id, tt.capabilities)
        ts._update(task_id, status="running")
        run(ts._run(task_id, tt, handle))


class CreationTests(Base):
    def test_draft_created_not_executed(self):
        did, msg = self.stage("turn on lamp")
        self.assertIsNotNone(did)
        self.assertIn("Draft prepared", msg)
        self.assertIn("draft_approve", msg)
        d = da.get_draft(did)
        self.assertEqual(d["status"], "pending")
        self.assertEqual(json.loads(d["payload_json"])["what"], "turn on lamp")
        self.assertTrue(d["payload_hash"])
        self.assertEqual(EXEC, [])            # NOTHING ran

    def test_home_control_is_gated(self):
        # The real consequential tool stages a draft and never executes.
        msg = run(tools.execute(
            "home_control", json.dumps({"request": "turn off the bedroom lamp"}),
            ctx(BOSS_ID)))
        self.assertIn("Draft prepared", msg)
        did = DRAFT_RE.search(msg).group(0)
        d = da.get_draft(did)
        self.assertEqual(d["action_type"], "ha_control")
        self.assertEqual(json.loads(d["payload_json"])["request"],
                         "turn off the bedroom lamp")
        self.assertEqual(d["status"], "pending")


class ApprovalTests(Base):
    def test_valid_approval_executes(self):
        did, _ = self.stage("turn on lamp", user=BOSS_ID)
        out = json.loads(run(da._tool_approve({"draft_id": did}, ctx(BOSS_ID))))
        self.assertTrue(out["ok"])
        self.assertEqual(da.get_draft(did)["status"], "approved")
        self.assertEqual(da.get_draft(did)["executing_task_id"], out["task_id"])
        self.drive(out["task_id"])
        self.assertEqual(da.get_draft(did)["status"], "executed")
        self.assertEqual([w for w, _ in EXEC], ["turn on lamp"])

    def test_rejection_blocks_execution(self):
        did, _ = self.stage("turn on lamp")
        out = json.loads(run(da._tool_reject({"draft_id": did}, ctx(BOSS_ID))))
        self.assertTrue(out["ok"])
        self.assertEqual(da.get_draft(did)["status"], "rejected")
        self.assertEqual(EXEC, [])

    def test_duplicate_approval_refused(self):
        did, _ = self.stage("turn on lamp", user=BOSS_ID)
        first = json.loads(run(da._tool_approve({"draft_id": did}, ctx(BOSS_ID))))
        self.assertTrue(first["ok"])
        second = json.loads(run(da._tool_approve({"draft_id": did}, ctx(BOSS_ID))))
        self.assertFalse(second["ok"])
        self.assertIn("not pending", second["error"])

    def test_execution_failure_marks_failed(self):
        did, _ = self.stage("explode", user=BOSS_ID, boom=True)
        out = json.loads(run(da._tool_approve({"draft_id": did}, ctx(BOSS_ID))))
        self.assertTrue(out["ok"])
        self.drive(out["task_id"])
        self.assertEqual(da.get_draft(did)["status"], "failed")
        self.assertEqual(EXEC, [])


class ExpiryTests(Base):
    def test_expired_draft_cannot_execute(self):
        did, _ = self.stage("turn on lamp")
        da._update(did, expires_at=1.0)       # already in the past
        out = json.loads(run(da._tool_approve({"draft_id": did}, ctx(BOSS_ID))))
        self.assertFalse(out["ok"])
        self.assertEqual(da.get_draft(did)["status"], "expired")
        self.assertEqual(EXEC, [])


class TamperTests(Base):
    def test_payload_tamper_detected(self):
        did, _ = self.stage("turn on lamp", user=BOSS_ID)
        # Mutate the payload but NOT the stored hash — simulates a tampered row.
        da._update(did, payload_json=json.dumps({"what": "unlock front door",
                                                 "boom": False}))
        out = json.loads(run(da._tool_approve({"draft_id": did}, ctx(BOSS_ID))))
        self.assertFalse(out["ok"])
        self.assertIn("modified", out["error"])
        self.assertEqual(da.get_draft(did)["status"], "failed")
        self.assertEqual(EXEC, [])

    def test_tamper_caught_at_execution_too(self):
        # Even if a draft is validly approved, the executor re-verifies.
        did, _ = self.stage("turn on lamp", user=BOSS_ID)
        out = json.loads(run(da._tool_approve({"draft_id": did}, ctx(BOSS_ID))))
        da._update(did, payload_json=json.dumps({"what": "unlock", "boom": False}))
        self.drive(out["task_id"])
        self.assertEqual(da.get_draft(did)["status"], "failed")
        self.assertEqual(EXEC, [])


class AuthzTests(Base):
    def test_crew_cannot_approve_boss_draft(self):
        did, _ = self.stage("turn on lamp", user=BOSS_ID)
        out = json.loads(run(da._tool_approve({"draft_id": did}, ctx(CREW_ID))))
        self.assertFalse(out["ok"])
        self.assertEqual(da.get_draft(did)["status"], "pending")   # untouched
        self.assertEqual(EXEC, [])

    def test_crew_cannot_view_boss_draft(self):
        did, _ = self.stage("turn on lamp", user=BOSS_ID)
        out = json.loads(run(da._tool_view({"draft_id": did}, ctx(CREW_ID))))
        self.assertFalse(out["ok"])

    def test_crew_can_approve_own_draft(self):
        did, _ = self.stage("turn on lamp", user=CREW_ID)
        out = json.loads(run(da._tool_approve({"draft_id": did}, ctx(CREW_ID))))
        self.assertTrue(out["ok"])
        self.drive(out["task_id"])
        self.assertEqual(da.get_draft(did)["status"], "executed")

    def test_boss_can_approve_crew_draft(self):
        did, _ = self.stage("turn on lamp", user=CREW_ID)
        out = json.loads(run(da._tool_approve({"draft_id": did}, ctx(BOSS_ID))))
        self.assertTrue(out["ok"])

    def test_list_hides_other_users_drafts(self):
        boss_did, _ = self.stage("boss thing", user=BOSS_ID)
        crew_did, _ = self.stage("crew thing", user=CREW_ID)
        out = json.loads(run(da._tool_list({}, ctx(CREW_ID))))
        joined = " ".join(out["drafts"])
        self.assertIn(crew_did, joined)
        self.assertNotIn(boss_did, joined)


class PersistenceTests(Base):
    def test_restart_persistence(self):
        did, _ = self.stage("turn on lamp")
        h = da.get_draft(did)["payload_hash"]
        da._conn = None                       # simulate a restart
        d = da.get_draft(did)
        self.assertIsNotNone(d)
        self.assertEqual(d["status"], "pending")
        self.assertEqual(d["payload_hash"], h)
        self.assertTrue(da._payload_intact(d))


if __name__ == "__main__":
    unittest.main()

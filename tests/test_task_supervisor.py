"""
Focused tests for the durable task supervisor (task_supervisor.py).

No network, no real Career-Ops bridge, no live Loki: the bridge client
(career_ops._api) is stubbed and notifications are captured in-memory. Covers
the durable queue, state transitions, heartbeat timeout, orphan recovery,
cancel, retry, duplicate-notification prevention, authorization, the Career-Ops
adapter's restart reconciliation (no duplicate submits), and restart persistence.

Run:  venv/bin/python -m unittest tests.test_task_supervisor -v
"""

import asyncio
import importlib
import json
import os
import tempfile
import unittest

BOSS_ID = "111111111111111111"
CREW_ID = "222222222222222222"

os.environ["OWNER_USER_ID"] = BOSS_ID
os.environ["CREW_USER_IDS"] = CREW_ID
# Give the Career-Ops adapter something to register against (never contacted).
os.environ["CAREER_OPS_API_URL"] = "http://bridge.invalid:1"
os.environ["CAREER_OPS_API_TOKEN"] = "unit-test-token-abcdefghijklmnopqrstuvwxyz012345"

import tools
tools.OWNER_USER_ID = BOSS_ID
tools.CREW_USER_IDS = {CREW_ID}

import career_ops
importlib.reload(career_ops)

import task_supervisor as ts


def ctx(user_id, channel="tg:424242"):
    return tools.ToolContext(user_id=user_id, user_name="t", channel_id=channel)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class Base(unittest.TestCase):
    def setUp(self):
        # Fresh DB per test; keep the worker pump quiet so we drive _run/sweep
        # deterministically.
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        ts.DB_PATH = self.tmp.name
        ts._conn = None
        ts._running.clear()
        ts._started = False
        ts.MAX_CONCURRENCY = 1
        self.sent = []

        async def fake_send(channel_id, text, file_path=None, filename=None):
            self.sent.append((channel_id, text))

        ts._send = fake_send
        ts._db()  # create schema

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def submit(self, task_type, user=BOSS_ID, channel="tg:424242", inp=None):
        tt = ts._TYPES[task_type]
        clean, err = (tt.validate(inp or {}, ctx(user, channel))
                      if tt.validate else (inp or {}, ""))
        assert not err, err
        return ts.submit(tt, ctx(user, channel), clean)


# ── durable queue + restart persistence ────────────────────────────────────
class QueueTests(Base):
    def test_submit_persists_queued(self):
        tid = self.submit("selftest", inp={"seconds": 1})
        row = ts.get_task(tid)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["task_type"], "selftest")
        self.assertEqual(row["requester_id"], BOSS_ID)
        self.assertEqual(json.loads(row["input_json"])["seconds"], 1)

    def test_restart_persistence(self):
        tid = self.submit("selftest", inp={"seconds": 1})
        # Simulate a process restart: drop the connection, reopen same file.
        ts._conn = None
        row = ts.get_task(tid)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "queued")


# ── state transitions ───────────────────────────────────────────────────────
class TransitionTests(Base):
    def test_full_lifecycle_to_completed(self):
        tid = self.submit("selftest", inp={"seconds": 1, "note": "hi"})
        tt = ts._TYPES["selftest"]
        ts._update(tid, status="running", started_at=1, heartbeat_at=1)
        run(ts._run(tid, tt, ts.TaskHandle(tid, tt.capabilities)))
        row = ts.get_task(tid)
        self.assertEqual(row["status"], "completed")
        self.assertIn("self-test ok", row["result_summary"])
        self.assertIsNotNone(row["completed_at"])
        states = [t for _, t in self.sent]
        self.assertTrue(any("started" in s for s in states))
        self.assertTrue(any("done" in s for s in states))

    def test_handler_exception_marks_failed(self):
        async def boom(h):
            raise RuntimeError("kaboom")
        ts.register_type(ts.TaskType(name="boom", handler=boom,
                                     validate=lambda r, c: ({}, "")))
        tid = self.submit("boom")
        tt = ts._TYPES["boom"]
        run(ts._run(tid, tt, ts.TaskHandle(tid, tt.capabilities)))
        row = ts.get_task(tid)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_category"], "exception")


# ── heartbeat timeout ───────────────────────────────────────────────────────
class HeartbeatTests(Base):
    def test_live_task_times_out(self):
        async def scenario():
            async def hang(h):
                await asyncio.sleep(100)
            ts.register_type(ts.TaskType(name="hang", handler=hang,
                                         default_timeout=1,
                                         validate=lambda r, c: ({}, "")))
            tid = self.submit("hang")
            tt = ts._TYPES["hang"]
            handle = ts.TaskHandle(tt.name, tt.capabilities)
            handle.task_id = tid
            # Mark running with a stale heartbeat and register a live worker.
            ts._update(tid, status="running", started_at=0, heartbeat_at=0,
                       timeout_secs=1)
            worker = asyncio.create_task(hang(handle))
            ts._running[tid] = ts._Run(worker, handle)
            await ts.sweep_stale()
            try:
                await worker           # drain the cancellation
            except asyncio.CancelledError:
                pass
            return ts.get_task(tid)
        row = run(scenario())
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_category"], "timeout")


# ── orphan recovery / restart reconciliation ───────────────────────────────
class OrphanTests(Base):
    def test_non_reattach_orphan_marked_interrupted(self):
        tid = self.submit("selftest", inp={"seconds": 1})
        # A 'running' row from a previous process, no live worker here.
        ts._update(tid, status="running", started_at=0, heartbeat_at=0)
        run(ts._recover_one(ts.get_task(tid)))
        row = ts.get_task(tid)
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_category"], "interrupted")

    def test_career_orphan_reattaches_without_resubmit(self):
        calls = []

        async def fake_api(method, path, payload=None, raw=False):
            calls.append((method, path))
            if method == "POST" and path == "/v1/jobs":
                raise AssertionError("must NOT re-submit an existing career job")
            if method == "GET":
                return {"job": {"id": "cj_existing123", "state": "completed",
                                "profile": "boss", "artifacts": []}}
            return {"job": {}}

        orig = career_ops._api
        career_ops._api = fake_api
        try:
            tid = self.submit("career_ops_eval", inp={
                "url": "https://example.com/job", "profile": "boss"})
            # It already ran once and has a bridge job id; process died mid-run.
            ts._update(tid, status="running", external_ref="cj_existing123",
                       started_at=0, heartbeat_at=0)
            # Recover: reattach queues it; then run the handler by hand.
            run(ts._recover_one(ts.get_task(tid)))
            self.assertEqual(ts.get_task(tid)["status"], "queued")
            tt = ts._TYPES["career_ops_eval"]
            handle = ts.TaskHandle(tid, tt.capabilities)
            ts._update(tid, status="running")
            run(ts._run(tid, tt, handle))
        finally:
            career_ops._api = orig
        row = ts.get_task(tid)
        self.assertEqual(row["status"], "completed")
        self.assertNotIn(("POST", "/v1/jobs"), calls)   # never re-submitted
        self.assertIn(("GET", "/v1/jobs/cj_existing123"), calls)


# ── cancel ───────────────────────────────────────────────────────────────────
class CancelTests(Base):
    def test_cancel_queued_task(self):
        tid = self.submit("selftest", inp={"seconds": 5})
        out = json.loads(run(ts._tool_cancel({"task_id": tid}, ctx(BOSS_ID))))
        self.assertTrue(out["ok"])
        self.assertEqual(ts.get_task(tid)["status"], "cancelled")

    def test_cancel_live_task_signals_handler(self):
        tid = self.submit("selftest", inp={"seconds": 5})
        tt = ts._TYPES["selftest"]
        handle = ts.TaskHandle(tid, tt.capabilities)
        ts._running[tid] = ts._Run(asyncio.Future(), handle)
        ts._update(tid, status="running")
        out = json.loads(run(ts._tool_cancel({"task_id": tid}, ctx(BOSS_ID))))
        self.assertTrue(out["ok"])
        self.assertTrue(handle.cancel_event.is_set())

    def test_cancelled_selftest_finalizes_cancelled(self):
        tid = self.submit("selftest", inp={"seconds": 5})
        tt = ts._TYPES["selftest"]
        handle = ts.TaskHandle(tid, tt.capabilities)
        handle.cancel_event.set()
        ts._update(tid, status="running", started_at=1, heartbeat_at=1)
        run(ts._run(tid, tt, handle))
        self.assertEqual(ts.get_task(tid)["status"], "cancelled")


# ── retry ────────────────────────────────────────────────────────────────────
class RetryTests(Base):
    def test_retry_reuses_input_and_new_request_id(self):
        tid = self.submit("selftest", inp={"seconds": 2, "note": "orig"})
        old_req = ts.get_task(tid)["request_id"]
        ts._update(tid, status="failed", error_category="exception",
                   external_ref="stale")
        out = json.loads(run(ts._tool_retry({"task_id": tid}, ctx(BOSS_ID))))
        self.assertTrue(out["ok"])
        row = ts.get_task(tid)
        self.assertEqual(row["status"], "queued")
        self.assertEqual(row["retry_count"], 1)
        self.assertEqual(json.loads(row["input_json"])["note"], "orig")  # same input
        self.assertNotEqual(row["request_id"], old_req)                  # fresh attempt
        self.assertIsNone(row["external_ref"])                          # cleared

    def test_retry_blocked_past_limit(self):
        tid = self.submit("selftest", inp={"seconds": 1})
        ts._update(tid, status="failed", retry_count=3, max_retries=3)
        out = json.loads(run(ts._tool_retry({"task_id": tid}, ctx(BOSS_ID))))
        self.assertFalse(out["ok"])
        self.assertIn("limit", out["error"])

    def test_completed_cannot_be_retried(self):
        tid = self.submit("selftest", inp={"seconds": 1})
        ts._update(tid, status="completed")
        out = json.loads(run(ts._tool_retry({"task_id": tid}, ctx(BOSS_ID))))
        self.assertFalse(out["ok"])


# ── duplicate-notification prevention ───────────────────────────────────────
class NotifyTests(Base):
    def test_same_transition_announced_once(self):
        tid = self.submit("selftest", inp={"seconds": 1})
        ts._update(tid, status="completed", result_summary="done")
        run(ts._maybe_announce(tid))
        run(ts._maybe_announce(tid))   # second call is a no-op
        completes = [t for _, t in self.sent if "done" in t]
        self.assertEqual(len(completes), 1)

    def test_no_reannounce_after_restart(self):
        tid = self.submit("selftest", inp={"seconds": 1})
        ts._update(tid, status="completed", last_announced="completed")
        ts._conn = None                # simulate restart, state reread from disk
        run(ts._maybe_announce(tid))
        self.assertEqual(self.sent, [])


# ── authorization ───────────────────────────────────────────────────────────
class AuthzTests(Base):
    def test_crew_cannot_view_boss_task(self):
        tid = self.submit("selftest", user=BOSS_ID, inp={"seconds": 1})
        out = json.loads(run(ts._tool_status({"task_id": tid}, ctx(CREW_ID))))
        self.assertFalse(out["ok"])
        self.assertIn("not yours", out["error"])

    def test_crew_cannot_cancel_boss_task(self):
        tid = self.submit("selftest", user=BOSS_ID, inp={"seconds": 1})
        out = json.loads(run(ts._tool_cancel({"task_id": tid}, ctx(CREW_ID))))
        self.assertFalse(out["ok"])
        self.assertEqual(ts.get_task(tid)["status"], "queued")  # untouched

    def test_crew_list_hides_boss_tasks(self):
        self.submit("selftest", user=BOSS_ID, inp={"seconds": 1})
        mine = self.submit("selftest", user=CREW_ID, inp={"seconds": 1})
        out = json.loads(run(ts._tool_list({"active_only": True}, ctx(CREW_ID))))
        self.assertTrue(out["ok"])
        self.assertTrue(all(mine in row or "no tasks" == row or row.startswith(mine)
                            for row in out["tasks"]) or len(out["tasks"]) == 1)
        joined = " ".join(out["tasks"])
        self.assertIn(mine, joined)

    def test_boss_sees_everything(self):
        a = self.submit("selftest", user=BOSS_ID, inp={"seconds": 1})
        b = self.submit("selftest", user=CREW_ID, inp={"seconds": 1})
        out = json.loads(run(ts._tool_list({"active_only": True}, ctx(BOSS_ID))))
        joined = " ".join(out["tasks"])
        self.assertIn(a, joined)
        self.assertIn(b, joined)

    def test_start_rejects_unknown_task_type(self):
        out = json.loads(run(ts._tool_start(
            {"task_type": "rm -rf", "input": {}}, ctx(BOSS_ID))))
        self.assertFalse(out["ok"])
        self.assertIn("unknown task_type", out["error"])


# ── Career-Ops adapter: profile authorization at creation ───────────────────
class CareerAdapterTests(Base):
    def test_crew_cannot_start_boss_profile(self):
        out = json.loads(run(ts._tool_start(
            {"task_type": "career_ops_eval",
             "input": {"url": "https://example.com/j", "profile": "boss"}},
            ctx(CREW_ID))))
        self.assertFalse(out["ok"])

    def test_non_http_url_rejected(self):
        out = json.loads(run(ts._tool_start(
            {"task_type": "career_ops_eval",
             "input": {"url": "file:///etc/passwd", "profile": "boss"}},
            ctx(BOSS_ID))))
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()

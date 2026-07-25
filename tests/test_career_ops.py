"""
Mocked tests for the Career-Ops liaison (career_ops.py). No network, no real
bridge: the API layer is stubbed. Verifies profile authorization (boss vs
crew), monitor announce-once semantics across restarts, state-file
persistence, artifact size limits, and that the bearer token never reaches
logs or tool output.

Run:  venv/bin/python -m unittest tests.test_career_ops -v
"""

import asyncio
import importlib
import json
import logging
import os
import tempfile
import unittest

FAKE_TOKEN = "unit-test-token-abcdefghijklmnopqrstuvwxyz012345"
BOSS_ID = "111111111111111111"
CREW_ID = "222222222222222222"

os.environ["CAREER_OPS_API_URL"] = "http://bridge.invalid:1"
os.environ["CAREER_OPS_API_TOKEN"] = FAKE_TOKEN
os.environ.setdefault("OWNER_USER_ID", BOSS_ID)
os.environ.setdefault("CREW_USER_IDS", CREW_ID)

import tools
tools.OWNER_USER_ID = BOSS_ID
tools.CREW_USER_IDS = {CREW_ID}

import career_ops
importlib.reload(career_ops)  # pick up env + patched identities


def ctx(user_id, channel="tg:424242"):
    return tools.ToolContext(user_id=user_id, user_name="t", channel_id=channel)


def run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


class FakeApi:
    """Replaces career_ops._api; records calls, serves canned jobs."""

    def __init__(self, jobs=None):
        self.jobs = jobs or {}
        self.calls = []

    async def __call__(self, method, path, payload=None, raw=False):
        self.calls.append((method, path, payload))
        if method == "POST" and path == "/v1/jobs":
            jid = "cj_%012x" % (len(self.jobs) + 1)
            job = {"id": jid, "state": "queued", "profile": payload["profile"],
                   "artifacts": [], "summary": None, "error": None}
            self.jobs[jid] = job
            return {"job": job}
        if raw:
            return b"%PDF-fake"
        jid = path.split("/")[3] if path.startswith("/v1/jobs/") else None
        if jid:
            if jid not in self.jobs:
                raise career_ops.BridgeError("job not found")
            if path.endswith("/artifacts"):
                return {"artifacts": self.jobs[jid].get("artifacts", [])}
            return {"job": self.jobs[jid]}
        return {"jobs": list(self.jobs.values())}


class Base(unittest.TestCase):
    def setUp(self):
        fd, self.state = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.state)
        self._orig_state = career_ops.STATE_FILE
        career_ops.STATE_FILE = self.state
        self._orig_api = career_ops._api
        self.sent = []

        async def sender(channel_id, text, file_path=None, filename=None):
            self.sent.append((channel_id, text, filename))
        career_ops._send = sender

    def tearDown(self):
        career_ops.STATE_FILE = self._orig_state
        career_ops._api = self._orig_api
        career_ops._send = None
        try:
            os.unlink(self.state)
        except OSError:
            pass


class AuthzTests(Base):
    def test_boss_can_pick_either_profile(self):
        for want in ("boss", "roommate", None):
            profile, err = career_ops._resolve_profile(ctx(BOSS_ID), want)
            self.assertEqual(err, "")
            self.assertEqual(profile, want or "boss")

    def test_crew_forced_to_roommate(self):
        profile, err = career_ops._resolve_profile(ctx(CREW_ID), None)
        self.assertEqual((profile, err), ("roommate", ""))

    def test_crew_cannot_use_boss_profile(self):
        profile, err = career_ops._resolve_profile(ctx(CREW_ID), "boss")
        self.assertIsNone(profile)
        self.assertIn("roommate", err)

    def test_crew_cannot_view_boss_job(self):
        api = FakeApi({"cj_000000000001": {"id": "cj_000000000001",
                                           "state": "completed",
                                           "profile": "boss"}})
        career_ops._api = api
        out = json.loads(run(career_ops._status(
            {"job_id": "cj_000000000001"}, ctx(CREW_ID))))
        self.assertFalse(out["ok"])

    def test_crew_list_never_shows_boss_jobs(self):
        api = FakeApi({"a": {"id": "a", "state": "queued", "profile": "boss"},
                       "b": {"id": "b", "state": "queued", "profile": "roommate"}})
        career_ops._api = api
        out = json.loads(run(career_ops._list({}, ctx(CREW_ID))))
        self.assertTrue(all("boss" not in row for row in out["jobs"]))
        # and the query itself was scoped to roommate
        self.assertIn("profile=roommate", api.calls[-1][1])


class SubmitTests(Base):
    def test_submit_tracks_channel_for_monitor(self):
        career_ops._api = FakeApi()
        out = json.loads(run(career_ops._submit(
            {"url": "https://jobs.example.com/roles/42"}, ctx(BOSS_ID))))
        self.assertTrue(out["ok"])
        st = career_ops._load_state()
        self.assertEqual(st["jobs"][out["job_id"]]["channel_id"], "tg:424242")

    def test_submit_rejects_non_http(self):
        career_ops._api = FakeApi()
        out = json.loads(run(career_ops._submit(
            {"url": "file:///etc/passwd"}, ctx(BOSS_ID))))
        self.assertFalse(out["ok"])

    def test_source_follows_channel(self):
        api = FakeApi()
        career_ops._api = api
        run(career_ops._submit({"url": "https://x.example.com/j"},
                               ctx(BOSS_ID, channel="987654")))
        self.assertEqual(api.calls[-1][2]["source"], "discord")


class MonitorTests(Base):
    def _tick(self):
        run(career_ops._monitor_tick())

    def test_announces_each_transition_once(self):
        api = FakeApi({"cj_000000000001": {"id": "cj_000000000001",
                                           "state": "queued",
                                           "profile": "boss", "summary": None,
                                           "error": None, "artifacts": []}})
        career_ops._api = api
        career_ops._track("cj_000000000001", "tg:1", "boss", "queued")
        self._tick()
        self.assertEqual(len(self.sent), 0)          # no change → silent
        api.jobs["cj_000000000001"]["state"] = "running"
        self._tick()
        self._tick()                                  # same state again
        self.assertEqual(len(self.sent), 1)           # exactly one "started"
        api.jobs["cj_000000000001"]["state"] = "completed"
        self._tick()
        self._tick()
        self.assertEqual(len(self.sent), 2)
        self.assertIn("completed", self.sent[-1][1])

    def test_no_duplicate_after_restart(self):
        api = FakeApi({"cj_000000000001": {"id": "cj_000000000001",
                                           "state": "running",
                                           "profile": "boss", "summary": None,
                                           "error": None, "artifacts": []}})
        career_ops._api = api
        career_ops._track("cj_000000000001", "tg:1", "boss", "queued")
        self._tick()  # announces running; state persisted to disk
        # simulate a Loki restart: nothing in memory, state reloaded from disk
        self._tick()
        self.assertEqual(len(self.sent), 1)

    def test_terminal_states_stop_polling(self):
        api = FakeApi({"cj_000000000001": {"id": "cj_000000000001",
                                           "state": "failed", "profile": "boss",
                                           "summary": None, "error": "boom",
                                           "artifacts": []}})
        career_ops._api = api
        career_ops._track("cj_000000000001", "tg:1", "boss", "running")
        self._tick()
        self.assertEqual(len(self.sent), 1)
        api.calls.clear()
        self._tick()
        self.assertEqual(api.calls, [])               # no further polling

    def test_pause_states_reported_plainly(self):
        api = FakeApi({"cj_000000000001": {"id": "cj_000000000001",
                                           "state": "paused_quota",
                                           "profile": "boss", "summary": None,
                                           "error": None, "artifacts": []}})
        career_ops._api = api
        career_ops._track("cj_000000000001", "tg:1", "boss", "running")
        self._tick()
        self.assertIn("quota", self.sent[0][1].lower())


class ArtifactTests(Base):
    def _job(self, size):
        return {"cj_000000000001": {
            "id": "cj_000000000001", "state": "completed", "profile": "boss",
            "summary": None, "error": None,
            "artifacts": [{"name": "cv.pdf", "size": size}]}}

    def test_oversize_artifact_refused(self):
        career_ops._api = FakeApi(self._job(60 * 1024 * 1024))
        out = json.loads(run(career_ops._send_artifact(
            {"job_id": "cj_000000000001", "artifact_name": "cv.pdf"},
            ctx(BOSS_ID))))
        self.assertFalse(out["ok"])
        self.assertIn("limit", out["error"])

    def test_artifact_sent_and_temp_cleaned(self):
        career_ops._api = FakeApi(self._job(100))
        out = json.loads(run(career_ops._send_artifact(
            {"job_id": "cj_000000000001", "artifact_name": "cv.pdf"},
            ctx(BOSS_ID))))
        self.assertTrue(out["ok"])
        self.assertEqual(self.sent[0][2], "cv.pdf")
        import glob
        self.assertEqual(glob.glob(os.path.join(
            tempfile.gettempdir(), "loki_co_*")), [])

    def test_crew_platform_limit_discord(self):
        career_ops._api = FakeApi(self._job(20 * 1024 * 1024))
        out = json.loads(run(career_ops._send_artifact(
            {"job_id": "cj_000000000001", "artifact_name": "cv.pdf"},
            ctx(BOSS_ID, channel="987"))))   # discord → 10 MB cap
        self.assertFalse(out["ok"])


class SecretHygieneTests(Base):
    def test_token_never_in_logs_or_output(self):
        records = []
        handler = logging.Handler()
        handler.emit = lambda r: records.append(r.getMessage())
        career_ops.log.addHandler(handler)
        try:
            api = FakeApi()
            career_ops._api = api
            out = run(career_ops._submit(
                {"url": "https://jobs.example.com/1"}, ctx(BOSS_ID)))
            self._tickless = out
        finally:
            career_ops.log.removeHandler(handler)
        blob = " ".join(records) + out + " ".join(t for _, t, _ in self.sent)
        self.assertNotIn(FAKE_TOKEN, blob)


if __name__ == "__main__":
    unittest.main()

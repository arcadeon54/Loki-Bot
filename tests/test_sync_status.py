"""
Tests for the read-only Joplin sync-health check and honest status wording
(joplin_integration.sync_health / sync_summary, joplin_sync_status tool,
note_create message). No real Joplin, no network: the sidecar log is a temp
file and the Data API calls are stubbed.

Run:  venv/bin/python -m unittest tests.test_sync_status -v
"""

import json
import os
import tempfile
import time
import unittest

import joplin_integration as jp
import assistant_tools
from tools import ToolContext

OWNER_ID = "111111111111111111"


def log_lines(*bodies, ts=None):
    ts = ts or time.strftime("%Y-%m-%d %H:%M:%S")
    return "".join(f"{ts}: {b}\n" for b in bodies)


def attempt(error=None, ts=None):
    lines = ["Synchronizer: Sync: starting: Starting synchronisation to target 9..."]
    if error:
        lines.append(f"Synchronizer: [error] {error}")
    lines.append("Synchronizer: Operations completed: ")
    return log_lines(*lines, ts=ts)


class SyncHealthTests(unittest.TestCase):
    def setUp(self):
        self._orig = jp.JOPLIN_SYNC_LOG
        fd, self.log_path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        jp.JOPLIN_SYNC_LOG = self.log_path

    def tearDown(self):
        jp.JOPLIN_SYNC_LOG = self._orig
        os.unlink(self.log_path)

    def write(self, content):
        with open(self.log_path, "w") as f:
            f.write(content)

    def test_healthy_sync(self):
        self.write(attempt())
        h = jp.sync_health()
        self.assertEqual(h["state"], "healthy")
        self.assertIn("healthy", jp.sync_summary(h))

    def test_failing_sync_reports_error(self):
        self.write(attempt(
            error="Error: In order to synchronise, please upgrade your "
                  "application to version 3.7.0+"))
        h = jp.sync_health()
        self.assertEqual(h["state"], "failing")
        self.assertIn("3.7.0", h["detail"])
        self.assertIn("FAILING", jp.sync_summary(h))

    def test_latest_completed_attempt_wins(self):
        self.write(attempt(error="Error: old failure") + attempt())
        self.assertEqual(jp.sync_health()["state"], "healthy")

    def test_in_flight_attempt_ignored(self):
        in_flight = log_lines(
            "Synchronizer: Sync: starting: Starting synchronisation to target 9...")
        self.write(attempt() + in_flight)
        self.assertEqual(jp.sync_health()["state"], "healthy")

    def test_stale_attempt(self):
        old = time.strftime("%Y-%m-%d %H:%M:%S",
                            time.localtime(time.time() - 48 * 3600))
        self.write(attempt(ts=old))
        h = jp.sync_health()
        self.assertEqual(h["state"], "stale")

    def test_missing_log_is_unknown(self):
        os.unlink(self.log_path)
        open(self.log_path, "a").close()  # keep tearDown happy
        jp.JOPLIN_SYNC_LOG = self.log_path + ".does-not-exist"
        h = jp.sync_health()
        self.assertEqual(h["state"], "unknown")

    def test_empty_log_is_unknown(self):
        self.write("")
        self.assertEqual(jp.sync_health()["state"], "unknown")

    def test_credentials_never_surfaced(self):
        self.write(attempt(error="Error: request failed token=supersecret123"))
        h = jp.sync_health()
        self.assertNotIn("supersecret123", json.dumps(h))


class HonestToolMessageTests(unittest.IsolatedAsyncioTestCase):
    """note_create / joplin_sync_status wording follows actual sync state."""

    def setUp(self):
        self._orig = (jp.sync_health, jp.create_note, jp.ping)
        self.ctx = ToolContext(user_id=OWNER_ID, user_name="Boss")

    def tearDown(self):
        jp.sync_health, jp.create_note, jp.ping = self._orig

    def _stub(self, state):
        jp.sync_health = lambda: {"state": state, "last_attempt": "2026-07-19 12:00:00",
                                  "detail": "stub"}

        async def create_note(title, body, notebook=None, tags=None):
            return {"id": "abc123"}

        async def ping():
            return True

        jp.create_note = create_note
        jp.ping = ping

    async def test_note_create_no_unconditional_promise_when_failing(self):
        self._stub("failing")
        out = await assistant_tools._note_create(
            {"title": "T", "body": "b"}, self.ctx)
        self.assertIn("FAILING", out)
        self.assertNotIn("~5 min", out)

    async def test_note_create_promises_only_when_healthy(self):
        self._stub("healthy")
        out = await assistant_tools._note_create(
            {"title": "T", "body": "b"}, self.ctx)
        self.assertIn("healthy", out)

    async def test_sync_status_tool_reports_separately(self):
        self._stub("failing")
        out = json.loads(await assistant_tools._joplin_sync_status({}, self.ctx))
        self.assertEqual(out["local_api"], "up")
        self.assertEqual(out["device_sync"], "failing")
        self.assertIn("NOT show up", out["meaning"])


if __name__ == "__main__":
    unittest.main()

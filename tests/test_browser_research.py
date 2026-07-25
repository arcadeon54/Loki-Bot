"""
Focused tests for Loki's browser-research client (browser_research.py).

No network, no real worker: the worker HTTP call is stubbed. Covers the
client-side pieces of the validation list — authentication header, worker
cancellation propagation, Career-Ops JS-failure detection, screenshot delivery,
and public-URL gating. (SSRF rejection, redirect revalidation, timeout,
oversized, screenshot allowlisting, no-path access, and profile isolation are
covered by the RAZR worker's own Node tests + live self-test.)

Run:  venv/bin/python -m unittest tests.test_browser_research -v
"""

import asyncio
import base64
import json
import os
import tempfile
import unittest

os.environ["BROWSER_WORKER_URL"] = "http://100.87.97.120:8879"
os.environ["BROWSER_WORKER_TOKEN"] = "unit-test-token-abcdefghijklmnopqrstuvwxyz012345"
os.environ.setdefault("TASKS_DB_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("OWNER_USER_ID", "111111111111111111")

import tools
import browser_research as br


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class FakeHandle:
    def __init__(self, inp, channel="tg:99", cancelled=False):
        self._inp = inp
        self.row = {"channel_id": channel}
        self._cancelled = cancelled
        self.task_id = "lt_test"

    @property
    def input(self):
        return self._inp

    def cancelled(self):
        return self._cancelled

    async def beat(self):
        pass


# ── public-URL gating ────────────────────────────────────────────────────────
class UrlGateTests(unittest.TestCase):
    def test_accepts_public_rejects_private(self):
        self.assertTrue(br.is_public_url("https://example.com/jobs/1"))
        self.assertTrue(br.is_public_url("http://jobs.example.org/x?y=1"))
        for bad in ["http://127.0.0.1/", "http://localhost/", "http://10.0.0.5/",
                    "http://192.168.1.31:8877/", "http://169.254.169.254/",
                    "file:///etc/passwd", "ftp://x.com/", "http://intranet/"]:
            self.assertFalse(br.is_public_url(bad), bad)


# ── Career-Ops JS-failure detection ─────────────────────────────────────────
class DetectTests(unittest.TestCase):
    def test_detects_js_required(self):
        self.assertTrue(br.needs_browser_fallback("This page requires JavaScript to view."))
        self.assertTrue(br.needs_browser_fallback("Please enable JavaScript and reload."))
        self.assertTrue(br.needs_browser_fallback("empty extraction — content did not render"))

    def test_ignores_normal_results(self):
        self.assertFalse(br.needs_browser_fallback("Senior Backend Engineer at Acme, remote."))
        self.assertFalse(br.needs_browser_fallback(""))


# ── authentication header on the worker call ────────────────────────────────
class FakeResp:
    def __init__(self, data, status=200):
        self._d, self.status = data, status
    async def json(self, content_type=None):
        return self._d


class FakeCM:
    def __init__(self, resp):
        self.resp = resp
    async def __aenter__(self):
        return self.resp
    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, rec, resp):
        self.rec, self.resp = rec, resp
    def post(self, url, json=None, headers=None, timeout=None):
        self.rec.update(url=url, json=json, headers=headers)
        return FakeCM(self.resp)


class AuthTests(unittest.TestCase):
    def test_extract_sends_bearer_token(self):
        rec = {}
        resp = FakeResp({"ok": True, "title": "T", "final_url": "https://e.com", "text": "x"})

        async def factory():
            return FakeSession(rec, resp)

        br.bind(factory, None)
        out = run(br._extract("https://example.com/x", screenshot=False))
        self.assertEqual(rec["headers"]["Authorization"],
                         f"Bearer {os.environ['BROWSER_WORKER_TOKEN']}")
        self.assertEqual(rec["json"]["url"], "https://example.com/x")
        self.assertTrue(out["ok"])


# ── handler: screenshot delivery + cancellation ─────────────────────────────
class HandlerTests(unittest.TestCase):
    def test_success_delivers_screenshot(self):
        sent = []

        async def fake_send(channel_id, text, file_path=None, filename=None):
            sent.append((channel_id, text, file_path, os.path.exists(file_path or "")))

        png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 2000).decode()

        async def fake_extract(url, screenshot, timeout_ms=None):
            return {"title": "Careers", "final_url": url, "text": "We are hiring",
                    "screenshot": png}

        br.bind(None, fake_send)
        orig = br._extract
        br._extract = fake_extract
        try:
            res = run(br._handler(FakeHandle({"url": "https://e.com", "screenshot": True})))
        finally:
            br._extract = orig
        self.assertEqual(res.status, "completed")
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][2] and sent[0][2].endswith(".png"))
        self.assertTrue(sent[0][3])                    # file existed at send time

    def test_cancellation_propagates(self):
        async def slow_extract(url, screenshot, timeout_ms=None):
            await asyncio.sleep(5)
            return {"title": "x"}

        br.bind(None, None)
        orig = br._extract
        br._extract = slow_extract
        try:
            res = run(br._handler(FakeHandle({"url": "https://e.com"}, cancelled=True)))
        finally:
            br._extract = orig
        self.assertEqual(res.status, "cancelled")

    def test_worker_error_marks_failed(self):
        async def boom_extract(url, screenshot, timeout_ms=None):
            raise br.BrowserError("bridge unreachable")

        br.bind(None, None)
        orig = br._extract
        br._extract = boom_extract
        try:
            res = run(br._handler(FakeHandle({"url": "https://e.com"})))
        finally:
            br._extract = orig
        self.assertEqual(res.status, "failed")
        self.assertEqual(res.error_category, "browser")


# ── tool gating ──────────────────────────────────────────────────────────────
class ToolTests(unittest.TestCase):
    def test_tool_rejects_private_url(self):
        ctx = tools.ToolContext(user_id="111111111111111111", user_name="b", channel_id="tg:1")
        out = json.loads(run(br._tool_browse({"url": "http://127.0.0.1/admin"}, ctx)))
        self.assertFalse(out["ok"])

    def test_tool_and_task_type_registered(self):
        self.assertIn("browser_research", tools.REGISTRY)
        import task_supervisor as ts
        self.assertIn("browser_research", ts._TYPES)


if __name__ == "__main__":
    unittest.main()

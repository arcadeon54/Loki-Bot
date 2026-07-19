"""
Tests for tool-call log redaction (tools.py): redact_log tools log only call
metadata, and the deterministic scrubber masks credential-shaped values for
every other tool. No real services touched; the audit log goes to a temp file.

Run:  venv/bin/python -m unittest tests.test_log_redaction -v
"""

import json
import tempfile
import unittest

import tools
import assistant_tools  # noqa: F401 — registers the production tools

OWNER_ID = "111111111111111111"
FAKE_SECRET = "hunter2-not-a-real-value"


class ScrubberTests(unittest.TestCase):
    def test_masks_credential_patterns(self):
        cases = [
            f"Password to Dex247: {FAKE_SECRET}",
            f"password={FAKE_SECRET}",
            f"api_key = {FAKE_SECRET}",
            f"the API key for HA: {FAKE_SECRET}",
            f"token: {FAKE_SECRET}",
            f"client secret = {FAKE_SECRET}",
        ]
        for text in cases:
            out = tools._scrub_text(text)
            self.assertNotIn(FAKE_SECRET, out, text)
            self.assertIn("[REDACTED]", out, text)

    def test_leaves_benign_text_alone(self):
        for text in ("buy milk, eggs, bread",
                     "the guests passed the gate at 5pm",
                     "top secrets of great pizza dough",
                     "tokens of appreciation were given"):
            self.assertEqual(tools._scrub_text(text), text)

    def test_scrub_recurses_into_structures(self):
        args = {"text": f"password: {FAKE_SECRET}",
                "items": [f"token: {FAKE_SECRET}", "milk"],
                "nested": {"note": f"secret: {FAKE_SECRET}"},
                "count": 3}
        out = tools._scrub(args)
        self.assertNotIn(FAKE_SECRET, json.dumps(out))
        self.assertEqual(out["count"], 3)
        self.assertEqual(out["items"][1], "milk")


class RedactLogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._saved = (tools.OWNER_USER_ID, tools.TOOL_LOG_PATH)
        tools.OWNER_USER_ID = OWNER_ID
        self.tmplog = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        tools.TOOL_LOG_PATH = self.tmplog.name
        self.ctx = tools.ToolContext(user_id=OWNER_ID, user_name="Boss")

        async def sensitive_handler(args, ctx):
            return f"recalled: the password is {FAKE_SECRET}"

        async def normal_handler(args, ctx):
            return f"done; server reported password: {FAKE_SECRET} in output"

        tools.register(tools.ToolSpec(
            name="_test_sensitive", description="t", parameters={"type": "object"},
            handler=sensitive_handler, permission="boss", redact_log=True))
        tools.register(tools.ToolSpec(
            name="_test_normal", description="t", parameters={"type": "object"},
            handler=normal_handler, permission="boss"))

    async def asyncTearDown(self):
        tools.REGISTRY.pop("_test_sensitive", None)
        tools.REGISTRY.pop("_test_normal", None)
        (tools.OWNER_USER_ID, tools.TOOL_LOG_PATH) = self._saved

    def _audit_lines(self):
        with open(self.tmplog.name) as f:
            return [json.loads(l) for l in f if l.strip()]

    async def test_redact_log_withholds_args_and_detail(self):
        with self.assertLogs("Tools", level="INFO") as captured:
            result = await tools.execute(
                "_test_sensitive",
                json.dumps({"query": f"password {FAKE_SECRET}"}), self.ctx)
        # The MODEL still gets the full result — only logs are redacted.
        self.assertIn(FAKE_SECRET, result)
        entry = self._audit_lines()[-1]
        blob = json.dumps(entry) + "\n".join(captured.output)
        self.assertNotIn(FAKE_SECRET, blob)
        self.assertIn("chars withheld", blob)
        self.assertEqual(entry["tool"], "_test_sensitive")
        self.assertTrue(entry["ok"])

    async def test_scrubber_applies_to_normal_tools(self):
        with self.assertLogs("Tools", level="INFO") as captured:
            result = await tools.execute("_test_normal", "{}", self.ctx)
        self.assertIn(FAKE_SECRET, result)  # model sees the real output
        entry = self._audit_lines()[-1]
        blob = json.dumps(entry) + "\n".join(captured.output)
        self.assertNotIn(FAKE_SECRET, blob)
        self.assertIn("[REDACTED]", entry["detail"])

    async def test_production_flags(self):
        for name in ("remember", "recall_memory", "note_read"):
            self.assertTrue(tools.REGISTRY[name].redact_log, name)
        for name in ("list_create", "note_create", "web_search", "note_search"):
            self.assertFalse(tools.REGISTRY[name].redact_log, name)


if __name__ == "__main__":
    unittest.main()

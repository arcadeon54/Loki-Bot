"""
Mocked tests for the IVN Media Access invitation tool (assistant_tools.
create_ivn_invite). No network and no real gateway: the aiohttp layer is
stubbed, so nothing here can reach razr or mint a live code.

What is pinned: the tool is owner-only through the SAME registry gate both
interfaces use, the 24-hour ceiling is refused locally before any request is
sent, every network failure mode degrades to one honest sentence, an ambiguous
timeout is never retried, and neither the bearer credential nor the raw
invitation code reaches a log or the tool-call audit file.

This module deliberately does NOT import loki_bot: that module binds other
modules' production paths at import time, and `unittest discover` runs every
file in one process. The Discord/Telegram wiring is asserted by reading those
files as text instead.

Run:  venv/bin/python -m unittest tests.test_ivn_invite -v
"""

import asyncio
import importlib
import json
import logging
import os
import pathlib
import tempfile
import unittest

import aiohttp

FAKE_TOKEN = "unit-test-ivn-token-abcdefghijklmnopqrstuvwxyz0123456789"
FAKE_URL = "http://ivn.invalid:5692"
BOSS_ID = "111111111111111111"
CREW_ID = "222222222222222222"
STRANGER_ID = "333333333333333333"

os.environ["IVN_INTERNAL_API_URL"] = FAKE_URL
os.environ["IVN_INTERNAL_API_TOKEN"] = FAKE_TOKEN
os.environ.setdefault("OWNER_USER_ID", BOSS_ID)
os.environ.setdefault("CREW_USER_IDS", CREW_ID)

import tools  # noqa: E402

tools.OWNER_USER_ID = BOSS_ID
tools.CREW_USER_IDS = {CREW_ID}

import assistant_tools  # noqa: E402

# Another test file may have imported assistant_tools before the environment
# above was set, which would have frozen empty credentials into the module.
importlib.reload(assistant_tools)

REPO = pathlib.Path(__file__).resolve().parents[1]

GOOD_BODY = {
    "code": "AB7K-3M9Q",
    "portal": "https://join.ivn-group.cc",
    "profile": "standard",
    "access": "Standard",
    "label": "loki-20260911-a1b2c3",
    "expires_at": "2026-09-12T23:00:00+00:00",
    "expires_in_hours": 24,
}


# ── aiohttp stand-in ────────────────────────────────────────────────────────
# The real exception classes are reused so the handler's except clauses are
# exercised exactly as they behave in production.
class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    async def text(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, plan):
        self._plan = plan

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def post(self, url, json=None, headers=None, timeout=None):
        self._plan.calls.append(
            {"url": url, "json": json, "headers": headers or {}})
        if self._plan.raises is not None:
            raise self._plan.raises
        return _FakeResponse(self._plan.status, self._plan.body)


class FakeAiohttp:
    """Drop-in for the `aiohttp` name inside assistant_tools."""

    ClientError = aiohttp.ClientError
    ServerTimeoutError = aiohttp.ServerTimeoutError

    def __init__(self, status=201, body=None, raises=None):
        self.status = status
        self.raises = raises
        self.calls = []
        if body is None:
            body = json.dumps(GOOD_BODY)
        self.body = body if isinstance(body, str) else json.dumps(body)

    def ClientSession(self, *a, **kw):          # noqa: N802 - mirrors aiohttp
        return _FakeSession(self)

    def ClientTimeout(self, *a, **kw):          # noqa: N802 - mirrors aiohttp
        return None


def ctx(user_id=BOSS_ID, name="Boss", channel="chan"):
    return tools.ToolContext(user_id=user_id, user_name=name, channel_id=channel)


class IvnToolTestCase(unittest.TestCase):
    """Runs every call through tools.execute(), which is the exact path both
    Discord and Telegram use -- permission gate, audit log and all."""

    def setUp(self):
        self.fake = FakeAiohttp()
        self._real_aiohttp = assistant_tools.aiohttp
        assistant_tools.aiohttp = self.fake
        # Never let a test append to the live tool_calls.jsonl.
        self._real_log = tools.TOOL_LOG_PATH
        self._log = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        self._log.close()
        tools.TOOL_LOG_PATH = self._log.name

    def tearDown(self):
        assistant_tools.aiohttp = self._real_aiohttp
        tools.TOOL_LOG_PATH = self._real_log
        os.unlink(self._log.name)

    def run_tool(self, args=None, user_id=BOSS_ID):
        return asyncio.run(tools.execute(
            "create_ivn_invite", json.dumps(args or {}), ctx(user_id=user_id)))

    def audit_text(self):
        with open(self._log.name) as f:
            return f.read()

    @property
    def sent(self):
        self.assertTrue(self.fake.calls, "no request was sent")
        return self.fake.calls[-1]


# ── registration & authorization ───────────────────────────────────────────
class RegistrationTests(IvnToolTestCase):
    def test_tool_is_registered_once(self):
        self.assertIn("create_ivn_invite", tools.REGISTRY)
        names = [n for n in tools.REGISTRY if "ivn" in n.lower()]
        self.assertEqual(names, ["create_ivn_invite"],
                         "IVN logic must not be duplicated per interface")

    def test_tool_is_boss_only(self):
        self.assertEqual(tools.REGISTRY["create_ivn_invite"].permission, "boss")

    def test_result_is_kept_out_of_the_audit_log(self):
        self.assertTrue(tools.REGISTRY["create_ivn_invite"].redact_log)

    def test_schema_is_hidden_from_everyone_but_the_owner(self):
        for uid in (CREW_ID, STRANGER_ID, "", None):
            names = [s["function"]["name"] for s in tools.schemas_for(uid)]
            self.assertNotIn("create_ivn_invite", names,
                             f"user {uid!r} must not even see the tool")
        owner = [s["function"]["name"] for s in tools.schemas_for(BOSS_ID)]
        self.assertIn("create_ivn_invite", owner)

    def test_name_is_not_required_by_the_schema(self):
        spec = tools.REGISTRY["create_ivn_invite"]
        self.assertNotIn("required", spec.parameters,
                         "an IVN invite must work with no arguments at all")

    def test_profile_is_not_a_caller_supplied_parameter(self):
        props = tools.REGISTRY["create_ivn_invite"].parameters["properties"]
        self.assertNotIn("profile", props,
                         "conversational callers must not choose a profile")


class AuthorizationTests(IvnToolTestCase):
    def test_owner_may_create_an_invitation(self):
        out = self.run_tool()
        self.assertIn("AB7K-3M9Q", out)
        self.assertTrue(self.fake.calls)

    def test_crew_is_denied_and_no_request_is_sent(self):
        out = self.run_tool(user_id=CREW_ID)
        self.assertIn("Permission denied", out)
        self.assertEqual(self.fake.calls, [], "a denied call must not reach IVN")

    def test_arbitrary_users_are_denied(self):
        for uid in (STRANGER_ID, "", "0", "999"):
            self.fake.calls.clear()
            out = self.run_tool(user_id=uid)
            self.assertIn("Permission denied", out, f"user {uid!r} got through")
            self.assertEqual(self.fake.calls, [])

    def test_blank_owner_id_does_not_promote_anyone(self):
        saved = tools.OWNER_USER_ID
        tools.OWNER_USER_ID = ""
        try:
            out = self.run_tool(user_id="")
            self.assertIn("Permission denied", out)
            self.assertEqual(self.fake.calls, [])
        finally:
            tools.OWNER_USER_ID = saved


# ── duration policy ────────────────────────────────────────────────────────
class DurationTests(IvnToolTestCase):
    def test_default_request_asks_for_24_hours(self):
        self.run_tool({})
        self.assertEqual(self.sent["json"]["hours"], 24)

    def test_six_hour_request_asks_for_six(self):
        self.fake.body = json.dumps({**GOOD_BODY, "expires_in_hours": 6})
        out = self.run_tool({"hours": 6})
        self.assertEqual(self.sent["json"]["hours"], 6)
        self.assertIn("Expires: 6h", out)

    def test_string_hours_are_accepted(self):
        self.run_tool({"hours": "6"})
        self.assertEqual(self.sent["json"]["hours"], 6)

    def test_boundaries_are_accepted(self):
        for hours in (1, 23, 24):
            self.fake.calls.clear()
            self.run_tool({"hours": hours})
            self.assertEqual(self.sent["json"]["hours"], hours)

    def test_over_the_ceiling_is_refused_locally_with_no_request(self):
        for hours in (25, 48, 72, 1000, "48"):
            self.fake.calls.clear()
            out = self.run_tool({"hours": hours})
            self.assertEqual(
                out, "IVN invitations can be created for up to 24 hours.")
            self.assertEqual(self.fake.calls, [],
                             f"hours={hours!r} must not reach the gateway")

    def test_zero_and_negative_are_refused_with_no_request(self):
        for hours in (0, -1, -24, "0", "-5"):
            self.fake.calls.clear()
            out = self.run_tool({"hours": hours})
            self.assertNotIn("Code:", out)
            self.assertEqual(self.fake.calls, [])

    def test_nonsense_duration_is_refused_with_no_request(self):
        for hours in ("abc", "soon", "6.5", {}, []):
            self.fake.calls.clear()
            out = self.run_tool({"hours": hours})
            self.assertNotIn("Code:", out)
            self.assertEqual(self.fake.calls, [])


# ── request shape ──────────────────────────────────────────────────────────
class RequestTests(IvnToolTestCase):
    def test_request_targets_the_private_endpoint(self):
        self.run_tool()
        self.assertEqual(self.sent["url"],
                         f"{FAKE_URL}/internal/v1/invitations")

    def test_credential_is_sent_as_a_bearer_token(self):
        self.run_tool()
        self.assertEqual(self.sent["headers"]["Authorization"],
                         f"Bearer {FAKE_TOKEN}")

    def test_profile_is_always_standard(self):
        self.run_tool({"label": "Sarah", "hours": 6})
        self.assertEqual(self.sent["json"]["profile"], "standard")

    def test_label_is_optional(self):
        self.run_tool({})
        self.assertIsNone(self.sent["json"]["label"],
                          "no label means the gateway generates one")

    def test_supplied_label_is_forwarded(self):
        self.run_tool({"label": "Sarah"})
        self.assertEqual(self.sent["json"]["label"], "Sarah")

    def test_absurd_label_is_truncated_before_sending(self):
        self.run_tool({"label": "S" * 5000})
        self.assertLessEqual(len(self.sent["json"]["label"]), 120)

    def test_loki_never_calls_wizarr_or_shells_out(self):
        """Checked against the parsed module, not its text: prose in the
        comments legitimately names the things the code must not do."""
        import ast
        tree = ast.parse((REPO / "assistant_tools.py").read_text())

        imported = set()
        literals = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.append(node.value.lower())

        for module in ("subprocess", "paramiko", "os.system", "docker"):
            self.assertNotIn(module, imported,
                             f"the IVN tool must not import {module}")

        # Docstrings are literals too, so scan only the ones that could be an
        # endpoint, a host or a command.
        for text in literals:
            if len(text) > 200:
                continue
            for forbidden in ("wizarr", ":5690", "sshpass", "ivn.db",
                              "docker exec", "/var/run/docker.sock"):
                self.assertNotIn(forbidden, text,
                                 f"a string literal references {forbidden}")


# ── response handling ──────────────────────────────────────────────────────
class ResponseTests(IvnToolTestCase):
    def test_successful_response_is_formatted_for_the_boss(self):
        out = self.run_tool()
        self.assertEqual(out.splitlines()[0], "IVN Invite")
        self.assertIn("Code: AB7K-3M9Q", out)
        self.assertIn("Access: Standard", out)
        self.assertIn("Expires: 24h", out)
        self.assertIn("Portal: https://join.ivn-group.cc", out)

    def test_response_exposes_no_backend_detail(self):
        out = self.run_tool()
        # "Access: Standard" is the intended display; the profile NAME being
        # shown is fine. What must never appear is where or how it was made.
        for leak in ("100.87.97.120", "5692", "internal", "bearer", "wizarr",
                     "gunicorn", "sqlite", FAKE_TOKEN):
            self.assertNotIn(leak.lower(), out.lower(),
                             f"{leak} must not be shown to the user")
        self.assertNotIn("profile", out.lower())

    def test_gateway_expiry_wins_over_the_requested_value(self):
        self.fake.body = json.dumps({**GOOD_BODY, "expires_in_hours": 3})
        out = self.run_tool({"hours": 6})
        self.assertIn("Expires: 3h", out)


class FailureTests(IvnToolTestCase):
    FAILED = "I couldn't create the IVN invitation."

    def test_connection_refused_is_handled(self):
        self.fake.raises = aiohttp.ClientConnectorError(
            connection_key=None, os_error=OSError(111, "Connection refused"))
        self.assertIn(self.FAILED, self.run_tool())

    def test_generic_client_error_is_handled(self):
        self.fake.raises = aiohttp.ClientError("boom")
        self.assertIn(self.FAILED, self.run_tool())

    def test_401_is_handled(self):
        self.fake.status = 401
        self.fake.body = json.dumps({"error": "unauthorized"})
        out = self.run_tool()
        self.assertIn(self.FAILED, out)
        self.assertNotIn("unauthorized", out)

    def test_403_is_handled(self):
        self.fake.status = 403
        self.fake.body = json.dumps({"error": "forbidden"})
        self.assertIn(self.FAILED, self.run_tool())

    def test_500_is_handled(self):
        self.fake.status = 500
        self.fake.body = "<html>Internal Server Error</html>"
        out = self.run_tool()
        self.assertIn(self.FAILED, out)
        self.assertNotIn("html", out.lower())

    def test_502_and_503_are_handled(self):
        for status in (502, 503):
            self.fake.status = status
            self.assertIn(self.FAILED, self.run_tool())

    def test_malformed_json_is_handled(self):
        for body in ("not json at all", "", "[1,2,3]", "null", '"a string"'):
            self.fake.status = 201
            self.fake.body = body
            self.assertIn(self.FAILED, self.run_tool(),
                          f"body {body!r} must not crash the tool")

    def test_success_without_a_code_is_a_failure(self):
        self.fake.body = json.dumps({k: v for k, v in GOOD_BODY.items()
                                     if k != "code"})
        self.assertIn(self.FAILED, self.run_tool())

    def test_gateway_ceiling_rejection_is_relayed_plainly(self):
        """Belt and braces: if the gateway is the one refusing, say the same
        thing the local check would have said."""
        self.fake.status = 400
        self.fake.body = json.dumps(
            {"error": "duration must be between 1 and 24 hours", "max_hours": 24})
        self.assertEqual(
            self.run_tool({"hours": 24}),
            "IVN invitations can be created for up to 24 hours.")

    def test_other_gateway_4xx_is_generic(self):
        self.fake.status = 400
        self.fake.body = json.dumps({"error": "library profile is not usable"})
        out = self.run_tool()
        self.assertIn(self.FAILED, out)
        self.assertNotIn("library", out)

    def test_unconfigured_credentials_fail_closed(self):
        saved_url, saved_token = (assistant_tools.IVN_API_URL,
                                  assistant_tools.IVN_API_TOKEN)
        try:
            for url, token in (("", FAKE_TOKEN), (FAKE_URL, ""), ("", "")):
                self.fake.calls.clear()
                assistant_tools.IVN_API_URL = url
                assistant_tools.IVN_API_TOKEN = token
                self.assertIn(self.FAILED, self.run_tool())
                self.assertEqual(self.fake.calls, [])
        finally:
            assistant_tools.IVN_API_URL = saved_url
            assistant_tools.IVN_API_TOKEN = saved_token


class TimeoutTests(IvnToolTestCase):
    def test_timeout_is_not_retried(self):
        self.fake.raises = aiohttp.ServerTimeoutError("timed out")
        out = self.run_tool()
        self.assertEqual(len(self.fake.calls), 1,
                         "a timed-out create must never be retried: the "
                         "gateway may already have minted a code")
        self.assertIn("I couldn't create the IVN invitation.", out)

    def test_asyncio_timeout_is_not_retried(self):
        self.fake.raises = TimeoutError()
        out = self.run_tool()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertIn("I couldn't create the IVN invitation.", out)

    def test_timeout_warns_that_the_outcome_is_unknown(self):
        self.fake.raises = aiohttp.ServerTimeoutError("timed out")
        out = self.run_tool()
        self.assertIn("can't tell", out.lower())

    def test_no_loop_surrounds_the_request_in_the_source(self):
        """A structural check: the handler body must contain no loop at all, so
        no future edit can turn the create into a retrying one."""
        import ast
        tree = ast.parse((REPO / "assistant_tools.py").read_text())
        handler = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "_create_ivn_invite")
        loops = [n for n in ast.walk(handler)
                 if isinstance(n, (ast.For, ast.AsyncFor, ast.While))]
        self.assertEqual(loops, [], "the invitation request must never loop")


# ── secret and code hygiene ────────────────────────────────────────────────
class HygieneTests(IvnToolTestCase):
    def _capture(self, fn):
        stream = logging.StreamHandler()
        records = []

        class _Collect(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))

        handler = _Collect()
        root = logging.getLogger()
        root.addHandler(handler)
        level = root.level
        root.setLevel(logging.DEBUG)
        try:
            fn()
        finally:
            root.removeHandler(handler)
            root.setLevel(level)
            del stream
        return "\n".join(records)

    def test_credential_never_reaches_the_logs(self):
        text = self._capture(lambda: self.run_tool({"label": "Sarah"}))
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn(FAKE_TOKEN, self.audit_text())

    def test_credential_never_reaches_the_logs_on_failure(self):
        self.fake.status = 401
        text = self._capture(self.run_tool)
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertNotIn(FAKE_TOKEN, self.audit_text())

    def test_raw_code_is_not_persisted_in_the_tool_audit_log(self):
        self.run_tool()
        audit = self.audit_text()
        self.assertNotIn("AB7K-3M9Q", audit)
        self.assertNotIn("AB7K3M9Q", audit)
        self.assertIn("create_ivn_invite", audit, "the call is still audited")

    def test_credential_is_not_hardcoded_anywhere(self):
        src = (REPO / "assistant_tools.py").read_text()
        self.assertIn('os.getenv("IVN_INTERNAL_API_TOKEN"', src)
        self.assertNotIn(FAKE_TOKEN, src)

    def test_credential_is_not_committed(self):
        gitignore = (REPO / ".gitignore").read_text()
        self.assertIn(".env", gitignore)


# ── both interfaces reach the one tool ─────────────────────────────────────
class InterfaceWiringTests(unittest.TestCase):
    """Read as text, never imported: importing loki_bot in a discover run can
    bind other modules' production paths (see .agents/rules/validation-policy)."""

    def setUp(self):
        self.bot = (REPO / "loki_bot.py").read_text()
        self.tg = (REPO / "telegram_interface.py").read_text()

    def test_both_interfaces_use_the_shared_tool_loop(self):
        self.assertIn("chat_with_tools", self.bot)
        self.assertIn("chat_with_tools", self.tg,
                      "Telegram must reach tools through the same loop")

    def test_discord_passes_the_real_author_id(self):
        self.assertIn("user_id=str(message.author.id)", self.bot,
                      "Discord authorization must key on the real author")

    def test_telegram_context_is_pinned_to_the_owner(self):
        self.assertIn("user_id=str(OWNER_USER_ID)", self.bot,
                      "the Telegram tool context must be the owner's")

    def test_telegram_rejects_strangers_before_the_tool_layer(self):
        self.assertIn("This is a private line", self.tg)
        self.assertIn("user_id != self.owner_id", self.tg)

    def test_no_interface_specific_ivn_logic(self):
        for name, src in (("loki_bot.py", self.bot),
                          ("telegram_interface.py", self.tg)):
            self.assertNotIn("internal/v1/invitations", src,
                             f"{name} must not talk to IVN directly")


if __name__ == "__main__":
    unittest.main()

"""
Tests for the list_create tool (Joplin checkbox lists) against a local fake
Joplin Data API. No real Joplin, Telegram, Discord, HA, or LLM traffic:
joplin_integration is pointed at an in-process aiohttp server and the tool
audit log is redirected to a temp file.

Run:  venv/bin/python -m unittest tests.test_list_create -v
"""

import asyncio
import json
import logging
import re
import tempfile
import unittest
import uuid

from aiohttp import web

import tools
import assistant_tools  # noqa: F401 — registers the tools
import joplin_integration as jp

FAKE_TOKEN = "test-token-not-a-real-secret-123"
OWNER_ID = "111111111111111111"
STRANGER_ID = "999999999999999999"


class FakeJoplin:
    """Minimal Joplin Data API: folders, notes, tags, search."""

    def __init__(self):
        self.folders: list[dict] = []
        self.notes: dict[str, dict] = {}
        self.tags: list[dict] = []
        self.note_posts = 0          # how many notes were created
        self.fail_note_create = False
        self.lag_search = False      # simulate the FTS index lagging new notes
        self.requests_seen: list[str] = []

    def _auth(self, request) -> bool:
        return request.query.get("token") == FAKE_TOKEN

    async def handle(self, request: web.Request) -> web.Response:
        self.requests_seen.append(f"{request.method} {request.path}")
        if not self._auth(request):
            return web.json_response({"error": "invalid token"}, status=401)
        m, p = request.method, request.path

        if m == "GET" and p == "/folders":
            return web.json_response({"items": self.folders, "has_more": False})
        if m == "POST" and p == "/folders":
            body = await request.json()
            folder = {"id": uuid.uuid4().hex, "title": body["title"],
                      "parent_id": body.get("parent_id", "")}
            self.folders.append(folder)
            return web.json_response(folder)

        fm = re.match(r"^/folders/([^/]+)/notes$", p)
        if m == "GET" and fm:
            items = [n for n in self.notes.values()
                     if n.get("parent_id") == fm.group(1)]
            return web.json_response({"items": items, "has_more": False})

        if m == "GET" and p == "/search":
            if self.lag_search:
                return web.json_response({"items": [], "has_more": False})
            q = request.query.get("query", "")
            tm = re.match(r'title:"(.+)"', q)
            hits = []
            for n in self.notes.values():
                if tm:
                    if n["title"].lower() == tm.group(1).lower():
                        hits.append(n)
                elif q.lower() in n["title"].lower() or q.lower() in n.get("body", "").lower():
                    hits.append(n)
            return web.json_response({"items": hits, "has_more": False})

        if m == "POST" and p == "/notes":
            if self.fail_note_create:
                return web.json_response({"error": "boom"}, status=500)
            body = await request.json()
            self.note_posts += 1
            note = {"id": uuid.uuid4().hex, "title": body["title"],
                    "body": body.get("body", ""), "parent_id": body.get("parent_id", "")}
            self.notes[note["id"]] = note
            return web.json_response(note)
        if m == "GET" and p.startswith("/notes/"):
            note = self.notes.get(p.split("/")[2])
            if not note:
                return web.json_response({}, status=404)
            return web.json_response(note)
        if m == "PUT" and p.startswith("/notes/"):
            note = self.notes.get(p.split("/")[2])
            if not note:
                return web.json_response({}, status=404)
            note.update(await request.json())
            return web.json_response(note)

        if m == "GET" and p == "/tags":
            return web.json_response({"items": self.tags, "has_more": False})
        if m == "POST" and p == "/tags":
            body = await request.json()
            tag = {"id": uuid.uuid4().hex, "title": body["title"]}
            self.tags.append(tag)
            return web.json_response(tag)
        if m == "POST" and re.match(r"^/tags/[^/]+/notes$", p):
            return web.json_response({})

        return web.json_response({"error": f"unhandled {m} {p}"}, status=404)


class ListCreateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeJoplin()
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self.fake.handle)
        # access_log off: the fake server logging its own inbound URLs is test
        # harness noise, not Loki-side behavior — the token check below must
        # only see what Loki's process would actually log.
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]

        # Point the integration at the fake server; fresh caches/session.
        self._saved = (jp.JOPLIN_API_URL, jp.JOPLIN_API_TOKEN, jp._folder_cache,
                       jp._session_factory, tools.OWNER_USER_ID, tools.TOOL_LOG_PATH)
        jp.JOPLIN_API_URL = f"http://127.0.0.1:{port}"
        jp.JOPLIN_API_TOKEN = FAKE_TOKEN
        jp._folder_cache = {}
        jp._session_factory = None
        jp._own_session = None
        tools.OWNER_USER_ID = OWNER_ID
        self.tmplog = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        tools.TOOL_LOG_PATH = self.tmplog.name

        self.ctx = tools.ToolContext(user_id=OWNER_ID, user_name="Boss",
                                     channel_id="tg:1")
        assistant_tools._recent_lists.clear()

    async def asyncTearDown(self):
        if jp._own_session and not jp._own_session.closed:
            await jp._own_session.close()
        (jp.JOPLIN_API_URL, jp.JOPLIN_API_TOKEN, jp._folder_cache,
         jp._session_factory, tools.OWNER_USER_ID, tools.TOOL_LOG_PATH) = self._saved
        jp._own_session = None
        await self.runner.cleanup()

    async def _call(self, **args) -> dict:
        raw = await assistant_tools._list_create(args, self.ctx)
        return json.loads(raw)

    # 1. Registry wiring: the boss-level context Telegram builds sees the tool;
    #    strangers don't.
    async def test_tool_visible_to_boss_context_only(self):
        boss_tools = {s["function"]["name"] for s in tools.schemas_for(OWNER_ID)}
        stranger_tools = {s["function"]["name"] for s in tools.schemas_for(STRANGER_ID)}
        self.assertIn("list_create", boss_tools)
        self.assertNotIn("list_create", stranger_tools)

    # 2. Title/items parsing, including comma-string and bulleted input.
    async def test_item_parsing(self):
        out = await self._call(title="Grocery List",
                               items="milk, eggs,\n- [ ] bread; butter, milk")
        self.assertTrue(out["success"])
        self.assertEqual(out["item_count"], 4)  # "milk" deduped
        body = list(self.fake.notes.values())[0]["body"]
        self.assertEqual(body.splitlines(),
                         ["- [ ] milk", "- [ ] eggs", "- [ ] bread", "- [ ] butter"])

    async def test_empty_items_is_actionable_error_not_denial(self):
        out = await self._call(title="Grocery List", items=[])
        self.assertFalse(out["success"])
        self.assertEqual(out["error"], "missing_items")
        self.assertIn("write access", out["fix"])
        self.assertEqual(self.fake.note_posts, 0)

    # 3. Markdown checkbox formatting.
    async def test_checkbox_markdown(self):
        out = await self._call(title="Packing", items=["Socks", "Charger"])
        self.assertTrue(out["success"])
        note = self.fake.notes[out["note_id"]]
        for line in note["body"].splitlines():
            self.assertRegex(line, r"^- \[ \] \S")

    # 4. Configured endpoint + token auth used; token never logged.
    async def test_auth_used_and_token_not_logged(self):
        with self.assertLogs(level=logging.DEBUG) as captured:
            logging.getLogger("test").debug("sentinel")  # assertLogs needs >=1
            out = await self._call(title="Auth List", items=["a"])
        self.assertTrue(out["success"])  # fake 401s on bad/missing token
        self.assertIn("POST /notes", self.fake.requests_seen)
        joined = "\n".join(captured.output) + json.dumps(out)
        self.assertNotIn(FAKE_TOKEN, joined)

    # 5. Success returns full metadata.
    async def test_success_metadata(self):
        out = await self._call(title="Grocery List",
                               items=["Milk", "Eggs", "Bread", "Butter"])
        self.assertTrue(out["success"])
        self.assertTrue(out["note_id"])
        self.assertEqual(out["title"], "Grocery List")
        self.assertEqual(out["notebook_title"], "Loki/Lists")
        self.assertTrue(out["notebook_id"])
        self.assertEqual(out["item_count"], 4)
        self.assertIn("Created 'Grocery List' in Joplin under 'Loki/Lists' with 4 items",
                      out["message"])

    # 6. API failure → honest failure, no success claim.
    async def test_api_failure_is_honest(self):
        self.fake.fail_note_create = True
        out = await self._call(title="Doomed", items=["x"])
        self.assertFalse(out["success"])
        self.assertEqual(out["error"], "joplin_api_error")
        self.assertIn("NOT saved", out["fix"])
        self.assertNotIn("message", out)

    # 7. Missing token → clear configuration error.
    async def test_missing_token(self):
        jp.JOPLIN_API_TOKEN = ""
        out = await self._call(title="Grocery List", items=["Milk"])
        self.assertFalse(out["success"])
        self.assertEqual(out["error"], "joplin_not_configured")

    # 8. Unauthorized user is blocked by the registry before the handler runs.
    async def test_unauthorized_user_denied(self):
        stranger = tools.ToolContext(user_id=STRANGER_ID, user_name="Stranger")
        result = await tools.execute(
            "list_create", json.dumps({"title": "Hack", "items": ["x"]}), stranger)
        self.assertIn("Permission denied", result)
        self.assertEqual(self.fake.note_posts, 0)

    # 9. Existing read/search path still works against the same client.
    async def test_note_search_unaffected(self):
        await self._call(title="Grocery List", items=["Milk"])
        result = await assistant_tools._note_search({"query": "Grocery"}, self.ctx)
        self.assertIn("Grocery List", result)

    # 10. Retry with the same arguments does not create a duplicate note —
    #     even while the full-text search index lags the new note (as observed
    #     live on 2026-07-19).
    async def test_retry_does_not_duplicate(self):
        self.fake.lag_search = True
        first = await self._call(title="Grocery List", items=["Milk", "Eggs"])
        second = await self._call(title="Grocery List", items=["Milk", "Eggs"])
        self.assertTrue(first["success"] and second["success"])
        self.assertEqual(self.fake.note_posts, 1)
        self.assertEqual(second["item_count"], 0)
        self.assertIn("already has all", second["message"])
        body = self.fake.notes[first["note_id"]]["body"]
        self.assertEqual(body.count("Milk"), 1)

    # 10b. Same, but with the in-process cache wiped (fresh process after a
    #      restart): the direct folder listing alone must dedupe, with search
    #      still lagging.
    async def test_retry_dedupe_survives_without_cache_or_search(self):
        self.fake.lag_search = True
        first = await self._call(title="Grocery List", items=["Milk"])
        assistant_tools._recent_lists.clear()
        second = await self._call(title="Grocery List", items=["Milk", "Eggs"])
        self.assertTrue(first["success"] and second["success"])
        self.assertEqual(self.fake.note_posts, 1)
        self.assertEqual(second["note_id"], first["note_id"])
        self.assertEqual(second["item_count"], 1)  # only Eggs is new
        body = self.fake.notes[first["note_id"]]["body"]
        self.assertEqual(body.count("Milk"), 1)

    # Named-notebook rules: existing notebook honored; unknown notebook is a
    # clear error instead of silently creating one.
    async def test_explicit_notebook(self):
        self.fake.folders.append({"id": "shop1", "title": "Shopping", "parent_id": ""})
        ok = await self._call(title="Costco Run", items=["Paper towels"],
                              notebook="Shopping")
        self.assertTrue(ok["success"])
        self.assertEqual(ok["notebook_id"], "shop1")
        bad = await self._call(title="X", items=["y"], notebook="No Such Notebook")
        self.assertFalse(bad["success"])
        self.assertEqual(bad["error"], "notebook_not_found")


if __name__ == "__main__":
    unittest.main()

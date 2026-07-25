"""
Regression tests for task/conversation routing — the browser-task /
"BLACK-BOXX has no internet" pollution.

Observed failure (Telegram, July 2026): the Boss asked Loki to browse
quotes.toscrape.com; the task completed and its result was announced to the
chat out-of-band, but that delivery never reached the LLM's conversation
history. On the next, unrelated message ("BLACK-BOXX has no internet") the
model still saw its own "I'll post the result here" promise as unresolved,
re-fetched the finished task, and pasted the stale page extract into the
incident reply.

These tests replay that exact sequence with a scripted LLM and no network,
and pin the fixes:
  - completion notifications are recorded into the Telegram history
    (delivered separately, visible to the model as already-delivered),
  - a completed task's result is not repeated by task_list once announced,
  - explicitly asking about the old task (task_status) still returns it,
  - tasks persist origin message + conversation correlation ids,
  - announcements don't expose the full internal task id,
  - a new homelab incident outranks an older queued informational task.

Run:  venv/bin/python -m unittest tests.test_task_routing -v
"""

import asyncio
import json
import os
import tempfile
import unittest

BOSS_ID = "111111111111111111"
os.environ["OWNER_USER_ID"] = BOSS_ID
os.environ.setdefault("HOMELAB_DB_PATH",
                      tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)

import tools
tools.OWNER_USER_ID = BOSS_ID

import task_supervisor as ts
from telegram_interface import TelegramInterface

CHAT_ID = 424242
CHANNEL = f"tg:{CHAT_ID}"
BROWSE_RESULT = ('Quotes to Scrape — https://quotes.toscrape.com/js/ · '
                 'extracted page text (verbatim): "The world as we have '
                 'created it is a process of our thinking."')


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


async def _noop_handler(h):
    return ts.TaskResult("completed", summary=BROWSE_RESULT)


class ScriptedLLM:
    """Stands in for chat_with_tools: records what the model would see and
    returns pre-scripted replies. reply hooks let a turn submit tasks the way
    the real tool loop would."""

    def __init__(self):
        self.turns = []          # [(messages, ctx)]
        self.script = []         # [(reply, hook|None)]

    async def chat_with_tools(self, messages, ctx):
        self.turns.append(([dict(m) for m in messages], ctx))
        reply, hook = self.script.pop(0)
        if hook:
            hook(ctx)
        return reply


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        ts.DB_PATH = self.tmp.name
        ts._conn = None
        ts._running.clear()
        ts._started = False
        ts._loops.clear()
        ts._db()

        # Task types: a stand-in for browser_research (informational) and a
        # high-priority incident type mirroring homelab_incident's priority.
        ts.register_type(ts.TaskType(
            name="fake_browse", handler=_noop_handler, permission="crew",
            title=lambda i: "Browse quotes.toscrape.com"))
        ts.register_type(ts.TaskType(
            name="fake_incident", handler=_noop_handler, permission="boss",
            priority=10, title=lambda i: "Homelab incident: black-boxx"))

        self.llm = ScriptedLLM()
        self.iface = TelegramInterface(
            self.llm,
            tool_ctx_factory=lambda uid, name, chat: tools.ToolContext(
                user_id=str(uid), user_name=name, channel_id=f"tg:{chat}"),
            session_factory=None)
        self.iface.owner_id = int(BOSS_ID) % (10 ** 9)

        # Wire ts._send the way loki_bot._channel_send does for Telegram:
        # deliver, then record the delivery into the LLM-visible history.
        self.delivered = []

        async def channel_send(channel_id, text, file_path=None, filename=None):
            self.delivered.append((channel_id, text))
            self.iface.note_outbound(int(channel_id[3:]), text)

        ts._send = channel_send

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def _msg(self, message_id, text):
        return {"chat": {"id": CHAT_ID}, "message_id": message_id,
                "from": {"id": self.iface.owner_id, "first_name": "Boss"},
                "text": text}


class BrowserBlackBoxxRegression(Base):
    """The exact observed sequence, step by step."""

    def test_full_sequence(self):
        submitted = {}

        def start_browse(ctx):
            submitted["task_id"] = ts.submit(
                ts._TYPES["fake_browse"], ctx, {"url": "https://quotes.toscrape.com/js/"})

        # Turn 1: the Boss asks for the browse; the model queues the task and
        # promises the result "here".
        self.llm.script = [("On it — browsing in the background; "
                            "I'll post the result here.", start_browse)]
        run(self.iface._handle(self._msg(1001, "browse quotes.toscrape.com/js")))
        tid = submitted["task_id"]

        # The task stays tied to its originating message and conversation.
        row = ts.get_task(tid)
        self.assertEqual(row["origin_message_id"], "1001")
        self.assertEqual(row["conversation_id"], CHANNEL)

        # The task completes; the completion notification is delivered
        # SEPARATELY (out-of-band announce), exactly once.
        ts._update(tid, status="completed", result_summary=BROWSE_RESULT)
        run(ts._maybe_announce(tid))
        run(ts._maybe_announce(tid))            # duplicate suppression
        completions = [t for _, t in self.delivered if "Quotes to Scrape" in t]
        self.assertEqual(len(completions), 1)
        # No noisy internal id in the user-facing notification.
        self.assertNotIn(tid, completions[0])

        # …and the delivery is now part of the model-visible history.
        history = list(self.iface._history[CHAT_ID])
        self.assertTrue(any("Quotes to Scrape" in m["content"]
                            and "delivered as a separate notification" in m["content"]
                            for m in history))

        # Turn 2: the unrelated incident message. The model must see (a) the
        # promise already resolved in history and (b) task_list refusing to
        # re-serve the old result — so nothing stale can leak into the reply.
        self.llm.script = [("Checking BLACK-BOXX now.", None)]
        run(self.iface._handle(self._msg(1002, "BLACK-BOXX has no internet")))

        messages, _ctx = self.llm.turns[-1]
        idx_delivery = next(i for i, m in enumerate(messages)
                            if "delivered as a separate notification" in m.get("content", ""))
        idx_new = next(i for i, m in enumerate(messages)
                       if m.get("content") == "BLACK-BOXX has no internet")
        self.assertLess(idx_delivery, idx_new)

        # task_list (what the model consults for open work) marks the old
        # result as already delivered instead of repeating it.
        ctx = tools.ToolContext(user_id=BOSS_ID, user_name="Boss",
                                channel_id=CHANNEL)
        listing = json.loads(run(ts._tool_list({"active_only": False}, ctx)))
        joined = " ".join(listing["tasks"])
        self.assertNotIn("Quotes to Scrape", joined)
        self.assertIn("already delivered", joined)

        # Explicitly asking about the old task still returns the result.
        status = json.loads(run(ts._tool_status({"task_id": tid}, ctx)))
        self.assertIn("Quotes to Scrape", status["task"])


class IncidentPriorityTests(Base):
    def test_new_incident_outranks_older_informational_task(self):
        ctx = tools.ToolContext(user_id=BOSS_ID, user_name="Boss",
                                channel_id=CHANNEL)
        order = []

        async def slow_recorder(h):
            order.append(h.row["task_type"])
            return ts.TaskResult("completed", summary="ok")

        ts._TYPES["fake_browse"].handler = slow_recorder
        ts._TYPES["fake_incident"].handler = slow_recorder

        async def scenario():
            # Older informational task queued FIRST, incident second.
            ts.submit(ts._TYPES["fake_browse"], ctx, {})
            ts.submit(ts._TYPES["fake_incident"], ctx, {})
            ts.MAX_CONCURRENCY = 1
            ts._started = True
            ts._pump()
            for _ in range(50):
                if len(order) == 2:
                    break
                await asyncio.sleep(0.05)

        run(scenario())
        self.assertEqual(order, ["fake_incident", "fake_browse"])


if __name__ == "__main__":
    unittest.main()

"""
Proof that autonomous maintenance cannot reach Telegram — and that ordinary
Telegram traffic is untouched.

Regression suite for a real production flood. The first ops-feed cut routed on
`channel_id == "ops:maintenance"`, but 302 autonomous hermes_escalation tasks
already existed with `channel_id = "tg:<owner>"` — created before the feed did.
Those rows are long-lived (a paused_quota task never finishes), so every
restart re-announced "started" / "paused" / "interrupted" straight to the
Boss's phone. Identity, not the stored channel, decides now.

Every Telegram write in this suite is captured in memory; nothing is sent.

Run:  venv/bin/python -m unittest tests.test_maintenance_telegram_isolation -v
"""

import asyncio
import os
import tempfile
import time
import unittest

BOSS_ID = "111111111111111111"
CREW_ID = "222222222222222222"
OWNER_TG = "tg:739041549"          # the real shape of the polluted rows

os.environ["OWNER_USER_ID"] = BOSS_ID
os.environ["CREW_USER_IDS"] = CREW_ID
os.environ["MAINTENANCE_OPS_CHANNEL_ID"] = "1534655910893457669"
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["HOMELAB_DB_PATH"] = _tmp_db.name
_tmp_dir = tempfile.mkdtemp(prefix="tg-isolation-test-")
os.environ["HOMELAB_LIFECYCLE_MIRROR"] = os.path.join(_tmp_dir, "lifecycle.yml")
os.environ["HOMELAB_DECOMMISSION_ARCHIVE_DIR"] = os.path.join(_tmp_dir, "archive")

import tools
tools.OWNER_USER_ID = BOSS_ID
tools.CREW_USER_IDS = {CREW_ID}

import maintenance_notify as mn
import homelab_monitor as mon
import task_supervisor as ts


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


ALL_STATUSES = ("running", "completed", "failed", "cancelled",
                "paused_auth", "paused_quota")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        ts.DB_PATH = self.tmp.name
        ts._conn = None
        ts._running.clear()
        ts._started = False

        self.ops, self.telegram, self.chat = [], [], []

        async def ops_send(text):
            self.ops.append(text)

        async def boss_send(text):
            # This is the Telegram line. Anything landing here is a Telegram DM.
            self.telegram.append(text)

        async def channel_send(channel_id, text, file_path=None, filename=None):
            self.chat.append((channel_id, text))
            if str(channel_id).startswith("tg:"):
                self.telegram.append(text)

        mn.OPS_CHANNEL_ID = "1534655910893457669"
        mn.bind(ops_send=ops_send, boss_send=boss_send)
        ts._send = channel_send
        ts._db()

    def tearDown(self):
        ts._send = None
        mn.bind(ops_send=None, boss_send=None)
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def make_task(self, status, channel, requester="Boss (auto)",
                  task_type="hermes_escalation", **extra):
        now = time.time()
        task_id = f"lt_{abs(hash((status, channel, requester, now))) % 10**12:012d}"
        row = {"task_id": task_id, "task_type": task_type,
               "title": "Hermes escalation: black-boxx", "channel_id": channel,
               "requester_name": requester, "requester_id": BOSS_ID,
               "status": status, "created_at": now, "updated_at": now}
        row.update(extra)
        ts._insert(row)
        return task_id


# ── 1. The polluted legacy rows ────────────────────────────────────────────
class LegacyAutonomousTaskTests(Base):
    def test_every_lifecycle_status_stays_off_telegram(self):
        """The exact rows found in production: autonomous, Telegram-addressed."""
        for status in ALL_STATUSES:
            self.ops.clear(); self.telegram.clear(); self.chat.clear()
            tid = self.make_task(status, OWNER_TG, result_summary="a summary")
            run(ts._maybe_announce(tid))
            self.assertEqual(self.telegram, [], f"{status} leaked to Telegram")
            self.assertEqual([c for c in self.chat if c[0].startswith("tg:")], [],
                             f"{status} used the chat sender")

    def test_started_and_paused_chatter_is_sent_nowhere_at_all(self):
        for status in ("running", "cancelled"):
            self.ops.clear(); self.telegram.clear()
            tid = self.make_task(status, OWNER_TG)
            run(ts._maybe_announce(tid))
            self.assertEqual(self.telegram, [], status)
            self.assertEqual(self.ops, [], f"{status} is routine chatter")

    def test_meaningful_result_reaches_the_ops_feed_instead(self):
        tid = self.make_task("completed", OWNER_TG,
                             result_summary="BLACK-BOXX: Hermes diagnosis — AP down")
        run(ts._maybe_announce(tid))
        self.assertEqual(self.ops, ["BLACK-BOXX: Hermes diagnosis — AP down"])
        self.assertEqual(self.telegram, [])

    def test_quota_pause_reaches_the_ops_feed_only(self):
        tid = self.make_task("paused_quota", OWNER_TG)
        run(ts._maybe_announce(tid))
        self.assertEqual(len(self.ops), 1)
        self.assertIn("quota", self.ops[0])
        self.assertEqual(self.telegram, [])

    def test_restart_interruption_does_not_page_the_boss(self):
        """_recover_one finalises orphans as failed — the per-restart flood."""
        tid = self.make_task("running", OWNER_TG, external_ref=None)
        run(ts._finalize(tid, ts.TaskResult(
            "failed", error_category="interrupted",
            summary="Interrupted by a Loki restart; no live worker to resume.")))
        self.assertEqual(self.telegram, [])
        self.assertEqual(len(self.ops), 1)
        self.assertIn("interrupted", self.ops[0])


# ── 2. The data repair that re-addresses them ──────────────────────────────
class ReaddressingTests(Base):
    def test_autonomous_rows_are_moved_to_the_ops_feed(self):
        ids = [self.make_task(s, OWNER_TG) for s in ALL_STATUSES]
        moved = ts._readdress_autonomous_tasks(ts._db())
        ts._db().commit()
        self.assertEqual(moved, len(ids))
        for tid in ids:
            self.assertEqual(ts.get_task(tid)["channel_id"], mn.OPS_CHANNEL)

    def test_repair_is_idempotent(self):
        self.make_task("paused_quota", OWNER_TG)
        ts._readdress_autonomous_tasks(ts._db())
        ts._db().commit()
        self.assertEqual(ts._readdress_autonomous_tasks(ts._db()), 0)

    def test_human_requested_rows_are_never_re_addressed(self):
        tid = self.make_task("completed", OWNER_TG, requester="Rico",
                             task_type="browser_research")
        ts._readdress_autonomous_tasks(ts._db())
        ts._db().commit()
        self.assertEqual(ts.get_task(tid)["channel_id"], OWNER_TG)


# ── 3. Human-requested work is untouched ───────────────────────────────────
class HumanRequestedTasksTests(Base):
    def test_a_boss_requested_task_still_answers_in_telegram(self):
        tid = self.make_task("completed", OWNER_TG, requester="Rico",
                             task_type="browser_research",
                             result_summary="here is your page")
        run(ts._maybe_announce(tid))
        self.assertEqual(len(self.telegram), 1)
        self.assertIn("here is your page", self.telegram[0])
        self.assertEqual(self.ops, [])

    def test_a_boss_requested_hermes_escalation_still_answers_in_telegram(self):
        """The Boss explicitly asking for a diagnosis is NOT autonomous."""
        tid = self.make_task("completed", OWNER_TG, requester="Rico",
                             result_summary="Joplin: Hermes diagnosis — db locked")
        run(ts._maybe_announce(tid))
        self.assertEqual(len(self.telegram), 1)
        self.assertEqual(self.ops, [])

    def test_a_boss_requested_task_still_announces_start(self):
        tid = self.make_task("running", OWNER_TG, requester="Rico",
                             task_type="browser_research")
        run(ts._maybe_announce(tid))
        self.assertEqual(len(self.telegram), 1)
        self.assertIn("started", self.telegram[0])

    def test_identity_check_distinguishes_the_two(self):
        auto = ts.get_task(self.make_task("running", OWNER_TG))
        human = ts.get_task(self.make_task("running", OWNER_TG, requester="Rico"))
        self.assertTrue(mn.is_autonomous_task(auto))
        self.assertFalse(mn.is_autonomous_task(human))
        self.assertFalse(mn.is_autonomous_task({}))


# ── 4. Monitor events: only genuine Boss decisions may page ────────────────
class MonitorEventRoutingTests(Base):
    def setUp(self):
        super().setUp()
        self.legacy = []

        async def legacy_notify(text):
            self.legacy.append(text)
        mon._notify_boss = legacy_notify

    def tearDown(self):
        mon._notify_boss = None
        super().tearDown()

    def test_needs_boss_hands_is_status_not_a_page(self):
        run(mon._notify("needs_boss_hands",
                        "🆘 BLACK-BOXX: Hermes is out of quota — needs your hands."))
        self.assertEqual(len(self.ops), 1)
        self.assertEqual(self.telegram, [], "quota status must not page the Boss")
        self.assertEqual(self.legacy, [])

    def test_escalation_status_is_not_an_approval_request(self):
        run(mon._notify("incident_escalated",
                        "🆘 Escalated — Joplin: handed to Hermes for a closer look"))
        self.assertEqual(len(self.ops), 1)
        self.assertEqual(self.telegram, [])

    def test_all_routine_incident_events_stay_off_telegram(self):
        for event in ("incident_opened", "repair_started", "repair_failed",
                      "incident_resolved", "incident_escalated",
                      "approval_required", "diagnostic_progress",
                      "diagnostic_result", "lifecycle_notice", "summary",
                      "needs_boss_hands"):
            run(mon._notify(event, f"text for {event}"))
        self.assertEqual(len(self.ops), 11)
        self.assertEqual(self.telegram, [])

    def test_a_genuine_consequential_approval_may_still_page(self):
        run(mn.notify("boss_approval_required",
                      "Approve wiping the ivn-site volume?"))
        self.assertEqual(len(self.telegram), 1)
        self.assertEqual(len(self.ops), 1)

    def test_safety_alerts_may_still_page(self):
        for event in ("security_alert", "data_loss_alert"):
            self.telegram.clear()
            run(mn.notify(event, f"{event} fired"))
            self.assertEqual(len(self.telegram), 1, event)


# ── 5. A full simulated incident produces zero Telegram traffic ────────────
class EndToEndSilenceTests(Base):
    def setUp(self):
        super().setUp()
        conn = mon._db()
        conn.execute("DELETE FROM monitor_incidents")
        conn.execute("DELETE FROM monitor_checks")
        conn.commit()

        async def legacy_notify(text):
            self.telegram.append(text)
        mon._notify_boss = legacy_notify

        self.submitted = []
        self._real_escalate = mon._escalate

        async def fake_escalate(incident_id, key, display, symptom, checks):
            # Submit a REAL task row the way the monitor does, so the
            # supervisor's own announcement path is exercised.
            tt = ts._TYPES.get("hermes_escalation")
            ctx = tools.ToolContext(user_id=BOSS_ID,
                                    user_name=mn.AUTONOMOUS_REQUESTER,
                                    channel_id=mon._boss_channel_id_fn())
            tid = self.make_task("queued", ctx.channel_id,
                                 requester=ctx.user_name)
            self.submitted.append(tid)
            mon._update_incident(incident_id, escalated_task_id=tid)
            return tid

        mon._escalate = fake_escalate

        async def no_joplin(_i):
            return None
        self._real_joplin = mon._write_joplin_summary
        mon._write_joplin_summary = no_joplin

    def tearDown(self):
        mon._escalate = self._real_escalate
        mon._write_joplin_summary = self._real_joplin
        mon._notify_boss = None
        super().tearDown()

    def test_incident_escalation_and_quota_pause_never_touch_telegram(self):
        fault = {"healthy": False, "diagnosis": "ap_interface down",
                 "checks": [{"name": "ap_interface", "ok": False, "detail": "wlan0"}],
                 "repair": None, "repair_result": None, "escalate": True}

        async def check(allow_repairs):
            return dict(fault)

        for _ in range(10):
            mon._update_check("black-boxx", cooldown_until=0)
            run(mon._process_check("black-boxx", "BLACK-BOXX", check, "BLACK-BOXX"))

        # The escalation task then runs, pauses on quota, and is announced.
        for tid in self.submitted:
            for status in ("running", "paused_quota"):
                ts._update(tid, status=status)
                run(ts._maybe_announce(tid))

        self.assertEqual(self.telegram, [],
                         f"autonomous maintenance leaked to Telegram: {self.telegram}")
        self.assertEqual(len(self.submitted), 1)
        self.assertTrue(any("Incident opened" in o for o in self.ops))
        self.assertTrue(any("Escalated" in o for o in self.ops))
        self.assertTrue(any("quota" in o for o in self.ops))
        # Started chatter appears nowhere.
        self.assertEqual([o for o in self.ops if "started" in o.lower()], [])


# ── 6. Ordinary conversation is structurally untouchable ───────────────────
class ConversationUnaffectedTests(Base):
    """`_channel_send` gained one branch: `is_ops_channel(cid)` short-circuits
    to the Discord ops feed BEFORE the existing `tg:` / numeric-id branches.
    These pin the properties that make that branch unable to capture a real
    conversation — the sentinel is neither a Telegram address nor a Discord id."""

    def test_the_sentinel_cannot_be_mistaken_for_a_telegram_chat(self):
        self.assertFalse(mn.OPS_CHANNEL.startswith("tg:"))

    def test_the_sentinel_cannot_be_mistaken_for_a_discord_channel_id(self):
        with self.assertRaises(ValueError):
            int(mn.OPS_CHANNEL)

    def test_real_conversation_channels_never_match_the_ops_branch(self):
        for cid in (OWNER_TG, "tg:1", "1534655910893457669", "987654321",
                    "", None, "ops:something-else"):
            self.assertFalse(mn.is_ops_channel(cid), repr(cid))

    def test_a_telegram_conversation_reply_is_delivered_unchanged(self):
        """A Boss-requested task answering into a Telegram chat."""
        tid = self.make_task("completed", OWNER_TG, requester="Rico",
                             task_type="browser_research",
                             result_summary="screenshot attached")
        run(ts._maybe_announce(tid))
        self.assertEqual(len(self.chat), 1)
        self.assertEqual(self.chat[0][0], OWNER_TG)

    def test_a_discord_conversation_reply_is_delivered_unchanged(self):
        tid = self.make_task("completed", "1122334455667788",
                             requester="Rico", task_type="browser_research",
                             result_summary="done")
        run(ts._maybe_announce(tid))
        self.assertEqual(len(self.chat), 1)
        self.assertEqual(self.chat[0][0], "1122334455667788")
        self.assertEqual(self.ops, [])
        self.assertEqual(self.telegram, [])

    def test_ordinary_chat_text_never_enters_the_maintenance_router(self):
        """Nothing in the chat path calls maintenance_notify — a plain message
        is not an event, so there is no name for the router to route."""
        self.assertIsNone(mn.EVENTS.get("chat"))
        self.assertIsNone(mn.EVENTS.get(""))
        self.assertFalse(mn.is_autonomous_task(
            {"channel_id": OWNER_TG, "requester_name": "Kavaris"}))


if __name__ == "__main__":
    unittest.main()

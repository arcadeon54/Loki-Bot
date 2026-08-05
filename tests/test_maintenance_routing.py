"""
Focused tests for autonomous-maintenance notification routing.

Nothing is sent anywhere: the Discord ops channel and the Boss's Telegram line
are both in-memory capture lists. Covers the actual requirement — the ops
channel is the canonical maintenance feed, Telegram sees only urgent events,
routine worker/task chatter is sent nowhere, and no event is ever lost when the
ops channel is unconfigured or its send fails.

Run:  venv/bin/python -m unittest tests.test_maintenance_routing -v
"""

import asyncio
import os
import tempfile
import time
import unittest

BOSS_ID = "111111111111111111"
CREW_ID = "222222222222222222"

os.environ["OWNER_USER_ID"] = BOSS_ID
os.environ["CREW_USER_IDS"] = CREW_ID
os.environ["MAINTENANCE_OPS_CHANNEL_ID"] = "999888777666555444"
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["HOMELAB_DB_PATH"] = _tmp_db.name
_tmp_dir = tempfile.mkdtemp(prefix="maint-routing-test-")
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


class Base(unittest.TestCase):
    def setUp(self):
        self.ops = []
        self.boss = []

        async def ops_send(text):
            self.ops.append(text)

        async def boss_send(text):
            self.boss.append(text)

        self._real_channel = mn.OPS_CHANNEL_ID
        mn.OPS_CHANNEL_ID = "999888777666555444"
        mn.bind(ops_send=ops_send, boss_send=boss_send)

    def tearDown(self):
        mn.OPS_CHANNEL_ID = self._real_channel
        mn.bind(ops_send=None, boss_send=None)


# ── 1. The routing table itself ────────────────────────────────────────────
class RoutingTableTests(Base):
    def test_ops_events_never_reach_telegram(self):
        for event in ("incident_opened", "repair_started", "repair_failed",
                      "incident_resolved", "incident_escalated",
                      "approval_required", "diagnostic_progress",
                      "diagnostic_result", "lifecycle_notice", "summary",
                      "needs_boss_hands"):
            self.ops.clear()
            self.boss.clear()
            route = run(mn.notify(event, f"test {event}"))
            self.assertEqual(route, mn.OPS, event)
            self.assertEqual(self.ops, [f"test {event}"], event)
            self.assertEqual(self.boss, [], event)

    def test_urgent_events_reach_both(self):
        # `needs_boss_hands` is deliberately NOT here: "nothing automatic is
        # left" is a status for the feed, not a decision that may page the Boss.
        for event in ("boss_approval_required",
                      "security_alert", "data_loss_alert"):
            self.ops.clear()
            self.boss.clear()
            route = run(mn.notify(event, f"urgent {event}"))
            self.assertEqual(route, mn.OPS_AND_BOSS, event)
            self.assertEqual(self.ops, [f"urgent {event}"], event)
            self.assertEqual(self.boss, [f"urgent {event}"], event)

    def test_routine_chatter_goes_nowhere(self):
        route = run(mn.notify("task_lifecycle", "task lt_abc claimed by worker"))
        self.assertEqual(route, mn.DROP)
        self.assertEqual(self.ops, [])
        self.assertEqual(self.boss, [])

    def test_unknown_event_defaults_to_the_ops_feed(self):
        route = run(mn.notify("something_new", "hello"))
        self.assertEqual(route, mn.OPS)
        self.assertEqual(self.ops, ["hello"])
        self.assertEqual(self.boss, [])


# ── 2. Nothing is ever lost — and nothing non-urgent reaches Telegram ──────
class FallbackTests(Base):
    def setUp(self):
        super().setUp()
        self.relay = []

        async def fallback_send(text):
            self.relay.append(text)

        self._fallback = fallback_send
        mn.bind(ops_send=self._ops_capture(), boss_send=self._boss_capture(),
                fallback_send=fallback_send)

    def test_unconfigured_ops_channel_uses_the_non_telegram_relay(self):
        mn.OPS_CHANNEL_ID = ""
        route = run(mn.notify("incident_opened", "🔴 something broke"))
        self.assertEqual(route, "fallback")
        self.assertEqual(self.ops, [])
        self.assertEqual(self.relay, ["🔴 something broke"])
        self.assertEqual(self.boss, [], "Telegram is never the fallback")

    def test_failed_ops_send_uses_the_non_telegram_relay(self):
        async def broken(_text):
            raise RuntimeError("channel gone")

        mn.bind(ops_send=broken, boss_send=self._boss_capture(),
                fallback_send=self._fallback)
        route = run(mn.notify("incident_opened", "🔴 something broke"))
        self.assertEqual(route, "fallback")
        self.assertEqual(self.relay, ["🔴 something broke"])
        self.assertEqual(self.boss, [],
                         "a Discord outage must not push maintenance to Telegram")

    def test_urgent_events_still_page_even_if_the_feed_is_down(self):
        async def broken(_text):
            raise RuntimeError("channel gone")

        mn.bind(ops_send=broken, boss_send=self._boss_capture(),
                fallback_send=self._fallback)
        route = run(mn.notify("security_alert", "🔐 key exposed"))
        self.assertEqual(route, "boss")
        self.assertEqual(self.boss, ["🔐 key exposed"])

    def test_dropped_chatter_is_not_resurrected_by_a_missing_ops_channel(self):
        mn.OPS_CHANNEL_ID = ""
        route = run(mn.notify("task_lifecycle", "worker started"))
        self.assertEqual(route, mn.DROP)
        self.assertEqual(self.boss, [])
        self.assertEqual(self.relay, [])

    def _ops_capture(self):
        async def ops_send(text):
            self.ops.append(text)
        return ops_send

    def _boss_capture(self):
        async def boss_send(text):
            self.boss.append(text)
        return boss_send


# ── 3. The monitor's own notifications ─────────────────────────────────────
class MonitorRoutingTests(Base):
    def setUp(self):
        super().setUp()
        self.legacy = []

        async def legacy_notify(text):
            self.legacy.append(text)

        mon._notify_boss = legacy_notify

    def tearDown(self):
        mon._notify_boss = None
        super().tearDown()

    def test_incident_notifications_go_to_the_ops_feed(self):
        run(mon._notify("incident_opened", "🔴 Incident opened — Jellyfin: down"))
        run(mon._notify("incident_resolved", "✅ Resolved — Jellyfin"))
        self.assertEqual(len(self.ops), 2)
        self.assertEqual(self.boss, [])
        # The Boss's legacy line is a fallback only — it must stay silent while
        # the ops channel is taking traffic.
        self.assertEqual(self.legacy, [])

    def test_needs_boss_hands_stays_on_the_feed(self):
        """An unrepairable incident is reported, not escalated to the Boss's
        phone — it is a status, and it repeats for as long as the fault lasts."""
        run(mon._notify("needs_boss_hands", "🆘 Jellyfin: needs your hands."))
        self.assertEqual(len(self.ops), 1)
        self.assertEqual(self.boss, [])
        self.assertEqual(self.legacy, [])

    def test_monitor_falls_back_to_its_bound_boss_notifier(self):
        mn.bind(ops_send=None, boss_send=None)
        run(mon._notify("incident_opened", "🔴 Incident opened — Joplin: down"))
        self.assertEqual(self.ops, [])
        self.assertEqual(self.legacy, ["🔴 Incident opened — Joplin: down"])


# ── 4. Maintenance summaries ────────────────────────────────────────────────
class SummaryTests(Base):
    def setUp(self):
        super().setUp()
        conn = mon._db()
        conn.execute("DELETE FROM monitor_incidents")
        conn.execute("DELETE FROM monitor_checks")
        conn.execute("DELETE FROM monitor_meta")
        conn.commit()

    def test_first_run_only_starts_the_clock(self):
        run(mon.maybe_send_summary())
        self.assertEqual(self.ops, [])
        self.assertTrue(float(mon._meta_get(mon._SUMMARY_META_KEY)) > 0)

    def test_quiet_window_sends_nothing(self):
        mon._meta_set(mon._SUMMARY_META_KEY, str(time.time() - 100_000))
        run(mon.maybe_send_summary())
        self.assertEqual(self.ops, [])
        self.assertEqual(self.boss, [])

    def test_summary_reports_incidents_to_the_ops_feed_only(self):
        now = time.time()
        mon._insert_incident({
            "incident_id": "mi_test1", "key": "jellyfin", "display_name": "Jellyfin",
            "status": mon.RESOLVED, "opened_at": now - 60, "updated_at": now,
            "closed_at": now})
        mon._meta_set(mon._SUMMARY_META_KEY, str(now - 100_000))
        run(mon.maybe_send_summary())
        self.assertEqual(len(self.ops), 1)
        self.assertIn("Maintenance summary", self.ops[0])
        self.assertIn("1 incident", self.ops[0])
        self.assertEqual(self.boss, [])

    def test_summary_can_be_disabled(self):
        mon._meta_set(mon._SUMMARY_META_KEY, str(time.time() - 100_000))
        real = mon.SUMMARY_INTERVAL_SECS
        mon.SUMMARY_INTERVAL_SECS = 0
        try:
            run(mon.maybe_send_summary())
        finally:
            mon.SUMMARY_INTERVAL_SECS = real
        self.assertEqual(self.ops, [])


# ── 5. Task-supervisor announcements for maintenance-originated work ───────
class TaskAnnounceTests(Base):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        ts.DB_PATH = self.tmp.name
        ts._conn = None
        ts._running.clear()
        ts._started = False
        self.chat = []

        async def fake_send(channel_id, text, file_path=None, filename=None):
            self.chat.append((channel_id, text))

        ts._send = fake_send
        ts._db()

    def tearDown(self):
        ts._send = None
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass
        super().tearDown()

    def _row(self, status, channel, **extra):
        now = time.time()
        task_id = "lt_" + status[:4] + str(int(now * 1000))[-8:]
        row = {"task_id": task_id, "task_type": "hermes_escalation",
               "title": "Hermes diagnosis — jellyfin", "channel_id": channel,
               "status": status, "created_at": now, "updated_at": now}
        row.update(extra)
        ts._insert(row)
        return task_id

    def test_worker_started_is_not_announced(self):
        tid = self._row("running", mn.OPS_CHANNEL)
        run(ts._maybe_announce(tid))
        self.assertEqual(self.ops, [])
        self.assertEqual(self.boss, [])
        self.assertEqual(self.chat, [])

    def test_result_is_announced_to_the_ops_feed(self):
        tid = self._row("completed", mn.OPS_CHANNEL,
                        result_summary="Jellyfin: Hermes diagnosis — stale mount")
        run(ts._maybe_announce(tid))
        self.assertEqual(self.ops, ["Jellyfin: Hermes diagnosis — stale mount"])
        self.assertEqual(self.boss, [])
        self.assertEqual(self.chat, [])

    def test_failure_keeps_the_task_id_for_troubleshooting(self):
        tid = self._row("failed", mn.OPS_CHANNEL, error_category="bridge_unreachable",
                        result_summary="Hermes bridge unreachable")
        run(ts._maybe_announce(tid))
        self.assertEqual(len(self.ops), 1)
        self.assertIn("bridge_unreachable", self.ops[0])
        self.assertIn(ts._short(tid), self.ops[0])

    def test_paused_task_is_an_approval_signal_on_the_ops_feed(self):
        tid = self._row("paused_auth", mn.OPS_CHANNEL)
        run(ts._maybe_announce(tid))
        self.assertEqual(len(self.ops), 1)
        self.assertIn("authentication", self.ops[0])
        self.assertEqual(self.boss, [])

    def test_a_chat_task_still_answers_in_its_own_conversation(self):
        tid = self._row("completed", "tg:424242", result_summary="done")
        run(ts._maybe_announce(tid))
        self.assertEqual(self.chat, [("tg:424242", "✅ Task " + ts._short(tid)
                                      + " — Hermes diagnosis — jellyfin: done. done")])
        self.assertEqual(self.ops, [])

    def test_announcement_is_still_recorded_once(self):
        tid = self._row("completed", mn.OPS_CHANNEL, result_summary="first")
        run(ts._maybe_announce(tid))
        run(ts._maybe_announce(tid))
        self.assertEqual(self.ops, ["first"])
        self.assertEqual(ts.get_task(tid)["last_announced"], "completed")


# ── 6. The sentinel channel ─────────────────────────────────────────────────
class SentinelTests(Base):
    def test_sentinel_is_recognised_and_is_not_a_telegram_channel(self):
        self.assertTrue(mn.is_ops_channel(mn.OPS_CHANNEL))
        self.assertFalse(mn.is_ops_channel("tg:424242"))
        self.assertFalse(mn.is_ops_channel(""))
        self.assertFalse(mn.OPS_CHANNEL.startswith("tg:"))

    def test_ops_channel_id_is_empty_when_unconfigured(self):
        self.assertEqual(mn.ops_channel_id(), mn.OPS_CHANNEL)
        mn.OPS_CHANNEL_ID = ""
        self.assertEqual(mn.ops_channel_id(), "")


if __name__ == "__main__":
    unittest.main()

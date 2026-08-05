"""
Focused tests for persistent active incidents (one per asset/fault condition).

This is a regression suite for a real production bug: BLACK-BOXX and Joplin
each reopened roughly every 30 minutes, and each recurrence created a fresh
incident AND a fresh billed Hermes job — 297 incidents and 297 Hermes jobs for
two faults that never went away. The cause was escalation CLOSING the incident,
so the cooldown expired and the whole cycle restarted.

Nothing here touches the network, Hermes, Docker or a real runbook: check
functions are scripted, `_escalate` is a counting stub, and notifications are
captured in memory. Replayed detections stand in for monitor cycles so the
behaviour is provable without waiting 30 minutes.

Run:  venv/bin/python -m unittest tests.test_incident_dedup -v
"""

import asyncio
import json
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
_tmp_dir = tempfile.mkdtemp(prefix="incident-dedup-test-")
os.environ["HOMELAB_LIFECYCLE_MIRROR"] = os.path.join(_tmp_dir, "lifecycle.yml")
os.environ["HOMELAB_DECOMMISSION_ARCHIVE_DIR"] = os.path.join(_tmp_dir, "archive")

import tools
tools.OWNER_USER_ID = BOSS_ID
tools.CREW_USER_IDS = {CREW_ID}

import maintenance_notify as mn
import homelab_monitor as mon


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Scripted faults, modelled on the two real production offenders ─────────
BLACK_BOXX_FAULT = {
    "healthy": False,
    "diagnosis": ("BLACK-BOXX is unhealthy in a way outside the known "
                  "auto-repairable condition: ap_interface; hostapd_process"),
    "checks": [{"name": "ap_interface", "ok": False, "detail": "wlan0 down"},
               {"name": "hostapd_process", "ok": False, "detail": "not running"},
               {"name": "ssh", "ok": True, "detail": "reachable"}],
    "repair": None, "repair_result": None, "escalate": True,
}

JOPLIN_FAULT = {
    "healthy": False,
    "diagnosis": ("Joplin is unhealthy at the container/database layer "
                  "(container:loki-joplin-api) — stateful stack, no automatic action"),
    "checks": [{"name": "container_running", "ok": False,
                "detail": "loki-joplin-api: restarting"},
               {"name": "api_ping", "ok": False, "detail": "connection refused"}],
    "repair": None, "repair_result": None, "escalate": True,
}

HEALTHY = {"healthy": True, "diagnosis": "all good", "checks": [],
           "repair": None, "repair_result": None, "escalate": False}


def scripted(*results):
    """A check function that returns each result in turn, repeating the last."""
    state = {"i": 0}

    async def check(allow_repairs: bool):
        i = min(state["i"], len(results) - 1)
        state["i"] += 1
        out = results[i]
        return json.loads(json.dumps(out))    # fresh copy per call
    return check


def constant(result):
    async def check(allow_repairs: bool):
        return json.loads(json.dumps(result))
    return check


class Base(unittest.TestCase):
    def setUp(self):
        conn = mon._db()
        conn.execute("DELETE FROM monitor_incidents")
        conn.execute("DELETE FROM monitor_checks")
        conn.execute("DELETE FROM monitor_meta")
        conn.commit()

        self.ops, self.boss = [], []

        async def ops_send(text):
            self.ops.append(text)

        async def boss_send(text):
            self.boss.append(text)

        mn.OPS_CHANNEL_ID = "999888777666555444"
        mn.bind(ops_send=ops_send, boss_send=boss_send)

        # Count Hermes submissions without touching the supervisor or bridge.
        self.submissions = []
        self._real_escalate = mon._escalate

        async def fake_escalate(incident_id, key, display_name, symptom, checks):
            self.submissions.append((incident_id, key))
            task_id = f"lt_fake{len(self.submissions):04d}"
            mon._update_incident(incident_id, escalated_task_id=task_id)
            return task_id

        mon._escalate = fake_escalate

        # Joplin summaries are a side effect we do not exercise here.
        self._real_joplin = mon._write_joplin_summary

        async def no_joplin(_incident):
            return None

        mon._write_joplin_summary = no_joplin

    def tearDown(self):
        mon._escalate = self._real_escalate
        mon._write_joplin_summary = self._real_joplin
        mn.bind(ops_send=None, boss_send=None)

    def poll(self, key, display, check_fn, times=1):
        for _ in range(times):
            run(mon._process_check(key, display, check_fn, display))

    def poll_over_time(self, key, display, check_fn, times=1):
        """Polls spread far enough apart that any cooldown has lapsed — the
        real 30-minute production cadence. Dedup must hold on its own here,
        with no help from the cooldown window."""
        for _ in range(times):
            mon._update_check(key, cooldown_until=0)
            run(mon._process_check(key, display, check_fn, display))

    def incidents(self, key):
        return [dict(r) for r in mon._db().execute(
            "SELECT * FROM monitor_incidents WHERE key=? ORDER BY opened_at", (key,))]

    def count(self, needle):
        return len([n for n in self.ops if needle in n])


# ── THE COMPLETION CONDITION ───────────────────────────────────────────────
class TenDetectionsTests(Base):
    def _assert_single_incident(self, key, display, fault):
        # Replayed at the production cadence: the cooldown has lapsed before
        # every one of the ten detections, so nothing here is masked by it.
        self.poll_over_time(key, display, constant(fault), times=10)

        rows = self.incidents(key)
        self.assertEqual(len(rows), 1, f"{key}: expected ONE incident, got {len(rows)}")
        inc = rows[0]
        self.assertTrue(mon._is_active(inc), f"{key}: incident must stay active")
        self.assertIsNone(inc["closed_at"], f"{key}: must not close on escalation")
        self.assertEqual(inc["status"], mon.ESCALATED)

        # Exactly one Hermes job for ten detections.
        self.assertEqual(len(self.submissions), 1,
                         f"{key}: expected ONE Hermes escalation, got {len(self.submissions)}")
        self.assertEqual(inc["hermes_attempts"], 1)

        # Detection 1 is below the failure threshold; the incident opens on
        # detection 2 and detections 3..10 attach to it as recurrences.
        self.assertEqual(inc["occurrence_count"], 9)
        self.assertGreater(inc["last_seen"], inc["first_seen"] - 1)
        self.assertEqual(inc["first_seen"], inc["opened_at"])

        # One notification each, never repeated.
        self.assertEqual(self.count("Incident opened"), 1, self.ops)
        self.assertEqual(self.count("Escalated"), 1, self.ops)
        self.assertEqual(self.boss, [], "Telegram must stay out of routine maintenance")

    def test_black_boxx_ten_detections_one_incident_one_escalation(self):
        self._assert_single_incident("black-boxx", "BLACK-BOXX", BLACK_BOXX_FAULT)

    def test_joplin_ten_detections_one_incident_one_escalation(self):
        self._assert_single_incident("joplin", "Joplin", JOPLIN_FAULT)

    def test_both_assets_together_stay_independent_and_deduped(self):
        for _ in range(10):
            self.poll_over_time("black-boxx", "BLACK-BOXX", constant(BLACK_BOXX_FAULT))
            self.poll_over_time("joplin", "Joplin", constant(JOPLIN_FAULT))
        self.assertEqual(len(self.incidents("black-boxx")), 1)
        self.assertEqual(len(self.incidents("joplin")), 1)
        self.assertEqual(len(self.submissions), 2)      # one each, not twenty
        self.assertEqual(self.count("Incident opened"), 2)
        self.assertEqual(self.count("Escalated"), 2)
        self.assertEqual(self.boss, [])

    def test_evidence_does_not_grow_without_bound(self):
        self.poll("black-boxx", "BLACK-BOXX", constant(BLACK_BOXX_FAULT), times=60)
        inc = self.incidents("black-boxx")[0]
        self.assertLessEqual(len(json.loads(inc["evidence_json"])), mon.EVIDENCE_LIMIT)
        self.assertEqual(inc["occurrence_count"], 59)


# ── The exact production cycle that must never happen again ────────────────
class NoReopenCycleTests(Base):
    def test_expired_cooldown_does_not_start_a_new_incident(self):
        """The production bug verbatim: escalate, let the cooldown lapse, and
        keep failing. Previously this minted a new incident + Hermes job every
        30 minutes."""
        self.poll("black-boxx", "BLACK-BOXX", constant(BLACK_BOXX_FAULT), times=2)
        self.assertEqual(len(self.submissions), 1)

        for _ in range(5):
            # Fast-forward past any cooldown the old code would have honoured.
            mon._update_check("black-boxx", cooldown_until=0)
            self.poll("black-boxx", "BLACK-BOXX", constant(BLACK_BOXX_FAULT), times=6)

        self.assertEqual(len(self.incidents("black-boxx")), 1)
        self.assertEqual(len(self.submissions), 1)
        self.assertEqual(self.count("Incident opened"), 1)
        self.assertEqual(self.count("Escalated"), 1)

    def test_stale_check_pointer_does_not_orphan_the_incident(self):
        """A restart (or manual DB edit) can clear monitor_checks.open_incident_id.
        The incident table is the truth, so dedup must still hold."""
        self.poll("joplin", "Joplin", constant(JOPLIN_FAULT), times=2)
        mon._update_check("joplin", open_incident_id=None)
        self.poll("joplin", "Joplin", constant(JOPLIN_FAULT), times=4)
        self.assertEqual(len(self.incidents("joplin")), 1)
        self.assertEqual(len(self.submissions), 1)


# ── Recovery: closing only on verified health ──────────────────────────────
class RecoveryTests(Base):
    def test_one_healthy_poll_is_not_recovery(self):
        self.poll("joplin", "Joplin", constant(JOPLIN_FAULT), times=2)
        self.poll("joplin", "Joplin", constant(HEALTHY), times=1)
        inc = self.incidents("joplin")[0]
        self.assertTrue(mon._is_active(inc), "one healthy poll must not close it")
        self.assertEqual(self.count("Resolved"), 0)

    def test_incident_closes_after_the_recovery_threshold(self):
        self.poll("joplin", "Joplin", constant(JOPLIN_FAULT), times=2)
        self.poll("joplin", "Joplin", constant(HEALTHY), times=mon.RECOVERY_THRESHOLD)
        inc = self.incidents("joplin")[0]
        self.assertEqual(inc["status"], mon.RESOLVED)
        self.assertIsNotNone(inc["closed_at"])
        self.assertEqual(self.count("Resolved"), 1)
        self.assertEqual(self.boss, [])

    def test_genuine_recurrence_after_resolution_escalates_again(self):
        """The one case that SHOULD produce a second incident and a second
        Hermes job: the fault verifiably cleared and later came back."""
        self.poll("joplin", "Joplin", constant(JOPLIN_FAULT), times=2)
        self.poll("joplin", "Joplin", constant(HEALTHY), times=mon.RECOVERY_THRESHOLD)
        self.assertEqual(len(self.submissions), 1)

        mon._update_check("joplin", cooldown_until=0)   # cooldown genuinely elapsed
        self.poll("joplin", "Joplin", constant(JOPLIN_FAULT), times=2)
        self.assertEqual(len(self.incidents("joplin")), 2)
        self.assertEqual(len(self.submissions), 2)
        self.assertEqual(self.count("Incident opened"), 2)

    def test_cooldown_still_blocks_flapping_right_after_a_close(self):
        self.poll("joplin", "Joplin", constant(JOPLIN_FAULT), times=2)
        self.poll("joplin", "Joplin", constant(HEALTHY), times=mon.RECOVERY_THRESHOLD)
        self.poll("joplin", "Joplin", constant(JOPLIN_FAULT), times=4)
        self.assertEqual(len(self.incidents("joplin")), 1)   # cooldown holds
        self.assertEqual(len(self.submissions), 1)


# ── Hermes unavailable / quota exhausted ───────────────────────────────────
class HermesBackoffTests(Base):
    def setUp(self):
        super().setUp()

        async def failing_escalate(incident_id, key, display_name, symptom, checks):
            self.submissions.append((incident_id, key))
            return None          # bridge unreachable / quota gone

        mon._escalate = failing_escalate

    def test_failed_submission_backs_off_instead_of_retrying_every_poll(self):
        self.poll("black-boxx", "BLACK-BOXX", constant(BLACK_BOXX_FAULT), times=12)
        inc = self.incidents("black-boxx")[0]
        self.assertEqual(len(self.submissions), 1,
                         "a failed submission must not be retried every poll")
        self.assertGreater(inc["hermes_backoff_until"], time.time())
        self.assertEqual(inc["hermes_block_reason"], "hermes unreachable")
        self.assertTrue(mon._is_active(inc))

    def test_unrepairable_notice_is_said_once_and_only_on_the_feed(self):
        self.poll("black-boxx", "BLACK-BOXX", constant(BLACK_BOXX_FAULT), times=12)
        # Said once for the whole incident, not once per poll...
        self.assertEqual(self.count("needs your hands"), 1)
        # ...and it never pages the Boss: it is a status, not a decision.
        self.assertEqual(self.boss, [])

    def test_retry_is_allowed_after_backoff_but_capped(self):
        self.poll("black-boxx", "BLACK-BOXX", constant(BLACK_BOXX_FAULT), times=3)
        for _ in range(mon.HERMES_MAX_SUBMIT_ATTEMPTS + 3):
            inc = self.incidents("black-boxx")[0]
            mon._update_incident(inc["incident_id"], hermes_backoff_until=0)
            self.poll("black-boxx", "BLACK-BOXX", constant(BLACK_BOXX_FAULT), times=1)
        self.assertEqual(len(self.submissions), mon.HERMES_MAX_SUBMIT_ATTEMPTS)
        inc = self.incidents("black-boxx")[0]
        self.assertEqual(inc["status"], mon.GAVE_UP)
        self.assertTrue(mon._is_active(inc), "given up on ≠ closed")
        self.assertEqual(len(self.incidents("black-boxx")), 1)

    def test_quota_exhaustion_reported_by_the_task_is_recorded_once(self):
        import task_supervisor as ts

        async def ok_escalate(incident_id, key, display_name, symptom, checks):
            self.submissions.append((incident_id, key))
            mon._update_incident(incident_id, escalated_task_id="lt_quota01")
            return "lt_quota01"

        mon._escalate = ok_escalate
        self.poll("black-boxx", "BLACK-BOXX", constant(BLACK_BOXX_FAULT), times=2)

        real_get = ts.get_task
        ts.get_task = lambda tid: {"task_id": tid, "status": "paused_quota",
                                   "error_category": "quota"}
        try:
            self.poll("black-boxx", "BLACK-BOXX", constant(BLACK_BOXX_FAULT), times=6)
        finally:
            ts.get_task = real_get

        inc = self.incidents("black-boxx")[0]
        self.assertEqual(inc["hermes_result"], "paused_quota")
        self.assertEqual(inc["hermes_block_reason"], "quota")
        self.assertGreater(inc["hermes_backoff_until"], time.time())
        self.assertEqual(len(self.submissions), 1, "never buy a second job")
        self.assertEqual(self.count("out of quota"), 1,
                         "quota notice said once, not per poll")
        self.assertEqual(self.boss, [], "quota exhaustion must not page Telegram")
        self.assertTrue(mon._is_active(inc))


# ── Repair budget still bounded, and still escalates once ──────────────────
class RepairBudgetTests(Base):
    def _repairable(self):
        return {"healthy": False, "diagnosis": "policy rules drifted",
                "checks": [{"name": "policy_rules", "ok": False, "detail": "missing"}],
                "repair": {"action": "restore_rules", "description": "restore policy rules"},
                "repair_result": {"ok": False}, "escalate": True}

    def test_repair_attempts_are_capped_then_escalated_once(self):
        fault = self._repairable()
        self.poll("black-boxx", "BLACK-BOXX", constant(fault), times=10)
        inc = self.incidents("black-boxx")[0]
        limit = 2   # black-boxx is the idempotent-double-attempt asset
        self.assertEqual(inc["repair_attempts"], limit)
        self.assertEqual(len(self.submissions), 1)
        self.assertEqual(self.count("Repair starting"), limit)
        self.assertEqual(self.count("Incident opened"), 1)
        self.assertEqual(len(self.incidents("black-boxx")), 1)

    def test_successful_verified_repair_resolves_immediately(self):
        fault = self._repairable()
        healed = dict(HEALTHY, repair_result={"ok": True})
        self.poll("cloudflare-ddns", "Cloudflare DDNS",
                  scripted(fault, fault, healed), times=3)
        rows = self.incidents("cloudflare-ddns")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], mon.RESOLVED)
        self.assertEqual(self.count("Resolved"), 1)


# ── The summary reflects the persistent incident ───────────────────────────
class SummaryTests(Base):
    def test_summary_lists_the_active_incident_with_its_occurrence_count(self):
        self.poll("black-boxx", "BLACK-BOXX", constant(BLACK_BOXX_FAULT), times=10)
        text = mon._summary_text(time.time() - 3600)
        self.assertIn("BLACK-BOXX", text)
        self.assertIn("9 detections", text)
        self.assertIn("escalated", text)


if __name__ == "__main__":
    unittest.main()

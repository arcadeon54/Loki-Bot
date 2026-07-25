"""
Focused tests for the Loki <-> Hermes escalation integration.

Nothing here touches a real network or the deployed bridge on razr: the
`homelab_hermes` client functions (submit_diagnosis / get_job / cancel_job)
are monkeypatched directly, and `homelab_maintenance.run_runbook` is
monkeypatched to return canned runbook results so the deterministic-runbook
routing decision (call Hermes or don't) is exercised without touching real
system commands.

What this suite intentionally does NOT test: which model (Sonnet vs Opus)
Hermes itself chooses, its budget enforcement, or its escalation gate — that
lives entirely on the bridge (razr) and is covered by hermes-bridge's own
42-test suite. This suite covers only Loki's side of the boundary: routing
(known runbook never reaches Hermes, unfamiliar failures do), the approval
boundary before any state change, authorization, restart persistence,
cancellation, notification correlation/dedup, old-task isolation, and
redaction.

Run:  venv/bin/python -m unittest tests.test_homelab_hermes -v
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
_tmp_hm = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_hm.close()
os.environ.setdefault("HOMELAB_DB_PATH", _tmp_hm.name)

import tools
tools.OWNER_USER_ID = BOSS_ID
tools.CREW_USER_IDS = {CREW_ID}

import homelab_maintenance as hm
import homelab_hermes as hh
import maintenance_policy as policy
import task_supervisor as ts

REG = hm.reload_registry()
JF = REG.get("jellyfin")


def ctx(user_id, channel="tg:424242"):
    return tools.ToolContext(user_id=user_id, user_name="t", channel_id=channel)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


HEALTHY_RESULT = {
    "checks": [{"name": "container_state", "ok": True, "detail": "state=running"}],
    "healthy": True, "escalate": False, "repair": None, "repair_result": None,
    "diagnosis": "Jellyfin is running and serving; mounts and disk healthy.",
    "runbook": "jellyfin_health",
}

UNFAMILIAR_RESULT = {
    "checks": [{"name": "log_scan", "ok": False, "detail": "unrecognized docker error"}],
    "healthy": False, "escalate": True, "repair": None, "repair_result": None,
    "diagnosis": "Jellyfin failure outside every known condition.",
    "runbook": "jellyfin_health",
}


class FakeOps:
    def __init__(self):
        self.log_excerpt = ""
        self.commands_run = []


def canned_runbook(result_dict):
    async def _run(asset, allow_repairs):
        return dict(result_dict), FakeOps()
    return _run


def job(state, **over):
    base = {
        "id": "hj_test0001", "state": state, "asset": "jellyfin",
        "cost_usd": 0.01, "escalated": False, "phases": [], "tool_calls": [],
        "diagnosis": None, "proposals": [], "error": None,
    }
    base.update(over)
    return base


DIAGNOSIS_APPROVAL = {
    "probable_root_cause": "CPU-bound transcoding, no GPU passthrough.",
    "evidence": ["container_inspect: no /dev/dri device"],
    "confidence": "medium", "matching_runbook": None,
    "proposed_action": "Pass /dev/dri into the container and re-test playback.",
    "risk_level": "approval", "verification": "Re-run jellyfin_health after the change.",
    "rollback": "Remove the device mapping.", "approval_required": True,
    "escalate": False, "escalation_reason": "",
}

DIAGNOSIS_MANUAL = dict(DIAGNOSIS_APPROVAL, risk_level="manual")
DIAGNOSIS_AUTO_NOOP = dict(DIAGNOSIS_APPROVAL, risk_level="auto",
                          proposed_action="none", approval_required=False)
DIAGNOSIS_MATCHING = dict(DIAGNOSIS_APPROVAL, risk_level="approval",
                          matching_runbook="jellyfin_health")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        hm.DB_PATH = self.tmp.name
        hm._conn = None
        hm._db()
        policy.configure(REG.allowed_values())

        self.ts_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.ts_tmp.close()
        ts.DB_PATH = self.ts_tmp.name
        ts._conn = None
        ts._running.clear()
        ts._started = False
        ts.MAX_CONCURRENCY = 1
        self.sent = []

        async def fake_send(channel_id, text, file_path=None, filename=None):
            self.sent.append((channel_id, text))

        ts._send = fake_send
        ts._db()

        self._orig_enabled = hh.enabled
        hh.enabled = True
        self._orig_run_runbook = hm.run_runbook

    def tearDown(self):
        hh.enabled = self._orig_enabled
        hm.run_runbook = self._orig_run_runbook
        for f in (self.tmp.name, self.ts_tmp.name):
            try:
                os.unlink(f)
            except OSError:
                pass

    def submit_hermes_task(self, incident_id="hi_test1", asset="jellyfin",
                          user=BOSS_ID, channel="tg:424242"):
        tt = ts._TYPES["hermes_escalation"]
        return ts.submit(tt, ctx(user, channel), {
            "asset": asset, "incident_id": incident_id, "symptom": "unusual docker error",
            "bundle": {"format": hm.BUNDLE_FORMAT, "asset": asset},
        })


# ── Routing ──────────────────────────────────────────────────────────────────
class RoutingTests(Base):
    def test_normal_chat_never_reaches_hermes(self):
        """A regular (non-Boss) conversation can't even see the Hermes tools,
        so the model has no way to call Hermes from ordinary chat."""
        everyone_schemas = tools.schemas_for("999999999999999999")
        crew_schemas = tools.schemas_for(CREW_ID)
        names = {s["function"]["name"] for s in everyone_schemas}
        crew_names = {s["function"]["name"] for s in crew_schemas}
        for tool_name in ("hermes_diagnose", "hermes_job_status",
                         "hermes_job_cancel", "hermes_escalate"):
            self.assertNotIn(tool_name, names)
            self.assertNotIn(tool_name, crew_names)
        boss_names = {s["function"]["name"] for s in tools.schemas_for(BOSS_ID)}
        self.assertIn("hermes_diagnose", boss_names)

    def test_known_runbook_never_calls_hermes(self):
        hm.run_runbook = canned_runbook(HEALTHY_RESULT)
        calls = []

        async def fake_submit(*a, **kw):
            calls.append((a, kw))
            return job("running")

        hh.submit_diagnosis = fake_submit
        out = json.loads(run(hm._tool_hermes_diagnose(
            {"asset": "jellyfin", "symptom": "Jellyfin is down"}, ctx(BOSS_ID))))
        self.assertTrue(out["ok"])
        self.assertFalse(out["used_hermes"])
        self.assertTrue(out["healthy"])
        self.assertEqual(calls, [], "a resolvable runbook must never call Hermes")

    def test_unfamiliar_incident_calls_sonnet_triage(self):
        # "Sonnet triage" is the bridge's own model choice (covered by
        # hermes-bridge's suite); Loki's side of the contract is: queue the
        # right background task with the right bundle, and — once that task
        # actually runs — submit exactly one job to the bridge.
        hm.run_runbook = canned_runbook(UNFAMILIAR_RESULT)
        submitted = {}

        async def fake_submit(asset_key, symptom, bundle, incident_id, request_id=None):
            submitted["args"] = (asset_key, symptom, incident_id)
            return job("running")

        hh.submit_diagnosis = fake_submit
        out = json.loads(run(hm._tool_hermes_diagnose(
            {"asset": "jellyfin", "symptom": "unusual docker error"}, ctx(BOSS_ID))))
        self.assertTrue(out["ok"])
        self.assertTrue(out["used_hermes"])
        self.assertIn("task_id", out)

        row = ts.get_task(out["task_id"])
        self.assertEqual(row["task_type"], "hermes_escalation")
        self.assertEqual(row["priority"], 10)
        self.assertEqual(json.loads(row["input_json"])["asset"], "jellyfin")
        inc = hm.get_incident(out["incident_id"])
        self.assertEqual(inc["status"], "escalating_to_hermes")
        self.assertEqual(inc["hermes_task_id"], out["task_id"])

        # Now actually run the queued task: this is where Loki submits to Hermes.
        tt = ts._TYPES["hermes_escalation"]
        handle = ts.TaskHandle(out["task_id"], tt.capabilities)
        ts._update(out["task_id"], status="running", started_at=1, heartbeat_at=1)

        async def fake_get_job(job_id):
            return job("completed",
                      diagnosis={"probable_root_cause": "x", "evidence": [],
                                "confidence": "high", "matching_runbook": None,
                                "proposed_action": "none", "risk_level": "auto",
                                "verification": "n/a", "rollback": "n/a",
                                "approval_required": False, "escalate": False,
                                "escalation_reason": ""})

        hh.get_job = fake_get_job
        run(ts._run(out["task_id"], tt, handle))
        self.assertEqual(submitted["args"][0], "jellyfin")
        self.assertEqual(ts.get_task(out["task_id"])["status"], "completed")


# ── Escalation boundary (Sonnet -> Opus, surfaced as a one-time notice) ─────
class EscalationBoundaryTests(Base):
    def test_mid_run_escalation_notice_sent_once(self):
        iid = "hi_test1"
        now = time.time()
        hm._db().execute(
            "INSERT INTO incidents (incident_id, task_id, asset, symptom, status,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (iid, "", "jellyfin", "x", "hermes_triage", now, now))
        hm._db().commit()
        tid = self.submit_hermes_task(incident_id=iid)
        tt = ts._TYPES["hermes_escalation"]
        handle = ts.TaskHandle(tid, tt.capabilities)
        ts._update(tid, status="running", started_at=1, heartbeat_at=1,
                   external_ref="hj_test0001")

        calls = {"n": 0}

        async def fake_get_job(job_id):
            calls["n"] += 1
            if calls["n"] < 3:
                return job("running", escalated=True)
            return job("completed", escalated=True,
                      diagnosis={"probable_root_cause": "root cause found",
                                "evidence": [], "confidence": "high",
                                "matching_runbook": None, "proposed_action": "none",
                                "risk_level": "auto", "verification": "n/a",
                                "rollback": "n/a", "approval_required": False,
                                "escalate": False, "escalation_reason": ""})

        hh.get_job = fake_get_job
        hh.POLL_SECS = 0
        result = run(ts._run(tid, tt, handle))
        notices = [t for _, t in self.sent if "deeper look" in t]
        self.assertEqual(len(notices), 1, "the escalation notice must fire exactly once")
        row = ts.get_task(tid)
        self.assertEqual(row["status"], "completed")


# ── Approval boundary ────────────────────────────────────────────────────────
class ApprovalBoundaryTests(Base):
    def _incident_with_diagnosis(self, diagnosis, iid="hi_appr1"):
        now = time.time()
        hm._db().execute(
            "INSERT INTO incidents (incident_id, task_id, asset, symptom, status,"
            " created_at, updated_at, hermes_diagnosis_json) VALUES (?,?,?,?,?,?,?,?)",
            (iid, "", "jellyfin", "unusual error", "hermes_needs_approval",
             now, now, json.dumps(diagnosis)))
        hm._db().commit()
        return iid

    def test_manual_tier_refused_outright(self):
        iid = self._incident_with_diagnosis(DIAGNOSIS_MANUAL)
        out = json.loads(run(hm._tool_hermes_escalate({"job_id": iid}, ctx(BOSS_ID))))
        self.assertFalse(out["ok"])
        self.assertIn("MANUAL", out["error"])

    def test_auto_readonly_diagnosis_has_nothing_to_approve(self):
        iid = self._incident_with_diagnosis(DIAGNOSIS_AUTO_NOOP)
        out = json.loads(run(hm._tool_hermes_escalate({"job_id": iid}, ctx(BOSS_ID))))
        self.assertTrue(out["ok"])
        self.assertIn("nothing to approve", out["note"])

    def test_approval_tier_stages_a_draft_never_executes_directly(self):
        iid = self._incident_with_diagnosis(DIAGNOSIS_APPROVAL)
        staged = []

        async def fake_intercept(spec, args, c):
            staged.append((spec.name, spec.action_type, args))
            return "📝 Draft prepared"

        orig = tools._approval_intercept
        tools.set_approval_intercept(fake_intercept)
        try:
            out = run(hm._tool_hermes_escalate({"job_id": iid}, ctx(BOSS_ID)))
        finally:
            tools.set_approval_intercept(orig)
        self.assertIn("Draft prepared", out)
        self.assertEqual(staged[0][0], "hermes_apply_action")
        self.assertEqual(staged[0][1], "hermes_repair")
        self.assertEqual(staged[0][2]["incident_id"], iid)

    def test_apply_rejects_tampered_diagnosis_hash(self):
        iid = self._incident_with_diagnosis(DIAGNOSIS_APPROVAL)
        payload, summary, err = hm._hermes_apply_prepare(
            {"incident_id": iid, "diagnosis_hash": "deadbeef"}, ctx(BOSS_ID))
        self.assertTrue(err)

    def test_apply_delegates_to_matching_runbook_when_named(self):
        iid = self._incident_with_diagnosis(DIAGNOSIS_MATCHING)
        hm.run_runbook = canned_runbook(HEALTHY_RESULT)
        payload = {"incident_id": iid,
                  "diagnosis_hash": hm._hash_diagnosis(DIAGNOSIS_MATCHING)}
        out = run(hm._hermes_apply_handler(payload, ctx(BOSS_ID)))
        self.assertIn("already healthy", out)
        self.assertEqual(hm.get_incident(iid)["status"], "repaired")

    def test_apply_with_no_matching_runbook_is_manual_followthrough_only(self):
        iid = self._incident_with_diagnosis(DIAGNOSIS_APPROVAL)  # matching_runbook=None
        payload = {"incident_id": iid,
                  "diagnosis_hash": hm._hash_diagnosis(DIAGNOSIS_APPROVAL)}
        out = run(hm._hermes_apply_handler(payload, ctx(BOSS_ID)))
        self.assertIn("no automated path", out)
        self.assertEqual(hm.get_incident(iid)["status"], "hermes_approved_manual")


# ── Authorization ────────────────────────────────────────────────────────────
class AuthzTests(Base):
    def test_crew_cannot_call_hermes_diagnose(self):
        out = run(tools.execute("hermes_diagnose",
                                json.dumps({"asset": "jellyfin", "symptom": "x"}),
                                ctx(CREW_ID)))
        self.assertIn("Permission denied", out)

    def test_crew_cannot_call_hermes_escalate(self):
        out = run(tools.execute("hermes_escalate", json.dumps({"job_id": "hi_x"}), ctx(CREW_ID)))
        self.assertIn("Permission denied", out)

    def test_boss_can_reach_hermes_diagnose(self):
        hm.run_runbook = canned_runbook(HEALTHY_RESULT)
        out = json.loads(run(hm._tool_hermes_diagnose(
            {"asset": "jellyfin", "symptom": "down"}, ctx(BOSS_ID))))
        self.assertTrue(out["ok"])


# ── Restart persistence ──────────────────────────────────────────────────────
class RestartPersistenceTests(Base):
    def test_orphan_reattaches_without_resubmitting_a_job(self):
        calls = []

        async def fake_submit(*a, **kw):
            calls.append(1)
            return job("running")

        hh.submit_diagnosis = fake_submit
        tid = self.submit_hermes_task()
        # First run: submits the job and starts polling, then "the process
        # dies" — simulate by marking it running with a stale heartbeat and no
        # live worker (an orphan) and never letting the poll loop finish.
        ts._update(tid, status="running", external_ref="hj_test0001",
                  started_at=0, heartbeat_at=0)

        run(ts._recover_one(ts.get_task(tid)))
        self.assertEqual(ts.get_task(tid)["status"], "queued",
                         "reattach must requeue, not fail, the task")

        tt = ts._TYPES["hermes_escalation"]
        handle = ts.TaskHandle(tid, tt.capabilities)
        ts._update(tid, status="running")

        async def fake_get_job(job_id):
            return job("completed",
                      diagnosis={"probable_root_cause": "x", "evidence": [],
                                "confidence": "high", "matching_runbook": None,
                                "proposed_action": "none", "risk_level": "auto",
                                "verification": "n/a", "rollback": "n/a",
                                "approval_required": False, "escalate": False,
                                "escalation_reason": ""})

        hh.get_job = fake_get_job
        run(ts._run(tid, tt, handle))
        row = ts.get_task(tid)
        self.assertEqual(row["status"], "completed")
        self.assertEqual(calls, [], "reattach must NEVER submit a second Hermes job")

    def test_incident_and_task_survive_a_reopened_connection(self):
        hm.run_runbook = canned_runbook(UNFAMILIAR_RESULT)

        async def fake_submit(*a, **kw):
            return job("running")

        hh.submit_diagnosis = fake_submit
        out = json.loads(run(hm._tool_hermes_diagnose(
            {"asset": "jellyfin", "symptom": "unusual docker error"}, ctx(BOSS_ID))))
        incident_id = out["incident_id"]

        hm._conn = None
        ts._conn = None
        inc = hm.get_incident(incident_id)
        self.assertIsNotNone(inc)
        self.assertEqual(inc["status"], "escalating_to_hermes")
        row = ts.get_task(out["task_id"])
        self.assertIsNotNone(row)
        self.assertEqual(row["conversation_id"], "tg:424242")


# ── Cancellation ─────────────────────────────────────────────────────────────
class CancellationTests(Base):
    def test_cancel_propagates_to_the_bridge(self):
        cancelled = []

        async def fake_cancel(job_id):
            cancelled.append(job_id)
            return {"ok": True}

        hh.cancel_job = fake_cancel
        tid = self.submit_hermes_task()
        tt = ts._TYPES["hermes_escalation"]
        handle = ts.TaskHandle(tid, tt.capabilities)
        handle.cancel_event.set()
        ts._update(tid, status="running", started_at=1, heartbeat_at=1,
                  external_ref="hj_test0001")
        result = run(ts._run(tid, tt, handle))
        self.assertEqual(ts.get_task(tid)["status"], "cancelled")
        self.assertEqual(cancelled, ["hj_test0001"])

    def test_hermes_job_cancel_tool_resolves_by_incident_id(self):
        iid = "hi_cancel1"
        tid = self.submit_hermes_task(incident_id=iid)
        now = time.time()
        hm._db().execute(
            "INSERT INTO incidents (incident_id, task_id, asset, symptom, status,"
            " created_at, updated_at, hermes_task_id) VALUES (?,?,?,?,?,?,?,?)",
            (iid, "", "jellyfin", "x", "hermes_triage", now, now, tid))
        hm._db().commit()
        out = json.loads(run(hm._tool_hermes_job_cancel({"job_id": iid}, ctx(BOSS_ID))))
        self.assertTrue(out["ok"])
        self.assertEqual(ts.get_task(tid)["status"], "cancelled")


# ── Duplicate-notification prevention / old-task isolation ─────────────────
class NotificationTests(Base):
    def test_completion_announced_once(self):
        tid = self.submit_hermes_task()
        ts._update(tid, status="completed", result_summary="Hermes diagnosis — done")
        run(ts._maybe_announce(tid))
        run(ts._maybe_announce(tid))
        completes = [t for _, t in self.sent if "done" in t]
        self.assertEqual(len(completes), 1)

    def test_old_hermes_result_marked_delivered_not_repeated(self):
        tid = self.submit_hermes_task()
        ts._update(tid, status="completed",
                  result_summary="Jellyfin: Hermes diagnosis — CPU-bound transcoding",
                  last_announced="completed")
        listing = json.loads(run(ts._tool_list({"active_only": False}, ctx(BOSS_ID))))
        joined = " ".join(listing["tasks"])
        self.assertNotIn("CPU-bound transcoding", joined)
        self.assertIn("already delivered", joined)
        # Explicitly asking about it by id still returns the full result.
        status = json.loads(run(ts._tool_status({"task_id": tid}, ctx(BOSS_ID))))
        self.assertIn("CPU-bound transcoding", status["task"])


# ── Redaction ────────────────────────────────────────────────────────────────
class RedactionTests(Base):
    def test_hermes_job_status_redacts_leaked_secrets(self):
        iid = "hi_redact1"
        now = time.time()
        leaky = dict(DIAGNOSIS_APPROVAL,
                    probable_root_cause="found password: hunter2hunter2 hardcoded in the config")
        hm._db().execute(
            "INSERT INTO incidents (incident_id, task_id, asset, symptom, status,"
            " created_at, updated_at, hermes_diagnosis_json) VALUES (?,?,?,?,?,?,?,?)",
            (iid, "", "jellyfin", "x", "hermes_completed", now, now, json.dumps(leaky)))
        hm._db().commit()
        hh.enabled = False  # skip the live bridge poll for this check
        out = json.loads(run(hm._tool_hermes_job_status({"job_id": iid}, ctx(BOSS_ID))))
        blob = json.dumps(out)
        self.assertNotIn("hunter2hunter2", blob)
        self.assertIn("REDACTED", blob)


if __name__ == "__main__":
    unittest.main()

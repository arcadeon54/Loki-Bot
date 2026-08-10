"""
Focused tests for the Hermes/OpenRouter provider circuit breaker and budgets
(hermes_guard.py).

The completion condition this suite exists to prove: a runaway loop cannot
drain paid OpenRouter credits even if incident deduplication breaks again.
Nothing here performs a network request — the bridge client's `_api` is
replaced with a counter that fails the test if a blocked call ever reaches it,
and the non-billable health probe is an in-memory stub.

Covers every required validation item:
  1  quota exhaustion opens the circuit
  2  100 subsequent escalation attempts produce ZERO provider requests
  3  circuit state survives a Loki restart
  4  half-open permits only one recovery attempt
  5  successful recovery closes the circuit
  6  failed recovery reopens it (bounded, doubling cooldown)
  7  hourly request ceiling
  8  daily request ceiling
  9  spend ceiling on observed bridge job cost
  10 deterministic local maintenance continues while the provider is down
  11 exactly ONE canonical provider incident
  12 notification dedupe (one ops message per transition)
  13 ordinary Telegram/Discord conversation unaffected

Run:  venv/bin/python -m unittest tests.test_hermes_guard -v
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
os.environ["MAINTENANCE_OPS_CHANNEL_ID"] = "1534655910893457669"
os.environ["HERMES_WORKER_URL"] = "http://bridge.invalid:1"
os.environ["HERMES_WORKER_TOKEN"] = "unit-test-token"
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["HOMELAB_DB_PATH"] = _tmp_db.name
_tmp_dir = tempfile.mkdtemp(prefix="hermes-guard-test-")
os.environ["HOMELAB_LIFECYCLE_MIRROR"] = os.path.join(_tmp_dir, "lifecycle.yml")
os.environ["HOMELAB_DECOMMISSION_ARCHIVE_DIR"] = os.path.join(_tmp_dir, "archive")

import tools
tools.OWNER_USER_ID = BOSS_ID
tools.CREW_USER_IDS = {CREW_ID}

import hermes_guard as hg
import homelab_hermes as hh
import homelab_monitor as mon
import maintenance_notify as mn
import task_supervisor as ts

QUOTA_MSG = "provider quota exhausted (job 12345678 paused_quota)"
UNREACHABLE_MSG = "unreachable: ClientConnectorError"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Base(unittest.TestCase):
    def setUp(self):
        conn = hg._db()
        conn.execute("DELETE FROM hermes_guard_state")
        conn.execute("DELETE FROM hermes_guard_requests")
        conn.execute("DELETE FROM hermes_guard_costs")
        conn.commit()
        mon._db().execute("DELETE FROM monitor_incidents")
        mon._db().execute("DELETE FROM monitor_checks")
        mon._db().commit()

        self.ops, self.telegram = [], []

        async def ops_send(text):
            self.ops.append(text)

        async def boss_send(text):
            self.telegram.append(text)

        mn.OPS_CHANNEL_ID = "1534655910893457669"
        mn.bind(ops_send=ops_send, boss_send=boss_send)

        # Booby-trap the bridge client: a blocked call must never reach it.
        self.api_calls = []
        self._real_api = hh._api

        async def fake_api(method, path, json_body=None):
            self.api_calls.append((method, path))
            return {"job": {"id": "job_fake01", "state": "queued"}}

        hh._api = fake_api
        self._real_probe = hg._health_probe
        hg.bind(health_probe=None)

    def tearDown(self):
        hh._api = self._real_api
        hg.bind(health_probe=self._real_probe)
        mn.bind(ops_send=None, boss_send=None)

    def submit(self):
        return run(hh.submit_diagnosis("black-boxx", "sym", {"b": 1}, "mi_x"))

    def state(self):
        return hg._get("state", hg.CLOSED)

    def ops_count(self, needle):
        return len([o for o in self.ops if needle in o])


# ── 1. Quota exhaustion opens the circuit ──────────────────────────────────
class OpenConditionsTests(Base):
    def test_quota_signal_opens_immediately(self):
        run(hg.record_failure(QUOTA_MSG))
        self.assertEqual(self.state(), hg.OPEN)
        self.assertEqual(hg._get("reason_class"), "billing")
        self.assertGreater(hg._getf("cooldown_until"), time.time() + 3600)

    def test_quota_signal_arrives_via_job_polling(self):
        run(hh.note_job_state({"id": "job_q1", "state": "paused_quota",
                               "cost_usd": 0.0}))
        self.assertEqual(self.state(), hg.OPEN)
        self.assertEqual(hg._get("reason_class"), "billing")

    def test_insufficient_credits_and_402_also_open(self):
        for msg in ("insufficient credits", "HTTP 402", "billing rejection"):
            hg._db().execute("DELETE FROM hermes_guard_state")
            hg._db().commit()
            run(hg.record_failure(msg))
            self.assertEqual(self.state(), hg.OPEN, msg)

    def test_unreachable_needs_consecutive_failures(self):
        run(hg.record_failure(UNREACHABLE_MSG))
        self.assertEqual(self.state(), hg.CLOSED)
        run(hg.record_failure(UNREACHABLE_MSG))
        self.assertEqual(self.state(), hg.CLOSED)
        run(hg.record_failure(UNREACHABLE_MSG))
        self.assertEqual(self.state(), hg.OPEN)
        self.assertEqual(hg._get("reason_class"), "unavailable")

    def test_a_success_resets_the_failure_streak(self):
        run(hg.record_failure(UNREACHABLE_MSG))
        run(hg.record_failure(UNREACHABLE_MSG))
        run(hg.record_success())
        run(hg.record_failure(UNREACHABLE_MSG))
        self.assertEqual(self.state(), hg.CLOSED)


# ── 2. Zero provider requests while open ───────────────────────────────────
class ZeroRequestsWhileOpenTests(Base):
    def test_100_submits_produce_zero_provider_requests(self):
        run(hg.record_failure(QUOTA_MSG))
        for _ in range(100):
            with self.assertRaises(hh.HermesProviderBlocked):
                self.submit()
        self.assertEqual(self.api_calls, [], "a blocked call reached the bridge")
        self.assertEqual(int(hg._getf("blocked_total")), 100)

    def test_100_monitor_escalations_create_no_tasks_and_no_requests(self):
        run(hg.record_failure(QUOTA_MSG))
        submitted = []
        real_submit = ts.submit
        ts.submit = lambda tt, ctx, inp: submitted.append(inp) or "lt_x"
        now = time.time()
        mon._insert_incident({
            "incident_id": "mi_guard01", "key": "black-boxx",
            "display_name": "BLACK-BOXX", "status": mon.OPEN,
            "opened_at": now, "updated_at": now,
            "detection_json": "{}", "evidence_json": "[]"})
        try:
            for _ in range(100):
                out = run(mon._escalate("mi_guard01", "black-boxx", "BLACK-BOXX",
                                        "sym", []))
                self.assertIsNone(out)
        finally:
            ts.submit = real_submit
        self.assertEqual(submitted, [], "escalation task created while blocked")
        self.assertEqual(self.api_calls, [])
        inc = mon.get_incident("mi_guard01")
        self.assertIn("provider unavailable", inc["hermes_block_reason"])

    def test_polling_an_existing_job_is_never_blocked(self):
        """get_job is non-billable bookkeeping — an in-flight job must remain
        observable while the circuit is open."""
        run(hg.record_failure(QUOTA_MSG))
        run(hh.get_job("job_live1"))
        self.assertEqual(self.api_calls, [("GET", "/jobs/job_live1")])


# ── 3. Restart persistence ─────────────────────────────────────────────────
class PersistenceTests(Base):
    def test_open_circuit_survives_restart(self):
        run(hg.record_failure(QUOTA_MSG))
        opened_at = hg._getf("opened_at")
        hg._conn = None            # a new process = a fresh connection
        self.assertEqual(self.state(), hg.OPEN)
        self.assertEqual(hg._getf("opened_at"), opened_at)
        with self.assertRaises(hh.HermesProviderBlocked):
            self.submit()
        self.assertEqual(self.api_calls, [])

    def test_budget_counters_survive_restart(self):
        for _ in range(3):
            self.submit()
        hg._conn = None
        self.assertEqual(hg._requests_since(3600), 3)


# ── 4/5/6. Controlled recovery ─────────────────────────────────────────────
class RecoveryTests(Base):
    def _open_billing_with_expired_cooldown(self):
        run(hg.record_failure(QUOTA_MSG))
        hg._set(cooldown_until=time.time() - 1)

    def test_half_open_permits_exactly_one_probe(self):
        self._open_billing_with_expired_cooldown()
        self.submit()                                   # the one probe
        self.assertEqual(len(self.api_calls), 1)
        with self.assertRaises(hh.HermesProviderBlocked):
            self.submit()                               # second is blocked
        self.assertEqual(len(self.api_calls), 1)
        self.assertEqual(self.state(), hg.HALF_OPEN)

    def test_billing_recovery_closes_only_on_job_completion(self):
        self._open_billing_with_expired_cooldown()
        self.submit()
        # Submit acceptance alone must NOT close a billing-class circuit.
        self.assertEqual(self.state(), hg.HALF_OPEN)
        run(hh.note_job_state({"id": "job_ok", "state": "completed",
                               "cost_usd": 0.0421}))
        self.assertEqual(self.state(), hg.CLOSED)
        self.assertEqual(self.ops_count("circuit closed"), 1)

    def test_failed_recovery_reopens_with_doubled_bounded_cooldown(self):
        self._open_billing_with_expired_cooldown()
        self.submit()
        run(hh.note_job_state({"id": "job_bad", "state": "paused_quota"}))
        self.assertEqual(self.state(), hg.OPEN)
        self.assertLessEqual(hg._getf("cooldown_secs"), hg.COOLDOWN_MAX_SECS)
        self.assertGreaterEqual(hg._getf("cooldown_secs"),
                                hg.BILLING_COOLDOWN_SECS)
        # Reopen during one continuous outage: no second "OPEN" ops message.
        self.assertEqual(self.ops_count("circuit OPEN"), 1)

    def test_unavailable_class_recovers_via_nonbillable_health_probe(self):
        probes = []

        async def health_ok():
            probes.append(1)
            return {"ok": True, "version": "test"}

        hg.bind(health_probe=health_ok)
        for _ in range(hg.FAILURE_THRESHOLD):
            run(hg.record_failure(UNREACHABLE_MSG))
        self.assertEqual(self.state(), hg.OPEN)
        hg._set(cooldown_until=time.time() - 1)
        self.submit()
        self.assertEqual(self.state(), hg.CLOSED)
        self.assertEqual(probes, [1], "health probe used exactly once")
        # The request itself then proceeded normally.
        self.assertEqual(len(self.api_calls), 1)

    def test_failed_health_probe_reopens_without_any_submit(self):
        async def health_bad():
            raise RuntimeError("still down")

        hg.bind(health_probe=health_bad)
        for _ in range(hg.FAILURE_THRESHOLD):
            run(hg.record_failure(UNREACHABLE_MSG))
        hg._set(cooldown_until=time.time() - 1)
        with self.assertRaises(hh.HermesProviderBlocked):
            self.submit()
        self.assertEqual(self.state(), hg.OPEN)
        self.assertEqual(self.api_calls, [], "no paid request during failed probe")


# ── 7/8/9. Budgets ─────────────────────────────────────────────────────────
class BudgetTests(Base):
    def test_hourly_ceiling_blocks_and_notifies_once(self):
        for _ in range(hg.MAX_PER_HOUR):
            self.submit()
        for _ in range(20):
            with self.assertRaises(hh.HermesProviderBlocked):
                self.submit()
        self.assertEqual(len(self.api_calls), hg.MAX_PER_HOUR)
        self.assertEqual(self.state(), hg.OPEN)
        self.assertEqual(self.ops_count("ceiling reached"), 1)
        self.assertEqual(self.telegram, [])

    def test_daily_ceiling_blocks(self):
        now = time.time()
        conn = hg._db()
        for i in range(hg.MAX_PER_DAY):
            # Spread outside the hourly window so only the daily cap trips.
            conn.execute("INSERT INTO hermes_guard_requests (at, detail) "
                         "VALUES (?,?)", (now - 4000 - i * 60, "submit"))
        conn.commit()
        with self.assertRaises(hh.HermesProviderBlocked):
            self.submit()
        self.assertEqual(self.api_calls, [])
        self.assertIn("24h", hg._get("reason"))

    def test_spend_ceiling_on_observed_job_cost(self):
        hg.record_cost("job_a", hg.MAX_SPEND_USD_PER_DAY * 0.6)
        hg.record_cost("job_b", hg.MAX_SPEND_USD_PER_DAY * 0.6)
        with self.assertRaises(hh.HermesProviderBlocked):
            self.submit()
        self.assertEqual(self.api_calls, [])
        self.assertIn("spend", hg._get("reason"))
        self.assertEqual(self.ops_count("ceiling reached"), 1)

    def test_cost_updates_do_not_double_count_a_job(self):
        hg.record_cost("job_a", 0.10)
        hg.record_cost("job_a", 0.25)     # same job, cost grew while running
        self.assertAlmostEqual(hg.spend_last_24h_usd(), 0.25)

    def test_budget_block_is_not_a_provider_incident(self):
        for _ in range(hg.MAX_PER_HOUR):
            self.submit()
        with self.assertRaises(hh.HermesProviderBlocked):
            self.submit()
        self.assertIsNone(mon.active_incident_for(hg.PROVIDER_KEY),
                          "a self-imposed ceiling is not a provider outage")


# ── 10. Deterministic local maintenance continues ──────────────────────────
class LocalMaintenanceContinuesTests(Base):
    def setUp(self):
        super().setUp()

        async def no_joplin(_i):
            return None
        self._real_joplin = mon._write_joplin_summary
        mon._write_joplin_summary = no_joplin

    def tearDown(self):
        mon._write_joplin_summary = self._real_joplin
        super().tearDown()

    def test_runbook_repair_resolves_while_provider_is_down(self):
        run(hg.record_failure(QUOTA_MSG))
        fault = {"healthy": False, "diagnosis": "rules drifted",
                 "checks": [{"name": "policy_rules", "ok": False, "detail": "x"}],
                 "repair": {"action": "restore", "description": "restore rules"},
                 "repair_result": None, "escalate": False}
        healed = {"healthy": True, "diagnosis": "ok", "checks": [],
                  "repair": None, "repair_result": {"ok": True}, "escalate": False}
        calls = {"n": 0}

        async def check(allow_repairs):
            calls["n"] += 1
            return dict(healed if allow_repairs else fault)

        for _ in range(3):
            run(mon._process_check("cloudflare-ddns", "Cloudflare DDNS",
                                   check, "ddns"))
        rows = [dict(r) for r in mon._db().execute(
            "SELECT * FROM monitor_incidents WHERE key='cloudflare-ddns'")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], mon.RESOLVED)
        self.assertEqual(self.api_calls, [], "local repair must not touch Hermes")

    def test_escalation_needing_incident_stays_active_and_records_provider(self):
        run(hg.record_failure(QUOTA_MSG))
        fault = {"healthy": False, "diagnosis": "unknown failure",
                 "checks": [{"name": "x", "ok": False, "detail": "y"}],
                 "repair": None, "repair_result": None, "escalate": True}

        async def check(allow_repairs):
            return dict(fault)

        for _ in range(6):
            mon._update_check("jellyfin", cooldown_until=0)
            run(mon._process_check("jellyfin", "Jellyfin", check, "Jellyfin"))
        rows = [dict(r) for r in mon._db().execute(
            "SELECT * FROM monitor_incidents WHERE key='jellyfin'")]
        self.assertEqual(len(rows), 1, "dedup must hold while provider is down")
        self.assertIn("provider unavailable", rows[0]["hermes_block_reason"])
        self.assertIsNone(rows[0]["closed_at"])
        self.assertEqual(self.api_calls, [])


# ── 11. One canonical provider incident ────────────────────────────────────
class ProviderIncidentTests(Base):
    def _provider_incidents(self):
        return [dict(r) for r in mon._db().execute(
            "SELECT * FROM monitor_incidents WHERE key=?", (hg.PROVIDER_KEY,))]

    def test_exactly_one_provider_incident_across_repeated_failures(self):
        for _ in range(5):
            run(hg.record_failure(QUOTA_MSG))
        active = [r for r in self._provider_incidents() if r["closed_at"] is None]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["display_name"], hg.PROVIDER_DISPLAY)

    def test_dependent_services_do_not_fork_provider_incidents(self):
        run(hg.record_failure(QUOTA_MSG))
        now = time.time()
        for key in ("joplin", "black-boxx", "jellyfin"):
            mon._insert_incident({
                "incident_id": f"mi_{key}", "key": key, "display_name": key,
                "status": mon.OPEN, "opened_at": now, "updated_at": now,
                "detection_json": "{}", "evidence_json": "[]"})
            run(mon._escalate(f"mi_{key}", key, key, "sym", []))
        active = [r for r in self._provider_incidents() if r["closed_at"] is None]
        self.assertEqual(len(active), 1)
        for key in ("joplin", "black-boxx", "jellyfin"):
            self.assertIn("provider unavailable",
                          mon.get_incident(f"mi_{key}")["hermes_block_reason"])

    def test_provider_incident_can_never_escalate_to_hermes(self):
        self.assertNotIn(hg.PROVIDER_KEY, mon.MONITORS)

    def test_recovery_resolves_the_provider_incident(self):
        run(hg.record_failure(QUOTA_MSG))
        hg._set(cooldown_until=time.time() - 1)
        self.submit()
        run(hh.note_job_state({"id": "job_ok", "state": "completed"}))
        active = [r for r in self._provider_incidents() if r["closed_at"] is None]
        self.assertEqual(active, [])


# ── 12/13. Notification dedupe + conversation untouched ────────────────────
class NotificationPolicyTests(Base):
    def test_open_notifies_ops_once_despite_repeated_blocks(self):
        run(hg.record_failure(QUOTA_MSG))
        for _ in range(50):
            with self.assertRaises(hh.HermesProviderBlocked):
                self.submit()
        self.assertEqual(self.ops_count("circuit OPEN"), 1)
        self.assertEqual(self.telegram, [], "provider events never page Telegram")

    def test_recovery_notifies_ops_once(self):
        run(hg.record_failure(QUOTA_MSG))
        hg._set(cooldown_until=time.time() - 1)
        self.submit()
        run(hh.note_job_state({"id": "job_ok", "state": "completed"}))
        run(hh.note_job_state({"id": "job_ok", "state": "completed"}))
        self.assertEqual(self.ops_count("circuit closed"), 1)
        self.assertEqual(self.telegram, [])

    def test_guard_state_does_not_touch_conversation_task_announcements(self):
        """A Boss-requested task still answers in its chat while the circuit
        is open — the guard gates provider submits, not conversations."""
        run(hg.record_failure(QUOTA_MSG))
        sent = []

        async def fake_send(channel_id, text, file_path=None, filename=None):
            sent.append((channel_id, text))

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        real_db, real_send = ts.DB_PATH, ts._send
        ts.DB_PATH, ts._conn, ts._send = tmp.name, None, fake_send
        try:
            ts._db()
            now = time.time()
            ts._insert({"task_id": "lt_chat01", "task_type": "browser_research",
                        "title": "look something up", "channel_id": "tg:424242",
                        "requester_name": "Rico", "requester_id": BOSS_ID,
                        "status": "completed", "created_at": now,
                        "updated_at": now, "result_summary": "here you go"})
            run(ts._maybe_announce("lt_chat01"))
        finally:
            ts.DB_PATH, ts._conn, ts._send = real_db, None, real_send
            os.unlink(tmp.name)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "tg:424242")

    def test_status_tool_exposes_state_but_no_secrets(self):
        run(hg.record_failure(QUOTA_MSG))
        out = hg.status()
        self.assertEqual(out["circuit"], hg.OPEN)
        self.assertIn("quota", out["reason"])
        self.assertIsNotNone(out["opened_at"])
        self.assertIsNotNone(out["next_recovery_attempt"])
        dump = json.dumps(out)
        self.assertNotIn("unit-test-token", dump)
        self.assertNotIn("TOKEN", dump)
        self.assertIn("hermes_provider_status", tools.REGISTRY)


# ── Auth-class failures, status vocabulary, provider hint ──────────────────
# Added for the Hermes provider-resilience task: a bad/revoked razr-side
# credential (bridge job state "paused_auth") was previously never fed to the
# guard at all — record_failure was only called for paused_quota/failed, so
# an auth outage left the circuit permanently CLOSED while every submit kept
# failing the same way. classify()/record_failure() now treat "auth" as its
# own class, opening immediately like billing (a bad credential doesn't
# self-resolve by waiting) but reported under its own status_label.
PAUSED_AUTH_MSG = "provider authentication failed (job 12345678 paused_auth)"


class AuthClassTests(Base):
    def test_classify_distinguishes_auth_from_billing_and_rate_limit(self):
        self.assertEqual(hg.classify("provider authentication failed — re-authenticate on razr"), "auth")
        self.assertEqual(hg.classify("401 unauthorized"), "auth")
        self.assertEqual(hg.classify(QUOTA_MSG), "billing")
        self.assertEqual(hg.classify("429 too many requests"), "rate_limit")

    def test_paused_auth_job_state_opens_circuit_immediately(self):
        """The gap this closes: note_job_state used to silently drop
        paused_auth — the circuit never opened and Loki kept submitting jobs
        against a credential that could never succeed."""
        run(hh.note_job_state({"id": "job_badkey", "state": "paused_auth"}))
        self.assertEqual(hg.status()["circuit"], hg.OPEN)
        self.assertEqual(hg.status()["reason_class"], "auth")
        with self.assertRaises(hh.HermesProviderBlocked):
            self.submit()

    def test_auth_failure_does_not_need_three_consecutive(self):
        run(hg.record_failure("provider authentication failed — re-authenticate on razr"))
        self.assertEqual(hg.status()["circuit"], hg.OPEN)

    def test_auth_recovery_is_one_controlled_submit_not_health_probe(self):
        probed = {"n": 0}

        async def fake_health():
            probed["n"] += 1
            return {"ok": True}

        hg.bind(health_probe=fake_health)
        try:
            run(hg.record_failure("401 unauthorized"))
            hg._set(cooldown_until=time.time() - 1)
            ok, why = run(hg.allow_request())
            self.assertTrue(ok, why)
            self.assertEqual(probed["n"], 0,
                             "auth-class recovery must not use the non-billable "
                             "health probe — it never calls the model provider, "
                             "so it can't prove a rotated credential works")
        finally:
            hg.bind(health_probe=None)


class StatusLabelTests(Base):
    def test_closed_is_operational(self):
        self.assertEqual(hg.status()["status_label"], "operational")

    def test_billing_open_is_protective_quota(self):
        run(hg.record_failure(QUOTA_MSG))
        self.assertEqual(hg.status()["status_label"], "protective_quota")

    def test_auth_open_is_authentication_failed(self):
        run(hg.record_failure(PAUSED_AUTH_MSG))
        self.assertEqual(hg.status()["status_label"], "authentication_failed")

    def test_rate_limit_needs_three_then_labels_unreachable_style(self):
        for _ in range(3):
            run(hg.record_failure("429 too many requests"))
        self.assertEqual(hg.status()["reason_class"], "rate_limit")
        self.assertEqual(hg.status()["status_label"], "rate_limited")

    def test_generic_unavailable_open_is_unreachable(self):
        for _ in range(3):
            run(hg.record_failure(UNREACHABLE_MSG))
        self.assertEqual(hg.status()["status_label"], "unreachable")

    def test_budget_open_is_protective_budget(self):
        for _ in range(hg.MAX_PER_HOUR):
            self.submit()
        with self.assertRaises(hh.HermesProviderBlocked):
            self.submit()
        self.assertEqual(hg.status()["status_label"], "protective_budget")

    def test_half_open_is_recovering(self):
        run(hg.record_failure(QUOTA_MSG))
        hg._set(state=hg.HALF_OPEN, probe_inflight_until=time.time() + 300)
        self.assertEqual(hg.status()["status_label"], "recovering")


class ProviderHintTests(Base):
    def test_priced_model_reports_reliable_cost_telemetry(self):
        run(hh.note_job_state({
            "id": "job1", "state": "completed",
            "phases": [{"model": "anthropic/claude-sonnet-5"}],
        }))
        out = hg.status()
        self.assertEqual(out["last_serving_model"], "anthropic/claude-sonnet-5")
        self.assertEqual(out["last_serving_model_cost_telemetry"], "reliable")

    def test_unpriced_model_eg_fable_flags_unreliable_cost_telemetry(self):
        """The bridge's own rate card (lib/budget.mjs) and spend probe
        (lib/usage.mjs, OpenRouter-only) don't know claude-fable-5 — a job
        served by it prices at $0 in the bridge's ledger. The guard must not
        silently agree that it cost nothing."""
        run(hh.note_job_state({
            "id": "job2", "state": "completed",
            "phases": [{"model": "claude-fable-5"}],
        }))
        out = hg.status()
        self.assertEqual(out["last_serving_model"], "claude-fable-5")
        self.assertIn("unreliable", out["last_serving_model_cost_telemetry"])

    def test_no_phases_leaves_last_serving_model_none(self):
        run(hh.note_job_state({"id": "job3", "state": "completed", "phases": []}))
        self.assertIsNone(hg.status()["last_serving_model"])

    def test_provider_hint_survives_restart(self):
        run(hh.note_job_state({
            "id": "job4", "state": "completed",
            "phases": [{"model": "anthropic/claude-opus-5"}],
        }))
        hg._conn.close()
        hg._conn = None
        self.assertEqual(hg.status()["last_serving_model"], "anthropic/claude-opus-5")


if __name__ == "__main__":
    unittest.main()

"""Reliability must measure the homelab, not the bookkeeping about it.

Three separate things were subtracting points for reasons that had nothing
to do with anything being broken:

  * Four unclosed skillkit solve-path records. `resolved` is written once,
    at record time, from the status of the run that wrote it — so a run
    that timed out left an open row behind forever, even after a later run
    (or a human) repaired the very same thing.
  * Two stopped containers, scored purely for being stopped. One was the
    obsolete Joplin CLI sidecar at `restart: no`, which nobody wants
    running; the other was a genuine boot failure. They cost the same.
  * One open `hermes-provider` fault, weighted as a live outage. It is the
    cost circuit breaker doing exactly what it was built to do after
    OpenRouter reported credit exhaustion — a real loss of escalation
    capability, but not a service failure and not repairable from here.

DESIRED STATE IS READ, NEVER GUESSED. A container's Docker restart policy
and the asset lifecycle registry's `expected_running` say whether a
workload is supposed to be up. Absent that evidence the container is
treated as expected-to-run, so a collector failure can never quietly erase
a penalty.

PROTECTIVE ≠ BROKEN, BUT ALSO ≠ FREE. A quota circuit is charged less than
an outage and described differently. A provider that is UNREACHABLE is not
protective and keeps full outage weight.

Nothing here touches the live databases, Joplin, Discord or Telegram, and
no provider request is ever made.

Run: venv/bin/python -m unittest tests.test_reliability_state -v
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

# Loki-side modules bind their DB path at import; point them at throwaway
# files BEFORE importing anything, or the suite writes to production.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["HOMELAB_DB_PATH"] = _tmp_db.name
_tmp_dir = tempfile.mkdtemp(prefix="reliability-state-test-")
os.environ["HOMELAB_LIFECYCLE_MIRROR"] = os.path.join(_tmp_dir, "lifecycle.yml")
os.environ["HOMELAB_DECOMMISSION_ARCHIVE_DIR"] = os.path.join(_tmp_dir, "arch")
os.environ.setdefault("MAINTENANCE_OPS_CHANNEL_ID", "1534655910893457669")
os.environ.setdefault("HERMES_WORKER_URL", "http://bridge.invalid:1")
os.environ.setdefault("HERMES_WORKER_TOKEN", "unit-test-token")

import hermes_guard as hg              # noqa: E402
import homelab_monitor as mon          # noqa: E402
import maintenance_notify as mn        # noqa: E402

sys.path.insert(0, "/home/g2k247/skillkit")

import skillkit.advisor as advisor          # noqa: E402
import skillkit.incidents as sk_incidents   # noqa: E402
import skillkit.reporting as reporting      # noqa: E402

DAY = 86400.0
NOW = time.time()

QUOTA_MSG = "provider quota exhausted (job 12345678 paused_quota)"
UNREACHABLE_MSG = "unreachable: ClientConnectorError"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def base_facts(**over):
    facts = {"docker": {}, "hosts": {},
             "incidents": {"monitor_active": 0, "solve_path_unresolved": 0}}
    facts.update(over)
    return facts


def host(*stopped_detail, running=10, restarting=()):
    """One host's docker facts, in the shape the collector emits."""
    return {"running": running,
            "stopped": [f"{e['name']} ({e.get('status', 'Exited')})"
                        for e in stopped_detail],
            "stopped_detail": list(stopped_detail),
            "restarting": list(restarting)}


def container(name, *, state, expected, policy="", code=0, error=""):
    return {"name": name, "status": f"Exited ({code})", "state": state,
            "expected_running": expected, "restart_policy": policy,
            "exit_code": code, "error": error}


# ── 1/2. Solve-path record lifecycle ──────────────────────────────────────
class SolvePathLifecycleTests(unittest.TestCase):
    """A finished repair must close the record it finished; an unfinished
    one must stay open and keep costing points."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self._real_db = sk_incidents.INCIDENT_DB
        sk_incidents.INCIDENT_DB = self.db
        # Joplin is a real production side effect; the DB is the subject.
        self._real_append = sk_incidents.append_resolution_to_history_note
        self._real_new = sk_incidents.append_to_history_note
        sk_incidents.append_resolution_to_history_note = lambda inc: None
        sk_incidents.append_to_history_note = lambda inc: None

    def tearDown(self):
        sk_incidents.INCIDENT_DB = self._real_db
        sk_incidents.append_resolution_to_history_note = self._real_append
        sk_incidents.append_to_history_note = self._real_new
        os.unlink(self.db)

    def record(self, *, resolved, signature, service="qbittorrent",
               symptoms="WebUI not loading"):
        return sk_incidents.record(
            service=service, symptoms=symptoms, diagnosis="d",
            repair=[f"{signature}"] if signature else [],
            verification=["http_check"], duration_ms=1000,
            resolved=resolved, signature=signature)

    def test_completed_repair_closes_its_open_bookkeeping_record(self):
        """The exact shape of live record #15: a run gives up and records
        an open row, a later run repairs the same thing and verifies it."""
        gave_up = self.record(resolved=False,
                              signature="restart_container:qbittorrent")
        fixed = self.record(resolved=True,
                            signature="restart_container:qbittorrent")

        closed = sk_incidents.resolve_superseded(
            "restart_container:qbittorrent", fixed["number"])

        self.assertEqual([c["number"] for c in closed], [gave_up["number"]])
        reread = sk_incidents.query(resolved=False)
        self.assertEqual(reread, [], "no open records should remain")
        note = sk_incidents.query(limit=50)[0]["resolution_note"]
        self.assertIn(f"#{fixed['number']}", note,
                      "the closure must name what superseded it")

    def test_unfinished_repair_stays_open(self):
        """A record for work that is genuinely not done keeps its penalty."""
        still_broken = self.record(resolved=False,
                                   signature="restart_container:jellyfin")
        other = self.record(resolved=True,
                            signature="restart_container:qbittorrent")

        sk_incidents.resolve_superseded("restart_container:qbittorrent",
                                        other["number"])

        open_now = sk_incidents.query(resolved=False)
        self.assertEqual([r["number"] for r in open_now],
                         [still_broken["number"]])
        facts = base_facts(incidents={"monitor_active": 0,
                                      "solve_path_unresolved": len(open_now)})
        self.assertEqual(reporting.reliability_terms(facts)["score"], 97)

    def test_a_run_that_mutated_nothing_supersedes_nothing(self):
        """Records #12-14 carried an empty signature because no repair ran.
        An empty signature must never match — it would close the world."""
        a = self.record(resolved=False, signature="", service="general")
        b = self.record(resolved=False, signature="", service="general")
        solved = self.record(resolved=True, signature="", service="general")

        self.assertEqual(sk_incidents.resolve_superseded("", solved["number"]),
                         [])
        self.assertEqual(sorted(r["number"] for r
                                in sk_incidents.query(resolved=False)),
                         sorted([a["number"], b["number"]]))

    def test_superseding_never_reaches_forward(self):
        """A record written after the solving one is newer work, not stale."""
        solved = self.record(resolved=True, signature="restart:x")
        later = self.record(resolved=False, signature="restart:x")

        self.assertEqual(
            sk_incidents.resolve_superseded("restart:x", solved["number"]), [])
        self.assertEqual([r["number"] for r
                          in sk_incidents.query(resolved=False)],
                         [later["number"]])

    def test_resolution_requires_a_written_reason(self):
        rec = self.record(resolved=False, signature="restart:x")
        with self.assertRaises(ValueError):
            sk_incidents.resolve(rec["number"], "   ")


# ── 3/4. Stopped containers vs desired state ──────────────────────────────
class StoppedContainerScoringTests(unittest.TestCase):

    def test_intentionally_stopped_container_costs_nothing(self):
        """loki-joplin-api: restart:no, obsolete sidecar, killed at
        shutdown. Nothing wants it running, so it is not an impairment."""
        facts = base_facts(docker={"dex247": host(
            container("loki-joplin-api", state="intentionally_stopped",
                      expected=False, policy="no", code=137))})
        b = reporting.reliability_terms(facts)

        self.assertEqual(b["score"], 100)
        term = [t for t in b["terms"] if t["name"] == "stopped containers"][0]
        self.assertEqual(term["count"], 0)
        self.assertIn("loki-joplin-api",
                      reporting.reliability_explainer(facts))

    def test_unexpectedly_stopped_container_costs_eight(self):
        """filebrowser: restart:unless-stopped, down across two reboots."""
        facts = base_facts(docker={"dex247": host(
            container("filebrowser", state="failed", expected=True,
                      policy="unless-stopped", code=128,
                      error="error while creating mount source path"))})
        b = reporting.reliability_terms(facts)

        self.assertEqual(b["score"], 92)
        self.assertIn("filebrowser", str(b["stopped_expected"]))
        self.assertIn("8×1 stopped containers",
                      reporting.reliability_explainer(facts))

    def test_the_two_live_stopped_workloads_score_as_one(self):
        facts = base_facts(docker={"dex247": host(
            container("loki-joplin-api", state="intentionally_stopped",
                      expected=False, policy="no", code=137),
            container("filebrowser", state="failed", expected=True,
                      policy="unless-stopped", code=128))})
        self.assertEqual(reporting.reliability_terms(facts)["score"], 92)

    def test_completed_one_shot_is_not_an_outage(self):
        facts = base_facts(docker={"dex247": host(
            container("db-migrate", state="completed", expected=False,
                      policy="no", code=0))})
        self.assertEqual(reporting.reliability_terms(facts)["score"], 100)

    def test_missing_desired_state_still_penalises(self):
        """An old fact snapshot, or a failed inspect, must not forgive."""
        facts = base_facts(docker={"dex247": {
            "running": 5, "stopped": ["mystery (Exited (1)"], "restarting": []}})
        self.assertEqual(reporting.reliability_terms(facts)["score"], 92)

    def test_healthy_line_and_score_cannot_disagree(self):
        """A host whose only stopped container is deliberate reads Healthy;
        one with a real failure does not."""
        deliberate = {"dex247": host(
            container("loki-joplin-api", state="intentionally_stopped",
                      expected=False, policy="no", code=137))}
        broken = {"dex247": host(
            container("filebrowser", state="failed", expected=True,
                      policy="unless-stopped", code=128))}

        self.assertIn("Docker (dex247)",
                      reporting.healthy_lines(base_facts(docker=deliberate)))
        self.assertNotIn("Docker (dex247)",
                         reporting.healthy_lines(base_facts(docker=broken)))

    def test_classifier_reads_policy_and_registry(self):
        retired = {"ivn-site"}
        cases = [
            ("ivn-site", "no", 0, ("decommissioned", False)),
            ("filebrowser", "unless-stopped", 128, ("failed", True)),
            ("worker", "always", 1, ("failed", True)),
            ("loki-joplin-api", "no", 137, ("intentionally_stopped", False)),
            ("db-migrate", "no", 0, ("completed", False)),
            ("weird", "wat", 3, ("unknown", True)),
        ]
        for name, policy, code, expected in cases:
            with self.subTest(name):
                self.assertEqual(
                    advisor.classify_stopped(name, "Exited", policy, code,
                                             retired),
                    expected)

    def test_a_retired_asset_is_excused_by_the_registry_not_by_name(self):
        """The old code hard-coded 'ivn-site'. Desired state must come from
        the lifecycle table, so any retired asset is handled."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE asset_lifecycle (name TEXT, "
                     "expected_running INTEGER, cleanup_scope_json TEXT)")
        conn.executemany(
            "INSERT INTO asset_lifecycle VALUES (?,?,?)",
            [("ivn-site", 0, json.dumps({"container": "ivn-site"})),
             ("old-blog", 0, json.dumps({"container": "blog-nginx"})),
             ("jellyfin", 1, json.dumps({"container": "jellyfin"}))])
        conn.commit()
        conn.close()
        try:
            with patch.dict(os.environ, {"HOMELAB_INCIDENTS_DB": path}):
                retired = advisor._retired_container_names()
        finally:
            os.unlink(path)

        self.assertIn("blog-nginx", retired)
        self.assertIn("ivn-site", retired)
        self.assertNotIn("jellyfin", retired)


# ── 5/6. Protective degradation vs provider outage ────────────────────────
class ProviderFaultClassificationTests(unittest.TestCase):

    def make_db(self, rows):
        """rows: (key, status, opened, closed, diagnosis)"""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE monitor_incidents (
                incident_id TEXT PRIMARY KEY, key TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                opened_at REAL NOT NULL, updated_at REAL NOT NULL,
                closed_at REAL, detection_json TEXT NOT NULL DEFAULT '{}',
                occurrence_count INTEGER NOT NULL DEFAULT 1)""")
        for i, (key, status, opened, closed, diag) in enumerate(rows):
            conn.execute(
                "INSERT INTO monitor_incidents (incident_id, key, status, "
                "opened_at, updated_at, closed_at, detection_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"mi_{i:04d}", key, status, opened, opened, closed,
                 json.dumps({"diagnosis": diag, "checks": []})))
        conn.commit()
        conn.close()
        return path

    def canonical(self, rows):
        path = self.make_db(rows)
        try:
            with patch.dict(os.environ, {"HOMELAB_INCIDENTS_DB": path}):
                return advisor._canonical_monitor_incidents(NOW - DAY)
        finally:
            os.unlink(path)

    def test_quota_circuit_is_protective_not_an_outage(self):
        """The live fault, verbatim: the guard opened on OpenRouter 402."""
        c = self.canonical([("hermes-provider", "open", NOW - 2 * DAY, None,
                             "provider quota exhausted "
                             "(job 53cebbb2 paused_quota)")])

        self.assertEqual(c["active"], 1, "still a real open fault")
        self.assertEqual(c["protective"], 1)
        self.assertEqual(c["protective_faults"], ["hermes-provider"])

    def test_unreachable_provider_is_a_genuine_outage(self):
        c = self.canonical([("hermes-provider", "open", NOW - 3600, None,
                             "unreachable: ClientConnectorError")])

        self.assertEqual(c["active"], 1)
        self.assertEqual(c["protective"], 0,
                         "a bridge that cannot be reached is broken, "
                         "not protecting anything")

    def test_a_broken_service_is_never_protective(self):
        c = self.canonical([("jellyfin", "open", NOW - 3600, None,
                             "quota exceeded on the media library")])
        self.assertEqual(c["protective"], 0,
                         "only the provider circuit can be protective")

    def test_a_closed_provider_fault_is_neither(self):
        c = self.canonical([("hermes-provider", "resolved", NOW - 2 * DAY,
                             NOW - 3600, "provider quota exhausted")])
        self.assertEqual(c["active"], 0)
        self.assertEqual(c["protective"], 0)

    def test_protective_and_outage_score_differently(self):
        protective = reporting.reliability_terms(base_facts(incidents={
            "monitor_active": 1, "monitor_protective": 1,
            "solve_path_unresolved": 0}))
        outage = reporting.reliability_terms(base_facts(incidents={
            "monitor_active": 1, "monitor_protective": 0,
            "solve_path_unresolved": 0}))

        self.assertEqual(protective["score"], 95)
        self.assertEqual(outage["score"], 88)
        self.assertGreater(protective["score"], outage["score"])

    def test_the_explainer_never_calls_a_quota_circuit_an_outage(self):
        text = reporting.reliability_explainer(base_facts(incidents={
            "monitor_active": 1, "monitor_protective": 1,
            "solve_path_unresolved": 0}))

        self.assertIn("protective degradation", text)
        self.assertNotIn("service fault", text)

    def test_protective_never_exceeds_the_open_fault_count(self):
        """Stale or mismatched inputs must not manufacture a credit."""
        b = reporting.reliability_terms(base_facts(incidents={
            "monitor_active": 0, "monitor_protective": 3,
            "solve_path_unresolved": 0}))
        self.assertEqual(b["score"], 100)
        self.assertTrue(all(t["penalty"] >= 0 for t in b["terms"]))

    def test_is_protective_fault_direct(self):
        for diag, expected in [
                ("provider quota exhausted (job x paused_quota)", True),
                ("insufficient credits", True),
                ("HTTP 402 from provider", True),
                ("unreachable: ClientConnectorError", False),
                ("timed out", False),
                ("", False)]:
            with self.subTest(diag):
                self.assertEqual(
                    advisor.is_protective_fault("hermes-provider", diag),
                    expected)


# ── 7. Recovery closes the provider incident ──────────────────────────────
class ProviderRecoveryTests(unittest.TestCase):
    """The circuit owns the provider incident's whole lifecycle: opening it
    on failure and closing it on a verified recovery. Nothing here spends
    provider credit — the bridge client is never called."""

    def setUp(self):
        conn = hg._db()
        for t in ("hermes_guard_state", "hermes_guard_requests",
                  "hermes_guard_costs"):
            conn.execute(f"DELETE FROM {t}")
        conn.commit()
        mon._db().execute("DELETE FROM monitor_incidents")
        mon._db().commit()
        self.ops = []

        async def ops_send(text):
            self.ops.append(text)

        mn.OPS_CHANNEL_ID = os.environ["MAINTENANCE_OPS_CHANNEL_ID"]
        mn.bind(ops_send=ops_send, boss_send=None)
        self._real_probe = hg._health_probe
        hg.bind(health_probe=None)

    def tearDown(self):
        mn.bind(ops_send=None, boss_send=None)
        hg.bind(health_probe=self._real_probe)

    def open_rows(self):
        return [dict(r) for r in mon._db().execute(
            "SELECT * FROM monitor_incidents WHERE key=? AND closed_at IS NULL",
            (hg.PROVIDER_KEY,))]

    def test_recovery_closes_the_stale_provider_incident(self):
        run(hg.record_failure(QUOTA_MSG))
        self.assertEqual(len(self.open_rows()), 1)

        run(hg.record_success(final=True))

        self.assertEqual(self.open_rows(), [],
                         "a verified recovery must close the fault it opened")
        self.assertEqual(hg._get("state"), hg.CLOSED)

    def test_a_stale_incident_is_not_closed_without_recovery(self):
        """Time passing is not proof. The cooldown elapsing leaves the
        circuit eligible to probe — the fault stays open until something
        actually succeeds."""
        run(hg.record_failure(QUOTA_MSG))
        hg._set(cooldown_until=time.time() - 1)

        self.assertEqual(hg.blocked_reason(), "",
                         "cooldown elapsed, a probe is allowed")
        self.assertEqual(len(self.open_rows()), 1,
                         "but nothing has proven the provider is back")

    def test_a_failed_probe_keeps_the_fault_open(self):
        run(hg.record_failure(QUOTA_MSG))
        hg._set(state=hg.HALF_OPEN)
        run(hg.record_failure(QUOTA_MSG))

        self.assertEqual(hg._get("state"), hg.OPEN)
        self.assertEqual(len(self.open_rows()), 1)

    def test_reachability_outage_and_quota_are_both_faults(self):
        """Distinct conditions, same bookkeeping: one canonical incident."""
        run(hg.record_failure(UNREACHABLE_MSG))
        run(hg.record_failure(UNREACHABLE_MSG))
        run(hg.record_failure(UNREACHABLE_MSG))
        self.assertEqual(hg._get("reason_class"), "unavailable")
        self.assertEqual(len(self.open_rows()), 1)

        run(hg.record_failure(QUOTA_MSG))
        self.assertEqual(hg._get("reason_class"), "billing",
                         "quota news upgrades the reason")
        self.assertEqual(len(self.open_rows()), 1, "still one fault")


if __name__ == "__main__":
    unittest.main()

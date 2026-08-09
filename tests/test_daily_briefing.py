"""Regression tests for Daily Briefing semantics (skillkit advisor/reporting).

These cover the three defects that made the briefing contradict itself:

  * Reliability was amplified by repeat detections, so one persistent fault
    logged hundreds of times could crater the score.
  * The incident trend diffed raw DB row counts ("15 ▲ +9"), turning repeat
    detections of one known fault into "nine new incidents".
  * Disk health was judged against a hard-coded 80% while the monitor
    alerts at 90%, so 78% was printed as "no action required" in one
    section and called "P1 — Act now" by the LLM in the next.
  * Local image build age was presented as though it proved an upstream
    update existed.

DATA SOURCE OWNERSHIP (asserted below, not just documented):
  Loki's homelab_incidents.db `monitor_incidents` is AUTHORITATIVE for real
  outages. One persistent fault = one incident, identified by `key` — the
  same dedup key homelab_monitor itself uses. skillkit's own incidents.db
  is solve-path bookkeeping: it records that the agent worked on something,
  never that the homelab had an outage.

Run: venv/bin/python -m unittest tests.test_daily_briefing -v
"""

import asyncio
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, "/home/g2k247/skillkit")

import skillkit.advisor as advisor          # noqa: E402
import skillkit.reporting as reporting      # noqa: E402

DAY = 86400.0
# Anchored to the real clock: _c_incidents derives its lookback window from
# datetime.now(), so a hard-coded epoch would drift out of every window.
NOW = time.time()
CUTOFF_1D = NOW - DAY


def make_monitor_db(rows):
    """Build a throwaway monitor_incidents DB.

    rows: (key, status, opened_at, closed_at, occurrence_count)
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE monitor_incidents (
            incident_id     TEXT PRIMARY KEY,
            key             TEXT NOT NULL,
            display_name    TEXT NOT NULL DEFAULT '',
            status          TEXT NOT NULL DEFAULT 'open',
            opened_at       REAL NOT NULL,
            updated_at      REAL NOT NULL,
            closed_at       REAL,
            occurrence_count INTEGER NOT NULL DEFAULT 1
        )""")
    for i, (key, status, opened, closed, occ) in enumerate(rows):
        conn.execute(
            "INSERT INTO monitor_incidents (incident_id, key, status, "
            "opened_at, updated_at, closed_at, occurrence_count) "
            "VALUES (?,?,?,?,?,?,?)",
            (f"mi_{i:04d}", key, status, opened, opened, closed, occ))
    conn.commit()
    conn.close()
    return path


def canonical(rows, cutoff=CUTOFF_1D):
    """Run the canonical folder over a temp DB and clean up."""
    path = make_monitor_db(rows)
    try:
        with patch.dict(os.environ, {"HOMELAB_INCIDENTS_DB": path}):
            return advisor._canonical_monitor_incidents(cutoff)
    finally:
        os.unlink(path)


def collect(rows, solve_records=(), cutoff_days=1):
    """Full _c_incidents with skillkit's solve-path store stubbed out."""
    path = make_monitor_db(rows)
    try:
        with patch.dict(os.environ, {"HOMELAB_INCIDENTS_DB": path}), \
                patch.object(advisor.incidents, "query",
                             return_value=list(solve_records)):
            return asyncio.run(advisor._c_incidents(cutoff_days))
    finally:
        os.unlink(path)


def base_facts(**over):
    facts = {"docker": {}, "hosts": {}, "ssl": {}, "automations": {},
             "playbooks": {"degraded_repairs": []},
             "image_ages": {"old_by_local_age_count": 0},
             "trends": {"history_points": 5, "deltas": {}},
             "incidents": {"canonical_unresolved": 0}}
    facts.update(over)
    return facts


def scores_for(facts):
    return reporting.health_scores(facts, [], True, 0, True)


# ── incident semantics ────────────────────────────────────────────────────
class TestIncidentSemantics(unittest.TestCase):

    def test_01_one_persistent_fault_many_occurrences(self):
        """One fault detected 136 times is ONE incident, not 136."""
        rows = [("joplin", "escalated", NOW - 3600 * i, NOW - 3600 * i, 1)
                for i in range(136)]
        rows.append(("joplin", "open", NOW - 600, None, 1))
        c = canonical(rows)

        self.assertEqual(c["active"], 1, "one persistent fault = one active")
        self.assertEqual(c["distinct_faults_all_time"], 1)
        self.assertEqual(c["rows_all_time"], 137)
        # The repeat detections are visible, but as detections.
        self.assertGreater(c["occurrences_in_window"], 1)
        self.assertEqual(c["new_in_window"], 0,
                         "fault first seen before the window is not new")

    def test_02_multiple_genuinely_new_incidents(self):
        """Three distinct faults first seen in-window = 3 new."""
        c = canonical([
            ("joplin", "open", NOW - 3600, None, 1),
            ("jellyfin", "open", NOW - 1800, None, 1),
            ("black-boxx", "open", NOW - 900, None, 1),
        ])
        self.assertEqual(c["active"], 3)
        self.assertEqual(c["new_in_window"], 3)
        self.assertEqual(sorted(c["new_faults"]),
                         ["black-boxx", "jellyfin", "joplin"])

    def test_03_resolved_incidents(self):
        """Closed in-window with nothing still open = resolved, not active."""
        c = canonical([
            ("jellyfin", "open", NOW - 5 * DAY, None, 1),
            ("jellyfin", "resolved", NOW - 5 * DAY, NOW - 3600, 57),
        ])
        # An open row still exists, so it is NOT resolved yet.
        self.assertEqual(c["active"], 1)
        self.assertEqual(c["resolved_in_window"], 0)

        c2 = canonical([
            ("jellyfin", "resolved", NOW - 5 * DAY, NOW - 3600, 57),
        ])
        self.assertEqual(c2["active"], 0)
        self.assertEqual(c2["resolved_in_window"], 1)
        self.assertEqual(c2["resolved_faults"], ["jellyfin"])
        self.assertEqual(c2["new_in_window"], 0,
                         "first seen 5 days ago — resolved, but not new")

    def test_04_active_unresolved_incidents(self):
        """Liveness is closed_at IS NULL, not the status string."""
        c = canonical([
            ("hermes-provider", "open", NOW - 2 * DAY, None, 1),
            # 'escalated' rows are CLOSED handoffs — not open faults.
            ("joplin", "escalated", NOW - 3600, NOW - 3600, 1),
        ])
        self.assertEqual(c["active"], 1)
        self.assertEqual(c["active_faults"], ["hermes-provider"])

    def test_05_escalation_without_new_underlying_incident(self):
        """Escalations are actions on an existing fault, never new ones."""
        rows = [("joplin", "open", NOW - 10 * DAY, None, 1)]
        rows += [("joplin", "escalated", NOW - 600 * i, NOW - 600 * i, 1)
                 for i in range(1, 6)]
        c = canonical(rows)

        self.assertEqual(c["active"], 1)
        self.assertEqual(c["new_in_window"], 0,
                         "escalating a 10-day-old fault creates no new one")
        self.assertEqual(c["escalations_in_window"], 5)

    def test_06_zero_incidents(self):
        c = canonical([])
        for field in ("active", "new_in_window", "resolved_in_window",
                      "occurrences_in_window", "escalations_in_window"):
            self.assertEqual(c[field], 0, field)
        self.assertTrue(c["available"])

    def test_missing_db_degrades_without_crashing(self):
        with patch.dict(os.environ,
                        {"HOMELAB_INCIDENTS_DB": "/nonexistent/none.db"}):
            c = advisor._canonical_monitor_incidents(CUTOFF_1D)
        self.assertFalse(c["available"])
        self.assertEqual(c["active"], 0)

    def test_solve_path_is_not_an_outage_count(self):
        """skillkit's own DB is agent bookkeeping, reported separately."""
        solve = [{"service": "general", "resolved": False,
                  "time": "2026-08-08 10:00 UTC"},
                 {"service": "cobalt", "resolved": True,
                  "time": "2026-08-08 09:00 UTC"}]
        inc = collect([("joplin", "open", NOW - 600, None, 1)], solve)

        self.assertEqual(inc["monitor_active"], 1, "authoritative outages")
        self.assertEqual(inc["solve_path_unresolved"], 1)
        self.assertEqual(inc["solve_path_total"], 2)
        # canonical_unresolved is the sum of open *state*, never of rows.
        self.assertEqual(inc["canonical_unresolved"], 2)
        self.assertEqual(inc["semantics"]["authoritative_outage_source"],
                         "loki homelab_incidents.db monitor_incidents")


# ── PROOF: repeat detections do not distort the briefing ──────────────────
class TestOccurrencesDoNotAmplify(unittest.TestCase):

    def test_repeated_occurrences_do_not_crater_reliability(self):
        """297 detection rows over 3 faults must cost the same as 3 faults."""
        rows = []
        for key in ("joplin", "black-boxx", "jellyfin"):
            rows.append((key, "open", NOW - 10 * DAY, None, 1))
            rows += [(key, "escalated", NOW - 300 * i, NOW - 300 * i, 1)
                     for i in range(1, 100)]
        inc = collect(rows)
        self.assertEqual(inc["monitor_active"], 3)
        self.assertGreater(inc["occurrences_in_window"], 100)

        quiet = scores_for(base_facts(incidents={
            "monitor_active": 3, "solve_path_unresolved": 0,
            "canonical_unresolved": 3}))
        noisy = scores_for(base_facts(incidents=inc))

        self.assertEqual(noisy["Reliability"], quiet["Reliability"],
                         "detections must not change the score")
        self.assertEqual(noisy["Reliability"], 100 - 12 * 3)
        self.assertGreater(noisy["Reliability"], 0,
                           "the old formula floored this at 0")

    def test_repeated_occurrences_are_not_multiple_new_incidents(self):
        rows = [("joplin", "open", NOW - 10 * DAY, None, 1)]
        rows += [("joplin", "escalated", NOW - 300 * i, NOW - 300 * i, 1)
                 for i in range(1, 50)]
        inc = collect(rows)

        self.assertEqual(inc["new_in_window"], 0)
        line = reporting.incident_line({"incidents": inc})
        self.assertIn("1 active", line)
        self.assertNotIn("49 new", line)
        self.assertNotIn("50 new", line)
        self.assertIn("repeat detection", line)

    def test_no_incidents_beside_a_positive_incident_delta(self):
        """The '"no incidents" next to "+9 incidents"' contradiction."""
        inc = collect([("joplin", "resolved", NOW - 5 * DAY, NOW - 60, 9)])
        line = reporting.incident_line({"incidents": inc})

        self.assertIn("0 active", line)
        self.assertIn("1 resolved", line)
        # If anything nonzero is shown beside "0 active", it must be
        # explicitly labelled as repeat activity on closed faults.
        self.assertIn("already closed", line)

    def test_reliability_is_explainable_from_measured_inputs(self):
        facts = base_facts(
            incidents={"canonical_unresolved": 2, "monitor_active": 1,
                       "solve_path_unresolved": 1},
            docker={"dex247": {"stopped": ["cobalt (Exited)"],
                               "restarting": []}},
            hosts={"razr": {"unreachable": "timeout"}})
        b = reporting.reliability_terms(facts)

        self.assertEqual(b["total_penalty"],
                         12 * 1 + 3 * 1 + 8 * 1 + 15 * 1)
        self.assertEqual(b["score"], 100 - b["total_penalty"])
        self.assertEqual(b["score"], scores_for(facts)["Reliability"])
        # No 'recent' term may exist at all.
        self.assertNotIn("recent", [t["name"] for t in b["terms"]])

        text = reporting.reliability_explainer(facts)
        # 'open service faults', not 'open monitor faults': the term now
        # excludes protective degradation, which is charged separately.
        # See tests/test_reliability_state.py.
        self.assertIn("12×1 open service faults", text)
        self.assertIn(str(b["score"]), text)

    def test_stale_solve_path_records_cannot_sink_reliability(self):
        """Bookkeeping nobody closed is visible, but capped."""
        facts = base_facts(incidents={"monitor_active": 0,
                                      "solve_path_unresolved": 40,
                                      "canonical_unresolved": 40})
        b = reporting.reliability_terms(facts)
        solve = next(t for t in b["terms"]
                     if t["name"] == "unclosed solve-path records")

        self.assertEqual(solve["penalty"], reporting.SOLVE_PATH_PENALTY_CAP)
        self.assertGreaterEqual(b["score"], 80,
                                "agent bookkeeping must not read as outage")

    def test_explainer_arithmetic_always_reconstructs_the_score(self):
        """Including when a term is capped — printed terms must sum to it."""
        import re
        for solve_open in (0, 2, 40):
            facts = base_facts(incidents={"monitor_active": 1,
                                          "solve_path_unresolved": solve_open})
            b = reporting.reliability_terms(facts)
            self.assertEqual(sum(t["penalty"] for t in b["terms"]),
                             b["total_penalty"])
            text = reporting.reliability_explainer(facts)
            self.assertIn(str(b["score"]), text)
            if solve_open == 40:
                self.assertIn("capped", text)
                self.assertNotIn("3×40", text)

    def test_trend_line_never_sums_faults_with_bookkeeping(self):
        """1 live fault + 4 stale records must not read as '5 active'."""
        line = reporting.incident_line({"incidents": {
            "monitor_active": 1, "monitor_active_faults": ["hermes-provider"],
            "solve_path_unresolved": 4, "canonical_unresolved": 5}})

        self.assertIn("1 active fault", line)
        self.assertNotIn("5 active", line)
        self.assertIn("4 unclosed agent repair record", line)
        self.assertIn("not outages", line)
        self.assertIn("hermes-provider", line)

    def test_one_real_outage_outweighs_stale_bookkeeping(self):
        outage = scores_for(base_facts(incidents={
            "monitor_active": 1, "solve_path_unresolved": 0}))
        bookkeeping = scores_for(base_facts(incidents={
            "monitor_active": 0, "solve_path_unresolved": 3}))
        self.assertLess(outage["Reliability"], bookkeeping["Reliability"])


# ── disk threshold vs trend ───────────────────────────────────────────────
class TestDiskSemantics(unittest.TestCase):

    def facts_for(self, pct, delta, threshold=90, host="razr"):
        return {"hosts": {host: {"disk_pct": pct}},
                "disk_monitor_threshold_pct": threshold,
                "trends": {"deltas": {f"disk_{host}": {
                    "then": pct - delta, "now": pct, "delta": delta}}}}

    def test_07_below_threshold_with_rapid_growth_is_watch(self):
        """78% with +21pp: below the 90% alert line, but worth planning."""
        facts = self.facts_for(78, 21)
        st = reporting.disk_status(facts)["razr"]

        self.assertEqual(st["state"], "watch")
        self.assertEqual(st["max_severity"], "P2", "planned attention, not P1")
        self.assertFalse(st["healthy"])

        facts["disk_status"] = reporting.disk_status(facts)
        healthy = reporting.healthy_lines(facts)
        self.assertIn("disk trend watch", healthy)
        self.assertIn("not an incident", healthy)

        trends = reporting.trend_lines(facts, {}, "since yesterday")
        self.assertIn("rapid growth", trends)
        self.assertIn("below the 90% threshold", trends)

    def test_08_near_threshold_is_approaching(self):
        st = reporting.disk_status(self.facts_for(82, 5))["razr"]
        self.assertEqual(st["state"], "approaching")
        self.assertEqual(st["max_severity"], "P2")

        trends = reporting.trend_lines(self.facts_for(82, 5), {}, "today")
        self.assertIn("approaching the 90% threshold", trends)

    def test_09_exceeding_threshold_is_p1_and_never_healthy(self):
        facts = self.facts_for(92, 7)
        st = reporting.disk_status(facts)["razr"]

        self.assertEqual(st["state"], "over_threshold")
        self.assertEqual(st["max_severity"], "P1")

        facts["disk_status"] = reporting.disk_status(facts)
        healthy = reporting.healthy_lines(facts)
        self.assertNotIn("razr", healthy)

        trends = reporting.trend_lines(facts, {}, "today")
        self.assertIn("at/above the 90% alert threshold", trends)

    def test_healthy_disk_cannot_also_be_p1(self):
        """The 78%-is-fine / 78%-is-P1 contradiction, both directions."""
        for pct, delta in ((29, 1), (78, 0), (60, 3)):
            facts = self.facts_for(pct, delta)
            facts["disk_status"] = reporting.disk_status(facts)
            st = facts["disk_status"]["razr"]

            self.assertEqual(st["state"], "ok", f"{pct}% should be ok")
            self.assertIsNone(st["max_severity"],
                              "an ok disk exposes no severity to escalate to")
            healthy = reporting.healthy_lines(facts)
            self.assertIn("razr", healthy)
            self.assertIn("No action required", healthy)
            # and the trend line must not carry an alarm tag
            trends = reporting.trend_lines(facts, {}, "today")
            self.assertNotIn("⚠️", trends)

    def test_threshold_comes_from_the_monitor_not_a_literal_80(self):
        with patch.dict(os.environ, {"MONITOR_DISK_MIN_FREE_PCT": "10"}):
            self.assertEqual(advisor.disk_alert_threshold_pct(), 90)
        with patch.dict(os.environ, {"MONITOR_DISK_MIN_FREE_PCT": "25"}):
            self.assertEqual(advisor.disk_alert_threshold_pct(), 75)
        with patch.dict(os.environ, {"MONITOR_DISK_MIN_FREE_PCT": "junk"}):
            self.assertEqual(advisor.disk_alert_threshold_pct(), 90)

    def test_disk_between_80_and_threshold_is_not_an_alert(self):
        """The old hard-coded 80% would have alarmed here; 90% does not."""
        facts = self.facts_for(84, 1)
        facts["disk_status"] = reporting.disk_status(facts)
        self.assertEqual(facts["disk_status"]["razr"]["state"], "ok")
        self.assertIn("razr (84%)", reporting.healthy_lines(facts))

    def test_unreachable_host_is_not_classified_as_healthy_disk(self):
        facts = {"hosts": {"razr": {"unreachable": "timeout"}},
                 "disk_monitor_threshold_pct": 90, "trends": {"deltas": {}}}
        self.assertEqual(reporting.disk_status(facts), {})
        self.assertNotIn("razr", reporting.healthy_lines(facts))


# ── image age vs upstream update availability ─────────────────────────────
class TestImageAgeSemantics(unittest.TestCase):

    OLD_ONLY = {"age_threshold_days": 60,
                "old_by_local_age": [{"container": "cobalt",
                                      "image": "cobalt:latest",
                                      "image_age_days": 465}],
                "old_by_local_age_count": 1,
                "upstream_checked": False,
                "update_available": [], "update_available_count": 0,
                "stale_count": 1}

    def test_10_old_local_image_claims_no_update(self):
        line = reporting.image_age_line({"image_ages": self.OLD_ONLY})

        self.assertIn("built over 60 days ago", line)
        self.assertIn("NOT checked", line)
        self.assertIn("not a known available update", line)
        for forbidden in ("update available", "outdated", "security risk"):
            self.assertNotIn(forbidden, line.lower().replace(
                "not a known available update", ""))

    def test_11_update_claimed_only_with_upstream_evidence(self):
        checked_none = dict(self.OLD_ONLY, upstream_checked=True)
        line = reporting.image_age_line({"image_ages": checked_none})
        self.assertIn("no updates available", line)

        with_evidence = dict(self.OLD_ONLY, upstream_checked=True,
                             update_available=["cobalt:2.1.0"],
                             update_available_count=1)
        line = reporting.image_age_line({"image_ages": with_evidence})
        self.assertIn("1 confirmed upstream update(s)", line)
        self.assertIn("cobalt:2.1.0", line)

    def test_age_alone_never_populates_update_fields(self):
        """The collector's own contract: age in, no update claim out."""
        self.assertFalse(self.OLD_ONLY["upstream_checked"])
        self.assertEqual(self.OLD_ONLY["update_available"], [])
        self.assertEqual(self.OLD_ONLY["update_available_count"], 0)
        self.assertEqual(reporting.image_age_line(
            {"image_ages": dict(self.OLD_ONLY, old_by_local_age_count=0)}), "")


# ── the live production database ──────────────────────────────────────────
class TestAgainstLiveData(unittest.TestCase):
    """Guards the exact shape of the real data that caused the bug."""

    DB = "/home/g2k247/loki-bot/homelab_incidents.db"

    @unittest.skipUnless(os.path.exists(DB), "live incident DB not present")
    def test_live_rows_fold_into_far_fewer_faults(self):
        with patch.dict(os.environ, {"HOMELAB_INCIDENTS_DB": self.DB}):
            c = advisor._canonical_monitor_incidents(0)  # all history

        self.assertTrue(c["available"])
        self.assertGreater(c["rows_all_time"], 100,
                           "live DB should hold the noisy detection rows")
        self.assertLess(c["distinct_faults_all_time"], 20,
                        "hundreds of rows must fold into a handful of faults")
        self.assertLess(c["active"], 10)
        # Reliability from live data must stay a sane, explainable number.
        rel = reporting.reliability_terms(
            base_facts(incidents={"canonical_unresolved": c["active"],
                                  "monitor_active": c["active"],
                                  "solve_path_unresolved": 0}))
        self.assertGreaterEqual(rel["score"], 50)


if __name__ == "__main__":
    unittest.main()

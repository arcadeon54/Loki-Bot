"""
Focused tests for the registered UGREEN NAS / Tracearr maintenance path.

Nothing here touches the NAS: run_action is replaced with canned dispatcher
payloads built from the real 2026-07-27 discovery, so no SSH, no docker, and
absolutely nothing is restarted, pulled or recreated. The one live-shaped test
asserts only that the action allowlist rejects anything off-table, which needs
no network at all.

Run:  venv/bin/python -m unittest tests.test_nas_tracearr -v
"""

import asyncio
import json
import os
import re
import tempfile
import unittest
from unittest import mock

BOSS_ID = "111111111111111111"
CREW_ID = "222222222222222222"
os.environ.setdefault("OWNER_USER_ID", BOSS_ID)
os.environ.setdefault("CREW_USER_IDS", CREW_ID)
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ.setdefault("HOMELAB_DB_PATH", _tmp.name)

import homelab_assets
import homelab_maintenance as hm
import nas_maint as nm
from tools import ToolContext


# ── canned dispatcher payloads (shape copied from the real dispatcher) ─────
def _c(name, health="healthy", restarts=0, state="running"):
    return {"name": name, "id_short": name[:12], "image": f"{name}:test",
            "image_id": "sha256:" + "a" * 64, "state": state,
            "running": state == "running", "health": health,
            "restart_count": restarts, "exit_code": 0, "oom_killed": False,
            "started_at": "2026-07-27T01:30:13Z", "restart_policy": "unless-stopped",
            "env_var_names": ["PATH"], "labels": {}, "networks":
                {"tracearr_tracearr-network": {"ip": "172.19.0.4", "aliases": [name]}}}


DEPS_OK = {
    "compose_project": "tracearr",
    "dependencies": {"tracearr": _c("tracearr", restarts=268),
                     "redis": _c("tracearr-redis"),
                     "postgres": _c("tracearr-db")},
    "shared_networks": {"redis": ["tracearr_tracearr-network"],
                        "postgres": ["tracearr_tracearr-network"]},
    "other_stack_members": [],
}


def boss_ctx():
    return ToolContext(user_id=BOSS_ID, user_name="Boss", channel_id="c")


def crew_ctx():
    return ToolContext(user_id=CREW_ID, user_name="Rob", channel_id="c")


def _dispatcher_src():
    return open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "nas", "loki-nas-maint")).read()


def run(coro):
    return asyncio.run(coro)


class AssetRegistration(unittest.TestCase):
    def setUp(self):
        self.reg = homelab_assets.load(force=True)

    def test_nas_resolves_to_the_canonical_ugreen_asset(self):
        for alias in ("nas", "the nas", "UGREEN", "UGREEN NAS", "Unimatrix",
                      "unimatrix-001"):
            with self.subTest(alias=alias):
                a = self.reg.resolve(alias)
                self.assertIsNotNone(a, alias)
                self.assertEqual(a["key"], "ugreen-nas")

    def test_only_one_canonical_nas_asset_exists(self):
        keys = [k for k, a in self.reg.assets.items()
                if a.get("host") == "nas" and a.get("type") == "nas_appliance"]
        self.assertEqual(keys, ["ugreen-nas"], "duplicate NAS records")

    def test_tracearr_resolves_including_dependency_phrasings(self):
        for alias in ("Tracearr", "tracearr redis", "Tracearr Database",
                      "tracearr postgres"):
            with self.subTest(alias=alias):
                a = self.reg.resolve(alias)
                self.assertIsNotNone(a, alias)
                self.assertEqual(a["key"], "tracearr")

    def test_tracearr_records_discovered_facts_only(self):
        d = self.reg.get("tracearr")["docker"]
        self.assertEqual(d["container"], "tracearr")
        self.assertEqual(d["compose_service"], "tracearr")
        self.assertEqual(d["compose_project"], "tracearr")
        self.assertEqual(d["network"], "tracearr_tracearr-network")
        self.assertEqual(d["compose_file"], "/volume2/tracearr/docker-compose.yml")

    def test_dependency_records_use_discovered_names(self):
        deps = self.reg.get("tracearr")["dependencies"]
        self.assertEqual(deps["redis"]["container"], "tracearr-redis")
        self.assertEqual(deps["redis"]["compose_service"], "redis")
        self.assertEqual(deps["postgres"]["container"], "tracearr-db")
        self.assertEqual(deps["postgres"]["compose_service"], "timescale")

    def test_nas_assets_are_not_local(self):
        for key in ("ugreen-nas", "tracearr"):
            with self.subTest(key=key):
                a = self.reg.get(key)
                self.assertFalse(homelab_assets.Registry.is_local(a))
                self.assertEqual(homelab_assets.Registry.executor(a),
                                 "nas_dispatcher")

    def test_remote_containers_never_widen_the_local_allowlist(self):
        """A NAS container name must not become legal for a dex247 command."""
        containers = self.reg.allowed_values()["container"]
        for name in ("tracearr", "tracearr-redis", "tracearr-db"):
            self.assertNotIn(name, containers)

    def test_raw_nas_ip_is_not_an_asset(self):
        self.assertIsNone(self.reg.resolve("192.168.1.63"))
        _, err = hm._resolve_or_error("192.168.1.63")
        self.assertIn("not a managed host", err)


class RemoteExecutorGuard(unittest.TestCase):
    def test_local_runbook_refuses_remote_asset_and_names_the_tools(self):
        asset = homelab_assets.load().get("tracearr")
        with self.assertRaises(Exception) as cm:
            run(hm.run_runbook(asset, allow_repairs=False))
        msg = str(cm.exception)
        self.assertIn("nas_dispatcher", msg)
        self.assertIn("tracearr_diagnose", msg)
        self.assertIn("do not fall back to manual docker", msg.lower())

    def test_local_assets_still_use_local_runbooks(self):
        asset = homelab_assets.load().get("jellyfin")
        self.assertTrue(homelab_assets.Registry.is_local(asset))


class DispatcherActionAllowlist(unittest.TestCase):
    def test_action_table_is_exactly_the_allowlist(self):
        self.assertEqual(sorted(nm.ACTIONS), sorted([
            "container_inventory", "host_status", "tracearr_dependencies",
            "tracearr_recent_logs", "tracearr_restart_forensics",
            "tracearr_exit_window_logs",
            "tracearr_status", "tracearr_update_check",
            "network_status", "network_tooling_check", "network_speed_test",
            "disk_status", "plex_status", "plex_dependencies",
            "plex_recent_logs", "plex_transcode_processes"]))

    def test_no_state_changing_verb_is_reachable(self):
        for bad in ("restart", "stop", "pull", "recreate", "exec", "docker",
                    "tracearr_restart", "host_status; id", "../../bin/sh"):
            with self.subTest(bad=bad):
                with self.assertRaises(nm.NasError):
                    run(nm.run_action(bad))

    def test_arbitrary_action_never_launches_a_subprocess(self):
        with mock.patch("asyncio.create_subprocess_exec") as spawn:
            with self.assertRaises(nm.NasError):
                run(nm.run_action("rm -rf /"))
            spawn.assert_not_called()


class ToolBehaviour(unittest.TestCase):
    def _patch(self, payloads):
        async def fake(action, timeout=None):
            if action not in payloads:
                raise nm.NasError(f"unexpected action {action}")
            return payloads[action]
        return mock.patch.object(nm, "run_action", fake)

    def test_diagnose_includes_both_registered_dependencies(self):
        with self._patch({"tracearr_dependencies": DEPS_OK}):
            out = json.loads(run(nm._tool_tracearr_diagnose({}, boss_ctx())))
        self.assertTrue(out["ok"])
        self.assertIn("redis", out["dependencies"])
        self.assertIn("postgres", out["dependencies"])
        self.assertTrue(out["dependencies_healthy"])

    def test_diagnose_confirms_dependencies_match_the_registry(self):
        """Guards against diagnosing the UGOS host redis by mistake."""
        with self._patch({"tracearr_dependencies": DEPS_OK}):
            out = json.loads(run(nm._tool_tracearr_diagnose({}, boss_ctx())))
        self.assertEqual(out["dependencies"]["redis"]["registered_container"],
                         "tracearr-redis")
        self.assertTrue(out["dependencies"]["redis"]["matches_registry"])
        self.assertTrue(out["dependencies"]["postgres"]["matches_registry"])

    def test_wrong_redis_container_is_flagged_not_silently_accepted(self):
        bad = json.loads(json.dumps(DEPS_OK))
        bad["dependencies"]["redis"]["name"] = "redis-server"   # the UGOS one
        with self._patch({"tracearr_dependencies": bad}):
            out = json.loads(run(nm._tool_tracearr_diagnose({}, boss_ctx())))
        self.assertFalse(out["dependencies"]["redis"]["matches_registry"])

    def test_unhealthy_dependency_produces_a_dependency_verdict(self):
        sick = json.loads(json.dumps(DEPS_OK))
        sick["dependencies"]["redis"]["health"] = "unhealthy"
        with self._patch({"tracearr_dependencies": sick}):
            out = json.loads(run(nm._tool_tracearr_diagnose({}, boss_ctx())))
        self.assertFalse(out["dependencies_healthy"])
        self.assertIn("redis", out["verdict"])

    def test_healthy_finding_is_preserved(self):
        with self._patch({"tracearr_dependencies": DEPS_OK}):
            out = json.loads(run(nm._tool_tracearr_diagnose({}, boss_ctx())))
        self.assertIn("healthy", out["verdict"].lower())

    def test_restart_churn_is_reported_as_an_open_finding(self):
        with self._patch({"tracearr_dependencies": DEPS_OK}):
            out = json.loads(run(nm._tool_tracearr_diagnose({}, boss_ctx())))
        self.assertIn("open_finding", out)
        self.assertIn("268", out["open_finding"])
        self.assertIn("unproven", out["open_finding"].lower())

    def test_executor_failure_returns_a_precise_component_not_a_tutorial(self):
        async def boom(action, timeout=None):
            raise nm.NasError("the NAS sudoers rule for /usr/local/sbin/"
                              "loki-nas-maint is missing")
        with mock.patch.object(nm, "run_action", boom):
            out = json.loads(run(nm._tool_tracearr_diagnose({}, boss_ctx())))
        self.assertFalse(out["ok"])
        self.assertIn("sudoers", out["error"])
        for banned in ("docker ps", "ssh into", "paste", "walk you through"):
            self.assertNotIn(banned, out["error"].lower())

    def test_update_check_never_offers_to_update(self):
        payload = {"compose_project": "tracearr",
                   "images": {"tracearr": {"container": "tracearr",
                                           "image": "ghcr.io/x/tracearr@sha256:4802c793336ec2393de8db1088b0c8aa170c8b8b0aa7bba33a56a599c5d72544"}},
                   "note": "local image metadata only"}
        async def no_upstream(asset):
            return {"release_source": None, "latest_stable_version": None,
                    "latest_stable_digest": None,
                    "upstream_check_status": "unavailable",
                    "update_available": None, "notes": []}
        with self._patch({"tracearr_update_check": payload}), \
             mock.patch.object(nm, "_upstream_for", no_upstream):
            out = json.loads(run(nm._tool_tracearr_update_check({}, boss_ctx())))
        self.assertTrue(out["ok"])
        self.assertTrue(out["deployment_matches_configuration"])
        self.assertEqual(out["approval_policy"], "approval_always")
        self.assertIn("approved plan", out["next_step"])

    def test_tools_are_boss_only(self):
        for tool in (nm._tool_nas_status, nm._tool_tracearr_status,
                     nm._tool_tracearr_diagnose, nm._tool_tracearr_update_check):
            with self.subTest(tool=tool.__name__):
                out = json.loads(run(tool({}, crew_ctx())))
                self.assertFalse(out["ok"])
                self.assertIn("Boss-only", out["error"])


class ToolRegistration(unittest.TestCase):
    EXPECTED = ("nas_status", "tracearr_status", "tracearr_diagnose",
                "tracearr_update_check")

    def test_all_live_tools_are_registered(self):
        import tools
        names = set(tools.REGISTRY)
        for name in self.EXPECTED:
            self.assertIn(name, names)

    def test_registered_tools_take_no_free_text_target(self):
        """No caller-supplied container/host/path can reach the dispatcher."""
        import tools
        specs = dict(tools.REGISTRY)
        for name in self.EXPECTED:
            props = specs[name].parameters.get("properties") or {}
            self.assertEqual(props, {}, f"{name} accepts parameters")

    def test_tools_are_boss_permissioned_in_the_registry(self):
        import tools
        specs = dict(tools.REGISTRY)
        for name in self.EXPECTED:
            self.assertEqual(specs[name].permission, "boss")

    def test_no_tool_declares_a_state_changing_action_type(self):
        import tools
        specs = dict(tools.REGISTRY)
        for name in self.EXPECTED:
            self.assertEqual(specs[name].action_type, "",
                             f"{name} is consequential; these must be read-only")


if __name__ == "__main__":
    unittest.main()


# ── Correction pass: honest update status + restart classification ─────────
def _upstream(status="verified", latest="v1.5.0", digest="sha256:2b6aca8d",
              update=None, notes=None):
    return {"release_source": "github:connorgallopo/Tracearr",
            "latest_stable_version": latest if status == "verified" else None,
            "latest_stable_digest": digest if status == "verified" else None,
            "upstream_check_status": status,
            "update_available": update,
            "notes": list(notes or [])}


PINNED = "sha256:4802c793336ec2393de8db1088b0c8aa170c8b8b0aa7bba33a56a599c5d72544"
UPDATE_PAYLOAD = {
    "compose_project": "tracearr",
    "images": {"tracearr": {"container": "tracearr",
                            "image": f"ghcr.io/connorgallopo/tracearr@{PINNED}"},
               "redis": {"container": "tracearr-redis", "image": "redis:8-alpine"}},
    "note": "local image metadata only",
}


class HonestUpdateStatus(unittest.TestCase):
    def _run(self, upstream):
        async def fake_action(action, timeout=None):
            return UPDATE_PAYLOAD
        async def fake_upstream(asset):
            return upstream
        with mock.patch.object(nm, "run_action", fake_action), \
             mock.patch.object(nm, "_upstream_for", fake_upstream):
            return json.loads(run(nm._tool_tracearr_update_check({}, boss_ctx())))

    def test_all_required_fields_are_present(self):
        out = self._run(_upstream(update=True))
        for field in ("installed_version", "configured_image", "configured_digest",
                      "running_digest", "deployment_matches_configuration",
                      "latest_stable_version", "latest_stable_digest",
                      "upstream_check_status", "update_available",
                      "release_source", "approval_policy", "confidence", "notes"):
            self.assertIn(field, out, field)

    def test_digest_matches_but_upstream_unavailable(self):
        out = self._run(_upstream(status="unavailable", update=None))
        self.assertTrue(out["deployment_matches_configuration"])
        self.assertEqual(out["upstream_check_status"], "unavailable")
        self.assertIsNone(out["update_available"])
        self.assertEqual(out["confidence"], "low")
        self.assertIn("could not yet verify", out["summary"])

    def test_no_false_up_to_date_claim_when_upstream_unknown(self):
        out = self._run(_upstream(status="unavailable"))
        low = out["summary"].lower()
        self.assertNotIn("up to date", low)
        self.assertNotIn("latest", low.replace("newest stable upstream", ""))

    def test_verified_and_current(self):
        out = self._run(_upstream(latest="v1.4.27", update=False))
        self.assertEqual(out["upstream_check_status"], "verified")
        self.assertFalse(out["update_available"])
        self.assertEqual(out["confidence"], "high")
        self.assertIn("latest verified stable release", out["summary"])

    def test_verified_and_update_available(self):
        out = self._run(_upstream(latest="v1.5.0", update=True))
        self.assertTrue(out["update_available"])
        self.assertIn("v1.5.0", out["summary"])
        self.assertIn("update is available", out["summary"])
        self.assertNotIn("up to date", out["summary"].lower())

    def test_deployment_match_alone_never_sets_update_available(self):
        """The exact bug this pass fixes."""
        out = self._run(_upstream(status="unavailable"))
        self.assertTrue(out["deployment_matches_configuration"])
        self.assertIsNot(out["update_available"], False)

    def test_drifted_deployment_lowers_confidence(self):
        payload = json.loads(json.dumps(UPDATE_PAYLOAD))
        payload["images"]["tracearr"]["image"] = "ghcr.io/x/tracearr@sha256:dead"
        async def fake_action(action, timeout=None):
            return payload
        async def fake_upstream(asset):
            return _upstream(update=False)
        with mock.patch.object(nm, "run_action", fake_action), \
             mock.patch.object(nm, "_upstream_for", fake_upstream):
            out = json.loads(run(nm._tool_tracearr_update_check({}, boss_ctx())))
        self.assertFalse(out["deployment_matches_configuration"])
        self.assertEqual(out["confidence"], "low")
        self.assertTrue(any("drifted" in n for n in out["notes"]))


class UpstreamReleaseSelection(unittest.TestCase):
    class _Resp:
        def __init__(self, payload, status=200, headers=None):
            self._p, self.status = payload, status
            self.headers = headers or {}
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def json(self): return self._p

    def test_prereleases_are_ignored(self):
        releases = [
            {"tag_name": "v1.5.0-beta.7", "prerelease": True, "draft": False},
            {"tag_name": "v1.5.0", "prerelease": False, "draft": False},
            {"tag_name": "v1.4.31", "prerelease": False, "draft": False},
        ]
        session = mock.MagicMock()
        session.get = lambda url, **kw: self._Resp(releases)
        tag, ver = run(nm._latest_stable_release(session,
                                                 "github:connorgallopo/Tracearr"))
        self.assertEqual(tag, "v1.5.0")
        self.assertEqual(ver, (1, 5, 0))

    def test_drafts_are_ignored(self):
        releases = [{"tag_name": "v9.9.9", "prerelease": False, "draft": True},
                    {"tag_name": "v1.5.0", "prerelease": False, "draft": False}]
        session = mock.MagicMock()
        session.get = lambda url, **kw: self._Resp(releases)
        tag, _ = run(nm._latest_stable_release(session, "github:x/y"))
        self.assertEqual(tag, "v1.5.0")

    def test_prerelease_tags_do_not_parse_as_stable(self):
        self.assertIsNone(nm._parse_stable("v1.5.0-beta.7"))
        self.assertIsNone(nm._parse_stable("nightly"))
        self.assertEqual(nm._parse_stable("v1.4.27"), (1, 4, 27))

    def test_registry_tag_strips_the_v_prefix(self):
        self.assertEqual(nm._registry_tag("v1.5.0", "strip_v"), "1.5.0")
        self.assertEqual(nm._registry_tag("1.5.0", "strip_v"), "1.5.0")
        self.assertEqual(nm._registry_tag("v1.5.0", ""), "v1.5.0")

    def test_upstream_failure_degrades_to_unavailable(self):
        async def boom(asset, session):
            raise nm.NasError("github unreachable")
        asset = homelab_assets.load().get("tracearr")
        session = mock.MagicMock()
        session.get = mock.MagicMock(side_effect=OSError("no network"))
        out = run(nm.check_upstream(asset, session))
        self.assertEqual(out["upstream_check_status"], "unavailable")
        self.assertIsNone(out["update_available"])
        self.assertTrue(out["notes"])


class RestartClassification(unittest.TestCase):
    def _f(self, events, restart_count=273, **state):
        st = {"started_at": "2026-07-27T07:02:29Z",
              "finished_at": "2026-07-27T07:02:27Z", "exit_code": 0,
              "error": "", "oom_killed": False}
        st.update(state)
        return {"events": events, "restart_count": restart_count,
                "restart_policy": "unless-stopped", "state": st,
                "healthcheck": {"status": "healthy", "failing_streak": 0}}

    def test_die_start_pairs_are_an_application_exit(self):
        out = nm.classify_restarts(self._f(
            [{"action": "die", "exit_code": "0"}, {"action": "start"}]))
        self.assertEqual(out["classification"], "confirmed_application_exit")

    def test_explicit_restart_action_is_external(self):
        out = nm.classify_restarts(self._f([{"action": "restart"}]))
        self.assertEqual(out["classification"], "confirmed_external_restart")

    def test_unhealthy_events_are_a_healthcheck_action(self):
        f = self._f([{"action": "health_status: unhealthy"}])
        f["healthcheck"]["failing_streak"] = 3
        out = nm.classify_restarts(f)
        self.assertEqual(out["classification"], "confirmed_healthcheck_action")

    def test_restart_count_without_events_still_implies_process_exit(self):
        out = nm.classify_restarts(self._f([]))
        self.assertEqual(out["classification"], "confirmed_application_exit")
        self.assertIn("RestartCount", out["reason"])

    def test_no_evidence_is_unresolved_not_a_guess(self):
        out = nm.classify_restarts(self._f([], restart_count=0))
        self.assertEqual(out["classification"], "unresolved")

    def test_missing_forensics_is_unresolved(self):
        self.assertEqual(nm.classify_restarts({})["classification"], "unresolved")

    def test_clean_exit_is_not_treated_as_healthy(self):
        out = nm.classify_restarts(self._f([{"action": "die", "exit_code": "0"},
                                            {"action": "start"}]))
        self.assertEqual(out["last_exit_code"], 0)
        self.assertTrue(out["clean_exit_is_not_healthy"])

    def test_no_automatic_repair_is_ever_offered(self):
        for events in ([], [{"action": "die"}, {"action": "start"}],
                       [{"action": "restart"}]):
            with self.subTest(events=events):
                out = nm.classify_restarts(self._f(events))
                self.assertIn("none", out["automatic_repair"])


class DependencyHealthDoesNotMaskAppChurn(unittest.TestCase):
    def test_healthy_dependencies_do_not_imply_a_healthy_app(self):
        async def fake(action, timeout=None):
            if action == "tracearr_dependencies":
                return DEPS_OK
            raise nm.NasError("forensics not installed")
        with mock.patch.object(nm, "run_action", fake):
            out = json.loads(run(nm._tool_tracearr_diagnose({}, boss_ctx())))
        self.assertTrue(out["dependencies_healthy"])
        self.assertFalse(out["app_healthy"])
        self.assertIn("NOT stable", out["verdict"])
        self.assertEqual(out["restart_evidence"]["classification"], "unresolved")


class DispatcherRedaction(unittest.TestCase):
    """The dispatcher is the last line before data leaves the NAS."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "nas", "loki-nas-maint")
        spec = importlib.util.spec_from_loader("dispatcher", None)
        cls.mod = importlib.util.module_from_spec(spec)
        exec(open(path).read().split("def find_tracearr")[0], cls.mod.__dict__)

    def test_credentials_are_redacted(self):
        for raw, must_not in [
            ("DATABASE_URL=postgres://user:hunter2@db:5432/t", "hunter2"),
            ("password=swordfish", "swordfish"),
            ("api_key: abcdef123456", "abcdef123456"),
            ("Authorization: Bearer aaaaaaaaaaaaaaaaaaaa", "aaaaaaaaaaaaaaaaaaaa"),
        ]:
            with self.subTest(raw=raw):
                self.assertNotIn(must_not, self.mod.redact(raw))

    def test_image_digests_survive_redaction(self):
        ref = "ghcr.io/connorgallopo/tracearr@sha256:" + "4" * 64
        self.assertEqual(self.mod.redact(ref), ref)

    def test_only_allowlisted_labels_are_emitted(self):
        raw = ("com.docker.compose.project=tracearr,"
               "com.acme.db_password=hunter2")
        keep = self.mod.parse_labels(raw)
        self.assertIn("com.docker.compose.project", keep)
        self.assertNotIn("com.acme.db_password", keep)


class EmptyEventsAreNotEvidence(unittest.TestCase):
    """The docker event ring buffer cannot reach a restart on these hosts."""

    def _f(self, events, restart_count=276):
        return {"events": events, "restart_count": restart_count,
                "restart_policy": "unless-stopped",
                "state": {"started_at": "2026-07-27T20:51:54.744283Z",
                          "finished_at": "2026-07-27T20:51:53.378794Z",
                          "exit_code": 0, "error": "", "oom_killed": False},
                "healthcheck": {"status": "healthy", "failing_streak": 0}}

    def test_empty_events_carry_an_explicit_caveat(self):
        out = nm.classify_restarts(self._f([]))
        self.assertFalse(out["events_usable"])
        self.assertIn("not evidence of stability", out["events_caveat"])

    def test_empty_events_do_not_imply_stability(self):
        out = nm.classify_restarts(self._f([]))
        self.assertEqual(out["classification"], "confirmed_application_exit")
        self.assertIn("ring buffer", out["reason"])

    def test_sub_ten_second_gap_is_reported_as_policy_signature(self):
        out = nm.classify_restarts(self._f([]))
        self.assertAlmostEqual(out["restart_gap_seconds"], 1.365489, places=3)
        self.assertIn("signature of the restart policy", out["reason"])

    def test_gap_is_none_when_timestamps_are_missing(self):
        f = self._f([])
        f["state"]["finished_at"] = None
        self.assertIsNone(nm.classify_restarts(f)["restart_gap_seconds"])

    def test_usable_events_clear_the_caveat(self):
        out = nm.classify_restarts(self._f([{"action": "die", "exit_code": "0"},
                                            {"action": "start"}]))
        self.assertTrue(out["events_usable"])
        self.assertIsNone(out["events_caveat"])


class PreparedHealthcheckFix(unittest.TestCase):
    """The start_period defect is recorded but must never auto-apply."""

    def setUp(self):
        self.fix = (homelab_assets.load(force=True).get("tracearr")
                    ["pending_fixes"]["healthcheck_start_period"])

    def test_fix_is_prepared_not_applied(self):
        self.assertEqual(self.fix["status"], "prepared_awaiting_approval")
        self.assertIn("approval", self.fix["blocked_on"].lower())

    def test_fix_records_observed_and_proposed_values(self):
        self.assertEqual(self.fix["observed"]["start_period"], "5s")
        self.assertEqual(self.fix["proposed"]["start_period"], "8m")

    def test_fix_is_not_applied_by_loki(self):
        self.assertEqual(self.fix["applied_by"], "manual_boss_edit_after_approval")

    def test_fix_does_not_claim_to_cause_the_restarts(self):
        self.assertIn("does not itself", self.fix["problem"].lower())

    def test_no_dispatcher_action_can_edit_compose(self):
        for action in nm.ACTIONS:
            self.assertFalse(any(v in action for v in
                                 ("edit", "write", "apply", "set", "compose")))


class ExitWindowLogAction(unittest.TestCase):
    def test_action_is_in_the_allowlist(self):
        self.assertIn("tracearr_exit_window_logs", nm.ACTIONS)

    def test_action_takes_no_arguments(self):
        for bad in ("tracearr_exit_window_logs x", "tracearr_exit_window_logs;id"):
            with self.subTest(bad=bad):
                with self.assertRaises(nm.NasError):
                    run(nm.run_action(bad))

    def test_dispatcher_never_invokes_a_destructive_verb(self):
        """The contract changed: approval-gated update actions legitimately
        pull, exec (pg_dump) and `compose up`. What must NEVER appear is a
        verb that destroys data or tears things down."""
        src = _dispatcher_src()
        for verb in ("rm", "rmi", "prune", "kill", "stop", "down", "volume",
                     "system", "network"):
            for call in (f'docker("{verb}"', f"docker('{verb}'",
                         f'docker_try("{verb}"', f"docker_try('{verb}'"):
                self.assertNotIn(call, src, f"dispatcher can invoke: {call}")

    def test_dispatcher_docker_verbs_are_the_declared_set(self):
        src = _dispatcher_src()
        verbs = set(re.findall(r'docker(?:_try)?\(\s*"([a-z]+)"', src))
        allowed = {"ps", "inspect", "logs", "stats", "events", "image",
                   "pull", "compose", "top", "start"}
        self.assertTrue(verbs <= allowed, f"unexpected docker verbs: {verbs}")

    def test_state_changing_verbs_live_only_in_write_actions(self):
        """pull / compose up must not be reachable from a read-only action."""
        src = _dispatcher_src()
        body = src[src.index("def act_tracearr_update_prepare"):]
        head = src[:src.index("def act_tracearr_update_prepare")]
        for marker in ('docker_try("pull"', '"up", "-d"'):
            self.assertIn(marker, body, f"{marker} should be in write actions")
            self.assertNotIn(marker, head,
                             f"{marker} leaked into a read-only action")

    def test_no_shell_execution_anywhere(self):
        src = _dispatcher_src()
        self.assertNotIn("shell=True", src)
        self.assertNotIn("os.system", src)


class RestartChurnFinding(unittest.TestCase):
    def setUp(self):
        self.f = (homelab_assets.load(force=True).get("tracearr")
                  ["known_issues"]["restart_churn"])

    def test_classified_as_application_bug(self):
        self.assertEqual(self.f["classification"], "application_bug")
        self.assertEqual(self.f["confidence"], "high")

    def test_no_automatic_repair(self):
        self.assertIn("none", self.f["automatic_repair"])

    def test_healthcheck_is_explicitly_excluded_as_cause(self):
        joined = " ".join(self.f["not_caused_by"]).lower()
        self.assertIn("healthcheck", joined)
        self.assertIn("watchtower", joined)

    def test_upstream_fix_not_claimed(self):
        self.assertFalse(self.f["upstream_fix_available"])


class ErrorExtractionRedaction(unittest.TestCase):
    """SQL parameter values must never leave the NAS."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "nas", "loki-nas-maint")
        spec = importlib.util.spec_from_loader("dispatcher2", None)
        cls.mod = importlib.util.module_from_spec(spec)
        exec(open(path).read().split("def act_tracearr_exit_window_logs")[0],
             cls.mod.__dict__)

    def test_sql_parameter_values_are_stripped(self):
        msg = ('Failed query: select "name" from "settings" where "name" '
               'in ($1, $2)\nparams: apiToken,dbPassword: Connection terminated')
        sql, reason = self.mod._strip_sql_params(msg)
        for leaked in ("apiToken", "dbPassword"):
            self.assertNotIn(leaked, sql)
            self.assertNotIn(leaked, reason)
        self.assertIn("2 value(s) REDACTED", sql)

    def test_postgres_reason_is_separated_from_sql(self):
        msg = ('Failed query: select 1\nparams: a: connect EHOSTUNREACH '
               '172.19.0.2:5432')
        sql, reason = self.mod._strip_sql_params(msg)
        self.assertEqual(reason, "connect EHOSTUNREACH 172.19.0.2:5432")
        self.assertNotIn("EHOSTUNREACH", sql)

    def test_errors_are_anchored_on_the_exit_not_the_tail_start(self):
        import datetime

        def parse(ts):
            return datetime.datetime.fromisoformat(
                ts.replace("Z", "+00:00").replace(".000000", ""))

        lines = [f'2026-07-27T{h}:00:00Z {{"level":50,"err":'
                 f'{{"type":"OLD{h}","message":"m"}}}}' for h in
                 ("11", "12", "13", "19", "20")]
        lines.append('2026-07-27T20:51:51Z {"level":50,"err":'
                     '{"type":"NEAREXIT","message":"boom"}}')
        picked = self.mod._extract_errors(
            lines, parse("2026-07-27T20:51:53Z"), parse)
        types = [e["error_type"] for e in picked]
        self.assertIn("NEAREXIT", types, "exit-adjacent error was dropped")
        self.assertNotIn("OLD11", types, "oldest tail error should not win")

    def test_error_output_is_bounded(self):
        lines = [f'2026-07-27T20:00:{i:02d}Z {{"level":50,"err":'
                 f'{{"type":"E","message":"{"x" * 5000}"}}}}' for i in range(30)]
        picked = self.mod._extract_errors(lines)
        self.assertLessEqual(len(picked), self.mod.MAX_ERROR_OBJECTS)
        for e in picked:
            self.assertLessEqual(len(e["error_message"]),
                                 self.mod.ERROR_LINE_CHAR_CAP)
            self.assertTrue(e["line_truncated"])


BACKUP_OK = {"backup_dir": "/volume2/loki-backups/tracearr/20260728-000000",
             "compose_ok": True, "database_ok": True,
             "database_size_bytes": 4096, "gzip_integrity_ok": True}
VERIFY_OK = {"verified": True, "failures": [], "checks": {}}
VERIFY_BAD = {"verified": False, "failures": ["local HTTP 502"], "checks": {}}


class UpdateToolIsApprovalGated(unittest.TestCase):
    def test_tool_is_registered_and_consequential(self):
        import tools
        spec = tools.REGISTRY["tracearr_update"]
        self.assertEqual(spec.action_type, "tracearr_update")
        self.assertEqual(spec.permission, "boss")
        self.assertTrue(callable(spec.prepare))
        self.assertEqual(spec.parameters.get("properties"), {})

    def test_write_actions_are_not_directly_callable_tools(self):
        import tools
        for a in nm.WRITE_ACTIONS:
            self.assertNotIn(a, tools.REGISTRY,
                             f"{a} must not be a model-callable tool")

    def test_write_action_params_are_validated(self):
        cases = [("tracearr_update_prepare", None), ("tracearr_update_prepare", "x"),
                 ("tracearr_apply_update", None), ("tracearr_apply_update", "zz"),
                 ("tracearr_backup", "extra"), ("tracearr_rollback", "extra")]
        for action, param in cases:
            with self.subTest(action=action, param=param):
                with self.assertRaises(nm.NasError):
                    run(nm.run_action(action, param=param))

    def test_wellformed_digest_and_prepare_id_are_accepted(self):
        """Shape validation only — no subprocess is spawned."""
        with mock.patch("asyncio.create_subprocess_exec") as spawn:
            spawn.side_effect = OSError("blocked")
            for action, param in (("tracearr_update_prepare", "sha256:" + "a" * 64),
                                  ("tracearr_apply_update", "0123456789abcdef")):
                with self.subTest(action=action):
                    with self.assertRaises(nm.NasError) as cm:
                        run(nm.run_action(action, param=param))
                    self.assertIn("could not launch ssh", str(cm.exception))


class ApprovedUpdateSequence(unittest.TestCase):
    def _ctx(self):
        return boss_ctx()

    def _payload(self):
        return {"prepare_id": "0123456789abcdef", "installed_version": "v1.4.27",
                "target_version": "v1.5.0", "target_digest": "sha256:" + "b" * 64,
                "backup_dir": BACKUP_OK["backup_dir"]}

    def _patch(self, mapping, joplin=True):
        async def fake(action, timeout=None, param=None):
            if action not in mapping:
                raise nm.NasError(f"unexpected action {action}")
            v = mapping[action]
            if isinstance(v, Exception):
                raise v
            return v
        ctxs = [mock.patch.object(nm, "run_action", fake)]
        if joplin:
            ctxs.append(mock.patch.object(nm, "_record_in_joplin",
                                          mock.AsyncMock(return_value="note1")))
        return ctxs

    def _run(self, mapping):
        patches = self._patch(mapping)
        for p in patches:
            p.start()
        try:
            return run(nm._run_approved_update(self._payload(), self._ctx()))
        finally:
            for p in patches:
                p.stop()

    def test_happy_path_reports_success(self):
        out = self._run({"tracearr_backup": BACKUP_OK,
                         "tracearr_apply_update": {"pulled": "img"},
                         "tracearr_verify_update": VERIFY_OK})
        self.assertIn("updated v1.4.27 → v1.5.0", out)
        self.assertIn("verified", out)

    def test_unverified_backup_aborts_before_any_change(self):
        bad = dict(BACKUP_OK, database_ok=False)
        out = self._run({"tracearr_backup": bad})
        self.assertIn("aborted before any change", out)
        self.assertIn("Nothing was pulled or recreated", out)

    def test_backup_failure_aborts_before_any_change(self):
        out = self._run({"tracearr_backup": nm.NasError("disk full")})
        self.assertIn("aborted before any change", out)
        self.assertIn("disk full", out)

    def test_failed_verification_triggers_rollback(self):
        out = self._run({"tracearr_backup": BACKUP_OK,
                         "tracearr_apply_update": {"pulled": "img"},
                         "tracearr_verify_update": VERIFY_BAD,
                         "tracearr_rollback": {"verify_after_rollback": VERIFY_OK}})
        self.assertIn("rolled back", out.lower())
        self.assertIn("local HTTP 502", out)
        self.assertIn("database dump was NOT restored", out)

    def test_rollback_that_does_not_verify_is_escalated(self):
        out = self._run({"tracearr_backup": BACKUP_OK,
                         "tracearr_apply_update": {"pulled": "img"},
                         "tracearr_verify_update": VERIFY_BAD,
                         "tracearr_rollback": {"verify_after_rollback": VERIFY_BAD}})
        self.assertIn("did not verify", out)
        self.assertIn("Hands-on attention needed", out)

    def test_rollback_error_is_escalated_not_swallowed(self):
        out = self._run({"tracearr_backup": BACKUP_OK,
                         "tracearr_apply_update": {"pulled": "img"},
                         "tracearr_verify_update": VERIFY_BAD,
                         "tracearr_rollback": nm.NasError("ssh died")})
        self.assertIn("rollback errored", out.lower())
        self.assertIn("hands-on attention", out.lower())

    def test_apply_failure_preserves_backups(self):
        out = self._run({"tracearr_backup": BACKUP_OK,
                         "tracearr_apply_update": nm.NasError("pull denied")})
        self.assertIn("failed while applying", out)
        self.assertIn("Backups are preserved", out)

    def test_success_and_failure_are_both_recorded_in_joplin(self):
        for mapping in (
            {"tracearr_backup": BACKUP_OK, "tracearr_apply_update": {"pulled": "i"},
             "tracearr_verify_update": VERIFY_OK},
            {"tracearr_backup": BACKUP_OK, "tracearr_apply_update": {"pulled": "i"},
             "tracearr_verify_update": VERIFY_BAD,
             "tracearr_rollback": {"verify_after_rollback": VERIFY_OK}},
        ):
            with self.subTest(mapping=sorted(mapping)):
                rec = mock.AsyncMock(return_value="n1")
                async def fake(action, timeout=None, param=None):
                    v = mapping[action]
                    if isinstance(v, Exception):
                        raise v
                    return v
                with mock.patch.object(nm, "run_action", fake), \
                     mock.patch.object(nm, "_record_in_joplin", rec):
                    run(nm._run_approved_update(self._payload(), self._ctx()))
                rec.assert_awaited()


class UpdatePreparePreconditions(unittest.TestCase):
    def test_refuses_when_upstream_unverified(self):
        async def up(asset):
            return {"upstream_check_status": "unavailable", "update_available": None,
                    "latest_stable_version": None, "latest_stable_digest": None,
                    "release_source": None, "notes": ["github unreachable"]}
        async def act(action, timeout=None, param=None):
            return {"images": {}}
        with mock.patch.object(nm, "_upstream_for", up), \
             mock.patch.object(nm, "run_action", act):
            plan, err = run(nm._prepare_update_plan())
        self.assertEqual(plan, {})
        self.assertIn("could not be verified", err)

    def test_refuses_when_already_current(self):
        async def up(asset):
            return {"upstream_check_status": "verified", "update_available": False,
                    "latest_stable_version": "v1.4.27",
                    "latest_stable_digest": "sha256:" + "c" * 64,
                    "release_source": "github:x/y", "notes": []}
        async def act(action, timeout=None, param=None):
            return {"images": {}}
        with mock.patch.object(nm, "_upstream_for", up), \
             mock.patch.object(nm, "run_action", act):
            plan, err = run(nm._prepare_update_plan())
        self.assertIn("nothing to update", err)

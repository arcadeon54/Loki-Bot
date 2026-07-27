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
    def test_only_the_six_actions_exist(self):
        self.assertEqual(sorted(nm.ACTIONS), sorted([
            "container_inventory", "host_status", "tracearr_dependencies",
            "tracearr_recent_logs", "tracearr_status", "tracearr_update_check"]))

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
        with self._patch({"tracearr_update_check": payload}):
            out = json.loads(run(nm._tool_tracearr_update_check({}, boss_ctx())))
        self.assertTrue(out["ok"])
        self.assertTrue(out["digest_matches_registry"])
        self.assertEqual(out["update_policy"], "approval_always")
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

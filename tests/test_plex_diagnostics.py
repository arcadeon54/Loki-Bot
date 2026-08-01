"""
Focused tests for Plex diagnostics added in Pass 3.

Nothing here touches the NAS, Docker, or Plex's real API: run_action and
_plex_http are stubbed with canned payloads. Same pattern as
tests/test_nas_tracearr.py.

Run:  venv/bin/python -m unittest tests.test_plex_diagnostics -v
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


def boss_ctx():
    return ToolContext(user_id=BOSS_ID, user_name="Boss", channel_id="c")


def crew_ctx():
    return ToolContext(user_id=CREW_ID, user_name="Rob", channel_id="c")


def run(coro):
    return asyncio.run(coro)


def _dispatcher_src():
    return open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "nas", "loki-nas-maint")).read()


def _plex_container(state="running", running=True, restarts=0):
    return {"name": "plex", "id_short": "abc123", "image": "plexinc/pms-docker:latest",
            "image_id": "sha256:" + "b" * 64, "state": state, "running": running,
            "health": None, "restart_count": restarts, "exit_code": 0,
            "oom_killed": False, "started_at": "2026-07-30T00:00:00Z",
            "restart_policy": "unless-stopped", "env_var_names": ["PATH"],
            "labels": {}, "networks": {"bridge": {"ip": "172.20.0.5", "aliases": ["plex"]}}}


PLEX_STATUS_OK = {"plex": _plex_container(), "resource_usage": [
    {"name": "plex", "cpu_pct": "5.0%", "mem_usage": "500MiB / 4GiB",
     "mem_pct": "12%", "pids": "20"}]}

PLEX_DEPS_OK = {"plex": {"name": "plex", "state": "running"},
                "mounts": [{"destination": "/media", "type": "bind", "rw": True,
                          "present": True, "avail": "500G", "use_pct": "40%"}],
                "networks": {"bridge": {"ip": "172.20.0.5", "aliases": ["plex"]}}}

PLEX_DEPS_MISSING_MOUNT = {**PLEX_DEPS_OK,
                          "mounts": [{"destination": "/media", "type": "bind",
                                     "rw": True, "present": False}]}

TRANSCODE_IDLE = {"container": "plex", "transcode_processes": [], "transcoding": False}
TRANSCODE_ACTIVE = {"container": "plex",
                    "transcode_processes": [{"cmd": "Plex Transcoder ...", "cpu": "150%"}],
                    "transcoding": True}

SESSION_DIRECT_PLAY = {"MediaContainer": {"Metadata": [{
    "title": "Some Movie", "Player": {"product": "Plex for TV", "platform": "tvOS"},
    "Media": [{"bitrate": 8000, "videoDecision": "directplay",
              "Part": [{"decision": "directplay"}]}],
}]}}

SESSION_TRANSCODE_SW = {"MediaContainer": {"Metadata": [{
    "title": "Some Movie", "Player": {"product": "Plex Web", "platform": "Chrome"},
    "Media": [{"bitrate": 4000, "videoDecision": "transcode",
              "Part": [{"decision": "transcode"}]}],
    "TranscodeSession": {"videoDecision": "transcode", "audioDecision": "transcode",
                         "transcodeHwRequested": False,
                         "transcodeHwFullPipeline": False,
                         "throttled": False, "progress": 12.5, "speed": 1.1},
}]}}

SESSION_TRANSCODE_HW_FALLBACK = {"MediaContainer": {"Metadata": [{
    "title": "Some Movie", "Player": {"product": "Plex Web", "platform": "Chrome"},
    "Media": [{"bitrate": 4000, "videoDecision": "transcode",
              "Part": [{"decision": "transcode"}]}],
    "TranscodeSession": {"videoDecision": "transcode", "audioDecision": "copy",
                         "transcodeHwRequested": True,
                         "transcodeHwFullPipeline": False,
                         "throttled": False, "progress": 5.0, "speed": 0.4},
}]}}

NO_SESSIONS = {"MediaContainer": {"Metadata": []}}


class AssetRegistration(unittest.TestCase):
    def setUp(self):
        self.reg = homelab_assets.load(force=True)

    def test_plex_resolves_via_registered_aliases(self):
        for alias in ("Plex", "ivn plex", "plex media server", "plex server"):
            with self.subTest(alias=alias):
                asset = self.reg.resolve(alias)
                self.assertIsNotNone(asset)
                self.assertEqual(asset["key"], "plex")

    def test_plex_uses_the_nas_dispatcher_executor(self):
        asset = self.reg.get("plex")
        self.assertEqual(asset["executor"], "nas_dispatcher")

    def test_plex_no_longer_hits_the_unmanaged_asset_blocker(self):
        asset, err = hm._resolve_or_error("Plex")
        self.assertIsNotNone(asset)
        self.assertEqual(err, "")
        self.assertNotIn("plex", hm.UNMANAGED_ASSETS)
        self.assertNotIn("ivn plex", hm.UNMANAGED_ASSETS)


class DispatcherPlexActions(unittest.TestCase):
    def test_new_read_only_plex_actions_are_in_the_allowlist(self):
        for a in ("plex_status", "plex_dependencies", "plex_recent_logs",
                 "plex_transcode_processes"):
            self.assertIn(a, nm.ACTIONS)

    def test_plex_restart_is_the_only_write_action_and_uses_docker_start_only(self):
        self.assertIn("plex_restart", nm.WRITE_ACTIONS)
        src = _dispatcher_src()
        block = src[src.index("def act_plex_restart"):src.index("# ── actions")]
        self.assertIn('docker("start", row["ID"])', block)
        for verb in ("restart", "stop", "kill"):
            self.assertNotIn(f'docker("{verb}"', block)

    def test_plex_restart_requires_stopped_state(self):
        src = _dispatcher_src()
        block = src[src.index("def act_plex_restart"):src.index("# ── actions")]
        self.assertIn('app["running"]', block)

    def test_find_plex_does_not_trust_caller_input(self):
        """Discovery is by label/image/name/port, mirroring find_tracearr —
        never by a caller-supplied string."""
        src = _dispatcher_src()
        block = src[src.index("def find_plex"):src.index("def plex_or_die")]
        self.assertNotIn("sys.argv", block)
        self.assertIn("PLEX_PORT", block)


class ToolsAreBossOnly(unittest.TestCase):
    TOOLS = ("_tool_plex_status", "_tool_plex_diagnose", "_tool_plex_sessions",
            "_tool_plex_playback_diagnose")

    def test_reject_non_boss(self):
        for name in self.TOOLS:
            tool = getattr(nm, name)
            with self.subTest(tool=name):
                out = json.loads(run(tool({}, crew_ctx())))
                self.assertFalse(out["ok"])
                self.assertIn("Boss-only", out["error"])


class PlexStatusTool(unittest.TestCase):
    def test_reports_running_state(self):
        with mock.patch.object(nm, "run_action", new=mock.AsyncMock(return_value=PLEX_STATUS_OK)):
            out = json.loads(run(nm._tool_plex_status({}, boss_ctx())))
        self.assertTrue(out["ok"])
        self.assertTrue(out["running"])

    def test_flags_high_restart_count(self):
        payload = {"plex": _plex_container(restarts=25), "resource_usage": []}
        with mock.patch.object(nm, "run_action", new=mock.AsyncMock(return_value=payload)):
            out = json.loads(run(nm._tool_plex_status({}, boss_ctx())))
        self.assertIn("anomaly", out)


class PlexDiagnoseTool(unittest.TestCase):
    def _mock_actions(self, deps=PLEX_DEPS_OK, transcode=TRANSCODE_IDLE, status=PLEX_STATUS_OK):
        async def _fake(action, timeout=45, param=None):
            return {"plex_status": status, "plex_dependencies": deps,
                    "plex_transcode_processes": transcode}[action]
        return _fake

    def test_healthy_verdict(self):
        with mock.patch.object(nm, "run_action", new=self._mock_actions()):
            out = json.loads(run(nm._tool_plex_diagnose({}, boss_ctx())))
        self.assertTrue(out["ok"])
        self.assertIn("healthy", out["verdict"])

    def test_media_mount_failure_is_surfaced(self):
        with mock.patch.object(nm, "run_action",
                              new=self._mock_actions(deps=PLEX_DEPS_MISSING_MOUNT)):
            out = json.loads(run(nm._tool_plex_diagnose({}, boss_ctx())))
        self.assertIn("/media", out["verdict"])
        self.assertEqual(out["missing_mounts"], ["/media"])

    def test_not_running_is_surfaced(self):
        stopped = {"plex": _plex_container(state="exited", running=False), "resource_usage": []}
        with mock.patch.object(nm, "run_action", new=self._mock_actions(status=stopped)):
            out = json.loads(run(nm._tool_plex_diagnose({}, boss_ctx())))
        self.assertIn("not running", out["verdict"])


class PlexTokenRedaction(unittest.TestCase):
    def test_token_env_var_never_appears_in_tool_output(self):
        with mock.patch.object(nm, "PLEX_TOKEN", "super-secret-token-value"):
            with mock.patch.object(nm, "PLEX_URL", "http://192.168.1.63:32400"):
                async def fake_get(*a, **k):
                    raise nm.NasError("could not reach Plex's local API")
                with mock.patch.object(nm, "_plex_http",
                                       new=mock.AsyncMock(side_effect=nm.NasError("boom"))):
                    out = run(nm._tool_plex_sessions({}, boss_ctx()))
        self.assertNotIn("super-secret-token-value", out)

    def test_plex_http_never_returns_the_token(self):
        src_uses_header_only = True  # documents intent; enforced by the assertion below
        import inspect
        src = inspect.getsource(nm._plex_http)
        self.assertIn("X-Plex-Token", src)
        self.assertNotIn("return PLEX_TOKEN", src)
        self.assertNotIn("print(", src)


class PlexSessionsDetection(unittest.TestCase):
    def test_direct_play_detected(self):
        with mock.patch.object(nm, "_plex_http",
                              new=mock.AsyncMock(return_value=SESSION_DIRECT_PLAY)):
            out = json.loads(run(nm._tool_plex_sessions({}, boss_ctx())))
        self.assertTrue(out["ok"])
        self.assertFalse(out["transcoding"])
        self.assertEqual(out["sessions"][0]["decision"], "directplay")

    def test_software_transcode_detected(self):
        with mock.patch.object(nm, "_plex_http",
                              new=mock.AsyncMock(return_value=SESSION_TRANSCODE_SW)):
            out = json.loads(run(nm._tool_plex_sessions({}, boss_ctx())))
        self.assertTrue(out["transcoding"])
        s = out["sessions"][0]
        self.assertEqual(s["decision"], "transcode")
        self.assertFalse(s["transcode_hw_requested"])

    def test_hardware_transcode_requested_and_active(self):
        payload = {"MediaContainer": {"Metadata": [{
            "title": "X", "Player": {"product": "p", "platform": "p"},
            "Media": [{"bitrate": 1, "Part": [{"decision": "transcode"}]}],
            "TranscodeSession": {"transcodeHwRequested": True,
                                 "transcodeHwFullPipeline": True},
        }]}}
        with mock.patch.object(nm, "_plex_http", new=mock.AsyncMock(return_value=payload)):
            out = json.loads(run(nm._tool_plex_sessions({}, boss_ctx())))
        s = out["sessions"][0]
        self.assertTrue(s["transcode_hw_requested"])
        self.assertTrue(s["transcode_hw_full_pipeline"])


class PlaybackDiagnoseClassification(unittest.TestCase):
    def _mock_actions(self, plex=PLEX_STATUS_OK, deps=PLEX_DEPS_OK,
                      disk=None, network=None, transcode=TRANSCODE_IDLE):
        disk = disk or {"filesystems": [{"mount": "/media", "use_pct": "40%"}]}
        network = network or {"link": {"speed_mbps": 1000, "operstate": "up"},
                              "counters": {"rx_errors": 0, "tx_errors": 0,
                                          "rx_dropped": 0, "tx_dropped": 0},
                              "saturation_pct_of_link": 5,
                              "kernel_network_errors": {"lines": []}}
        async def _fake(action, timeout=90, param=None):
            return {"host_status": {}, "plex_status": plex,
                    "plex_dependencies": deps, "disk_status": disk,
                    "network_status": network, "plex_transcode_processes": transcode,
                    "plex_recent_logs": {"tail": ""}}[action]
        return _fake

    def test_container_down_yields_container_or_network_issue(self):
        stopped = {"plex": _plex_container(state="exited", running=False), "resource_usage": []}
        with mock.patch.object(nm, "run_action", new=self._mock_actions(plex=stopped)):
            with mock.patch.object(nm, "_plex_http",
                                   new=mock.AsyncMock(return_value=NO_SESSIONS)):
                out = json.loads(run(nm._tool_plex_playback_diagnose({}, boss_ctx())))
        self.assertEqual(out["conclusion"], "container_or_network_issue")
        self.assertEqual(out["confidence"], "high")

    def test_missing_mount_yields_disk_latency(self):
        with mock.patch.object(nm, "run_action",
                              new=self._mock_actions(deps=PLEX_DEPS_MISSING_MOUNT)):
            with mock.patch.object(nm, "_plex_http",
                                   new=mock.AsyncMock(return_value=NO_SESSIONS)):
                out = json.loads(run(nm._tool_plex_playback_diagnose({}, boss_ctx())))
        self.assertEqual(out["conclusion"], "disk_latency")

    def test_network_errors_yield_network_bottleneck(self):
        bad_net = {"link": {"speed_mbps": 1000, "operstate": "up"},
                  "counters": {"rx_errors": 500, "tx_errors": 0,
                              "rx_dropped": 0, "tx_dropped": 0},
                  "saturation_pct_of_link": 95,
                  "kernel_network_errors": {"lines": []}}
        with mock.patch.object(nm, "run_action", new=self._mock_actions(network=bad_net)):
            with mock.patch.object(nm, "_plex_http",
                                   new=mock.AsyncMock(return_value=NO_SESSIONS)):
                out = json.loads(run(nm._tool_plex_playback_diagnose({}, boss_ctx())))
        self.assertEqual(out["conclusion"], "network_bottleneck")

    def test_software_transcode_session_yields_software_bottleneck(self):
        with mock.patch.object(nm, "run_action", new=self._mock_actions(transcode=TRANSCODE_ACTIVE)):
            with mock.patch.object(nm, "_plex_http",
                                   new=mock.AsyncMock(return_value=SESSION_TRANSCODE_SW)):
                out = json.loads(run(nm._tool_plex_playback_diagnose({}, boss_ctx())))
        self.assertEqual(out["conclusion"], "software_transcode_bottleneck")

    def test_hw_transcode_fallback_yields_hardware_failure_conclusion(self):
        with mock.patch.object(nm, "run_action", new=self._mock_actions(transcode=TRANSCODE_ACTIVE)):
            with mock.patch.object(
                    nm, "_plex_http",
                    new=mock.AsyncMock(return_value=SESSION_TRANSCODE_HW_FALLBACK)):
                out = json.loads(run(nm._tool_plex_playback_diagnose({}, boss_ctx())))
        self.assertEqual(out["conclusion"], "hardware_transcode_failure_or_fallback")

    def test_direct_play_and_no_other_findings_is_client_side_likely(self):
        with mock.patch.object(nm, "run_action", new=self._mock_actions()):
            with mock.patch.object(nm, "_plex_http",
                                   new=mock.AsyncMock(return_value=SESSION_DIRECT_PLAY)):
                out = json.loads(run(nm._tool_plex_playback_diagnose({}, boss_ctx())))
        self.assertEqual(out["conclusion"], "wifi_or_client_side_likely")

    def test_no_evidence_yields_insufficient_evidence(self):
        with mock.patch.object(nm, "run_action", new=self._mock_actions()):
            with mock.patch.object(nm, "_plex_http",
                                   new=mock.AsyncMock(return_value=NO_SESSIONS)):
                out = json.loads(run(nm._tool_plex_playback_diagnose({}, boss_ctx())))
        self.assertEqual(out["conclusion"], "insufficient_evidence")

    def test_never_returns_a_raw_checklist(self):
        """One conclusion + evidence_summary list, never a numbered
        troubleshooting checklist string."""
        with mock.patch.object(nm, "run_action", new=self._mock_actions()):
            with mock.patch.object(nm, "_plex_http",
                                   new=mock.AsyncMock(return_value=NO_SESSIONS)):
                out = json.loads(run(nm._tool_plex_playback_diagnose({}, boss_ctx())))
        self.assertIsInstance(out["evidence_summary"], list)
        self.assertIn(out["conclusion"], nm._PLAYBACK_CONCLUSIONS)


class PlexRestartApprovalGate(unittest.TestCase):
    def test_prepare_refuses_when_already_running(self):
        with mock.patch.object(nm, "run_action",
                              new=mock.AsyncMock(return_value=PLEX_STATUS_OK)):
            payload, summary, err = nm._plex_restart_prepare({}, boss_ctx())
        self.assertIn("already running", err)
        self.assertEqual(payload, {})

    def test_prepare_stages_when_stopped(self):
        stopped = {"plex": _plex_container(state="exited", running=False), "resource_usage": []}
        with mock.patch.object(nm, "run_action", new=mock.AsyncMock(return_value=stopped)):
            payload, summary, err = nm._plex_restart_prepare({}, boss_ctx())
        self.assertEqual(err, "")
        self.assertIn("START Plex", summary)
        self.assertNotIn("docker stop", summary.lower())
        self.assertNotIn("docker restart", summary.lower())
        self.assertNotIn("docker kill", summary.lower())

    def test_prepare_is_boss_only(self):
        payload, summary, err = nm._plex_restart_prepare({}, crew_ctx())
        self.assertIn("Boss-only", err)

    def test_apply_uses_the_dispatcher_write_action(self):
        with mock.patch.object(nm, "run_action",
                              new=mock.AsyncMock(return_value={"started": True,
                                                               "container": "plex",
                                                               "state": "running",
                                                               "running": True})) as ra:
            out = run(nm._run_approved_plex_restart({"container_state": "exited"}, boss_ctx()))
        ra.assert_awaited_with("plex_restart", timeout=45)
        self.assertIn("Plex started", out)


class ToolRegistration(unittest.TestCase):
    EXPECTED_READ_ONLY = ("plex_status", "plex_diagnose", "plex_sessions",
                         "plex_playback_diagnose")

    def test_all_registered(self):
        import tools
        for name in self.EXPECTED_READ_ONLY + ("plex_start",):
            self.assertIn(name, tools.REGISTRY)

    def test_read_only_tools_take_no_parameters_and_are_boss_only(self):
        import tools
        for name in self.EXPECTED_READ_ONLY:
            spec = tools.REGISTRY[name]
            self.assertEqual(spec.parameters.get("properties") or {}, {})
            self.assertEqual(spec.permission, "boss")
            self.assertEqual(spec.action_type, "",
                             f"{name} must be read-only, not consequential")

    def test_plex_start_is_the_only_consequential_plex_tool(self):
        import tools
        spec = tools.REGISTRY["plex_start"]
        self.assertEqual(spec.action_type, "plex_start")
        self.assertIsNotNone(spec.prepare)

    def test_write_action_name_is_not_itself_a_model_callable_tool(self):
        """Mirrors test_nas_tracearr's invariant: the dispatcher-level write
        action name (plex_restart) must never be directly callable — only the
        friendlier, approval-wrapped tool name (plex_start) is."""
        import tools
        self.assertNotIn("plex_restart", tools.REGISTRY)


class NoManualInstructionFallback(unittest.TestCase):
    def test_no_manual_step_language_anywhere_in_new_tool_output(self):
        with mock.patch.object(nm, "run_action",
                              new=mock.AsyncMock(return_value=PLEX_STATUS_OK)):
            out = run(nm._tool_plex_status({}, boss_ctx()))
        low = out.lower()
        for phrase in ("check plex now playing manually", "ssh into the nas",
                      "send me docker output", "run this yourself"):
            self.assertNotIn(phrase, low)


if __name__ == "__main__":
    unittest.main()

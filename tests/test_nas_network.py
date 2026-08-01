"""
Focused tests for the NAS network + disk diagnostics added in Pass 3.

Nothing here touches the NAS or dex247's real network: run_action is stubbed
with canned dispatcher payloads, and no subprocess is spawned. The dispatcher
source is loaded as text for the structural/allowlist assertions, same
pattern as tests/test_nas_tracearr.py.

Run:  venv/bin/python -m unittest tests.test_nas_network -v
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


HEALTHY_NETWORK_STATUS = {
    "interface": "eth0", "address": "192.168.1.63", "gateway": "192.168.1.1",
    "link": {"speed_mbps": 1000, "duplex": "full", "mtu": 1500,
            "operstate": "up", "carrier": 1, "carrier_changes": 1},
    "counters": {"rx_bytes": 1000, "tx_bytes": 1000, "rx_errors": 0,
                "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0},
    "current_throughput": {"rx_mbps": 12.3, "tx_mbps": 4.2, "sample_seconds": 2.0},
    "saturation_pct_of_link": 1.2,
    "reachability": {"gateway": {"host": "192.168.1.1", "reachable": True, "rtt_ms": 0.4},
                     "dex247": {"host": "192.168.1.50", "reachable": True, "rtt_ms": 0.6},
                     "razr": {"host": "192.168.1.70", "reachable": True, "rtt_ms": 1.1}},
    "dns": {"nas_hostname": {"resolved": True, "resolve_ms": 0.2},
           "gateway": {"resolved": True, "resolve_ms": 0.1}},
    "routes": [{"dst": "default", "dev": "eth0", "gateway": "192.168.1.1"}],
    "kernel_network_errors": {"available": True, "lines": []},
    "plex_docker_networks": {},
}

DEGRADED_NETWORK_STATUS = {
    **HEALTHY_NETWORK_STATUS,
    "counters": {**HEALTHY_NETWORK_STATUS["counters"], "rx_errors": 42, "tx_dropped": 9},
    "saturation_pct_of_link": 91.5,
    "kernel_network_errors": {"available": True, "lines": ["eth0: link is down"]},
}

HEALTHY_DISK_STATUS = {
    "filesystems": [{"fs": "/dev/sda1", "type": "ext4", "size": "1T", "used": "500G",
                     "avail": "500G", "use_pct": "50%", "mount": "/volume1"}],
    "device_io": [{"device": "sda", "reads_completed": 100, "writes_completed": 50,
                  "io_in_progress": 0, "io_ms_weighted": 10}],
    "mdraid": None,
    "smart": [{"device": "/dev/sda", "available": True, "healthy": True}],
    "kernel_disk_errors": [],
}


class ToolsAreBossOnly(unittest.TestCase):
    def test_network_and_disk_tools_reject_non_boss(self):
        for tool in (nm._tool_nas_network_status, nm._tool_nas_disk_status,
                    nm._tool_nas_network_speed_test):
            with self.subTest(tool=tool.__name__):
                out = json.loads(run(tool({}, crew_ctx())))
                self.assertFalse(out["ok"])
                self.assertIn("Boss-only", out["error"])

    def test_network_and_disk_tools_accept_boss(self):
        with mock.patch.object(nm, "run_action",
                              new=mock.AsyncMock(return_value=HEALTHY_NETWORK_STATUS)):
            out = json.loads(run(nm._tool_nas_network_status({}, boss_ctx())))
        self.assertTrue(out["ok"])


class DispatcherActionAllowlist(unittest.TestCase):
    def test_new_read_only_actions_are_in_the_allowlist(self):
        for a in ("network_status", "network_tooling_check", "network_speed_test",
                 "disk_status"):
            self.assertIn(a, nm.ACTIONS)

    def test_no_state_changing_verb_added_to_dispatcher(self):
        """No stop/kill/restart/rm/rmi/down/prune verb anywhere, except the
        one narrowly-scoped `docker start` inside act_plex_restart."""
        src = _dispatcher_src()
        forbidden = ("\"stop\"", "\"kill\"", "\"restart\"", "\"rm\"", "\"rmi\"",
                    "\"down\"", "\"prune\"")
        for verb in forbidden:
            self.assertNotIn(verb, src, f"forbidden docker verb {verb} found")

    def test_interface_discovery_does_not_hardcode_a_name(self):
        src = _dispatcher_src()
        self.assertNotIn('"eth0"', src)
        self.assertNotIn("'eth0'", src)
        self.assertIn("_default_route", src)

    def test_iperf3_uses_fixed_path_port_and_one_off_flag(self):
        src = _dispatcher_src()
        self.assertIn('IPERF3 = "/usr/bin/iperf3"', src)
        self.assertIn("IPERF3_PORT = 5201", src)
        self.assertIn('"-1"', src)  # one-off server mode
        self.assertIn("timeout", src.lower())

    def test_new_actions_are_registered_with_their_handlers(self):
        src = _dispatcher_src()
        self.assertIn('"network_status": act_network_status,', src)
        self.assertIn('"disk_status": act_disk_status,', src)
        self.assertIn('"plex_status": act_plex_status,', src)

    def test_new_read_only_actions_reject_any_argument(self):
        """Same contract as the rest of ACTIONS: zero args, enforced centrally
        in main(), not per-action."""
        for name in ("network_status", "disk_status", "plex_status"):
            with self.assertRaises(nm.NasError):
                run(nm.run_action(name, param="unexpected"))


class InterfaceAndLinkParsing(unittest.TestCase):
    def test_link_speed_duplex_mtu_present_in_payload_shape(self):
        link = HEALTHY_NETWORK_STATUS["link"]
        self.assertEqual(link["speed_mbps"], 1000)
        self.assertEqual(link["duplex"], "full")
        self.assertEqual(link["mtu"], 1500)

    def test_classify_network_healthy(self):
        diag = nm._classify_network(HEALTHY_NETWORK_STATUS)
        self.assertTrue(diag["healthy"])
        self.assertEqual(diag["findings"], [])

    def test_classify_network_flags_saturation_and_errors(self):
        diag = nm._classify_network(DEGRADED_NETWORK_STATUS)
        self.assertFalse(diag["healthy"])
        joined = " ".join(diag["findings"])
        self.assertIn("saturation", joined)
        self.assertIn("rx_errors", joined)


class ThroughputSamplingArithmetic(unittest.TestCase):
    def test_expected_capacity_from_link_speed(self):
        self.assertEqual(nm._expected_capacity_mbps(HEALTHY_NETWORK_STATUS), 1000)

    def test_expected_capacity_missing_is_none(self):
        self.assertIsNone(nm._expected_capacity_mbps({"link": {}}))


class SpeedTestClassification(unittest.TestCase):
    def test_healthy_for_gigabit(self):
        self.assertEqual(nm._classify_speed_test(940, 900, 1000),
                         "healthy_for_link_capacity")

    def test_degraded(self):
        self.assertEqual(nm._classify_speed_test(500, 400, 1000), "degraded")

    def test_severely_degraded(self):
        self.assertEqual(nm._classify_speed_test(100, 80, 1000), "severely_degraded")

    def test_inconclusive_when_no_data(self):
        self.assertEqual(nm._classify_speed_test(None, None, 1000), "test_inconclusive")

    def test_inconclusive_when_no_expected_capacity(self):
        self.assertEqual(nm._classify_speed_test(500, 500, None), "test_inconclusive")


class NoCallerControlledSpeedTestInputs(unittest.TestCase):
    def test_tool_takes_no_parameters(self):
        import tools
        props = tools.REGISTRY["nas_network_speed_test"].parameters.get("properties") or {}
        self.assertEqual(props, {})

    def test_missing_iperf3_on_dex247_degrades_gracefully(self):
        with mock.patch.object(nm, "IPERF3_LOCAL", None):
            out = json.loads(run(nm._tool_nas_network_speed_test({}, boss_ctx())))
        self.assertFalse(out["ok"])
        self.assertIn("not installed", out["error"])

    def test_missing_iperf3_on_nas_is_reported_not_run_anyway(self):
        with mock.patch.object(nm, "IPERF3_LOCAL", "/usr/bin/iperf3"):
            with mock.patch.object(
                    nm, "run_action",
                    new=mock.AsyncMock(return_value={"iperf3_installed": False})):
                out = json.loads(run(nm._tool_nas_network_speed_test({}, boss_ctx())))
        self.assertFalse(out["ok"])
        self.assertIn("not installed on the NAS", out["error"])


class IperfServerLifecycle(unittest.TestCase):
    """The dispatcher-side server action always exits: -1 (one-off) plus a
    fixed `timeout` wrapper. No persistent listener is ever left behind."""

    def test_server_uses_one_off_flag_and_fixed_timeout_in_source(self):
        src = _dispatcher_src()
        block = src[src.index("def act_network_speed_test"):
                    src.index("def act_disk_status")]
        self.assertIn('"-1"', block)
        self.assertIn("IPERF3_SERVER_TIMEOUT", block)
        self.assertIn('"/usr/bin/timeout"', block)

    def test_server_binds_only_the_discovered_lan_address(self):
        src = _dispatcher_src()
        block = src[src.index("def act_network_speed_test"):
                    src.index("def act_disk_status")]
        self.assertNotIn('"0.0.0.0"', block)
        self.assertIn('"-B", addr', block)


class DiskDiagnostics(unittest.TestCase):
    def test_disk_status_boss_only(self):
        with mock.patch.object(nm, "run_action",
                              new=mock.AsyncMock(return_value=HEALTHY_DISK_STATUS)):
            out = json.loads(run(nm._tool_nas_disk_status({}, boss_ctx())))
        self.assertTrue(out["ok"])
        self.assertTrue(out["healthy"])

    def test_disk_status_flags_near_full_filesystem(self):
        payload = {**HEALTHY_DISK_STATUS,
                  "filesystems": [{"mount": "/volume2", "use_pct": "95%"}]}
        with mock.patch.object(nm, "run_action", new=mock.AsyncMock(return_value=payload)):
            out = json.loads(run(nm._tool_nas_disk_status({}, boss_ctx())))
        self.assertFalse(out["healthy"])
        self.assertTrue(any("95%" in f for f in out["findings"]))

    def test_no_write_or_benchmark_verb_in_disk_action(self):
        src = _dispatcher_src()
        block = src[src.index("def act_disk_status"):src.index("def find_plex")]
        self.assertNotIn("dd if=", block)
        self.assertNotIn("open(", block.replace("open(f", ""))  # no new file writes


class ToolRegistration(unittest.TestCase):
    EXPECTED = ("nas_network_status", "nas_network_speed_test", "nas_disk_status")

    def test_all_registered(self):
        import tools
        for name in self.EXPECTED:
            self.assertIn(name, tools.REGISTRY)

    def test_no_free_text_parameters(self):
        import tools
        for name in self.EXPECTED:
            props = tools.REGISTRY[name].parameters.get("properties") or {}
            self.assertEqual(props, {})

    def test_boss_permission(self):
        import tools
        for name in self.EXPECTED:
            self.assertEqual(tools.REGISTRY[name].permission, "boss")

    def test_all_read_only_read_only_action_type(self):
        import tools
        for name in self.EXPECTED:
            self.assertEqual(tools.REGISTRY[name].action_type, "")


class NoManualInstructionFallback(unittest.TestCase):
    def test_no_manual_step_language_in_responses(self):
        with mock.patch.object(nm, "run_action",
                              new=mock.AsyncMock(return_value=HEALTHY_NETWORK_STATUS)):
            out = run(nm._tool_nas_network_status({}, boss_ctx()))
        low = out.lower()
        for phrase in ("run iperf yourself", "ssh into the nas", "copy a file",
                      "send me docker output", "check plex now playing manually"):
            self.assertNotIn(phrase, low)


if __name__ == "__main__":
    unittest.main()

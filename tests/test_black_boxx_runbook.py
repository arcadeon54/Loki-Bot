"""
Focused tests for the BLACK-BOXX diagnostic/repair decision logic.

The production failure this suite exists to prevent: the monitor reported
BLACK-BOXX with twelve subsystems "failing" simultaneously. They were not
twelve faults — one unit (black-boxx-ap.service) owns the whole stack (wg-ap,
hostapd, dnsmasq, marking, table, rule, NAT), and when it dies every
downstream probe fails as a SYMPTOM. A flat list of twelve names told the Boss
nothing and pushed an unknown-condition escalation to Hermes.

Nothing here touches the real AP, WireGuard, iptables or systemd: `ops` is a
scripted fake whose command table is keyed exactly like the policy allowlist,
so every decision is exercised without a single real command.

Run:  venv/bin/python -m unittest tests.test_black_boxx_runbook -v
"""

import asyncio
import os
import tempfile
import time
import unittest

BOSS_ID = "111111111111111111"
os.environ.setdefault("OWNER_USER_ID", BOSS_ID)
os.environ.setdefault("CREW_USER_IDS", "222222222222222222")
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ.setdefault("HOMELAB_DB_PATH", _tmp.name)

from maintenance_runbooks import black_boxx_connectivity as bb


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


ASSET = {
    "key": "black-boxx",
    "display_name": "BLACK-BOXX",
    "network": {
        "wireless_interface": "wlp2s0", "vpn_interface": "wg-ap",
        "ap_address": "192.168.10.1", "client_subnet": "192.168.10.0/24",
        "routing_mark": 100, "routing_table": 100, "rule_priority": 100,
        "dhcp_leases_file": "/var/lib/misc/dnsmasq.leases",
        "hostapd_conf": "/etc/hostapd/black-boxx.conf",
        "dnsmasq_conf": "/etc/dnsmasq.d/black-boxx-ap.conf",
    },
    "systemd": {"ap_unit": "black-boxx-ap.service"},
}

FRESH_HS = str(int(time.time()) - 30)

# A fully healthy host, keyed the way ops.run() is called.
HEALTHY = {
    ("systemctl_is_active", "black-boxx-ap.service"): (0, "active"),
    ("ip_addr", "wlp2s0"): (0, "wlp2s0 UP 192.168.10.1/24"),
    ("ip_addr", "wg-ap"): (0, "wg-ap UP 10.9.0.2/32"),
    ("pgrep_hostapd", None): (0, "1234 hostapd /etc/hostapd/black-boxx.conf"),
    ("pgrep_dnsmasq", None): (0, "1235 dnsmasq /etc/dnsmasq.d/black-boxx-ap.conf"),
    ("wg_handshake", "wg-ap"): (0, f"peerkey {FRESH_HS}"),
    ("wg_transfer", "wg-ap"): (0, "peerkey 12345 67890"),
    ("ip_forward", None): (0, "1"),
    ("iptables_mangle", None): (0, "-A PREROUTING -i wlp2s0 -j MARK --set-xmark 0x64"),
    ("ip_route_table", None): (0, "default dev wg-ap scope link"),
    ("ip_rule_show", None): (0, "100:\tfrom all fwmark 0x64 lookup 100"),
    ("iptables_nat", None): (0, "-A POSTROUTING -s 192.168.10.0/24 -o wg-ap -j MASQUERADE"),
    ("ip_route_get_marked", None): (0, "1.1.1.1 dev wg-ap src 10.9.0.2"),
    ("ping_iface", "wg-ap"): (0, "1 packets transmitted, 1 received"),
    # Repair-class commands succeed at the command level; whether the
    # repair WORKED is decided by the verification re-reads, not by rc.
    ("ip_rule_add_fwmark", None): (0, ""),
    ("ip_rule_del_fwmark", None): (0, ""),
    ("systemctl_restart_unit", "black-boxx-ap.service"): (0, ""),
}


class FakeOps:
    """Scripted executor. `broken` makes every command raise, standing in for
    a dead transport; `missing` makes a single command unrunnable."""

    def __init__(self, table=None, allow_repairs=False, broken=False,
                 missing=(), attempted=()):
        self.table = dict(HEALTHY if table is None else table)
        self.allow_repairs = allow_repairs
        self.auto_repair_allowed = allow_repairs
        self.broken = broken
        self.missing = set(missing)
        self.commands_run = []
        self._attempted = set(attempted)
        self.recorded = []
        self.dns_ok = True

    def _key(self, name, params):
        for k in ("unit", "iface"):
            if k in params:
                return (name, params[k])
        return (name, None)

    async def run(self, name, **params):
        if self.broken:
            raise OSError("executor unavailable: [Errno 2] no such file: 'ip'")
        if name in self.missing:
            raise PermissionError(f"repair command '{name}' refused in read-only mode")
        self.commands_run.append({"name": name, "params": params})
        return self.table.get(self._key(name, params), (1, ""))

    async def lease_meta(self, path):
        return {"ok": True, "count": 2, "newest_age_secs": 60}

    async def dns_check(self, host):
        return self.dns_ok

    async def attempted(self, action, target):
        return (action, target) in self._attempted

    async def record_attempt(self, action, target):
        self.recorded.append((action, target))


def check(result, name):
    return next((c for c in result["checks"] if c["name"] == name), None)


def names_with_status(result, status):
    return [c["name"] for c in result["checks"] if c.get("status") == status]


# ── 1. Healthy ─────────────────────────────────────────────────────────────
class HealthySystem(unittest.TestCase):
    def test_healthy_needs_no_repair_and_no_escalation(self):
        r = run(bb.run(ASSET, FakeOps()))
        self.assertTrue(r["healthy"], [c for c in r["checks"] if not c["ok"]])
        self.assertIsNone(r["repair"])
        self.assertFalse(r["escalate"])
        self.assertEqual(check(r, "diagnostic_transport")["status"], bb.OK)


# ── 10. Executor unavailable (must NOT become twelve faults) ───────────────
class DiagnosticTransportFailure(unittest.TestCase):
    def test_broken_executor_reports_transport_not_dead_subsystems(self):
        r = run(bb.run(ASSET, FakeOps(broken=True)))
        self.assertFalse(r["healthy"])
        t = check(r, "diagnostic_transport")
        self.assertEqual(t["status"], bb.UNAVAILABLE)
        self.assertEqual(len(r["checks"]), 1,
                         "a dead executor must not manufacture subsystem checks")
        self.assertIn("UNKNOWN", r["diagnosis"])
        self.assertIn("executor", r["diagnosis"].lower())

    def test_transport_failure_does_not_escalate_to_hermes(self):
        """Loki's own broken executor is not a BLACK-BOXX incident for Hermes."""
        r = run(bb.run(ASSET, FakeOps(broken=True)))
        self.assertFalse(r["escalate"])
        self.assertIsNone(r["repair"])

    def test_permission_refusal_is_reported_as_unavailable_not_as_a_fault(self):
        """A refused/unrunnable inspection command means Loki could not look,
        which is not the same as looking and finding the AP dead."""
        ops = FakeOps(missing={"systemctl_is_active"})
        r = run(bb.run(ASSET, ops))
        t = check(r, "diagnostic_transport")
        self.assertEqual(t["status"], bb.UNAVAILABLE)
        self.assertIn("PermissionError", t["detail"])
        self.assertFalse(r["escalate"])
        self.assertIsNone(check(r, "ap_service"),
                          "no subsystem verdict may be invented from a refusal")

    def test_probe_costs_no_extra_command(self):
        """The transport probe doubles as the AP-unit read, so it neither adds
        a command nor consumes another check's scripted output."""
        ops = FakeOps()
        run(bb.run(ASSET, ops))
        self.assertEqual(
            [c["name"] for c in ops.commands_run].count("systemctl_is_active"), 1)


# ── The shared-prerequisite case: the real production failure ──────────────
class ApUnitDownIsOneFault(unittest.TestCase):
    def _ops(self, **kw):
        t = dict(HEALTHY)
        t[("systemctl_is_active", "black-boxx-ap.service")] = (3, "failed")
        return FakeOps(t, **kw)

    def test_downstream_checks_are_skipped_not_failed(self):
        r = run(bb.run(ASSET, self._ops()))
        self.assertEqual(check(r, "ap_service")["status"], bb.FAILED)
        skipped = names_with_status(r, bb.SKIPPED)
        for n in ("ap_interface", "vpn_interface", "policy_rule",
                  "nat_masquerade", "tunnel_internet"):
            self.assertIn(n, skipped)
        self.assertEqual(names_with_status(r, bb.FAILED), ["ap_service"],
                         "only the root cause may be reported as a real failure")

    def test_diagnosis_names_one_root_cause(self):
        r = run(bb.run(ASSET, self._ops()))
        self.assertIn("Root cause", r["diagnosis"])
        self.assertIn("black-boxx-ap.service", r["diagnosis"])
        self.assertIn("SYMPTOM", r["diagnosis"])

    def test_offers_the_registered_restart_repair(self):
        r = run(bb.run(ASSET, self._ops()))
        self.assertEqual(r["repair"]["action"], "restart_stateless_service")
        self.assertEqual(r["repair"]["commands"][0]["params"]["unit"],
                         "black-boxx-ap.service")

    def test_read_only_mode_proposes_but_never_acts(self):
        ops = self._ops(allow_repairs=False)
        r = run(bb.run(ASSET, ops))
        self.assertIsNone(r["repair_result"])
        self.assertNotIn("systemctl_restart_unit",
                         [c["name"] for c in ops.commands_run])

    def test_successful_restart_verifies_readiness_and_does_not_escalate(self):
        ops = self._ops(allow_repairs=True)
        # After the restart the unit and interfaces come up.
        real_run = ops.run
        async def run_then_heal(name, **params):
            out = await real_run(name, **params)
            if name == "systemctl_restart_unit":
                ops.table[("systemctl_is_active", "black-boxx-ap.service")] = (0, "active")
            return out
        ops.run = run_then_heal
        r = run(bb.run(ASSET, ops))
        self.assertTrue(r["repair_result"]["ok"])
        self.assertTrue(r["repair_result"]["verified"])
        self.assertFalse(r["escalate"], "a known safe repair must not need Hermes")

    def test_restart_that_does_not_come_up_escalates_once(self):
        ops = self._ops(allow_repairs=True)
        bb.READY_DEADLINE_SECS, real = 0, bb.READY_DEADLINE_SECS
        try:
            r = run(bb.run(ASSET, ops))
        finally:
            bb.READY_DEADLINE_SECS = real
        self.assertFalse(r["repair_result"]["ok"])
        self.assertTrue(r["escalate"])

    def test_second_failure_after_a_recent_restart_does_not_loop(self):
        ops = self._ops(allow_repairs=True,
                        attempted={("restart_stateless_service",
                                    "black-boxx-ap.service")})
        r = run(bb.run(ASSET, ops))
        self.assertIsNone(r["repair_result"], "must not restart twice")
        self.assertTrue(r["escalate"])
        self.assertNotIn("systemctl_restart_unit",
                         [c["name"] for c in ops.commands_run])


# ── 2. The known policy-rule condition ─────────────────────────────────────
class MissingPolicyRuleOnly(unittest.TestCase):
    def _ops(self, **kw):
        t = dict(HEALTHY)
        t[("ip_rule_show", None)] = (0, "32766:\tfrom all lookup main")
        t[("ip_route_get_marked", None)] = (0, "1.1.1.1 via 192.168.1.1 dev eth0")
        return FakeOps(t, **kw)

    def test_recognised_as_the_known_repairable_condition(self):
        r = run(bb.run(ASSET, self._ops()))
        self.assertEqual(r["repair"]["action"], "restore_blackboxx_ip_rule")
        self.assertIn("Known failure", r["diagnosis"])
        self.assertFalse(check(r, "policy_rule")["ok"])

    def test_repair_is_verified_and_needs_no_hermes(self):
        ops = self._ops(allow_repairs=True)
        real_run = ops.run
        async def run_then_heal(name, **params):
            out = await real_run(name, **params)
            if name == "ip_rule_add_fwmark":
                ops.table[("ip_rule_show", None)] = (0, "100:\tfrom all fwmark 0x64 lookup 100")
                ops.table[("ip_route_get_marked", None)] = (0, "1.1.1.1 dev wg-ap")
            return out
        ops.run = run_then_heal
        r = run(bb.run(ASSET, ops))
        self.assertTrue(r["repair_result"]["ok"])
        self.assertTrue(r["repair_result"]["verified"])
        self.assertFalse(r["escalate"])

    def test_repair_is_idempotent_when_the_rule_is_already_back(self):
        ops = FakeOps(allow_repairs=True)   # healthy: rule present
        out = run(bb._apply_rule_repair(ops, {"commands": [
            {"name": "ip_rule_add_fwmark", "params": {}}]}, 100, 100, "wg-ap"))
        self.assertTrue(out["ok"])
        self.assertIn("already present", out["steps"][0])
        self.assertNotIn("ip_rule_add_fwmark",
                         [c["name"] for c in ops.commands_run])

    def test_failed_verification_rolls_back(self):
        ops = self._ops(allow_repairs=True)   # rule never comes back
        r = run(bb.run(ASSET, ops))
        self.assertFalse(r["repair_result"]["ok"])
        self.assertTrue(r["repair_result"].get("rolled_back"))
        self.assertIn("ip_rule_del_fwmark",
                      [c["name"] for c in ops.commands_run])


# ── 3-9, 11. Individual and multiple genuine failures ──────────────────────
class IndividualFailures(unittest.TestCase):
    """With the AP unit ACTIVE, a single dead component must be reported as
    itself — not swept into the shared-prerequisite path."""

    def _ops_without(self, key, value=(1, "")):
        t = dict(HEALTHY)
        t[key] = value
        return FakeOps(t)

    def test_hostapd_stopped(self):
        r = run(bb.run(ASSET, self._ops_without(("pgrep_hostapd", None))))
        self.assertFalse(check(r, "hostapd_process")["ok"])
        self.assertEqual(check(r, "hostapd_process")["status"], bb.FAILED)
        self.assertTrue(r["escalate"])
        self.assertIn("hostapd_process", r["diagnosis"])

    def test_dnsmasq_stopped(self):
        r = run(bb.run(ASSET, self._ops_without(("pgrep_dnsmasq", None))))
        self.assertFalse(check(r, "dnsmasq_process")["ok"])
        self.assertIn("dnsmasq_process", r["diagnosis"])

    def test_wg_ap_down(self):
        r = run(bb.run(ASSET, self._ops_without(("ip_addr", "wg-ap"))))
        self.assertFalse(check(r, "vpn_interface")["ok"])
        self.assertIsNone(r["repair"], "no rule repair without a healthy tunnel")
        self.assertTrue(r["escalate"])

    def test_stale_handshake(self):
        stale = str(int(time.time()) - 4000)
        r = run(bb.run(ASSET, self._ops_without(("wg_handshake", "wg-ap"),
                                                (0, f"peerkey {stale}"))))
        self.assertFalse(check(r, "vpn_handshake")["ok"])
        self.assertIn("ago", check(r, "vpn_handshake")["detail"])

    def test_missing_handshake_entirely(self):
        r = run(bb.run(ASSET, self._ops_without(("wg_handshake", "wg-ap"),
                                                (0, "peerkey 0"))))
        self.assertFalse(check(r, "vpn_handshake")["ok"])
        self.assertIn("no completed handshake", check(r, "vpn_handshake")["detail"])

    def test_packet_marking_missing(self):
        r = run(bb.run(ASSET, self._ops_without(("iptables_mangle", None),
                                                (0, "-A PREROUTING -j ACCEPT"))))
        self.assertFalse(check(r, "packet_mark")["ok"])
        self.assertIsNone(r["repair"], "rule repair requires marking present")

    def test_routing_table_missing(self):
        r = run(bb.run(ASSET, self._ops_without(("ip_route_table", None),
                                                (2, "FIB table does not exist"))))
        self.assertFalse(check(r, "routing_table")["ok"])
        self.assertIsNone(r["repair"])

    def test_nat_missing(self):
        r = run(bb.run(ASSET, self._ops_without(("iptables_nat", None),
                                                (0, "-A POSTROUTING -j ACCEPT"))))
        self.assertFalse(check(r, "nat_masquerade")["ok"])
        self.assertIsNone(r["repair"])

    def test_multiple_genuine_failures_name_the_earliest_root(self):
        t = dict(HEALTHY)
        t[("ip_addr", "wg-ap")] = (1, "")
        t[("iptables_nat", None)] = (0, "-A POSTROUTING -j ACCEPT")
        t[("ping_iface", "wg-ap")] = (1, "unreachable")
        r = run(bb.run(ASSET, FakeOps(t)))
        self.assertIn("vpn_interface", r["diagnosis"])
        self.assertIn("downstream", r["diagnosis"])
        self.assertTrue(r["escalate"])
        # Still one incident's worth of information, not a bare name dump.
        self.assertIn("Earliest failure", r["diagnosis"])


class NoAssetSystemdSectionStillWorks(unittest.TestCase):
    """Assets without an ap_unit declared must keep the old behaviour."""

    def test_runs_all_checks_without_an_ap_unit(self):
        asset = {**ASSET, "systemd": {}}
        r = run(bb.run(asset, FakeOps()))
        self.assertTrue(r["healthy"])
        self.assertIsNone(check(r, "ap_service"))


class BootOwnershipConflict(unittest.TestCase):
    """The boot-time race that left the AP dead after a reboot: wg-quick@wg-ap
    was still enabled alongside black-boxx-ap.service, both ran `ip link add
    wg-ap`, and the loser's cleanup (`ip link delete dev wg-ap`) destroyed the
    winner's interface.

    It is invisible in every check of the RUNNING path, so it must never be
    reported by failing one of those checks — a working AP is not unhealthy."""

    CONFLICT_ASSET = {**ASSET,
                      "systemd": {"ap_unit": "black-boxx-ap.service",
                                  "conflicting_units": ["wg-quick@wg-ap.service"]}}

    def _table(self, enabled_state, rc=0):
        t = dict(HEALTHY)
        t[("systemctl_is_enabled", "wg-quick@wg-ap.service")] = (rc, enabled_state)
        t[("systemctl_disable_unit", "wg-quick@wg-ap.service")] = (0, "")
        t[("systemctl_enable_unit", "wg-quick@wg-ap.service")] = (0, "")
        return t

    def test_enabled_conflict_is_an_advisory_not_a_failed_check(self):
        r = run(bb.run(self.CONFLICT_ASSET, FakeOps(self._table("enabled"))))
        self.assertTrue(r["healthy"], [c for c in r["checks"] if not c["ok"]])
        self.assertEqual(len(r["advisories"]), 1)
        self.assertIn("wg-quick@wg-ap.service", r["advisories"][0])
        # No check was invented to carry it.
        self.assertIsNone(check(r, "boot_ownership"))

    def test_plan_is_approval_tier_disable_only_with_an_enable_rollback(self):
        r = run(bb.run(self.CONFLICT_ASSET, FakeOps(self._table("enabled"))))
        plan = r["repair"]
        self.assertEqual(plan["action"], "service_enable_disable")
        self.assertEqual([c["name"] for c in plan["commands"]],
                         ["systemctl_disable_unit"])
        self.assertEqual(plan["commands"][0]["params"],
                         {"unit": "wg-quick@wg-ap.service"})
        self.assertEqual([c["name"] for c in plan["rollback"]],
                         ["systemctl_enable_unit"])
        # Stopping the unit would run `wg-quick down` and delete the live wg-ap.
        self.assertNotIn("systemctl_restart_unit",
                         [c["name"] for c in plan["commands"]])

    def test_never_auto_applied_even_with_repairs_allowed(self):
        ops = FakeOps(self._table("enabled"), allow_repairs=True)
        r = run(bb.run(self.CONFLICT_ASSET, ops))
        self.assertIsNone(r["repair_result"])
        self.assertNotIn("systemctl_disable_unit",
                         [c["name"] for c in ops.commands_run])

    def test_disabled_conflict_produces_nothing(self):
        # `systemctl is-enabled` exits 1 for "disabled" — the word decides.
        r = run(bb.run(self.CONFLICT_ASSET, FakeOps(self._table("disabled", rc=1))))
        self.assertTrue(r["healthy"])
        self.assertEqual(r["advisories"], [])
        self.assertIsNone(r["repair"])

    def test_unreadable_enablement_invents_no_conflict(self):
        ops = FakeOps(self._table("enabled"), missing=("systemctl_is_enabled",))
        r = run(bb.run(self.CONFLICT_ASSET, ops))
        self.assertTrue(r["healthy"])
        self.assertEqual(r["advisories"], [])

    def test_asset_without_conflicting_units_is_unchanged(self):
        r = run(bb.run(ASSET, FakeOps()))
        self.assertEqual(r["advisories"], [])
        self.assertIsNone(r["repair"])

    def test_live_outage_still_outranks_the_boot_advisory(self):
        t = self._table("enabled")
        t[("ip_rule_show", None)] = (0, "0:\tfrom all lookup local")
        r = run(bb.run(self.CONFLICT_ASSET, FakeOps(t)))
        self.assertFalse(r["healthy"])
        # The AP is down NOW; the known rule repair keeps the plan slot.
        self.assertEqual(r["repair"]["action"], "restore_blackboxx_ip_rule")
        self.assertEqual(len(r["advisories"]), 1)


if __name__ == "__main__":
    unittest.main()

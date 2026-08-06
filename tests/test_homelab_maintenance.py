"""
Focused tests for the homelab maintenance controller.

Everything system-touching is mocked: runbooks run against a MockOps with
scripted command output — no real ip/iptables/docker/wg calls, no network,
and absolutely nothing is restarted or modified. Covers asset aliases,
command allowlisting, authorization, the BLACK-BOXX diagnosis (healthy /
missing-rule / idempotent repair / rollback), Jellyfin stopped-container
recovery and unsafe-failure escalation, the update inventory, the approval
boundary, incident persistence, and redaction.

Run:  venv/bin/python -m unittest tests.test_homelab_maintenance -v
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
os.environ["HOMELAB_DB_PATH"] = _tmp_hm.name

import tools
tools.OWNER_USER_ID = BOSS_ID
tools.CREW_USER_IDS = {CREW_ID}

import homelab_maintenance as hm
import maintenance_policy as policy
from maintenance_runbooks import (black_boxx_connectivity, jellyfin_health,
                                  joplin_sync, loki_interfaces)


def ctx(user_id, channel="tg:424242"):
    return tools.ToolContext(user_id=user_id, user_name="t", channel_id=channel)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


REG = hm.reload_registry()
BB = REG.get("black-boxx")
JF = REG.get("jellyfin")
IM = REG.get("immich")
JP = REG.get("joplin")
LI = REG.get("loki-interfaces")

NOW = int(time.time())

# ── Scripted system output ──────────────────────────────────────────────────
BB_HEALTHY = {
    # black-boxx-ap.service owns the whole stack (wg-ap, hostapd, dnsmasq,
    # marking, table, rule, NAT). The runbook reads it first and treats a dead
    # unit as the single root cause of every downstream check.
    "systemctl_is_active": (0, "active"),
    "ip_addr": lambda p: (0, "wlp2s0 UP 192.168.10.1/24")
        if p["iface"] == "wlp2s0" else (0, "wg-ap UNKNOWN 100.64.145.100/32"),
    "pgrep_hostapd": (0, "7274 hostapd -B /etc/hostapd/black-boxx.conf"),
    "pgrep_dnsmasq": (0, "7135 dnsmasq --conf-file=/etc/dnsmasq.d/black-boxx-ap.conf"),
    "wg_handshake": (0, f"AAAA\t{NOW - 30}"),
    "wg_transfer": (0, "AAAA\t149144078868\t4819529848"),
    "ip_forward": (0, "1"),
    "iptables_mangle": (0, "-A PREROUTING -i wlp2s0 -j MARK --set-xmark 0x64/0xffffffff"),
    "ip_route_table": (0, "default dev wg-ap scope link"),
    "ip_rule_show": (0, "0:\tfrom all lookup local\n"
                        "100:\tfrom all fwmark 0x64 lookup 100\n"
                        "32766:\tfrom all lookup main"),
    "iptables_nat": (0, "-A POSTROUTING -s 192.168.10.0/24 -o wg-ap -j MASQUERADE"),
    "ip_route_get_marked": (0, "1.1.1.1 dev wg-ap table 100 src 100.64.145.100"),
    "ping_iface": (0, "1 packets transmitted, 1 received, 0% packet loss"),
}
RULE_MISSING = (0, "0:\tfrom all lookup local\n32766:\tfrom all lookup main")

JELLY_RUNNING = (0, json.dumps([{"State": {"Status": "running", "ExitCode": 0},
                                 "RestartCount": 0}]))
JELLY_EXITED = (0, json.dumps([{"State": {"Status": "exited", "ExitCode": 143},
                                "RestartCount": 0}]))
DF_OK = (0, "Mounted on Size Avail Use%\n/ 100G 55G 45%")
DF_FULL = (0, "Mounted on Size Avail Use%\n/ 100G 1G 99%")
LOGS_CLEAN = (0, "[10:00:01] INF Main: Jellyfin version 10\n[10:00:02] INF started")
LOGS_MIGRATION = (0, "[10:00:01] ERR Database migration failed: table corrupt")


class MockOps:
    """Mirror of hm.Ops with scripted outputs. Repair-command gating and the
    command audit trail behave exactly like the real facade."""

    def __init__(self, outputs, allow_repairs=False, http=None, paths=None):
        self.outputs = dict(outputs)
        self.allow_repairs = allow_repairs
        self.auto_repair_allowed = allow_repairs
        self.commands_run = []
        self.log_excerpt = ""
        self.http = dict(http or {})
        self.paths = dict(paths or {})
        self.attempts = set()
        self.joplin_ping_ok = True
        self.joplin_sync = {"healthy": True, "detail": "sync fresh"}
        self.iface = {"telegram": {"configured": True, "alive": True}}
        self.iface_restart_ok = True
        self.iface_restarts = []

    redact = staticmethod(hm.redact)

    def _next(self, v, params):
        if callable(v):
            return v(params)
        if isinstance(v, list):
            return v.pop(0) if len(v) > 1 else v[0]
        return v

    async def run(self, name, **params):
        if policy.is_repair_command(name) and not self.allow_repairs:
            raise policy.PolicyError(f"repair command '{name}' refused")
        self.commands_run.append({"name": name, "params": params})
        v = self.outputs.get(name)
        if v is None:
            return 1, f"no scripted output for {name}"
        rc, out = self._next(v, params)
        if name == "docker_logs_tail":
            self.log_excerpt = hm.redact(out)[-2000:]
        return rc, out

    async def http_get(self, url, timeout=8):
        v = self.http.get(url)
        if v is None:
            return 0, "unscripted"
        return self._next(v, None)

    async def path_meta(self, path):
        return self.paths.get(path, {"exists": True, "mode": "0o755",
                                     "uid": 1000, "gid": 1000,
                                     "entries": 4, "empty": False})

    async def lease_meta(self, path):
        return {"ok": True, "count": 2, "newest_age_secs": 60}

    async def dns_check(self, host):
        return True

    async def joplin_ping(self):
        return self.joplin_ping_ok

    async def joplin_sync_health(self):
        return dict(self.joplin_sync)

    async def interface_status(self):
        return None if self.iface is None else {k: dict(v)
                                                for k, v in self.iface.items()}

    async def interface_restart(self, worker):
        if not self.allow_repairs:
            raise policy.PolicyError("interface restart refused")
        self.iface_restarts.append(worker)
        if self.iface_restart_ok:
            self.iface[worker]["alive"] = True
        return self.iface_restart_ok

    async def attempted(self, action, target):
        return (action, target) in self.attempts

    async def record_attempt(self, action, target):
        self.attempts.add((action, target))

    async def sleep(self, secs):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        # Fresh incident DB per test.
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        hm.DB_PATH = self.tmp.name
        hm._conn = None
        hm._db()
        policy.configure(REG.allowed_values())

    def tearDown(self):
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass


# ── Asset aliases ───────────────────────────────────────────────────────────
class AliasTests(Base):
    def test_blackboxx_aliases(self):
        for phrase in ("BLACK-BOXX", "black boxx", "blackboxx", "my AP",
                       "the ap", "Access Point"):
            asset = REG.resolve(phrase)
            self.assertIsNotNone(asset, phrase)
            self.assertEqual(asset["key"], "black-boxx", phrase)

    def test_jellyfin_and_immich_aliases(self):
        self.assertEqual(REG.resolve("jellyfin")["key"], "jellyfin")
        self.assertEqual(REG.resolve("media server")["key"], "jellyfin")
        self.assertEqual(REG.resolve("Immich")["key"], "immich")
        self.assertEqual(REG.resolve("photo server")["key"], "immich")

    def test_unknown_asset_rejected(self):
        self.assertIsNone(REG.resolve("the mainframe"))
        out = json.loads(run(hm._tool_diagnose(
            {"asset": "the mainframe", "symptom": "sad"}, ctx(BOSS_ID))))
        self.assertFalse(out["ok"])


# ── Command allowlisting ────────────────────────────────────────────────────
class AllowlistTests(Base):
    def test_declared_values_build(self):
        argv = policy.build_command("ip_addr", iface="wlp2s0")
        self.assertEqual(argv, ["ip", "-br", "addr", "show", "wlp2s0"])

    def test_unknown_command_rejected(self):
        with self.assertRaises(policy.PolicyError):
            policy.build_command("rm_rf", path="/")

    def test_injection_shape_rejected(self):
        with self.assertRaises(policy.PolicyError):
            policy.build_command("ip_addr", iface="wlp2s0; rm -rf /")

    def test_undeclared_interface_rejected(self):
        with self.assertRaises(policy.PolicyError):
            policy.build_command("ip_addr", iface="eth0")   # valid shape, not in registry

    def test_undeclared_container_rejected(self):
        with self.assertRaises(policy.PolicyError):
            policy.build_command("docker_restart", container="nginx-proxy-manager")

    def test_repair_command_refused_in_readonly_ops(self):
        ops = MockOps(BB_HEALTHY, allow_repairs=False)
        with self.assertRaises(policy.PolicyError):
            run(ops.run("ip_rule_add_fwmark", num=100, num2=100, num3=100))

    def test_http_get_refuses_undeclared_url(self):
        ops = hm.Ops(allow_repairs=False)
        with self.assertRaises(policy.PolicyError):
            run(ops.http_get("http://169.254.169.254/latest/meta-data"))


# ── Authorization ───────────────────────────────────────────────────────────
class AuthzTests(Base):
    def test_all_homelab_tools_are_boss_only(self):
        # The update inventory tool moved to container_updates.py as
        # `container_update_inventory`; the old duplicate is deliberately
        # no longer registered (see homelab_maintenance._register_tools).
        for name in ("homelab_diagnose", "homelab_status",
                     "homelab_incident_status", "homelab_repair",
                     "homelab_runbook_list", "homelab_apply_repair"):
            self.assertEqual(tools.REGISTRY[name].permission, "boss", name)
        self.assertNotIn("container_update_status", tools.REGISTRY)

    def test_crew_execution_denied(self):
        out = run(tools.execute("homelab_diagnose",
                                json.dumps({"asset": "jellyfin", "symptom": "down"}),
                                ctx(CREW_ID)))
        self.assertIn("Permission denied", out)


# ── BLACK-BOXX runbook ──────────────────────────────────────────────────────
class BlackBoxxTests(Base):
    def test_healthy_diagnosis(self):
        ops = MockOps(BB_HEALTHY)
        result = run(black_boxx_connectivity.run(BB, ops))
        self.assertTrue(result["healthy"])
        self.assertIsNone(result["repair"])
        self.assertFalse(result["escalate"])

    def test_missing_rule_detected_and_planned(self):
        outputs = dict(BB_HEALTHY, ip_rule_show=RULE_MISSING)
        ops = MockOps(outputs, allow_repairs=False)
        result = run(black_boxx_connectivity.run(BB, ops))
        self.assertFalse(result["healthy"])
        plan = result["repair"]
        self.assertIsNotNone(plan)
        self.assertEqual(plan["action"], "restore_blackboxx_ip_rule")
        self.assertEqual(plan["commands"][0]["name"], "ip_rule_add_fwmark")
        # read-only mode: nothing was executed
        self.assertIsNone(result["repair_result"])
        ran = [c["name"] for c in ops.commands_run]
        self.assertNotIn("ip_rule_add_fwmark", ran)

    def test_auto_repair_applies_and_verifies(self):
        outputs = dict(
            BB_HEALTHY,
            # diagnosis sees it missing; pre-repair recheck still missing;
            # post-repair verification sees it restored.
            ip_rule_show=[RULE_MISSING, RULE_MISSING, BB_HEALTHY["ip_rule_show"]],
            ip_rule_add_fwmark=(0, ""),
        )
        ops = MockOps(outputs, allow_repairs=True)
        result = run(black_boxx_connectivity.run(BB, ops))
        rr = result["repair_result"]
        self.assertTrue(rr["ok"])
        self.assertTrue(rr["verified"])
        ran = [c["name"] for c in ops.commands_run]
        self.assertEqual(ran.count("ip_rule_add_fwmark"), 1)
        self.assertNotIn("ip_rule_del_fwmark", ran)

    def test_repair_is_idempotent(self):
        # Rule reappears (e.g. restored by hand) before the repair acts:
        outputs = dict(
            BB_HEALTHY,
            ip_rule_show=[RULE_MISSING, BB_HEALTHY["ip_rule_show"]],
            ip_rule_add_fwmark=(0, ""),
        )
        ops = MockOps(outputs, allow_repairs=True)
        result = run(black_boxx_connectivity.run(BB, ops))
        rr = result["repair_result"]
        self.assertTrue(rr["ok"])
        ran = [c["name"] for c in ops.commands_run]
        self.assertNotIn("ip_rule_add_fwmark", ran)   # nothing to do → no add

    def test_failed_verification_rolls_back(self):
        outputs = dict(
            BB_HEALTHY,
            ip_rule_show=RULE_MISSING,          # never comes back
            ip_rule_add_fwmark=(0, ""),
            ip_rule_del_fwmark=(0, ""),
        )
        ops = MockOps(outputs, allow_repairs=True)
        result = run(black_boxx_connectivity.run(BB, ops))
        rr = result["repair_result"]
        self.assertFalse(rr["ok"])
        self.assertTrue(rr.get("rolled_back"))
        ran = [c["name"] for c in ops.commands_run]
        self.assertIn("ip_rule_del_fwmark", ran)
        self.assertTrue(result["escalate"])

    def test_unrelated_failure_escalates_without_repair(self):
        # Tunnel dead: rule present but no handshake → NOT the known condition.
        outputs = dict(BB_HEALTHY, wg_handshake=(0, "AAAA\t0"),
                       ping_iface=(1, "100% packet loss"))
        ops = MockOps(outputs, allow_repairs=True)
        result = run(black_boxx_connectivity.run(BB, ops))
        self.assertFalse(result["healthy"])
        self.assertIsNone(result["repair"])
        self.assertTrue(result["escalate"])


# ── Jellyfin runbook ────────────────────────────────────────────────────────
def _jelly_outputs(inspect, logs=LOGS_CLEAN, df=DF_OK):
    return {"docker_inspect": inspect, "docker_logs_tail": logs, "df_path": df,
            "docker_start": (0, "jellyfin"), "docker_restart": (0, "jellyfin")}


class JellyfinTests(Base):
    LOCAL = JF["health"]["local_url"]
    PUBLIC = JF["health"]["public_url"]

    def test_stopped_container_recovered(self):
        outputs = _jelly_outputs([JELLY_EXITED, JELLY_RUNNING])
        http = {self.LOCAL: [(0, "refused"), (200, "Healthy")],
                self.PUBLIC: (200, "ok")}
        ops = MockOps(outputs, allow_repairs=True, http=http)
        result = run(jellyfin_health.run(JF, ops))
        self.assertEqual(result["repair"]["action"], "restart_stateless_service")
        rr = result["repair_result"]
        self.assertTrue(rr["ok"])
        self.assertTrue(rr["verified"])
        ran = [c["name"] for c in ops.commands_run]
        self.assertIn("docker_start", ran)
        self.assertNotIn("docker_restart", ran)

    def test_unsafe_failure_escalates_untouched(self):
        outputs = _jelly_outputs(JELLY_EXITED, logs=LOGS_MIGRATION)
        http = {self.LOCAL: (0, "refused"), self.PUBLIC: (200, "ok")}
        ops = MockOps(outputs, allow_repairs=True, http=http)
        result = run(jellyfin_health.run(JF, ops))
        self.assertTrue(result["escalate"])
        self.assertIsNone(result["repair"])
        ran = [c["name"] for c in ops.commands_run]
        self.assertNotIn("docker_start", ran)
        self.assertNotIn("docker_restart", ran)

    def test_full_disk_blocks_auto_restart(self):
        outputs = _jelly_outputs(JELLY_EXITED, df=DF_FULL)
        http = {self.LOCAL: (0, "refused"), self.PUBLIC: (200, "ok")}
        ops = MockOps(outputs, allow_repairs=True, http=http)
        result = run(jellyfin_health.run(JF, ops))
        self.assertTrue(result["escalate"])
        self.assertNotIn("docker_start", [c["name"] for c in ops.commands_run])

    def test_unhealthy_restart_only_once(self):
        outputs = _jelly_outputs(JELLY_RUNNING)
        http = {self.LOCAL: (0, "refused"), self.PUBLIC: (200, "ok")}
        ops = MockOps(outputs, allow_repairs=True, http=http)
        ops.attempts.add(("restart_unhealthy_container_once", "jellyfin"))
        result = run(jellyfin_health.run(JF, ops))
        self.assertTrue(result["escalate"])
        self.assertNotIn("docker_restart", [c["name"] for c in ops.commands_run])


# ── Joplin sync runbook ─────────────────────────────────────────────────────
def _joplin_outputs(unit_active=(0, "active")):
    return {"docker_inspect": JELLY_RUNNING,
            "systemctl_is_active": unit_active,
            "systemctl_restart_unit": (0, "")}


class JoplinSyncTests(Base):
    def test_stale_sync_retried_and_verified(self):
        ops = MockOps(_joplin_outputs(), allow_repairs=True)
        ops.joplin_sync = {"healthy": False, "detail": "last sync 5400s ago"}
        result = run(joplin_sync.run(JP, ops))
        self.assertEqual(result["repair"]["action"], "retry_joplin_sync")
        self.assertTrue(result["repair_result"]["ok"])
        ran = [c["name"] for c in ops.commands_run]
        self.assertEqual(ran.count("systemctl_restart_unit"), 1)

    def test_dead_unit_restarted(self):
        ops = MockOps(_joplin_outputs(unit_active=[(3, "inactive"), (0, "active")]),
                      allow_repairs=True)
        result = run(joplin_sync.run(JP, ops))
        self.assertTrue(result["repair_result"]["ok"])

    def test_container_failure_escalates_without_restart(self):
        outputs = _joplin_outputs()
        outputs["docker_inspect"] = JELLY_EXITED
        ops = MockOps(outputs, allow_repairs=True)
        result = run(joplin_sync.run(JP, ops))
        self.assertTrue(result["escalate"])
        self.assertNotIn("systemctl_restart_unit",
                         [c["name"] for c in ops.commands_run])

    def test_retry_only_once_per_window(self):
        ops = MockOps(_joplin_outputs(), allow_repairs=True)
        ops.joplin_sync = {"healthy": False, "detail": "stale"}
        ops.attempts.add(("retry_joplin_sync", "loki-joplin-desktop.service"))
        result = run(joplin_sync.run(JP, ops))
        self.assertTrue(result["escalate"])
        self.assertNotIn("systemctl_restart_unit",
                         [c["name"] for c in ops.commands_run])

    def test_undeclared_unit_rejected_by_allowlist(self):
        argv = policy.build_command("systemctl_restart_unit",
                                    unit="loki-joplin-desktop.service")
        self.assertEqual(argv[-1], "loki-joplin-desktop.service")
        with self.assertRaises(policy.PolicyError):
            policy.build_command("systemctl_restart_unit", unit="loki.service")


# ── Interface worker runbook ────────────────────────────────────────────────
class InterfaceWorkerTests(Base):
    def test_dead_worker_restarted_and_verified(self):
        ops = MockOps({}, allow_repairs=True)
        ops.iface["telegram"]["alive"] = False
        result = run(loki_interfaces.run(LI, ops))
        self.assertEqual(result["repair"]["action"], "restart_interface_worker")
        self.assertTrue(result["repair_result"]["ok"])
        self.assertEqual(ops.iface_restarts, ["telegram"])

    def test_restart_failure_escalates(self):
        ops = MockOps({}, allow_repairs=True)
        ops.iface["telegram"]["alive"] = False
        ops.iface_restart_ok = False
        result = run(loki_interfaces.run(LI, ops))
        self.assertTrue(result["escalate"])
        self.assertFalse(result["repair_result"]["ok"])

    def test_standalone_mode_escalates_without_action(self):
        ops = MockOps({}, allow_repairs=True)
        ops.iface = None                      # hooks unbound (outside the bot)
        result = run(loki_interfaces.run(LI, ops))
        self.assertTrue(result["escalate"])
        self.assertEqual(ops.iface_restarts, [])

    def test_unconfigured_worker_is_healthy_dormant(self):
        ops = MockOps({}, allow_repairs=True)
        ops.iface["telegram"] = {"configured": False, "alive": False}
        result = run(loki_interfaces.run(LI, ops))
        self.assertTrue(result["healthy"])


# ── Hermes handoff ──────────────────────────────────────────────────────────
class HermesHandoffTests(Base):
    def test_unconfigured_records_skip(self):
        orig = hm.HERMES_WORKER_URL, hm.HERMES_WORKER_TOKEN
        hm.HERMES_WORKER_URL, hm.HERMES_WORKER_TOKEN = "", ""
        try:
            out = run(hm.hermes_handoff({"format": hm.BUNDLE_FORMAT}))
        finally:
            hm.HERMES_WORKER_URL, hm.HERMES_WORKER_TOKEN = orig
        self.assertFalse(out["delivered"])
        self.assertIn("not configured", out["reason"])

    def test_configured_posts_bundle(self):
        class FakeResp:
            status = 202

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class FakeSess:
            def __init__(self):
                self.calls = []

            def post(self, url, **kw):
                self.calls.append((url, kw.get("json")))
                return FakeResp()

        fake = FakeSess()

        async def factory():
            return fake

        orig = (hm.HERMES_WORKER_URL, hm.HERMES_WORKER_TOKEN,
                hm._session_factory)
        hm.HERMES_WORKER_URL = "http://razr-1.invalid:9911"
        hm.HERMES_WORKER_TOKEN = "unit-test-token"
        hm._session_factory = factory
        try:
            out = run(hm.hermes_handoff({"format": hm.BUNDLE_FORMAT,
                                         "asset": "jellyfin"}))
        finally:
            (hm.HERMES_WORKER_URL, hm.HERMES_WORKER_TOKEN,
             hm._session_factory) = orig
        self.assertTrue(out["delivered"])
        url, body = fake.calls[0]
        self.assertTrue(url.endswith("/v1/escalations"))
        self.assertEqual(body["format"], hm.BUNDLE_FORMAT)


# ── Update inventory ────────────────────────────────────────────────────────
class UpdateInventoryTests(Base):
    def test_update_detection_and_flags(self):
        image = "lscr.io/linuxserver/jellyfin:latest"
        outputs = {
            "docker_inspect": (0, json.dumps([{
                "Config": {"Image": image,
                           "Labels": {"com.docker.compose.project": "privacyserver"}},
                "HostConfig": {"PortBindings": {"8096/tcp": [{"HostIp": "",
                                                              "HostPort": "8096"}]}},
                "State": {"Status": "running"}}])),
            "docker_image_inspect": (0, json.dumps([{
                "Id": "sha256:aaa111",
                "RepoDigests": [f"{image.split(':')[0]}@sha256:aaa111"],
                "Created": "2026-06-01T00:00:00Z"}])),
            "docker_imagetools_inspect": (0, "Name: x\nDigest:    sha256:bbb222\n"),
        }
        ops = MockOps(outputs)
        entry = run(hm._update_status_for("jellyfin", JF, ops))
        self.assertTrue(entry["update_available"])
        self.assertTrue(entry["approval_required"])
        self.assertFalse(entry["backup_required"])       # stateless
        self.assertEqual(entry["exposure"], "internet (reverse proxy)")
        self.assertIn("approval", entry["recommendation"])

    def test_up_to_date_and_stateful_backup_flag(self):
        image = "ghcr.io/immich-app/immich-server:release"
        outputs = {
            "docker_inspect": (0, json.dumps([{
                "Config": {"Image": image, "Labels": {}},
                "HostConfig": {"PortBindings": {}},
                "State": {"Status": "running"}}])),
            "docker_image_inspect": (0, json.dumps([{
                "Id": "sha256:ccc333",
                "RepoDigests": [f"{image.split(':')[0]}@sha256:ccc333"],
                "Created": "2026-07-01T00:00:00Z"}])),
            "docker_imagetools_inspect": (0, "Digest: sha256:ccc333\n"),
        }
        ops = MockOps(outputs)
        entry = run(hm._update_status_for("immich_server", IM, ops))
        self.assertFalse(entry["update_available"])
        self.assertTrue(entry["backup_required"])        # stateful
        self.assertEqual(entry["recommendation"], "up to date")

    def test_unknown_service_rejected(self):
        out = json.loads(run(hm._tool_update_status(
            {"service": "watchtower"}, ctx(BOSS_ID))))
        self.assertFalse(out["ok"])


# ── Approval boundary ───────────────────────────────────────────────────────
class ApprovalBoundaryTests(Base):
    def _incident_with_plan(self, action):
        iid = "hi_test1"
        plan = {"action": action, "description": "restart immich stack",
                "commands": [{"name": "docker_restart",
                              "params": {"container": "immich_server"}}],
                "rollback": []}
        now = time.time()
        hm._db().execute(
            "INSERT INTO incidents (incident_id, task_id, asset, symptom, status,"
            " created_at, updated_at, repair_json) VALUES (?,?,?,?,?,?,?,?)",
            (iid, "lt_task1", "immich", "down", "awaiting_approval", now, now,
             json.dumps(plan)))
        hm._db().commit()
        return iid, plan

    def test_approval_tier_routes_to_draft_gate(self):
        iid, plan = self._incident_with_plan("service_enable_disable")
        staged = []

        async def fake_intercept(spec, args, c):
            staged.append((spec.name, args))
            return "📝 Draft prepared"

        orig = tools._approval_intercept
        tools.set_approval_intercept(fake_intercept)
        try:
            out = run(hm._tool_repair({"task_id": "lt_task1"}, ctx(BOSS_ID)))
        finally:
            tools.set_approval_intercept(orig)
        self.assertIn("Draft prepared", out)
        self.assertEqual(staged[0][0], "homelab_apply_repair")
        self.assertEqual(staged[0][1]["plan_hash"], hm._plan_hash(plan))

    def test_manual_tier_refused(self):
        iid, _ = self._incident_with_plan("database_repair")
        out = json.loads(run(hm._tool_repair({"task_id": "lt_task1"}, ctx(BOSS_ID))))
        self.assertFalse(out["ok"])
        self.assertIn("MANUAL", out["error"])

    def test_apply_repair_rejects_tampered_hash(self):
        iid, _ = self._incident_with_plan("service_enable_disable")
        payload, summary, err = hm._apply_repair_prepare(
            {"incident_id": iid, "plan_hash": "deadbeef"}, ctx(BOSS_ID))
        self.assertTrue(err)

    def test_immich_updates_always_need_approval(self):
        self.assertEqual(IM.get("update_policy"), "approval_always")
        self.assertEqual(policy.action_tier("immich_update"), policy.APPROVAL)
        self.assertEqual(policy.action_tier("container_image_update"),
                         policy.APPROVAL)


# ── Incident persistence + redaction ───────────────────────────────────────
class IncidentPersistenceTests(Base):
    def test_incident_persists_and_survives_reopen(self):
        import task_supervisor as ts
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        ts.DB_PATH = tmp.name
        ts._conn = None
        ts._started = False
        ts._running.clear()
        ts._send = None
        ts._db()

        canned = {"checks": [{"name": "policy_rule", "ok": False,
                              "detail": "fwmark rule MISSING"}],
                  "healthy": False, "escalate": True, "repair": None,
                  "repair_result": None,
                  "diagnosis": "known failure", "runbook": "black_boxx_connectivity"}

        async def fake_run_runbook(asset, allow_repairs):
            return canned, MockOps({})

        orig = hm.run_runbook
        hm.run_runbook = fake_run_runbook
        try:
            tt = ts._TYPES["homelab_incident"]
            tid = ts.submit(tt, ctx(BOSS_ID), {"asset": "black-boxx",
                                               "symptom": "no internet"})
            handle = ts.TaskHandle(tid, tt.capabilities)
            ts._update(tid, status="running", started_at=1, heartbeat_at=1)
            run(ts._run(tid, tt, handle))
        finally:
            hm.run_runbook = orig
            os.unlink(tmp.name)

        inc = hm.incident_for_task(tid)
        self.assertIsNotNone(inc)
        self.assertEqual(inc["asset"], "black-boxx")
        self.assertEqual(inc["status"], "escalated")
        bundle = json.loads(inc["bundle_json"])
        self.assertEqual(bundle["format"], hm.BUNDLE_FORMAT)
        # Hermes is not configured in tests: the skip is recorded, not raised.
        delivery = json.loads(inc["hermes_json"])
        self.assertFalse(delivery["delivered"])
        # Correlation: the task row carries conversation/message linkage.
        row = ts.get_task(tid)
        self.assertEqual(row["conversation_id"], "tg:424242")
        self.assertEqual(row["priority"], 10)
        # Survives a reopen (fresh connection, same file).
        hm._conn = None
        self.assertIsNotNone(hm.get_incident(inc["incident_id"]))

    def test_bundle_is_redacted_and_bounded(self):
        result = {
            "runbook": "jellyfin_health",
            "checks": [{"name": "log_scan", "ok": False,
                        "detail": "client aa:bb:cc:dd:ee:11 password: hunter2 "
                                  + "x" * 900}],
        }
        ops = MockOps({})
        ops.log_excerpt = ("wg key D2Tx/zEgTy2uoH2HLp5EBIFyLkHGEhkhLMYYedpcUFw= "
                           "api_key=supersecret123 mac aa:bb:cc:dd:ee:ff")
        bundle = hm.build_escalation_bundle(
            JF, "it is down and the password: hunter2 was in the message",
            result, ops, [])
        blob = json.dumps(bundle)
        self.assertNotIn("hunter2", blob)
        self.assertNotIn("supersecret123", blob)
        self.assertNotIn("aa:bb:cc:dd:ee:ff", blob)
        self.assertNotIn("D2Tx/zEgTy2uoH2HLp5EBIFyLkHGEhkhLMYYedpcUFw=", blob)
        self.assertLessEqual(len(bundle["logs"]), hm.LOG_EXCERPT_CAP)
        for c in bundle["checks"]:
            self.assertLessEqual(len(c["detail"]), 300)
        # The Hermes contract fields are all present.
        for field in ("format", "asset", "symptom", "runbook", "checks",
                      "service_state", "logs", "previous_repair_attempts",
                      "allowed_actions", "prohibited_actions"):
            self.assertIn(field, bundle)
        self.assertIn("database_repair", bundle["prohibited_actions"])


if __name__ == "__main__":
    unittest.main()

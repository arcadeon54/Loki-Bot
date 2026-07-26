"""
Focused tests for the asset lifecycle / decommission registry and the
lifecycle-aware container sweep.

Everything system-touching is mocked: Docker is a scripted MockOps, the
reverse-proxy scan and DNS lookup are canned, and no command is ever spawned.
Nothing here touches ivn-site or any other production resource — the tests run
against a temporary SQLite file and a temporary mirror/archive directory.

Covers the production failure directly: the Boss decommissions an asset, the
state persists across a restart, the sweep sees the container stopped, and no
incident and no Hermes job are produced — while a genuinely managed container
that is down still raises a real incident.

Run:  venv/bin/python -m unittest tests.test_homelab_lifecycle -v
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
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["HOMELAB_DB_PATH"] = _tmp_db.name
_tmp_dir = tempfile.mkdtemp(prefix="lifecycle-test-")
os.environ["HOMELAB_LIFECYCLE_MIRROR"] = os.path.join(_tmp_dir, "lifecycle.yml")
os.environ["HOMELAB_DECOMMISSION_ARCHIVE_DIR"] = os.path.join(_tmp_dir, "archive")

import tools
tools.OWNER_USER_ID = BOSS_ID
tools.CREW_USER_IDS = {CREW_ID}

import homelab_maintenance as hm
import homelab_lifecycle as lc
import homelab_monitor as mon
import maintenance_policy as policy

BOSS = tools.ToolContext(user_id=BOSS_ID, user_name="Boss", channel_id="tg:1")
ROB = tools.ToolContext(user_id=CREW_ID, user_name="Rob", channel_id="tg:2")


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── Mocked Docker ───────────────────────────────────────────────────────────
# A tiny fake host: one retired site, one shared-image service, one managed
# container, plus a proxy. The sharing relationships are what the cleanup
# planner must respect.
DEFAULT_CONTAINERS = [
    # name, compose project, image, status, mounts, networks
    ("ivn-site", "ivn-group", "nginx:1.27-alpine", "Exited (0) 2 months ago",
     "", "npm_default"),
    ("other-site", "other-group", "nginx:1.27-alpine", "Up 3 days",
     "shared_assets", "npm_default"),
    ("nginx-proxy-manager", "npm", "jc21/npm:2", "Up 6 days", "npm_data", "npm_default"),
    ("jellyfin", "privacyserver", "lscr.io/jellyfin:latest", "Up 5 hours", "", "npm_default"),
    ("worker", "worker", "worker:1", "Up 2 days", "worker_data", "worker_default"),
]


class MockOps:
    """Stand-in for hm.Ops. Records every command so a test can prove that no
    prune / no shared-resource deletion was ever issued."""

    def __init__(self, allow_repairs=False, containers=None, proxy_hits=(),
                 fail=()):
        self.allow_repairs = allow_repairs
        self.auto_repair_allowed = allow_repairs
        self.commands_run = []
        self.log_excerpt = ""
        self.containers = list(DEFAULT_CONTAINERS if containers is None else containers)
        self.proxy_hits = list(proxy_hits)
        self.fail = set(fail)
        self.removed = []

    redact = staticmethod(hm.redact)

    def _find(self, name):
        return next((c for c in self.containers if c[0] == name), None)

    async def run(self, name, **params):
        self.commands_run.append({"name": name, "params": params})
        if policy.is_repair_command(name) and not self.allow_repairs:
            raise policy.PolicyError(f"repair command '{name}' refused in read-only mode")
        if name in self.fail:
            return 1, "mock failure"

        if name == "docker_ps_projects":
            return 0, "\n".join(f"{c[0]}\t{c[1]}\t{c[2]}\t{c[3]}" for c in self.containers)
        if name == "docker_ps_status":
            return 0, "\n".join(f"{c[0]}\t{c[3]}" for c in self.containers)
        if name == "docker_ps_mounts":
            return 0, "\n".join(f"{c[0]}\t{c[4]}" for c in self.containers)
        if name == "docker_ps_networks":
            return 0, "\n".join(f"{c[0]}\t{c[5]}" for c in self.containers)
        if name == "docker_inspect":
            c = self._find(params["container"])
            if c is None:
                return 1, "No such object"
            mounts = []
            for vol in [v for v in c[4].split(",") if v]:
                mounts.append({"Type": "volume", "Name": vol,
                               "Destination": f"/data/{vol}", "RW": True})
            if c[0] == "ivn-site":
                mounts.append({"Type": "bind",
                               "Source": f"{_tmp_dir}/sites/{c[1]}/site",
                               "Destination": "/usr/share/nginx/html", "RW": False})
            return 0, json.dumps([{
                "Config": {"Labels": {
                    "com.docker.compose.project": c[1],
                    "com.docker.compose.service": c[0],
                    "com.docker.compose.project.working_dir": f"{_tmp_dir}/sites/{c[1]}",
                    "com.docker.compose.project.config_files":
                        f"{_tmp_dir}/sites/{c[1]}/docker-compose.yml",
                }},
                "Mounts": mounts,
                "State": {"Status": "running" if c[3].startswith("Up") else "exited"},
            }])
        if name == "proxy_conf_grep":
            return (0, "\n".join(self.proxy_hits)) if self.proxy_hits else (1, "")
        if name in ("compose_down_project", "docker_container_rm"):
            target = params.get("container") or "ivn-site"
            self.removed.append(target)
            self.containers = [c for c in self.containers
                               if c[0] != target and c[1] != "ivn-group"]
            return 0, ""
        if name in ("docker_image_rm", "docker_volume_rm"):
            self.removed.append(params.get("image") or params.get("volume"))
            return 0, ""
        return 0, ""

    async def record_attempt(self, action, target):
        pass

    async def attempted(self, action, target):
        return False

    async def sleep(self, secs):
        pass


def install_ops(ops):
    """Point both the lifecycle module and the monitor at one MockOps."""
    hm.Ops = lambda allow_repairs=False: ops
    return ops


class LifecycleTestCase(unittest.TestCase):
    def setUp(self):
        mon._db()          # ensures the monitor's own tables exist
        conn = lc._db()
        conn.execute("DELETE FROM asset_lifecycle")
        conn.execute("DELETE FROM lifecycle_requests")
        conn.execute("DELETE FROM monitor_incidents")
        conn.execute("DELETE FROM monitor_checks")
        conn.execute("DELETE FROM incidents")
        conn.commit()
        self._real_ops = hm.Ops
        self.notices = []

        async def _capture(text):
            self.notices.append(text)
        mon._notify_boss = _capture
        # Make the project directory the archive step copies from.
        os.makedirs(f"{_tmp_dir}/sites/ivn-group", exist_ok=True)
        with open(f"{_tmp_dir}/sites/ivn-group/docker-compose.yml", "w") as f:
            f.write("services:\n  ivn-site:\n    image: nginx:1.27-alpine\n")
        with open(f"{_tmp_dir}/sites/ivn-group/nginx.conf", "w") as f:
            f.write("server {\n  server_name ivn-group.cc www.ivn-group.cc;\n"
                    "  # see merge.rocks for the design reference\n}\n")

    def tearDown(self):
        hm.Ops = self._real_ops
        mon._notify_boss = None


# ── 1. Boss marks an asset decommissioned ──────────────────────────────────
class TestDecommissionIntent(LifecycleTestCase):
    def test_boss_decommission_marks_and_suppresses(self):
        install_ops(MockOps())
        out = json.loads(run(lc._tool_decommission(
            {"asset": "IVN site",
             "reason": "feel free to obliterate it, not coming back"}, BOSS)))
        self.assertTrue(out["ok"])
        self.assertEqual(out["asset"], "ivn-site")
        self.assertEqual(out["state"], lc.DECOMMISSION_PENDING_CLEANUP)
        self.assertTrue(out["monitoring_suppressed"])
        self.assertTrue(out["hermes_suppressed"])
        self.assertIn("decommissioned", out["say_now"])
        self.assertIn("shared", out["say_now"])

        rec = lc.get("ivn-site")
        self.assertEqual(rec["state"], lc.DECOMMISSION_PENDING_CLEANUP)
        self.assertTrue(rec["monitoring_suppressed"])
        self.assertTrue(rec["tombstone_retained"])
        self.assertEqual(rec["cleanup_status"], lc.CLEANUP_PENDING_APPROVAL)

    def test_intent_phrases_resolve_the_named_asset_only(self):
        install_ops(MockOps())
        for phrase in ("IVN site", "ivn-site", "ivn group", "IVN"):
            name, err = run(lc.resolve_target(phrase))
            self.assertEqual((name, err), ("ivn-site", ""), phrase)
        # Ambiguity is an error, never a guess.
        name, err = run(lc.resolve_target("site"))
        self.assertIsNone(name)
        self.assertIn("ambiguous", err)

    def test_intentionally_stopped_is_not_a_decommission(self):
        install_ops(MockOps())
        out = json.loads(run(lc._tool_decommission(
            {"asset": "worker", "permanent": False,
             "reason": "this is intentionally stopped"}, BOSS)))
        self.assertEqual(out["state"], lc.EXPECTED_STOPPED)
        self.assertNotIn(lc.get("worker")["state"], lc.TOMBSTONE_STATES)


# ── 2. State persists across a Loki restart ────────────────────────────────
class TestPersistence(LifecycleTestCase):
    def test_state_survives_module_reload(self):
        install_ops(MockOps())
        run(lc.mark("ivn-site", lc.DECOMMISSIONED, ctx=BOSS, reason="obliterate it"))

        # Simulate a restart: drop the cached connection and re-read from disk,
        # exactly as a fresh process would.
        import importlib
        hm._conn = None
        importlib.reload(lc)
        rec = lc.get("ivn-site")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["state"], lc.DECOMMISSIONED)
        self.assertTrue(rec["monitoring_suppressed"])
        self.assertFalse(lc.incident_allowed("ivn-site")[0])

    def test_mirror_file_is_written_and_readable(self):
        install_ops(MockOps())
        run(lc.mark("ivn-site", lc.DECOMMISSIONED, ctx=BOSS, reason="retired"))
        import yaml
        with open(os.environ["HOMELAB_LIFECYCLE_MIRROR"]) as f:
            data = yaml.safe_load(f)
        self.assertEqual(data["assets"]["ivn-site"]["state"], lc.DECOMMISSIONED)
        self.assertTrue(data["assets"]["ivn-site"]["tombstone_retained"])


# ── 3/4/5. Sweep sees it stopped: no incident, no Hermes job ───────────────
class TestSweep(LifecycleTestCase):
    def test_decommissioned_stopped_container_opens_no_incident(self):
        ops = install_ops(MockOps())
        run(lc.mark("ivn-site", lc.DECOMMISSIONED, ctx=BOSS, reason="obliterate"))
        result = run(mon._check_container_sweep(False))
        self.assertTrue(result["healthy"])
        self.assertFalse(result["escalate"])
        # Two polls past the failure threshold must still open nothing.
        for _ in range(3):
            run(mon._process_check("container-sweep", "Unregistered containers",
                                   mon._check_container_sweep, "sweep"))
        self.assertEqual(mon.list_open_incidents(), [])

    def test_no_hermes_job_is_created(self):
        install_ops(MockOps())
        run(lc.mark("ivn-site", lc.DECOMMISSIONED, ctx=BOSS, reason="obliterate"))
        submitted = []
        original = mon._escalate

        async def _spy(*a, **kw):
            submitted.append(a)
            return await original(*a, **kw)
        mon._escalate = _spy
        try:
            for _ in range(3):
                run(mon._process_check("container-sweep", "Unregistered containers",
                                       mon._check_container_sweep, "sweep"))
        finally:
            mon._escalate = original
        self.assertEqual(submitted, [])
        self.assertFalse(lc.hermes_allowed("ivn-site")[0])

    def test_unmanaged_stopped_container_creates_no_outage_incident(self):
        # No lifecycle record at all — the exact pre-fix scenario.
        install_ops(MockOps())
        self.assertEqual(lc.classify("ivn-site")["state"], lc.UNMANAGED)
        result = run(mon._check_container_sweep(False))
        self.assertTrue(result["healthy"])
        self.assertFalse(result["escalate"])
        inventory = next(c for c in result["checks"]
                         if c["name"] == "unmanaged_inventory")
        self.assertIn("ivn-site", inventory["detail"])   # quiet inventory only
        self.assertTrue(inventory["ok"])
        self.assertFalse(lc.hermes_allowed("ivn-site")[0])

    def test_managed_expected_running_container_still_opens_a_real_incident(self):
        install_ops(MockOps())
        run(lc.mark("worker", lc.MANAGED, ctx=BOSS, reason="watch this one"))
        ops = install_ops(MockOps(containers=[
            c if c[0] != "worker" else ("worker", "worker", "worker:1",
                                        "Exited (1) 5 minutes ago", "worker_data",
                                        "worker_default")
            for c in DEFAULT_CONTAINERS]))
        result = run(mon._check_container_sweep(False))
        self.assertFalse(result["healthy"])
        self.assertTrue(result["escalate"])
        self.assertIn("worker", result["diagnosis"])
        self.assertTrue(lc.incident_allowed("worker")[0])
        self.assertTrue(lc.hermes_allowed("worker")[0])

    def test_ignored_and_expected_stopped_never_alert(self):
        install_ops(MockOps())
        for state in (lc.IGNORED, lc.EXPECTED_STOPPED):
            run(lc.mark("ivn-site", state, ctx=BOSS, reason="test"))
            result = run(mon._check_container_sweep(False))
            self.assertTrue(result["healthy"], state)
            self.assertFalse(lc.incident_allowed("ivn-site")[0], state)


# ── 6/7/8. Incident reconciliation ─────────────────────────────────────────
class TestReconciliation(LifecycleTestCase):
    def _open_sweep_incident(self, task_id=""):
        now = time.time()
        mon._insert_incident({
            "incident_id": "mi_test01", "key": "container-sweep",
            "display_name": "Unregistered containers", "status": mon.OPEN,
            "opened_at": now, "updated_at": now,
            "detection_json": json.dumps(
                {"diagnosis": "Unregistered container(s) stopped: ivn-site: Exited (0)"}),
            "evidence_json": json.dumps(["ivn-site: Exited (0)"]),
            "escalated_task_id": task_id,
        })
        mon._update_check("container-sweep", open_incident_id="mi_test01")

    def test_existing_incident_resolves_as_intentional_decommission(self):
        install_ops(MockOps())
        self._open_sweep_incident()
        self.assertEqual(len(mon.list_open_incidents()), 1)

        rec = run(lc.mark("ivn-site", lc.DECOMMISSIONED, ctx=BOSS, reason="obliterate"))
        self.assertIn("mi_test01", rec["reconciliation"]["closed_incidents"])
        self.assertEqual(mon.list_open_incidents(), [])
        inc = mon.get_incident("mi_test01")
        self.assertEqual(inc["status"], mon.RESOLVED)
        self.assertEqual(inc["result"], lc.INTENTIONAL_DECOMMISSION)
        # History is preserved, never deleted.
        self.assertIsNotNone(inc["detection_json"])
        self.assertIn("mi_test01", lc.get("ivn-site")["prior_incidents"])

    def test_pending_hermes_job_is_cancelled(self):
        install_ops(MockOps())
        import task_supervisor as ts
        cancelled = []
        real_get, real_update = ts.get_task, ts._update
        ts.get_task = lambda tid: {"task_id": tid, "status": "queued"}
        ts._update = lambda tid, **f: cancelled.append((tid, f))
        try:
            self._open_sweep_incident(task_id="lt_pending01")
            rec = run(lc.mark("ivn-site", lc.DECOMMISSIONED, ctx=BOSS, reason="obliterate"))
        finally:
            ts.get_task, ts._update = real_get, real_update
        self.assertIn("lt_pending01", rec["reconciliation"]["cancelled_tasks"])
        # Cancelled quietly: last_announced is pre-set so the supervisor does
        # not emit its own duplicate message.
        self.assertTrue(any(f.get("last_announced") == "cancelled"
                            for _t, f in cancelled))

    def test_late_hermes_result_does_not_reopen_the_incident(self):
        install_ops(MockOps())
        self._open_sweep_incident(task_id="lt_pending02")
        run(lc.mark("ivn-site", lc.DECOMMISSIONED, ctx=BOSS, reason="obliterate"))
        # A Hermes job landing after the close is not actionable...
        self.assertFalse(lc.hermes_result_actionable("mi_test01"))
        self.assertFalse(hm._hermes_result_actionable("mi_test01"))
        # ...and the incident stays closed.
        self.assertEqual(mon.get_incident("mi_test01")["status"], mon.RESOLVED)
        self.assertEqual(mon.list_open_incidents(), [])

    def test_maintenance_incident_is_closed_too(self):
        install_ops(MockOps())
        now = time.time()
        hm._db().execute(
            "INSERT INTO incidents (incident_id, task_id, asset, symptom, status,"
            " created_at, updated_at, diagnosis) VALUES (?,?,?,?,?,?,?,?)",
            ("hi_test01", "", "ivn-site", "ivn-site is down", "escalating_to_hermes",
             now, now, "ivn-site container stopped"))
        hm._db().commit()
        run(lc.mark("ivn-site", lc.DECOMMISSIONED, ctx=BOSS, reason="obliterate"))
        self.assertEqual(hm.get_incident("hi_test01")["status"],
                         "closed_intentional_decommission")


# ── 9/10/11/12. Decommission vs. destructive cleanup ───────────────────────
class TestCleanupSeparation(LifecycleTestCase):
    def test_decommission_is_distinct_from_cleanup(self):
        ops = install_ops(MockOps())
        run(lc._tool_decommission({"asset": "IVN site", "reason": "obliterate"}, BOSS))
        # Suppression is live, but nothing destructive ran.
        self.assertTrue(lc.get("ivn-site")["monitoring_suppressed"])
        self.assertEqual(ops.removed, [])
        self.assertFalse(any(policy.is_repair_command(c["name"])
                             for c in ops.commands_run))
        self.assertEqual(lc.get("ivn-site")["cleanup_status"],
                         lc.CLEANUP_PENDING_APPROVAL)

    def test_shared_resources_are_never_selected_for_deletion(self):
        install_ops(MockOps())
        scope = run(lc.discover("ivn-site"))
        plan = lc.plan_cleanup(scope)
        removed = {(i["kind"], i["name"]) for i in plan["remove"]}
        skipped = {(i["kind"], i["name"]) for i in plan["skipped_shared"]}

        # nginx:1.27-alpine is also used by other-site -> shared, never removed.
        self.assertIn(("image", "nginx:1.27-alpine"), skipped)
        self.assertNotIn(("image", "nginx:1.27-alpine"), removed)
        # npm_default carries the proxy and other sites -> shared, never removed.
        self.assertIn(("network", "npm_default"), skipped)
        self.assertNotIn(("network", "npm_default"), removed)
        # No network is ever a removal candidate, shared or not.
        self.assertEqual([i for i in plan["remove"] if i["kind"] == "network"], [])
        # The project directory is retained, never deleted.
        self.assertTrue(any(i["kind"] == "directory" for i in plan["retain"]))
        # Proxy and DNS are manual follow-ups only.
        self.assertTrue(all(m["kind"] in ("proxy", "dns")
                            for m in plan["manual_follow_up"]))

    def test_dns_refs_come_from_directives_not_prose(self):
        install_ops(MockOps())
        hosts = {r["host"] for r in run(lc.discover("ivn-site"))["dns_refs"]}
        self.assertEqual(hosts, {"ivn-group.cc", "www.ivn-group.cc"})
        self.assertNotIn("merge.rocks", hosts)   # a comment is not a DNS record

    def test_unshared_volume_is_removable_but_shared_one_is_not(self):
        install_ops(MockOps(containers=[
            ("ivn-site", "ivn-group", "ivn:1", "Exited (0) 2 months ago",
             "ivn_private,shared_assets", "npm_default"),
            ("other-site", "other-group", "other:1", "Up 3 days",
             "shared_assets", "npm_default"),
        ]))
        plan = lc.plan_cleanup(run(lc.discover("ivn-site")))
        removed = {(i["kind"], i["name"]) for i in plan["remove"]}
        skipped = {(i["kind"], i["name"]) for i in plan["skipped_shared"]}
        self.assertIn(("volume", "ivn_private"), removed)
        self.assertIn(("volume", "shared_assets"), skipped)
        self.assertNotIn(("volume", "shared_assets"), removed)

    def test_cleanup_requires_a_valid_approval(self):
        install_ops(MockOps())
        run(lc._tool_decommission({"asset": "IVN site", "reason": "obliterate"}, BOSS))
        spec = tools.REGISTRY["homelab_apply_decommission_cleanup"]
        # The executing tool is registered as consequential -> the draft gate
        # intercepts it; it is never run inline.
        self.assertEqual(spec.action_type, "decommission_cleanup")
        self.assertEqual(policy.action_tier("decommission_cleanup"), policy.APPROVAL)

        scope = lc.get("ivn-site")["cleanup_scope"]
        good = lc.plan_hash(lc.plan_cleanup(scope))
        payload, summary, err = lc._cleanup_prepare(
            {"asset": "ivn-site", "plan_hash": good}, BOSS)
        self.assertEqual(err, "")
        self.assertIn("REMOVE", summary)
        # A stale/tampered plan hash is refused outright.
        _p, _s, err2 = lc._cleanup_prepare(
            {"asset": "ivn-site", "plan_hash": "deadbeef"}, BOSS)
        self.assertIn("changed", err2)
        with self.assertRaises(ValueError):
            run(lc._cleanup_handler({"asset": "ivn-site", "plan_hash": "deadbeef"}, BOSS))

    def test_cleanup_refuses_an_asset_that_was_never_decommissioned(self):
        install_ops(MockOps())
        out = json.loads(run(lc._tool_cleanup({"asset": "worker"}, BOSS)))
        self.assertFalse(out["ok"])
        self.assertIn("not marked for decommissioning", out["error"])

    def test_tombstone_remains_after_cleanup(self):
        ops = install_ops(MockOps())
        run(lc._tool_decommission({"asset": "IVN site", "reason": "obliterate"}, BOSS))
        rec = lc.get("ivn-site")
        plan = lc.plan_cleanup(rec["cleanup_scope"])
        ops.allow_repairs = True
        ops.auto_repair_allowed = True
        result = run(lc.execute_cleanup("ivn-site", plan, rec["cleanup_scope"]))

        self.assertEqual(result["cleanup_status"], lc.CLEANUP_COMPLETED)
        self.assertTrue(result["verification"]["ok"])
        after = lc.get("ivn-site")
        self.assertIsNotNone(after)                       # tombstone survives
        self.assertTrue(after["tombstone_retained"])
        self.assertEqual(after["state"], lc.DECOMMISSIONED)
        self.assertTrue(after["monitoring_suppressed"])
        self.assertTrue(os.path.isdir(result["archive_path"]))
        # Still excluded from monitoring after cleanup — it can never be
        # rediscovered as an unknown container.
        self.assertFalse(lc.incident_allowed("ivn-site")[0])
        self.assertIn("ivn-site", [t["name"] for t in lc.tombstones()])

    def test_cleanup_never_prunes_or_touches_shared_resources(self):
        ops = install_ops(MockOps())
        run(lc._tool_decommission({"asset": "IVN site", "reason": "obliterate"}, BOSS))
        rec = lc.get("ivn-site")
        ops.allow_repairs = True
        run(lc.execute_cleanup("ivn-site", lc.plan_cleanup(rec["cleanup_scope"]),
                               rec["cleanup_scope"]))
        names = [c["name"] for c in ops.commands_run]
        for forbidden in ("docker_system_prune", "docker_volume_prune",
                          "docker_image_prune", "rm_rf"):
            self.assertNotIn(forbidden, names)
        # Nothing shared was removed.
        self.assertNotIn("nginx:1.27-alpine", ops.removed)
        self.assertNotIn("npm_default", ops.removed)
        # No prune command exists in the allowlist at all.
        self.assertFalse([c for c in policy.command_names() if "prune" in c])


# ── 13. A recreated decommissioned container warns exactly once ────────────
class TestReappearance(LifecycleTestCase):
    def test_recreated_container_produces_one_warning(self):
        run(lc.mark("ivn-site", lc.DECOMMISSIONED, ctx=BOSS, reason="obliterate"))
        install_ops(MockOps(containers=[
            ("ivn-site", "ivn-group", "nginx:1.27-alpine", "Up 2 minutes", "", "npm_default"),
        ]))
        for _ in range(3):
            result = run(mon._check_container_sweep(False))
            self.assertTrue(result["healthy"])       # reappearance is not an outage
        warnings = [n for n in self.notices if "reappeared" in n]
        self.assertEqual(len(warnings), 1)
        self.assertIn("ivn-site", warnings[0])
        self.assertIn("previously decommissioned", warnings[0])
        # It is NOT deleted automatically.
        self.assertEqual(lc.get("ivn-site")["state"], lc.DECOMMISSIONED)


# ── 16. Authorization and log redaction ────────────────────────────────────
class TestAuthorizationAndRedaction(LifecycleTestCase):
    def test_roommate_may_request_but_not_decommission(self):
        install_ops(MockOps())
        out = json.loads(run(lc._tool_decommission(
            {"asset": "IVN site", "reason": "rob wants it gone"}, ROB)))
        self.assertFalse(out["ok"])
        self.assertIn("Boss", out["error"])
        self.assertIsNone(lc.get("ivn-site"))          # nothing changed
        req = json.loads(run(lc._tool_lifecycle_list({}, BOSS)))
        self.assertEqual(req["pending_requests"][0]["asset"], "ivn-site")
        self.assertEqual(req["pending_requests"][0]["role"], "crew")

    def test_lifecycle_tools_are_permission_gated(self):
        self.assertEqual(tools.REGISTRY["homelab_lifecycle_set"].permission, "boss")
        self.assertEqual(tools.REGISTRY["homelab_lifecycle_list"].permission, "boss")
        self.assertEqual(tools.REGISTRY["homelab_decommission_cleanup"].permission, "boss")
        self.assertEqual(tools.REGISTRY["homelab_apply_decommission_cleanup"].permission,
                         "boss")

    def test_reason_is_redacted_before_it_is_stored(self):
        install_ops(MockOps())
        # The reason is free text the Boss dictated, so it goes through the
        # same scrubber every other persisted homelab string uses.
        secret = ("obliterate it — deploy token: sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 "
                  "on aa:bb:cc:dd:ee:ff")
        run(lc.mark("ivn-site", lc.DECOMMISSIONED, ctx=BOSS, reason=secret))
        stored = lc.get("ivn-site")["reason"]
        self.assertNotIn("sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", stored)
        self.assertNotIn("aa:bb:cc:dd:ee:ff", stored)
        self.assertIn("[REDACTED]", stored)
        self.assertIn("[MAC]", stored)
        self.assertIn("obliterate it", stored)      # the intent itself survives
        with open(os.environ["HOMELAB_LIFECYCLE_MIRROR"]) as f:
            mirror = f.read()
        self.assertNotIn("sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345", mirror)
        self.assertNotIn("aa:bb:cc:dd:ee:ff", mirror)

    def test_cleanup_commands_reject_undeclared_values(self):
        # A container name that is not on the live host can never reach a
        # command, however it was supplied.
        with self.assertRaises(policy.PolicyError):
            policy.build_command("docker_container_rm", container="not-a-real-container")


# ── 17. No duplicate Telegram notifications ────────────────────────────────
class TestNotificationHygiene(LifecycleTestCase):
    def test_decommission_and_sweep_send_no_incident_noise(self):
        install_ops(MockOps())
        run(lc._tool_decommission({"asset": "IVN site", "reason": "obliterate"}, BOSS))
        for _ in range(4):
            run(mon._process_check("container-sweep", "Unregistered containers",
                                   mon._check_container_sweep, "sweep"))
        # The chat reply is the single notification; the monitor stays silent.
        self.assertEqual([n for n in self.notices if "Incident opened" in n], [])
        self.assertEqual([n for n in self.notices if "Approval needed" in n], [])
        self.assertEqual([n for n in self.notices if "needs your hands" in n], [])

    def test_suppressed_escalation_is_silent_not_a_failure_alert(self):
        install_ops(MockOps())
        run(lc.mark("ivn-site", lc.DECOMMISSIONED, ctx=BOSS, reason="obliterate"))
        now = time.time()
        mon._insert_incident({
            "incident_id": "mi_quiet1", "key": "container-sweep",
            "display_name": "Unregistered containers", "status": mon.OPEN,
            "opened_at": now, "updated_at": now,
            "detection_json": "{}", "evidence_json": "[]"})
        checks = [{"name": "managed_containers", "ok": False,
                   "detail": "ivn-site: Exited (0) 2 months ago"}]
        run(mon._give_up_and_escalate(
            mon.get_incident("mi_quiet1"),
            {"checks": checks, "diagnosis": "stopped"}, reason="no runbook"))
        self.assertEqual(mon.get_incident("mi_quiet1")["status"], mon.GAVE_UP)
        self.assertEqual(self.notices, [])   # nothing sent to the Boss at all

    def test_cleanup_reminder_is_rate_limited(self):
        install_ops(MockOps())
        run(lc.mark("ivn-site", lc.DECOMMISSION_PENDING_CLEANUP, ctx=BOSS,
                    reason="obliterate"))
        self.assertFalse(lc.due_for_cleanup_reminder("ivn-site"))
        lc._write("ivn-site", reminder_last_at=time.time() - lc.CLEANUP_REMINDER_SECS - 1)
        self.assertTrue(lc.due_for_cleanup_reminder("ivn-site"))
        run(mon._check_container_sweep(False))
        self.assertEqual(len([n for n in self.notices if "Reminder" in n]), 1)
        run(mon._check_container_sweep(False))
        self.assertEqual(len([n for n in self.notices if "Reminder" in n]), 1)


# ── Escalation bundle carries lifecycle + intent ───────────────────────────
class TestEscalationBundle(LifecycleTestCase):
    def test_bundle_includes_lifecycle_state_and_boss_intent(self):
        install_ops(MockOps())
        run(lc.mark("ivn-site", lc.DECOMMISSIONED, ctx=BOSS, reason="obliterate"))
        bundle = mon._monitor_escalation_bundle(
            "container-sweep", "Unregistered containers", "stopped", [], [],
            subjects=["ivn-site"])
        self.assertEqual(bundle["lifecycle"][0]["state"], lc.DECOMMISSIONED)
        self.assertEqual(bundle["boss_intent"], "automatic monitoring escalation")

        asset = hm._reg().get("jellyfin")
        hm_bundle = hm.build_escalation_bundle(
            asset, "down", {"checks": []}, MockOps(), [], user_requested=True)
        self.assertEqual(hm_bundle["boss_intent"], "explicit investigation request")
        self.assertEqual(hm_bundle["lifecycle"][0]["state"], lc.MANAGED)

    def test_boss_request_can_still_reach_hermes_for_an_unknown_container(self):
        install_ops(MockOps())
        # Unmanaged: never automatic...
        self.assertFalse(lc.hermes_allowed("ivn-site", user_requested=False)[0])
        # ...but "investigate this unknown container" is allowed.
        self.assertTrue(lc.hermes_allowed("ivn-site", user_requested=True)[0])
        # A decommissioned asset is never worth diagnosing, even on request.
        run(lc.mark("ivn-site", lc.DECOMMISSIONED, ctx=BOSS, reason="gone"))
        self.assertFalse(lc.hermes_allowed("ivn-site", user_requested=True)[0])


if __name__ == "__main__":
    unittest.main()

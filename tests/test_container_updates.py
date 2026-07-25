"""
Focused tests for the safe container-update workflows.

Everything external is mocked: the GitHub release feed, every docker/compose/
postgres command (through a MockOps that mirrors homelab_maintenance.Ops), and
the filesystem (a throwaway registry pointed at tmp dirs). No registry is
contacted, no image is pulled, no container is recreated, and no live database
is touched.

Covers the required scenarios: an up-to-date service, an available update, a
prerelease being ignored, a breaking release, a missing backup, insufficient
disk, approval being required, exact-version pinning, a successful update, a
failed health check, a safe rollback, refusal to roll back across an unsafe
migration, and the guarantee that nothing deletes data.

Run:  venv/bin/python -m unittest tests.test_container_updates -v
"""

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest

BOSS_ID = "111111111111111111"
CREW_ID = "222222222222222222"
os.environ["OWNER_USER_ID"] = BOSS_ID
os.environ["CREW_USER_IDS"] = CREW_ID
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("HOMELAB_DB_PATH", _tmp_db.name)

import tools
tools.OWNER_USER_ID = BOSS_ID
tools.CREW_USER_IDS = {CREW_ID}

import yaml

import homelab_assets
import homelab_maintenance as hm
import maintenance_policy as policy
import container_updates as cu


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def ctx(user_id=BOSS_ID):
    return tools.ToolContext(user_id=user_id, user_name="t", channel_id="tg:1")


# ── A throwaway registry so tests never touch the real homelab ─────────────
def build_registry(root: str) -> dict:
    compose = os.path.join(root, "app", "docker-compose.yml")
    envf = os.path.join(root, "app", "and.env")
    os.makedirs(os.path.dirname(compose), exist_ok=True)
    with open(compose, "w") as f:
        f.write("name: testapp\n")
    with open(envf, "w") as f:
        f.write("DB_USERNAME=testuser\nDB_DATABASE_NAME=testdb\n"
                "UPLOAD_LOCATION=" + os.path.join(root, "upload") + "\n"
                "#APP_VERSION=\n")
    os.makedirs(os.path.join(root, "upload"), exist_ok=True)
    os.makedirs(os.path.join(root, "backups", "testapp"), exist_ok=True)
    return {
        "version": 1,
        "assets": {
            "testapp": {
                "display_name": "TestApp", "host": os.uname().nodename,
                "type": "docker_stack", "aliases": ["testapp", "test app"],
                "runbook": "immich_status", "stateful": True,
                "update_policy": "approval_always",
                "docker": {
                    "compose_project": "testapp", "compose_file": compose,
                    "env_file": envf,
                    "containers": ["testapp_server", "testapp_db"],
                },
                "health": {"local_url": "http://127.0.0.1:9/ping"},
                "updates": {
                    "runbook": "immich_update",
                    "release_source": "github:example/testapp",
                    "version_api": "http://127.0.0.1:9/version",
                    "version_env": "APP_VERSION",
                    "tag_style": "moving",
                    "versioned_images": ["ghcr.io/example/testapp-server"],
                    "database": {"container": "testapp_db",
                                 "user_env": "DB_USERNAME",
                                 "name_env": "DB_DATABASE_NAME",
                                 "data_path_env": "DB_DATA_LOCATION"},
                    "backup": {"required": True,
                               "dir": os.path.join(root, "backups", "testapp"),
                               "min_free_gb": 1},
                    "estimated_interruption": "2-5 minutes",
                    "migration_makes_rollback_unsafe": True,
                },
            }
        },
    }


REL_STABLE = {"tag_name": "v2.0.0", "prerelease": False, "draft": False,
              "published_at": "2026-07-01T00:00:00Z",
              "html_url": "https://example/releases/v2.0.0",
              "body": "Adds a new gallery view and fixes upload retries."}
REL_BREAKING = {"tag_name": "v2.0.0", "prerelease": False, "draft": False,
                "published_at": "2026-07-01T00:00:00Z",
                "html_url": "https://example/releases/v2.0.0",
                "body": "BREAKING CHANGE: the old API is removed.\n"
                        "A database migration will run on first start."}
REL_PRE = {"tag_name": "v3.0.0-rc1", "prerelease": True, "draft": False,
           "published_at": "2026-07-20T00:00:00Z",
           "html_url": "https://example/releases/v3.0.0-rc1",
           "body": "Release candidate."}


class MockOps:
    """Mirrors homelab_maintenance.Ops, including its repair-command gate."""

    def __init__(self, outputs=None, allow_repairs=True, http=None,
                 free_bytes=50 * 1024 ** 3, dump_bytes=1_000_000,
                 dump_verifies=True):
        self.outputs = dict(outputs or {})
        self.allow_repairs = allow_repairs
        self.auto_repair_allowed = allow_repairs
        self.commands_run = []
        self.log_excerpt = ""
        self.http = dict(http or {})
        self.free_bytes = free_bytes
        self.dump_bytes = dump_bytes
        self.dump_verifies = dump_verifies
        self.slept = 0

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
        policy.build_command(name, **params)   # enforce the real allowlist
        self.commands_run.append({"name": name, "params": params})
        if name == "df_path_bytes":
            return 0, f"Avail\n{self.free_bytes}\n"
        v = self.outputs.get(name)
        if v is None:
            return 1, f"no scripted output for {name}"
        rc, out = self._next(v, params)
        if name == "docker_logs_tail":
            self.log_excerpt = hm.redact(out)[-2000:]
        return rc, out

    async def run_to_file(self, name, out_path, **params):
        if policy.is_repair_command(name) and not self.allow_repairs:
            raise policy.PolicyError(f"repair command '{name}' refused")
        if not hm._under_declared_backup_dir(out_path):
            raise policy.PolicyError("backup destination not declared")
        policy.build_command(name, **params)
        self.commands_run.append({"name": name, "params": params,
                                  "stdout_to": out_path})
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "wb") as f:
            f.write(b"P" * self.dump_bytes)
        return (0, "") if self.dump_bytes else (1, "pg_dump failed")

    async def run_from_file(self, name, in_path, **params):
        policy.build_command(name, **params)
        self.commands_run.append({"name": name, "params": params,
                                  "stdin_from": in_path})
        if self.dump_verifies:
            return 0, "; Archive created\n123; 456 TABLE assets\n789; 12 TABLE users\n"
        return 1, "pg_restore: error: did not find magic string"

    async def http_get(self, url, timeout=8):
        v = self.http.get(url)
        return self._next(v, None) if v is not None else (0, "unscripted")

    async def path_meta(self, path):
        return {"exists": True}

    async def sleep(self, secs):
        self.slept += secs


def inspect_out(image, state="running", health="healthy", version=None):
    labels = {"org.opencontainers.image.version": version} if version else {}
    return (0, json.dumps([{
        "Config": {"Image": image, "Labels": labels},
        "State": {"Status": state, "Health": {"Status": health}},
        "HostConfig": {"PortBindings": {}},
    }]))


def image_inspect_out(digest, created="2026-06-01T00:00:00Z"):
    return (0, json.dumps([{"Id": f"sha256:{digest}",
                            "RepoDigests": [f"ghcr.io/example/testapp-server@sha256:{digest}"],
                            "Created": created}]))


class Base(unittest.TestCase):
    def setUp(self):
        # Swapping in a throwaway registry is global state: remember what was
        # there so tearDown can put it back, or every later test module in the
        # same process inherits this fake homelab.
        self._saved_registry = hm._registry
        self._saved_cached = homelab_assets._cached

        self.root = tempfile.mkdtemp(prefix="cu-")
        self.reg_path = os.path.join(self.root, "assets.yml")
        with open(self.reg_path, "w") as f:
            yaml.safe_dump(build_registry(self.root), f)
        self.registry = homelab_assets.load(self.reg_path, force=True)
        hm._registry = self.registry
        policy.configure(self.registry.allowed_values())

        self.db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db.close()
        hm.DB_PATH = self.db.name
        hm._conn = None
        cu._db()

        cu._release_cache.clear()
        self._orig_fetch = cu.fetch_releases
        self.asset = self.registry.get("testapp")

    def tearDown(self):
        cu.fetch_releases = self._orig_fetch
        # Restore the global registry and re-point the policy allowlist at it,
        # so no later test module runs against this module's fake homelab.
        hm._registry = self._saved_registry
        homelab_assets._cached = self._saved_cached
        if self._saved_registry is not None:
            policy.configure(self._saved_registry.allowed_values())
        shutil.rmtree(self.root, ignore_errors=True)
        try:
            os.unlink(self.db.name)
        except OSError:
            pass

    def stub_releases(self, releases):
        async def _fake(source, session_factory=None):
            return list(releases)
        cu.fetch_releases = _fake

    def ops_for(self, *, installed="v1.0.0", local_digest="aaa", remote_digest="aaa",
                **kw):
        outputs = {
            # Container-aware: the db is a pinned dependency on a different
            # image, exactly like Immich's Postgres.
            "docker_inspect": lambda p: (
                inspect_out("docker.io/postgres:16-alpine", version=None)
                if p.get("container", "").endswith("_db")
                else inspect_out("ghcr.io/example/testapp-server:release",
                                 version=installed)),
            "docker_image_inspect": image_inspect_out(local_digest),
            "docker_imagetools_inspect": (0, f"Name: x\nDigest: sha256:{remote_digest}\n"),
            "docker_ps_names": (0, "testapp_server\tghcr.io/example/testapp-server:release\n"),
            "pg_isready": (0, "/var/run/postgresql:5432 - accepting connections"),
            "pg_checksum_failures": (0, "0"),
            "pg_database_size": (0, str(2 * 1024 ** 3)),
            "docker_pull_image": (0, "Status: Downloaded"),
            "compose_up_all": (0, "Container testapp_server Started"),
            "docker_logs_tail": (0, "INF server listening"),
        }
        outputs.update(kw.pop("outputs", {}))
        http = {"http://127.0.0.1:9/ping": (200, "ok"),
                "http://127.0.0.1:9/version": (200, json.dumps(
                    {"major": int(installed.lstrip("v").split(".")[0]),
                     "minor": 0, "patch": 0}))}
        http.update(kw.pop("http", {}))
        return MockOps(outputs=outputs, http=http, **kw)


# ── Inventory ──────────────────────────────────────────────────────────────
class InventoryTests(Base):
    def test_up_to_date_service_reports_no_action(self):
        self.stub_releases([REL_STABLE])
        ops = self.ops_for(installed="v2.0.0", local_digest="same", remote_digest="same")
        inv = run(cu.build_inventory("testapp_server", ops))
        svc = inv["services"][0]
        self.assertFalse(svc["update_available"])
        self.assertIn("up to date", svc["recommendation"])
        # No pending update means no risk score to alarm about.
        self.assertEqual(svc["risk"], "none")

    def test_pinned_dependency_is_not_given_the_app_version(self):
        """A pinned Postgres/Redis is not the app's version, and saying so
        would also imply the update runbook manages it. It does not."""
        self.stub_releases([REL_STABLE])
        ops = self.ops_for(installed="v1.0.0")
        inv = run(cu.build_inventory("testapp_db", ops))
        svc = inv["services"][0]
        self.assertFalse(svc["managed_by_update_runbook"])
        self.assertIsNone(svc["available_version"])
        self.assertIn("pinned dependency", svc.get("note", ""))

    def test_available_update_is_flagged_with_digests_and_release(self):
        self.stub_releases([REL_STABLE])
        ops = self.ops_for(installed="v1.0.0", local_digest="old", remote_digest="new")
        inv = run(cu.build_inventory("testapp_server", ops))
        svc = inv["services"][0]
        self.assertTrue(svc["update_available"])
        self.assertEqual(svc["available_version"], "v2.0.0")
        self.assertEqual(svc["release_url"], "https://example/releases/v2.0.0")
        self.assertTrue(svc["current_digest"] and svc["available_digest"])
        self.assertIn("approval", svc["recommendation"])
        self.assertTrue(svc["approval_required"])

    def test_prerelease_is_never_the_target(self):
        self.stub_releases([REL_PRE, REL_STABLE])
        ops = self.ops_for(installed="v1.0.0")
        inv = run(cu.build_inventory("testapp_server", ops))
        self.assertEqual(inv["services"][0]["available_version"], "v2.0.0")
        # And a prerelease tag alone is rejected even without the API flag.
        self.assertIsNone(cu.latest_stable([
            {"tag_name": "v9.9.9-beta1", "prerelease": False, "draft": False}]))

    def test_breaking_release_raises_risk_and_warns(self):
        self.stub_releases([REL_BREAKING])
        ops = self.ops_for(installed="v1.0.0", local_digest="old", remote_digest="new")
        inv = run(cu.build_inventory("testapp_server", ops))
        svc = inv["services"][0]
        self.assertTrue(svc["breaking_changes"])
        self.assertTrue(svc["migration_required"])
        self.assertEqual(svc["risk"], "high")
        self.assertIn("RELEASE NOTES", svc["recommendation"])

    def test_image_age_alone_is_not_treated_as_needing_an_update(self):
        self.stub_releases([REL_STABLE])
        ops = self.ops_for(installed="v2.0.0", local_digest="same", remote_digest="same",
                           outputs={"docker_image_inspect":
                                    image_inspect_out("same", created="2020-01-01T00:00:00Z")})
        svc = run(cu.build_inventory("testapp_server", ops))["services"][0]
        self.assertGreater(svc["image_age_days"], 1000)
        self.assertFalse(svc["update_available"], "old image is not itself an update signal")
        self.assertIn("up to date", svc["recommendation"])

    def test_unreachable_release_feed_reports_unknown_not_a_guess(self):
        self.stub_releases([])
        ops = self.ops_for(installed="v1.0.0",
                           outputs={"docker_imagetools_inspect": (1, "unreachable")})
        svc = run(cu.build_inventory("testapp_server", ops))["services"][0]
        self.assertIsNone(svc["update_available"])
        self.assertIn("unknown", svc["recommendation"])

    def test_external_autoupdater_is_detected_and_surfaced(self):
        self.stub_releases([REL_STABLE])
        ops = self.ops_for()
        ops.outputs["docker_ps_names"] = (
            0, "watchtower\tnickfedor/watchtower:latest\n"
               "testapp_server\tghcr.io/example/testapp-server:release\n")
        ops.outputs["docker_inspect"] = lambda p: (
            (0, json.dumps([{"Config": {"Image": "nickfedor/watchtower:latest",
                                        "Cmd": ["--cleanup", "--interval", "86400"],
                                        "Env": [], "Labels": {}},
                             "State": {"Status": "running", "Health": {}},
                             "HostConfig": {"PortBindings": {}}}]))
            if p.get("container") == "watchtower"
            else inspect_out("ghcr.io/example/testapp-server:release", version="v1.0.0"))
        inv = run(cu.build_inventory("testapp_server", ops))
        self.assertTrue(inv["external_autoupdate_active"])
        detail = inv["external_updaters"][0]
        self.assertFalse(detail["monitor_only"])
        self.assertEqual(detail["scope"], "ALL running containers")
        self.assertTrue(detail["cleanup"])


# ── Pre-flight gates ───────────────────────────────────────────────────────
class PreflightTests(Base):
    def test_insufficient_disk_blocks_before_any_change(self):
        self.stub_releases([REL_STABLE])
        ops = self.ops_for(installed="v1.0.0", free_bytes=100 * 1024 ** 2)  # 0.1 GB
        res = run(cu.prepare_update("testapp", ops=ops))
        self.assertFalse(res["ok"])
        self.assertEqual(res["blocked_on"], "disk_space")
        self.assertNotIn("docker_pull_image", [c["name"] for c in ops.commands_run])

    def test_missing_backup_blocks_the_update(self):
        self.stub_releases([REL_STABLE])
        ops = self.ops_for(installed="v1.0.0", dump_verifies=False)
        res = run(cu.prepare_update("testapp", ops=ops))
        self.assertFalse(res["ok"])
        self.assertEqual(res["blocked_on"], "database_backup")
        self.assertNotIn("docker_pull_image", [c["name"] for c in ops.commands_run])

    def test_truncated_dump_is_a_failed_backup_not_a_backup(self):
        self.stub_releases([REL_STABLE])
        ops = self.ops_for(installed="v1.0.0", dump_bytes=10)
        res = run(cu.prepare_update("testapp", ops=ops))
        self.assertFalse(res["ok"])
        self.assertEqual(res["blocked_on"], "database_backup")

    def test_unhealthy_database_blocks_the_update(self):
        self.stub_releases([REL_STABLE])
        ops = self.ops_for(installed="v1.0.0",
                           outputs={"pg_checksum_failures": (0, "7")})
        res = run(cu.prepare_update("testapp", ops=ops))
        self.assertFalse(res["ok"])
        self.assertEqual(res["blocked_on"], "database_health")

    def test_already_current_prepares_nothing(self):
        self.stub_releases([REL_STABLE])
        ops = self.ops_for(installed="v2.0.0")
        res = run(cu.prepare_update("testapp", ops=ops))
        self.assertTrue(res["ok"])
        self.assertTrue(res["up_to_date"])
        self.assertNotIn("pg_dump_custom", [c["name"] for c in ops.commands_run])

    def test_prerelease_target_is_refused(self):
        self.stub_releases([REL_STABLE])
        ops = self.ops_for(installed="v1.0.0")
        res = run(cu.prepare_update("testapp", target_version="v3.0.0-rc1", ops=ops))
        self.assertFalse(res["ok"])
        self.assertIn("prerelease", res["error"])

    def test_successful_prepare_takes_verified_backups_and_records_digests(self):
        self.stub_releases([REL_STABLE])
        ops = self.ops_for(installed="v1.0.0", local_digest="old", remote_digest="new")
        res = run(cu.prepare_update("testapp", ops=ops))
        self.assertTrue(res["ok"])
        plan = res["plan"]
        self.assertEqual(plan["target_version"], "v2.0.0")
        self.assertTrue(plan["backup_config_ok"])
        self.assertTrue(plan["backup_database"]["ok"])
        self.assertTrue(plan["backup_database"]["verified"])
        self.assertTrue(plan["rollback_digests"])
        self.assertTrue(os.path.isdir(plan["backup_dir"]))
        # Prepare must not have pulled or recreated anything.
        ran = [c["name"] for c in ops.commands_run]
        self.assertNotIn("docker_pull_image", ran)
        self.assertNotIn("compose_up_all", ran)


# ── Approval boundary + exact pinning ──────────────────────────────────────
class ApprovalTests(Base):
    def _prepare(self, ops=None):
        self.stub_releases([REL_STABLE])
        ops = ops or self.ops_for(installed="v1.0.0")
        res = run(cu.prepare_update("testapp", ops=ops))
        self.assertTrue(res["ok"], res)
        return res, ops

    def test_prepare_stages_a_draft_and_applies_nothing(self):
        # The tool builds its own Ops against the real host, so stub the
        # pre-flight itself; what is under test here is the draft wiring.
        res, _ = self._prepare()
        prepared = res

        async def fake_prepare(asset_key, target_version="", ops=None):
            return prepared

        orig_prepare = cu.prepare_update
        cu.prepare_update = fake_prepare
        staged = []

        async def fake_intercept(spec, args, c):
            staged.append((spec.name, spec.action_type, args))
            return "📝 Draft prepared"

        orig = tools._approval_intercept
        tools.set_approval_intercept(fake_intercept)
        try:
            out = run(cu._tool_update_prepare({"asset": "testapp"}, ctx()))
        finally:
            tools.set_approval_intercept(orig)
            cu.prepare_update = orig_prepare
        self.assertIn("Draft prepared", out)
        self.assertEqual(staged[0][0], "container_apply_update")
        self.assertEqual(staged[0][1], "container_update")
        self.assertEqual(staged[0][2]["plan_id"], res["plan_id"])

    def test_apply_refuses_a_tampered_plan_hash(self):
        res, _ = self._prepare()
        payload, summary, err = cu._apply_prepare(
            {"plan_id": res["plan_id"], "plan_hash": "deadbeef"}, ctx())
        self.assertTrue(err)
        self.assertIn("changed", err)

    def test_draft_summary_states_versions_backups_and_rollback_limits(self):
        res, _ = self._prepare()
        payload, summary, err = cu._apply_prepare(
            {"plan_id": res["plan_id"],
             "plan_hash": cu.plan_hash(res["plan"])}, ctx())
        self.assertEqual(err, "")
        for token in ("v1.0.0", "v2.0.0", "Backups", "Estimated interruption",
                      "Rollback", "Verification after"):
            self.assertIn(token, summary)

    def test_exact_version_pinning_no_moving_tag_is_pulled(self):
        res, _ = self._prepare()
        # Post-update ops must report the NEW version, or verification correctly
        # fails and the rollback pulls the old refs too.
        ops = self.ops_for(installed="v2.0.0")
        out = run(cu.apply_update(res["plan_id"], ops=ops))
        self.assertTrue(out["ok"], out)
        pulls = [c["params"]["image"] for c in ops.commands_run
                 if c["name"] == "docker_pull_image"]
        self.assertEqual(pulls, ["ghcr.io/example/testapp-server:v2.0.0"])
        for ref in pulls:
            self.assertNotIn(":release", ref)
            self.assertNotIn(":latest", ref)
        env = open(os.path.join(self.root, "app", "and.env")).read()
        self.assertIn("APP_VERSION=v2.0.0", env)

    def test_crew_cannot_reach_the_update_tools(self):
        for name in ("container_update_prepare", "container_rollback",
                     "container_apply_update", "container_update_inventory"):
            self.assertEqual(tools.REGISTRY[name].permission, "boss", name)
        out = run(tools.execute("container_update_prepare",
                                json.dumps({"asset": "testapp"}), ctx(CREW_ID)))
        self.assertIn("Permission denied", out)


# ── Apply / verify / rollback ──────────────────────────────────────────────
class ApplyTests(Base):
    def _plan(self, ops=None):
        self.stub_releases([REL_STABLE])
        res = run(cu.prepare_update("testapp", ops=ops or self.ops_for(installed="v1.0.0")))
        self.assertTrue(res["ok"], res)
        return res

    def test_successful_update_verifies_and_preserves_backups(self):
        res = self._plan()
        ops = self.ops_for(installed="v2.0.0")
        out = run(cu.apply_update(res["plan_id"], ops=ops))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["to_version"], "v2.0.0")
        self.assertTrue(out["verification"]["ok"])
        self.assertTrue(os.path.isdir(out["backup_dir"]),
                        "backups must still exist after a successful update")
        self.assertEqual(cu.get_plan(res["plan_id"])["status"], cu.VERIFIED)

    def test_failed_health_check_triggers_a_safe_rollback(self):
        res = self._plan()
        # No migration ran, so rollback is safe; server unhealthy after update.
        ops = self.ops_for(installed="v2.0.0")
        ops.outputs["docker_inspect"] = inspect_out(
            "ghcr.io/example/testapp-server:v2.0.0", state="restarting", health="unhealthy")
        out = run(cu.apply_update(res["plan_id"], ops=ops))
        self.assertFalse(out["ok"])
        self.assertEqual(out["failed_at"], "verification")
        self.assertFalse(out["rollback"]["refused"])
        self.assertIn("compose_up_all", [c["name"] for c in ops.commands_run])

    def test_failed_pull_leaves_the_old_version_running(self):
        res = self._plan()
        ops = self.ops_for(installed="v1.0.0")
        ops.outputs["docker_pull_image"] = (1, "manifest unknown")
        out = run(cu.apply_update(res["plan_id"], ops=ops))
        self.assertFalse(out["ok"])
        self.assertEqual(out["failed_at"], "pull")
        self.assertNotIn("compose_up_all", [c["name"] for c in ops.commands_run])
        self.assertIn("old version is still running", out["note"])

    def test_unsafe_migration_refuses_rollback_and_explains(self):
        res = self._plan()
        ops = self.ops_for(installed="v2.0.0")
        # Migration in the logs AND a failing health check.
        ops.outputs["docker_logs_tail"] = (0, "INF running migration AddNewColumn")
        ops.outputs["docker_inspect"] = inspect_out(
            "ghcr.io/example/testapp-server:v2.0.0", state="restarting", health="unhealthy")
        out = run(cu.apply_update(res["plan_id"], ops=ops))
        self.assertFalse(out["ok"])
        rb = out["rollback"]
        self.assertTrue(rb["refused"])
        self.assertFalse(rb["rolled_back"])
        self.assertIn("migration", rb["reason"])
        self.assertIn("does NOT undo", rb["explanation"])
        self.assertEqual(cu.get_plan(res["plan_id"])["status"], cu.ROLLBACK_REFUSED)

    def test_safe_rollback_restores_recorded_digests(self):
        res = self._plan()
        ops = self.ops_for(installed="v2.0.0")
        run(cu.apply_update(res["plan_id"], ops=ops))
        rb_ops = self.ops_for(installed="v1.0.0")
        rb = run(cu.rollback_plan(res["plan_id"], ops=rb_ops, reason="test"))
        self.assertTrue(rb["rolled_back"])
        self.assertFalse(rb["refused"])
        env = open(os.path.join(self.root, "app", "and.env")).read()
        self.assertIn("APP_VERSION=v1.0.0", env)
        self.assertTrue(os.path.isdir(rb["backup_dir"]))

    def test_no_command_can_delete_data(self):
        """Nothing in the allowlist removes images, volumes, or databases."""
        for name in policy.command_names():
            argv = " ".join(policy._COMMANDS[name]["argv"])
            for banned in (" rm", "rmi", "prune", "down", "volume", "DROP",
                           "TRUNCATE", "DELETE", "unlink", "rm -"):
                self.assertNotIn(banned, argv,
                                 f"command '{name}' must not be able to delete data")

    def test_apply_never_runs_without_going_through_the_gate(self):
        """container_apply_update is registered as a consequential tool, so
        tools.execute() stages a draft instead of running the handler."""
        spec = tools.REGISTRY["container_apply_update"]
        self.assertEqual(spec.action_type, "container_update")
        self.assertIsNotNone(spec.prepare)
        spec_rb = tools.REGISTRY["container_rollback_update"]
        self.assertEqual(spec_rb.action_type, "container_rollback")


# ── Release-note classification ────────────────────────────────────────────
class ReleaseNoteTests(Base):
    def test_breaking_and_migration_detection(self):
        s = cu.summarize_release("BREAKING CHANGE: config format changed.\n"
                                 "A database migration will run automatically.")
        self.assertTrue(s["breaking_changes"])
        self.assertTrue(s["migration_required"])

    def test_ordinary_release_is_not_flagged(self):
        s = cu.summarize_release("Fixes a crash when opening the settings page.")
        self.assertFalse(s["breaking_changes"])
        self.assertFalse(s["migration_required"])

    def test_version_comparison(self):
        self.assertEqual(cu.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(cu.parse_version("2.0"), (2, 0, 0))
        self.assertIsNone(cu.parse_version("release"))
        self.assertGreater(cu.parse_version("v2.0.0"), cu.parse_version("v1.9.9"))


if __name__ == "__main__":
    unittest.main()

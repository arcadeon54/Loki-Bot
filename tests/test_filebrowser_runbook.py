"""
Focused tests for the Filebrowser mount-path failure class.

The production failure this suite exists to prevent: filebrowser sat down from
2026-08-03 to 2026-08-09 and every reboot re-failed identically with

    error while creating mount source path '/mnt/unicron-downloads':
    mkdir /mnt/unicron-downloads: file exists

The path was not a file and was not "in the way". sshfs to unicron had died
without unmounting, leaving a STALE FUSE ENDPOINT: the dentry still resolves,
but every syscall on it returns ENOTCONN, and mkdir on it returns EEXIST —
which is the whole of Docker's misleading "file exists".

Two behaviours are pinned here, because getting either wrong recreates the
outage or invents a new one:

  A stale mountpoint must NEVER be restarted into. The bind fails identically
  every time; clearing it needs root fusermount3 (filesystem_repair, MANUAL).
  The runbook must escalate and name the path, not burn its auto-repair.

  A share being unmounted must NOT read as filebrowser being down. sshfs being
  inactive leaves an empty directory that binds fine — the service is healthy
  and /srv/unicron is merely empty. Scoring that as an outage would put back
  the 8-point Reliability deduction for a remote host being offline.

`ops` is a scripted fake keyed exactly like the policy allowlist, so no
container, mount, systemd unit or HTTP endpoint is touched.

Run:  venv/bin/python -m unittest tests.test_filebrowser_runbook -v
"""

import asyncio
import errno
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

# Bind DB/mirror paths at throwaway locations BEFORE importing anything that
# resolves them at import time, or the suite writes to production files.
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ.setdefault("HOMELAB_DB_PATH", _tmp_db.name)
_tmp_dir = tempfile.mkdtemp(prefix="filebrowser-runbook-test-")
os.environ.setdefault("HOMELAB_LIFECYCLE_MIRROR",
                      os.path.join(_tmp_dir, "lifecycle.yml"))
os.environ.setdefault("HOMELAB_DECOMMISSION_ARCHIVE_DIR",
                      os.path.join(_tmp_dir, "arch"))

from maintenance_runbooks import filebrowser_health as fb   # noqa: E402

sys.path.insert(0, "/home/g2k247/skillkit")
import skillkit.advisor as advisor                          # noqa: E402


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


ASSET = {
    "key": "filebrowser",
    "display_name": "Filebrowser",
    "runbook": "filebrowser_health",
    "docker": {
        "container": "filebrowser",
        "compose_file": "/home/g2k247/docker/filebrowser/docker-compose.yml",
    },
    "health": {
        "local_url": "http://127.0.0.1:8090/",
        "public_url": "https://media.ivn-group.cc",
    },
    "systemd": {"share_unit": "sshfs-unicron.service"},
    "mounts": {
        "database": "/home/g2k247/docker/filebrowser/data/filebrowser.db",
        "unicron": "/mnt/unicron-downloads",
        "nextcloud": "/mnt/nextcloud-webdav",
        "nas": "/mnt/nas",
    },
}


def inspect_json(state="running", exit_code=0, error="", health="healthy"):
    return json.dumps([{
        "State": {"Status": state, "ExitCode": exit_code, "Error": error,
                  "Health": {"Status": health} if health else None},
        "RestartCount": 0,
    }])


# The real create-time error Docker left on the container for six days.
BIND_ERROR = ("error while creating mount source path "
              "'/mnt/unicron-downloads': mkdir /mnt/unicron-downloads: "
              "file exists")

OK_DIR = {"exists": True, "mode": "0o755", "uid": 1000, "gid": 1000,
          "entries": 12, "empty": False}
EMPTY_DIR = dict(OK_DIR, entries=0, empty=True)
STALE = {"exists": False, "error": "OSError", "errno": errno.ENOTCONN,
         "stale_mount": True}
GONE = {"exists": False, "error": "FileNotFoundError",
        "errno": errno.ENOENT, "stale_mount": False}
DB_FILE = {"exists": True, "mode": "0o664", "uid": 1000, "gid": 1000,
           "entries": None, "empty": False}


class FakeOps:
    """Scripted executor. Command table is keyed like the policy allowlist."""

    def __init__(self, state="running", http=200, paths=None,
                 share_active=True, allow_repairs=False, attempted=(),
                 error="", restart_rc=0, http_after_restart=None):
        self.state = state
        self.http = http
        self.error = error
        self.share_active = share_active
        self.auto_repair_allowed = allow_repairs
        self.restart_rc = restart_rc
        self.http_after_restart = http_after_restart
        self.restarted = False
        self.paths = {
            "/mnt/unicron-downloads": OK_DIR,
            "/mnt/nextcloud-webdav": OK_DIR,
            "/mnt/nas": OK_DIR,
            "/home/g2k247/docker/filebrowser/data/filebrowser.db": DB_FILE,
            "/home/g2k247/docker/filebrowser/docker-compose.yml": DB_FILE,
        }
        self.paths.update(paths or {})
        self.commands_run = []
        self._attempted = set(attempted)
        self.recorded = []

    async def run(self, name, **params):
        self.commands_run.append({"name": name, "params": params})
        if name == "docker_inspect":
            return 0, inspect_json(self.state, 128 if self.error else 0,
                                   self.error,
                                   "healthy" if self.state == "running" else "")
        if name == "systemctl_is_active":
            return (0, "active") if self.share_active else (3, "inactive")
        if name == "docker_restart":
            if self.restart_rc == 0:
                self.restarted = True
                self.state = "running"
                if self.http_after_restart is not None:
                    self.http = self.http_after_restart
            return self.restart_rc, "" if self.restart_rc == 0 else BIND_ERROR
        return 1, ""

    async def path_meta(self, path):
        return self.paths.get(path, {"exists": False, "error": "unknown"})

    async def http_get(self, url, **_):
        return (self.http, "")

    def redact(self, text):
        return text

    async def sleep(self, _secs):
        return None

    async def attempted(self, action, target):
        return (action, target) in self._attempted

    async def record_attempt(self, action, target):
        self.recorded.append((action, target))


def check(result, name):
    return next((c for c in result["checks"] if c["name"] == name), None)


# ── 1. Healthy ─────────────────────────────────────────────────────────────
class Healthy(unittest.TestCase):
    def test_running_and_serving_needs_no_repair(self):
        r = run(fb.run(ASSET, FakeOps()))
        self.assertTrue(r["healthy"], [c for c in r["checks"] if not c["ok"]])
        self.assertIsNone(r["repair"])
        self.assertFalse(r["escalate"])
        self.assertTrue(check(r, "http_local")["ok"])

    def test_health_is_proven_by_http_not_by_container_state(self):
        """A running container that does not answer is not healthy."""
        r = run(fb.run(ASSET, FakeOps(http=502)))
        self.assertFalse(r["healthy"])


# ── 2. The stale mountpoint: the actual outage ─────────────────────────────
class StaleMountpoint(unittest.TestCase):
    def setUp(self):
        self.ops = FakeOps(state="exited", error=BIND_ERROR, http=0,
                           paths={"/mnt/unicron-downloads": STALE},
                           allow_repairs=True)
        self.r = run(fb.run(ASSET, self.ops))

    def test_it_is_not_healthy(self):
        self.assertFalse(self.r["healthy"])

    def test_it_never_restarts_into_a_bind_that_cannot_succeed(self):
        self.assertIsNone(self.r["repair"])
        self.assertFalse(self.ops.restarted)
        self.assertNotIn("docker_restart",
                         [c["name"] for c in self.ops.commands_run])

    def test_it_escalates_naming_the_path_and_the_real_cause(self):
        self.assertTrue(self.r["escalate"])
        d = self.r["diagnosis"]
        self.assertIn("/mnt/unicron-downloads", d)
        self.assertIn("stale", d.lower())
        self.assertIn("fusermount3", d)

    def test_it_does_not_spend_an_auto_repair_attempt(self):
        self.assertEqual(self.ops.recorded, [])

    def test_the_misleading_docker_wording_is_surfaced_as_a_bind_failure(self):
        c = check(self.r, "bind_mount_error")
        self.assertIsNotNone(c)
        self.assertFalse(c["ok"])

    def test_the_mount_check_says_stale_not_missing(self):
        detail = check(self.r, "mount:unicron")["detail"]
        self.assertIn("STALE", detail)
        self.assertIn("ENOTCONN", detail)


# ── 3. A share that is merely down is not an outage ────────────────────────
class ShareDownIsNotServiceDown(unittest.TestCase):
    def test_unmounted_share_leaves_filebrowser_healthy(self):
        """sshfs inactive → empty dir → the bind succeeds and HTTP works.
        Scoring this as failed would re-earn the 8-point deduction for a
        remote host being offline."""
        ops = FakeOps(share_active=False,
                      paths={"/mnt/unicron-downloads": EMPTY_DIR})
        r = run(fb.run(ASSET, ops))
        self.assertTrue(r["healthy"])
        self.assertFalse(r["escalate"])
        self.assertIn("/srv/unicron is empty", r["diagnosis"])

    def test_the_share_unit_state_is_reported_not_penalised(self):
        ops = FakeOps(share_active=False,
                      paths={"/mnt/unicron-downloads": EMPTY_DIR})
        r = run(fb.run(ASSET, ops))
        c = check(r, "share_unit")
        self.assertTrue(c["ok"])
        self.assertIn("inactive", c["detail"])

    def test_a_missing_mount_source_is_flagged_but_still_bindable(self):
        """Docker creates a missing bind source; that is degraded, not the
        stale case, and must not be described as one."""
        ops = FakeOps(paths={"/mnt/nextcloud-webdav": GONE})
        r = run(fb.run(ASSET, ops))
        self.assertFalse(check(r, "mount:nextcloud")["ok"])
        self.assertNotIn("stale", check(r, "mount:nextcloud")["detail"].lower())
        self.assertNotIn("fusermount3", r["diagnosis"])


# ── 4. Repair, and the proof it worked ─────────────────────────────────────
class RepairAndVerification(unittest.TestCase):
    def test_stopped_with_clean_mounts_restarts_and_verifies_http(self):
        ops = FakeOps(state="exited", http=0, allow_repairs=True,
                      http_after_restart=200)
        r = run(fb.run(ASSET, ops))
        self.assertEqual(r["repair"]["action"], "restart_stateless_service")
        self.assertTrue(ops.restarted)
        self.assertTrue(r["repair_result"]["ok"])
        self.assertTrue(r["repair_result"]["verified"])
        self.assertFalse(r["escalate"])

    def test_a_restart_that_does_not_restore_http_is_not_success(self):
        ops = FakeOps(state="exited", http=0, allow_repairs=True,
                      http_after_restart=0)
        r = run(fb.run(ASSET, ops))
        self.assertFalse(r["repair_result"]["ok"])
        self.assertTrue(r["escalate"])

    def test_a_bind_that_still_fails_is_caught_at_the_restart_not_later(self):
        ops = FakeOps(state="exited", http=0, allow_repairs=True,
                      restart_rc=1)
        r = run(fb.run(ASSET, ops))
        self.assertFalse(ops.restarted)
        self.assertFalse(r["repair_result"]["ok"])
        self.assertTrue(r["escalate"])

    def test_one_restart_per_window(self):
        ops = FakeOps(state="exited", http=0, allow_repairs=True,
                      attempted=[("restart_stateless_service", "filebrowser")])
        r = run(fb.run(ASSET, ops))
        self.assertIsNone(r["repair"])
        self.assertTrue(r["escalate"])
        self.assertFalse(ops.restarted)

    def test_missing_compose_never_restarts_blind(self):
        ops = FakeOps(state="exited", http=0, allow_repairs=True,
                      paths={"/home/g2k247/docker/filebrowser/"
                             "docker-compose.yml": GONE})
        r = run(fb.run(ASSET, ops))
        self.assertIsNone(r["repair"])
        self.assertTrue(r["escalate"])
        self.assertFalse(ops.restarted)


# ── 5. ENOTCONN is what makes the path distinguishable ─────────────────────
class PathMetaDistinguishesStaleFromMissing(unittest.TestCase):
    """Without an errno the runbook cannot tell a dead mountpoint from a
    deleted directory, and the two need opposite responses."""

    def _meta(self, exc):
        import homelab_maintenance as hm
        ops = hm.Ops.__new__(hm.Ops)
        path = "/mnt/unicron-downloads"
        with patch.object(hm, "_reg") as reg:
            reg.return_value.allowed_values.return_value = {"path": {path}}
            with patch("os.stat", side_effect=exc):
                return run(ops.path_meta(path))

    def test_enotconn_is_reported_as_a_stale_mount(self):
        m = self._meta(OSError(errno.ENOTCONN, "Transport endpoint is not connected"))
        self.assertFalse(m["exists"])
        self.assertTrue(m["stale_mount"])
        self.assertEqual(m["errno"], errno.ENOTCONN)

    def test_a_missing_path_is_not_a_stale_mount(self):
        m = self._meta(FileNotFoundError(errno.ENOENT, "No such file or directory"))
        self.assertFalse(m["exists"])
        self.assertFalse(m["stale_mount"])

    def test_undeclared_paths_are_still_refused(self):
        import homelab_maintenance as hm
        ops = hm.Ops.__new__(hm.Ops)
        with patch.object(hm, "_reg") as reg:
            reg.return_value.allowed_values.return_value = {"path": set()}
            m = run(ops.path_meta("/etc/shadow"))
        self.assertFalse(m["exists"])
        self.assertIn("not declared", m["error"])


# ── 6. Desired state: this outage vs. a workload nobody wants running ──────
class DesiredStateIsNotGuessed(unittest.TestCase):
    def test_filebrowser_down_is_expected_running_and_costs_points(self):
        state, expected = advisor.classify_stopped(
            "filebrowser", "Exited (128)", "unless-stopped", 128, set())
        self.assertEqual((state, expected), ("failed", True))

    def test_an_intentionally_stopped_sibling_is_not_confused_with_it(self):
        """Same host, same "not running", opposite meaning — the difference is
        read from the restart policy, never from the container's name."""
        state, expected = advisor.classify_stopped(
            "loki-joplin-api", "Exited (137)", "no", 137, set())
        self.assertEqual((state, expected), ("intentionally_stopped", False))

    def test_the_registry_entry_matches_the_running_deployment(self):
        """The runbook reads its container, compose file and share unit from
        the registry; a drifted entry would probe the wrong things."""
        import homelab_assets
        asset = homelab_assets.load().assets["filebrowser"]
        self.assertEqual(asset["runbook"], "filebrowser_health")
        self.assertEqual(asset["docker"]["container"], "filebrowser")
        self.assertEqual(asset["mounts"]["unicron"], "/mnt/unicron-downloads")
        self.assertEqual(asset["systemd"]["share_unit"], "sshfs-unicron.service")

    def test_the_runbook_named_by_the_registry_is_registered(self):
        import maintenance_runbooks
        self.assertIn("filebrowser_health", maintenance_runbooks.RUNBOOKS)


if __name__ == "__main__":
    unittest.main()

"""
Regression tests for NAS/Tracearr maintenance routing.

Background: Loki used to claim shell/Docker access to the UGREEN NAS (because
skillkit's hosts.json declared `nas` with an ssh transport) and then, finding
no way to act, improvise a manual `docker ps` tutorial for the Boss. The NAS
has no executor path at all: SSH is off in its UI and no credential exists.

These tests pin the two halves of the fix:
  - skillkit never advertises a non-operable host and fails it precisely
  - Loki answers NAS-hosted asset names with the concrete blocker, never a
    generic "unknown asset" and never manual instructions

Nothing here touches the network, SSH, or Docker: the disabled-host guard
returns before any transport runs, and asset resolution is pure config.

Run:  venv/bin/python -m unittest tests.test_nas_host_routing -v
"""

import asyncio
import os
import sys
import tempfile
import unittest

BOSS_ID = "111111111111111111"
os.environ.setdefault("OWNER_USER_ID", BOSS_ID)
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ.setdefault("HOMELAB_DB_PATH", _tmp.name)

import homelab_assets
import homelab_maintenance as hm

SKILLKIT = "/home/g2k247/skillkit"


def _remote():
    """skillkit lives in its own repo; import it only if present."""
    if SKILLKIT not in sys.path:
        sys.path.insert(0, SKILLKIT)
    from skillkit import remote
    remote.reload()
    return remote


class NasIsNotAdvertisedAsOperable(unittest.TestCase):
    """The planner must not be told it can reach a host it cannot."""

    def setUp(self):
        try:
            self.remote = _remote()
        except Exception as e:            # skillkit not installed here
            self.skipTest(f"skillkit unavailable: {e}")

    def test_nas_is_declared_but_not_operable(self):
        self.assertIn("nas", self.remote.host_names(),
                      "nas must stay declared so errors can name it precisely")
        self.assertNotIn("nas", self.remote.operable_host_names(),
                         "nas has no credential/SSH — never advertise it")

    def test_reachable_hosts_are_still_advertised(self):
        self.assertIn("dex247", self.remote.operable_host_names())

    def test_disabled_host_fails_with_the_concrete_blocker(self):
        r = asyncio.run(self.remote.shell("nas", "docker ps"))
        self.assertFalse(r.ok)
        self.assertEqual(r.error_type, "host_disabled")
        # Names what is actually wrong, so the model reports instead of guesses.
        self.assertIn("not operable", r.error)
        self.assertIn("SSH", r.error)

    def test_docker_helper_cannot_bypass_the_guard(self):
        """docker() routes through shell(); the guard must cover it too."""
        r = asyncio.run(self.remote.docker("nas", ["ps"]))
        self.assertFalse(r.ok)
        self.assertEqual(r.error_type, "host_disabled")

    def test_disabled_host_error_leaks_no_secret(self):
        r = asyncio.run(self.remote.shell("nas", "docker ps"))
        # The credential's NAME may appear; a value must never be reachable.
        self.assertNotIn(os.environ.get("NAS_SSH_PASSWORD", "\0unset\0"), r.error)
        self.assertNotIn("sshpass", r.error.lower())


class NasAssetsResolveToTheRealBlocker(unittest.TestCase):
    """Tracearr and friends must produce an accurate answer, not a guess."""

    NAS_NAMES = ["Tracearr", "tracearr", "nas", "NAS", "UGREEN",
                 "UGREEN NAS", "Unimatrix", "192.168.1.63", "Plex",
                 "Jellyseerr", "Watchtower"]

    def test_nas_hosted_names_report_the_unmanaged_host(self):
        for name in self.NAS_NAMES:
            with self.subTest(name=name):
                asset, err = hm._resolve_or_error(name)
                self.assertIsNone(asset, f"{name} must not resolve to a runbook")
                self.assertIn("UGREEN NAS", err)
                self.assertIn("not a managed host", err)

    def test_phrases_containing_a_nas_asset_still_hit_the_blocker(self):
        """The Boss types sentences, not registry keys."""
        for phrase in ("Tracearr Redis", "the NAS Plex server",
                       "Tracearr redis backend"):
            with self.subTest(phrase=phrase):
                asset, err = hm._resolve_or_error(phrase)
                self.assertIsNone(asset)
                self.assertIn("not a managed host", err)

    def test_registered_assets_win_over_unmanaged_matching(self):
        """Containment matching must never steal a real registered asset."""
        for name in ("media server", "Immich", "jellyfin"):
            with self.subTest(name=name):
                asset, _ = hm._resolve_or_error(name)
                self.assertIsNotNone(asset)

    def test_nas_names_do_not_fall_through_to_unknown_asset(self):
        """The old path said 'unknown asset', which reads as 'try harder'."""
        _, err = hm._resolve_or_error("Tracearr")
        self.assertNotIn("unknown asset", err)

    def test_manual_instruction_fallback_is_explicitly_blocked(self):
        for name in ("Tracearr", "bogus-thing"):
            with self.subTest(name=name):
                _, err = hm._resolve_or_error(name)
                self.assertIn("manual", err.lower())
                self.assertRegex(err.lower(), r"do not|must not")

    def test_raw_nas_ip_does_not_resolve_to_a_registered_asset(self):
        """A raw address must never become an actionable target."""
        self.assertIsNone(homelab_assets.load().resolve("192.168.1.63"))
        asset, err = hm._resolve_or_error("192.168.1.63")
        self.assertIsNone(asset)
        self.assertIn("not a managed host", err)

    def test_registered_local_assets_still_resolve(self):
        """The fix must not regress the assets that do work."""
        for name in ("jellyfin", "media server", "Immich", "BLACK-BOXX"):
            with self.subTest(name=name):
                asset, err = hm._resolve_or_error(name)
                self.assertIsNotNone(asset, err)

    def test_no_registered_asset_claims_a_non_dex247_host(self):
        """The controller refuses remote action; nothing should promise it."""
        reg = homelab_assets.load()
        for key, asset in reg.assets.items():
            with self.subTest(asset=key):
                self.assertEqual(asset.get("host"), "dex247")


if __name__ == "__main__":
    unittest.main()

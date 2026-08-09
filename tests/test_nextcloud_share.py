"""What a recipient outside the LAN is allowed to be sent.

The production failure this suite exists to prevent: Loki DM'd people
`http://192.168.1.63:8082/s/<token>`. That is RFC1918 space — nobody without
LAN or VPN access could open it, so the whole private-download feature was
delivering dead links that looked like success.

The subtle part, and the reason a naive fix would not have worked: this
Nextcloud reports the SAME internal address in the OCS response's own `url`
field, because `overwritehost`/`overwrite.cli.url` are unset behind the reverse
proxy. Simply "using the URL the API gave us" still leaks 192.168.1.63. So the
token is taken from the API — never invented — and only its origin is re-based
onto NEXTCLOUD_PUBLIC_BASE_URL.

Two rules are pinned hardest:

  A LINK IS NEVER FABRICATED. If Nextcloud did not create the share, no URL
  comes back. A plausible-looking guess would be indistinguishable from success
  to the caller and dead to the recipient.

  A PRIVATE ADDRESS IS NEVER EMITTED. If the only URL we could form points
  somewhere unroutable, that is reported as failure, because sending it is
  strictly worse than saying the upload failed.

No network, no Nextcloud, no credentials: `requests` is replaced wholesale.

Run:  venv/bin/python -m unittest tests.test_nextcloud_share -v
"""

import asyncio
import importlib
import os
import unittest
from unittest.mock import patch

TOKEN = "AbC123XyZ789Qwe"


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.content = text.encode() if text else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def ocs(data, statuscode=200):
    return {"ocs": {"meta": {"status": "ok", "statuscode": statuscode},
                    "data": data}}


# What this server actually returns: an internal host in its own `url` field.
SHARE_DATA = {"id": 42, "token": TOKEN,
              "url": f"https://192.168.1.63:8082/s/{TOKEN}",
              "expiration": "2026-08-12 23:59:59"}


class FakeRequests:
    """Stands in for the `requests` module. Records every call."""

    def __init__(self, share=SHARE_DATA, share_status=200, put_ok=True):
        self.share = share
        self.share_status = share_status
        self.put_ok = put_ok
        self.calls = []
        self.deleted_shares = []
        self.deleted_paths = []
        self.share_alive = True
        self.path_alive = True

    # -- helpers -------------------------------------------------------
    def _record(self, method, url, kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})

    def post(self, url, **kw):
        self._record("POST", url, kw)
        if self.share_status != 200:
            return FakeResponse(self.share_status, text="boom")
        if self.share is None:
            return FakeResponse(200, ocs([], statuscode=403))
        return FakeResponse(200, ocs(self.share))

    def get(self, url, **kw):
        self._record("GET", url, kw)
        if "/shares/" in url:
            if not self.share_alive:
                return FakeResponse(404, ocs([], statuscode=404))
            return FakeResponse(200, ocs([SHARE_DATA]))
        return FakeResponse(200, ocs([]))

    def delete(self, url, **kw):
        self._record("DELETE", url, kw)
        if "/shares/" in url:
            self.deleted_shares.append(url.rsplit("/", 1)[-1])
            self.share_alive = False
            return FakeResponse(200, ocs([]))
        self.deleted_paths.append(url)
        self.path_alive = False
        return FakeResponse(204, None)

    def put(self, url, **kw):
        self._record("PUT", url, kw)
        return FakeResponse(200 if self.put_ok else 500, ocs([]))

    def request(self, method, url, **kw):
        self._record(method, url, kw)
        if method == "MKCOL":
            return FakeResponse(201, None)
        if method == "PROPFIND":
            return FakeResponse(207 if self.path_alive else 404, None)
        return FakeResponse(200, None)


def tearDownModule():
    """Leave the module bound to the REAL environment.

    Every load_nc() call reloads nextcloud_integration under test env vars, and
    the module object is shared process-wide. Without this, running the whole
    suite with `discover` would leave a later-importing module holding a
    Nextcloud client pointed at cloud.example.test.
    """
    import nextcloud_integration
    importlib.reload(nextcloud_integration)


def load_nc(**env):
    """Import nextcloud_integration with a chosen environment."""
    base = {
        "NEXTCLOUD_URL": "http://192.168.1.63:8082",
        "NEXTCLOUD_USER": "svc",
        "NEXTCLOUD_PASS": "secret",
        "NEXTCLOUD_PUBLIC_BASE_URL": "https://cloud.example.test",
        "NEXTCLOUD_SHARE_EXPIRY_DAYS": "3",
        "NEXTCLOUD_KEEP_CLEARS_EXPIRY": "true",
    }
    base.update(env)
    with patch.dict(os.environ, base, clear=False):
        import nextcloud_integration
        return importlib.reload(nextcloud_integration)


# ── 1. The bug itself ──────────────────────────────────────────────────────
class PublicUrlNeverLeaksInternalAddress(unittest.TestCase):
    def setUp(self):
        self.nc = load_nc()
        self.fake = FakeRequests()

    def test_url_is_rebased_onto_the_public_host(self):
        with patch.object(self.nc, "requests", self.fake):
            share = run(self.nc.create_share("Loki Downloads/x/f.mp4"))
            url = self.nc._public_share_url(share)
        self.assertEqual(url, f"https://cloud.example.test/s/{TOKEN}")

    def test_the_internal_address_the_api_returned_is_discarded(self):
        """The OCS response's own url field says 192.168.1.63 — using it
        verbatim was never going to be enough."""
        self.assertIn("192.168.1.63", SHARE_DATA["url"])
        with patch.object(self.nc, "requests", self.fake):
            share = run(self.nc.create_share("x/f.mp4"))
            url = self.nc._public_share_url(share)
        self.assertNotIn("192.168.", url)
        self.assertNotIn("8082", url)

    def test_token_comes_from_the_api_and_is_not_invented(self):
        with patch.object(self.nc, "requests", self.fake):
            share = run(self.nc.create_share("x/f.mp4"))
            url = self.nc._public_share_url(share)
        self.assertTrue(url.endswith(f"/s/{TOKEN}"))

    def test_an_already_public_url_survives_rebasing_unchanged(self):
        """If Nextcloud's overwritehost is ever fixed, this must stay correct."""
        share = {"id": "1", "token": TOKEN,
                 "url": f"https://cloud.example.test/s/{TOKEN}"}
        self.assertEqual(self.nc._public_share_url(share),
                         f"https://cloud.example.test/s/{TOKEN}")

    def test_private_hosts_are_recognised(self):
        for host in ("192.168.1.63", "10.0.0.5", "172.16.4.1", "127.0.0.1",
                     "localhost", "nas.local", "box.lan", ""):
            self.assertTrue(self.nc._is_private_host(host), host)
        for host in ("cloud.ivn-group.cc", "example.com", "8.8.8.8"):
            self.assertFalse(self.nc._is_private_host(host), host)


# ── 2. Never fabricate a link ──────────────────────────────────────────────
class FailedShareNeverReturnsAUrl(unittest.TestCase):
    def test_http_error_yields_no_share(self):
        nc = load_nc()
        with patch.object(nc, "requests", FakeRequests(share_status=500)):
            self.assertIsNone(run(nc.create_share("x/f.mp4")))

    def test_ocs_level_refusal_yields_no_share(self):
        """HTTP 200 with an OCS failure code is still a failure."""
        nc = load_nc()
        with patch.object(nc, "requests", FakeRequests(share=None)):
            self.assertIsNone(run(nc.create_share("x/f.mp4")))

    def test_upload_and_share_returns_none_when_share_fails(self):
        nc = load_nc()
        fake = FakeRequests(share_status=500)
        with patch.object(nc, "requests", fake), \
             patch.object(nc, "_upload_file_sync", lambda a, b: True), \
             patch("os.remove", lambda p: None):
            self.assertIsNone(run(nc.upload_and_share(["/tmp/a.mp4"], "u", "d")))

    def test_no_public_base_and_private_server_refuses_rather_than_lying(self):
        """Worse than an error: a link that looks fine and cannot be opened."""
        nc = load_nc(NEXTCLOUD_PUBLIC_BASE_URL="")
        self.assertIsNone(nc._public_share_url(SHARE_DATA))

    def test_an_unpresentable_share_is_revoked_not_abandoned(self):
        nc = load_nc(NEXTCLOUD_PUBLIC_BASE_URL="")
        fake = FakeRequests()
        with patch.object(nc, "requests", fake), \
             patch.object(nc, "_upload_file_sync", lambda a, b: True), \
             patch("os.remove", lambda p: None):
            self.assertIsNone(run(nc.upload_and_share(["/tmp/a.mp4"], "u", "d")))
        self.assertEqual(fake.deleted_shares, ["42"],
                         "an unusable share must not be left published")


# ── 3. Expiration policy ───────────────────────────────────────────────────
class ExpirationIsConfigurable(unittest.TestCase):
    def test_default_is_72_hours(self):
        nc = load_nc()
        self.assertEqual(nc.SHARE_EXPIRY_DAYS, 3)

    def test_expiry_is_sent_on_creation(self):
        nc = load_nc(NEXTCLOUD_SHARE_EXPIRY_DAYS="7")
        fake = FakeRequests()
        with patch.object(nc, "requests", fake):
            run(nc.create_share("x/f.mp4"))
        post = [c for c in fake.calls if c["method"] == "POST"][0]
        self.assertIn("expireDate", post["data"])

    def test_zero_disables_expiry(self):
        nc = load_nc(NEXTCLOUD_SHARE_EXPIRY_DAYS="0")
        fake = FakeRequests()
        with patch.object(nc, "requests", fake):
            run(nc.create_share("x/f.mp4"))
        post = [c for c in fake.calls if c["method"] == "POST"][0]
        self.assertNotIn("expireDate", post["data"])

    def test_a_nonsense_value_falls_back_instead_of_crashing_startup(self):
        nc = load_nc(NEXTCLOUD_SHARE_EXPIRY_DAYS="soon")
        self.assertEqual(nc.SHARE_EXPIRY_DAYS, 3)


# ── 4. Keep ────────────────────────────────────────────────────────────────
class KeepPreservesTheLink(unittest.TestCase):
    def test_keep_clears_the_expiry_by_default(self):
        """Keep historically meant the link lasted indefinitely."""
        nc = load_nc()
        fake = FakeRequests()
        with patch.object(nc, "requests", fake):
            self.assertTrue(run(nc.keep_share({"share_id": "42"})))
        put = [c for c in fake.calls if c["method"] == "PUT"]
        self.assertEqual(len(put), 1)
        self.assertEqual(put[0]["data"], {"expireDate": ""})

    def test_keep_deletes_nothing(self):
        nc = load_nc()
        fake = FakeRequests()
        with patch.object(nc, "requests", fake):
            run(nc.keep_share({"share_id": "42"}))
        self.assertEqual(fake.deleted_shares, [])
        self.assertEqual(fake.deleted_paths, [])

    def test_keep_can_be_configured_to_respect_the_expiry(self):
        nc = load_nc(NEXTCLOUD_KEEP_CLEARS_EXPIRY="false")
        fake = FakeRequests()
        with patch.object(nc, "requests", fake):
            run(nc.keep_share({"share_id": "42"}))
        self.assertEqual([c for c in fake.calls if c["method"] == "PUT"], [])


# ── 5. Delete ──────────────────────────────────────────────────────────────
class DeleteRevokesAndVerifies(unittest.TestCase):
    def setUp(self):
        self.nc = load_nc()
        self.record = {"share_id": "42", "folder": "Loki Downloads/u/d/ab12"}

    def test_delete_revokes_the_share_and_removes_the_files(self):
        fake = FakeRequests()
        with patch.object(self.nc, "requests", fake):
            result = run(self.nc.revoke_and_delete(self.record))
        self.assertTrue(result["ok"])
        self.assertEqual(fake.deleted_shares, ["42"])
        self.assertEqual(len(fake.deleted_paths), 1)

    def test_delete_verifies_rather_than_assuming(self):
        """Reporting 'gone' while the link still resolves is the worst case."""
        fake = FakeRequests()
        with patch.object(self.nc, "requests", fake):
            run(self.nc.revoke_and_delete(self.record))
        methods = [c["method"] for c in fake.calls]
        self.assertIn("GET", methods, "share existence was never re-checked")
        self.assertIn("PROPFIND", methods, "file removal was never re-checked")

    def test_a_share_that_survives_revocation_is_reported_as_failure(self):
        fake = FakeRequests()
        with patch.object(self.nc, "requests", fake):
            with patch.object(self.nc, "_share_exists_sync", lambda i: True):
                result = run(self.nc.revoke_and_delete(self.record))
        self.assertFalse(result["share_revoked"])
        self.assertFalse(result["ok"])

    def test_an_already_absent_share_counts_as_revoked(self):
        nc = self.nc
        fake = FakeRequests()
        fake.share_alive = False
        with patch.object(nc, "requests", fake):
            self.assertTrue(run(nc.delete_share("42")))


# ── 6. Blast radius: one request, one share ────────────────────────────────
class ASharePublishesOnlyItsOwnBatch(unittest.TestCase):
    def _upload(self, files):
        nc = load_nc()
        fake = FakeRequests()
        with patch.object(nc, "requests", fake), \
             patch.object(nc, "_upload_file_sync", lambda a, b: True), \
             patch("os.remove", lambda p: None):
            rec = run(nc.upload_and_share(files, "user", "2026-08-09"))
        post = [c for c in fake.calls if c["method"] == "POST"][0]
        return rec, post["data"]["path"]

    def test_a_single_file_shares_the_file_not_its_folder(self):
        _rec, shared = self._upload(["/tmp/one.mp4"])
        self.assertTrue(shared.endswith("one.mp4"),
                        f"shared a container instead of the file: {shared}")

    def test_two_requests_the_same_day_do_not_share_a_folder(self):
        """The dated folder used to be reused, so one share exposed every file
        the requester had downloaded that day, and delete removed all of them."""
        a, _ = self._upload(["/tmp/one.mp4"])
        b, _ = self._upload(["/tmp/two.mp4"])
        self.assertNotEqual(a["folder"], b["folder"])

    def test_a_multi_file_share_is_scoped_to_that_batch_folder(self):
        rec, shared = self._upload(["/tmp/a.mp4", "/tmp/b.mp4"])
        self.assertEqual(shared.lstrip("/"), rec["folder"])
        self.assertNotEqual(rec["folder"], "Loki Downloads")
        self.assertTrue(rec["folder"].startswith("Loki Downloads/user/2026-08-09/"))

    def test_the_share_is_read_only(self):
        nc = load_nc()
        fake = FakeRequests()
        with patch.object(nc, "requests", fake):
            run(nc.create_share("x/f.mp4"))
        data = [c for c in fake.calls if c["method"] == "POST"][0]["data"]
        self.assertEqual(data["permissions"], nc.PERM_READ_ONLY)
        self.assertEqual(data["publicUpload"], "false")
        self.assertEqual(data["shareType"], nc.SHARE_TYPE_PUBLIC_LINK)


# ── 7. Internal vs public separation ───────────────────────────────────────
class InternalAndPublicUrlsStaySeparate(unittest.TestCase):
    def test_api_traffic_uses_the_internal_url(self):
        nc = load_nc()
        fake = FakeRequests()
        with patch.object(nc, "requests", fake):
            run(nc.create_share("x/f.mp4"))
            run(nc.delete_share("42"))
        for call in fake.calls:
            self.assertTrue(call["url"].startswith(nc.NC_URL),
                            f"API call left the internal endpoint: {call['url']}")

    def test_the_recipient_facing_url_uses_the_public_base(self):
        nc = load_nc()
        url = nc._public_share_url(SHARE_DATA)
        self.assertTrue(url.startswith(nc.NC_PUBLIC_BASE))
        self.assertFalse(url.startswith(nc.NC_URL))

    def test_public_base_defaults_to_internal_when_unset(self):
        """Sane on a LAN-only deploy — but _public_share_url still refuses."""
        nc = load_nc(NEXTCLOUD_PUBLIC_BASE_URL="")
        self.assertEqual(nc.NC_PUBLIC_BASE, nc.NC_URL)
        self.assertIsNone(nc._public_share_url(SHARE_DATA))

    def test_no_credentials_appear_in_a_recipient_url(self):
        nc = load_nc()
        url = nc._public_share_url(SHARE_DATA)
        self.assertNotIn("svc", url)
        self.assertNotIn("secret", url)
        self.assertNotIn("@", url.split("//", 1)[1])

    def test_the_url_exposes_no_filesystem_path(self):
        nc = load_nc()
        share = dict(SHARE_DATA, path="/Loki Downloads/user/2026-08-09/ab12/f.mp4")
        url = nc._public_share_url(share)
        self.assertNotIn("Loki", url)
        self.assertNotIn("remote.php", url)


# ── 8. The load-order invariant that made all of this moot ─────────────────
class DotenvLoadsBeforeProjectImports(unittest.TestCase):
    """systemd starts loki.service with a bare environment — no EnvironmentFile
    — so .env is the ONLY source of configuration. Several project modules read
    their config with os.getenv at import time, which means a load_dotenv()
    placed after those imports leaves them pinned to their hard-coded
    fallbacks, silently and permanently.

    That is not hypothetical: it is why nextcloud_integration was talking to
    192.168.1.247:8082 — the pre-rebuild asus box, long unreachable — instead
    of the NEXTCLOUD_URL in .env, and why jd_integration held empty
    MyJDownloader credentials. Nothing logged an error; the feature simply
    could not work.
    """

    def _source(self):
        with open("/home/g2k247/loki-bot/loki_bot.py") as f:
            return f.read().split("\n")

    def test_load_dotenv_precedes_every_project_import(self):
        lines = self._source()
        first_load = next(i for i, l in enumerate(lines)
                          if l.strip() == "load_dotenv()")
        project_imports = [
            "import nextcloud_integration",
            "import jd_integration",
            "import ha_integration",
            "import jobsite_db",
            "from grammar_roast",
        ]
        for needle in project_imports:
            idx = next((i for i, l in enumerate(lines) if l.strip().startswith(needle)), None)
            self.assertIsNotNone(idx, f"{needle} not found in loki_bot.py")
            self.assertLess(
                first_load, idx,
                f"load_dotenv() must precede `{needle}` — that module reads "
                "os.getenv at import time and systemd supplies no environment")

    def test_the_stale_fallback_host_is_not_what_dotenv_supplies(self):
        """A guard against the fallback quietly becoming correct-looking."""
        from dotenv import dotenv_values
        env = dotenv_values("/home/g2k247/loki-bot/.env")
        self.assertIn("NEXTCLOUD_URL", env)
        self.assertNotIn("192.168.1.247", env["NEXTCLOUD_URL"])
        self.assertIn("NEXTCLOUD_PUBLIC_BASE_URL", env)
        self.assertTrue(env["NEXTCLOUD_PUBLIC_BASE_URL"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()

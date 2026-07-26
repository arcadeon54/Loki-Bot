"""
Focused tests for social_link_dedup.py: URL canonicalization, hashing,
atomic claim/duplicate recording, escalation windows, and retention.

No discord.py dependency — pure logic + an in-memory sqlite DB.

Run:  venv/bin/python -m unittest tests.test_social_link_dedup -v
"""
import datetime
import sqlite3
import unittest

import social_link_dedup as sld


def _now(offset_days=0):
    return (
        datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        + datetime.timedelta(days=offset_days)
    ).isoformat()


class URLCanonicalizationTests(unittest.TestCase):
    def test_youtube_watch_vs_short_link(self):
        a = sld.canonicalize("https://www.youtube.com/watch?v=abc123XYZ_-", "youtube")
        b = sld.canonicalize("https://youtu.be/abc123XYZ_-", "youtube")
        self.assertEqual(a, b)

    def test_youtube_tracking_params_ignored(self):
        a = sld.canonicalize("https://youtu.be/abc123XYZ_-?si=trackingjunk", "youtube")
        b = sld.canonicalize("https://youtu.be/abc123XYZ_-", "youtube")
        self.assertEqual(a, b)

    def test_youtube_different_videos_do_not_match(self):
        a = sld.canonicalize("https://youtu.be/abc123XYZ_-", "youtube")
        b = sld.canonicalize("https://youtu.be/differentID1", "youtube")
        self.assertNotEqual(a, b)

    def test_instagram_reel_id_extraction(self):
        a = sld.canonicalize("https://www.instagram.com/reel/Cxyz123ABC/", "instagram")
        b = sld.canonicalize("https://instagram.com/reel/Cxyz123ABC/?igshid=abc123", "instagram")
        self.assertEqual(a, b)

    def test_instagram_different_posts_do_not_match(self):
        a = sld.canonicalize("https://www.instagram.com/p/AAA111/", "instagram")
        b = sld.canonicalize("https://www.instagram.com/p/BBB222/", "instagram")
        self.assertNotEqual(a, b)

    def test_tiktok_video_id_with_tracking_variation(self):
        a = sld.canonicalize(
            "https://www.tiktok.com/@someuser/video/7123456789012345678", "tiktok"
        )
        b = sld.canonicalize(
            "https://www.tiktok.com/@otheruser/video/7123456789012345678?is_from_webapp=1&sender_device=pc",
            "tiktok",
        )
        self.assertEqual(a, b)

    def test_tiktok_different_videos_do_not_match(self):
        a = sld.canonicalize("https://www.tiktok.com/@u/video/111111111111111111", "tiktok")
        b = sld.canonicalize("https://www.tiktok.com/@u/video/222222222222222222", "tiktok")
        self.assertNotEqual(a, b)

    def test_http_vs_https_and_case_and_trailing_slash(self):
        a = sld.canonicalize("http://YOUTU.BE/xyz987654321", "youtube")
        b = sld.canonicalize("https://youtu.be/xyz987654321/", "youtube")
        self.assertEqual(a, b)

    def test_fragment_and_utm_params_ignored_in_fallback(self):
        a = sld.canonicalize(
            "https://www.reddit.com/r/foo/some/weird/path?utm_source=share#comments", "reddit"
        )
        b = sld.canonicalize("https://reddit.com/r/foo/some/weird/path", "reddit")
        self.assertEqual(a, b)

    def test_mobile_vs_normal_hostname(self):
        a = sld.canonicalize("https://m.youtube.com/watch?v=mobiletest1", "youtube")
        b = sld.canonicalize("https://www.youtube.com/watch?v=mobiletest1", "youtube")
        self.assertEqual(a, b)

    def test_discord_formatting_wrapper_stripped(self):
        raw = "<https://youtu.be/wrapped12345>"
        clean = sld.strip_wrapping(raw)
        self.assertEqual(clean, "https://youtu.be/wrapped12345")

    def test_extract_supported_links_ignores_unsupported_platform(self):
        text = "check this out https://example.com/foo and https://youtu.be/abc123"
        found = sld.extract_supported_links(text)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0][1], "youtube")

    def test_hash_is_sha256_hex(self):
        h = sld.canonical_hash("https://youtu.be/abc123XYZ_-", "youtube")
        self.assertEqual(len(h), 64)
        int(h, 16)  # doesn't raise


class DuplicateClaimTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        sld.ensure_schema(self.conn)

    def test_first_claim_is_new(self):
        r = sld.claim_or_record_duplicate(
            self.conn, guild_id="g1", link_hash="h1", platform="youtube",
            message_id="m1", channel_id="c1", user_id="u1", now_iso=_now(),
        )
        self.assertTrue(r.is_new)
        self.assertEqual(r.duplicate_count, 0)

    def test_second_claim_same_hash_is_duplicate(self):
        sld.claim_or_record_duplicate(
            self.conn, guild_id="g1", link_hash="h1", platform="youtube",
            message_id="m1", channel_id="c1", user_id="u1", now_iso=_now(),
        )
        r2 = sld.claim_or_record_duplicate(
            self.conn, guild_id="g1", link_hash="h1", platform="youtube",
            message_id="m2", channel_id="c2", user_id="u2", now_iso=_now(),
        )
        self.assertFalse(r2.is_new)
        self.assertEqual(r2.duplicate_count, 1)

    def test_different_guild_does_not_collide(self):
        sld.claim_or_record_duplicate(
            self.conn, guild_id="g1", link_hash="h1", platform="youtube",
            message_id="m1", channel_id="c1", user_id="u1", now_iso=_now(),
        )
        r2 = sld.claim_or_record_duplicate(
            self.conn, guild_id="g2", link_hash="h1", platform="youtube",
            message_id="m2", channel_id="c2", user_id="u2", now_iso=_now(),
        )
        self.assertTrue(r2.is_new)

    def test_simultaneous_claim_exactly_one_winner(self):
        # No await between INSERT and rowcount check in claim_or_record_duplicate,
        # so simulating "simultaneous" posts as back-to-back calls is representative
        # of the actual concurrency guarantee on a single event loop.
        results = [
            sld.claim_or_record_duplicate(
                self.conn, guild_id="g1", link_hash="race", platform="tiktok",
                message_id=f"m{i}", channel_id=f"c{i}", user_id=f"u{i}", now_iso=_now(),
            )
            for i in range(5)
        ]
        winners = [r for r in results if r.is_new]
        self.assertEqual(len(winners), 1)

    def test_state_survives_simulated_restart(self):
        sld.claim_or_record_duplicate(
            self.conn, guild_id="g1", link_hash="persist", platform="youtube",
            message_id="m1", channel_id="c1", user_id="u1", now_iso=_now(),
        )
        # Simulate restart: reconnect using the same on-disk file.
        import tempfile, os
        path = tempfile.mktemp(suffix=".db")
        try:
            disk_conn = sqlite3.connect(path)
            sld.ensure_schema(disk_conn)
            sld.claim_or_record_duplicate(
                disk_conn, guild_id="g1", link_hash="persist", platform="youtube",
                message_id="m1", channel_id="c1", user_id="u1", now_iso=_now(),
            )
            disk_conn.close()

            reopened = sqlite3.connect(path)
            r = sld.claim_or_record_duplicate(
                reopened, guild_id="g1", link_hash="persist", platform="youtube",
                message_id="m2", channel_id="c2", user_id="u2", now_iso=_now(),
            )
            self.assertFalse(r.is_new)  # still recognized as duplicate after "restart"
            reopened.close()
        finally:
            if os.path.exists(path):
                os.remove(path)


class EscalationWindowTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        sld.ensure_schema(self.conn)

    def test_level_increases_on_repeat_within_window(self):
        l1 = sld.bump_user_warning_level(
            self.conn, guild_id="g1", user_id="u1", now_iso=_now(0), window_days=30
        )
        l2 = sld.bump_user_warning_level(
            self.conn, guild_id="g1", user_id="u1", now_iso=_now(1), window_days=30
        )
        l3 = sld.bump_user_warning_level(
            self.conn, guild_id="g1", user_id="u1", now_iso=_now(2), window_days=30
        )
        self.assertEqual([l1, l2, l3], [1, 2, 3])

    def test_level_resets_after_window_expires(self):
        sld.bump_user_warning_level(
            self.conn, guild_id="g1", user_id="u1", now_iso=_now(0), window_days=30
        )
        sld.bump_user_warning_level(
            self.conn, guild_id="g1", user_id="u1", now_iso=_now(1), window_days=30
        )
        level_after_gap = sld.bump_user_warning_level(
            self.conn, guild_id="g1", user_id="u1", now_iso=_now(40), window_days=30
        )
        self.assertEqual(level_after_gap, 1)

    def test_different_users_track_independently(self):
        sld.bump_user_warning_level(
            self.conn, guild_id="g1", user_id="u1", now_iso=_now(0), window_days=30
        )
        l_other = sld.bump_user_warning_level(
            self.conn, guild_id="g1", user_id="u2", now_iso=_now(0), window_days=30
        )
        self.assertEqual(l_other, 1)


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        sld.ensure_schema(self.conn)

    def test_zero_retention_means_indefinite_no_purge(self):
        sld.claim_or_record_duplicate(
            self.conn, guild_id="g1", link_hash="h1", platform="youtube",
            message_id="m1", channel_id="c1", user_id="u1", now_iso=_now(0),
        )
        purged = sld.purge_expired(self.conn, retention_days=0, now_iso=_now(9999))
        self.assertEqual(purged, 0)

    def test_positive_retention_purges_old_rows(self):
        sld.claim_or_record_duplicate(
            self.conn, guild_id="g1", link_hash="h1", platform="youtube",
            message_id="m1", channel_id="c1", user_id="u1", now_iso=_now(0),
        )
        purged = sld.purge_expired(self.conn, retention_days=10, now_iso=_now(20))
        self.assertEqual(purged, 1)


class MixedContentTests(unittest.TestCase):
    def test_short_caption_with_link_is_primarily_links(self):
        content = "lol check this out https://youtu.be/abc123"
        self.assertTrue(sld.is_primarily_links(content, ["https://youtu.be/abc123"], 100))

    def test_long_unrelated_text_is_not_primarily_links(self):
        content = (
            "So I was thinking about the whole plan for this weekend, and honestly "
            "I think we should just wing it, also here's that clip https://youtu.be/abc123 "
            "anyway let me know what you think about dinner"
        )
        self.assertFalse(sld.is_primarily_links(content, ["https://youtu.be/abc123"], 100))


if __name__ == "__main__":
    unittest.main()

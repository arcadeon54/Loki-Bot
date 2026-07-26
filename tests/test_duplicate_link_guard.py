"""
Focused tests for the repaired cross-channel duplicate-link guard wired into
loki_bot.py's on_message pipeline (_handle_duplicate_link_guard et al).

This guard suppresses/deletes reposted supported social-media links across
all channels in a guild and warns the poster — it never forwards anywhere.

Discord objects are duck-typed fakes/mocks — no live Discord connection.
MEMORY_DB_PATH is redirected to a temp file before importing loki_bot so
these tests never touch the production database.

Run:  venv/bin/python -m unittest tests.test_duplicate_link_guard -v
"""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("MEMORY_DB_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("OWNER_USER_ID", "111111111111111111")

# loki_bot optionally imports homelab_lifecycle/homelab_monitor, which bind an
# env-derived mirror-file path at import time. If this test file (unrelated
# to homelab lifecycle) happens to import loki_bot before
# tests/test_homelab_lifecycle.py sets its own sandboxed path, that module
# gets permanently cached with the wrong path. Block those unrelated optional
# imports here — loki_bot degrades gracefully (they're try/except-guarded) —
# so import order never determines which test suite "wins" that binding.
_blocked_for_import = [
    name for name in ("homelab_lifecycle", "homelab_monitor")
    if sys.modules.setdefault(name, None) is None
]

import discord  # noqa: E402
import loki_bot  # noqa: E402

# Undo the block once loki_bot has finished its own optional imports, so any
# other test module that legitimately needs the real thing still can.
for _name in _blocked_for_import:
    if sys.modules.get(_name) is None:
        del sys.modules[_name]


class FakeAuthor:
    def __init__(self, user_id, name="tester", bot=False):
        self.id = user_id
        self.display_name = name
        self.mention = f"<@{user_id}>"
        self.bot = bot


class FakeChannel:
    def __init__(self, channel_id, name="general"):
        self.id = channel_id
        self.name = name
        self.send = AsyncMock()


class FakeGuild:
    def __init__(self, guild_id):
        self.id = guild_id


class FakeMessage:
    def __init__(self, content, author, channel, guild, msg_id=1, delete_error=None):
        self.content = content
        self.author = author
        self.channel = channel
        self.guild = guild
        self.id = msg_id
        self.delete = AsyncMock(side_effect=delete_error)


def tearDownModule():
    # IsolatedAsyncioTestCase clears the thread's default event loop on
    # cleanup (Python 3.12 no longer auto-creates one). Some other test
    # modules in this suite still rely on the deprecated implicit
    # asyncio.get_event_loop() pattern; restore a loop so discovery order
    # doesn't matter for them.
    asyncio.set_event_loop(asyncio.new_event_loop())


class DuplicateLinkGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import sqlite3
        import social_link_dedup as sld
        loki_bot.memory.conn = sqlite3.connect(":memory:")
        sld.ensure_schema(loki_bot.memory.conn)

        self.guild = FakeGuild(1000)

        loki_bot.DUPLICATE_LINK_DETECTION_ENABLED = True
        loki_bot.DUPLICATE_LINK_ESCALATION_WINDOW_DAYS = 30
        loki_bot.DUPLICATE_LINK_WARNING_DELETE_AFTER = 20
        loki_bot.DUPLICATE_LINK_CAPTION_MAX_CHARS = 100
        loki_bot.DUPLICATE_LINK_EXCLUDED_CHANNEL_IDS = set()

        self.llm_calls = []

        async def fake_chat(msgs):
            self.llm_calls.append(msgs)
            return "That one already made the rounds, champ."

        loki_bot.llm.chat = fake_chat

    # First occurrence is left alone
    async def test_first_link_untouched(self):
        chan_a = FakeChannel(1)
        msg = FakeMessage(
            "check this out https://youtu.be/abc123", FakeAuthor(1), chan_a, self.guild
        )
        handled = await loki_bot._handle_duplicate_link_guard(msg)
        self.assertFalse(handled)  # doesn't short-circuit on_message
        msg.delete.assert_not_called()
        chan_a.send.assert_not_called()

    # Root-cause regression: a link posted in an ORDINARY channel (not the
    # download channel, not an auto-watch channel) must still be caught on
    # repost. Before the fix, nothing ever recorded it in this scenario.
    async def test_duplicate_caught_in_ordinary_channel_no_download_trigger(self):
        chan_a = FakeChannel(1, name="general")
        chan_b = FakeChannel(2, name="off-topic")
        # Neither channel is DOWNLOAD_CHANNEL_ID or in AUTO_WATCH_CHANNEL_IDS,
        # and no "save this"/"post this" trigger phrase is present — this is
        # exactly the case that silently fell through before the repair.
        first = FakeMessage("just saw this lol https://youtu.be/rootcause1", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        handled_first = await loki_bot._handle_duplicate_link_guard(first)
        self.assertFalse(handled_first)

        dupe = FakeMessage("https://youtu.be/rootcause1", FakeAuthor(2), chan_b, self.guild, msg_id=2)
        handled_dupe = await loki_bot._handle_duplicate_link_guard(dupe)

        self.assertTrue(handled_dupe)
        dupe.delete.assert_awaited_once()
        chan_b.send.assert_awaited_once()

    # Exact duplicate in the same channel is deleted and suppressed
    async def test_duplicate_same_channel_deleted(self):
        chan_a = FakeChannel(1)
        first = FakeMessage("https://youtu.be/dupe111", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_duplicate_link_guard(first)

        dupe = FakeMessage("https://youtu.be/dupe111", FakeAuthor(1), chan_a, self.guild, msg_id=2)
        handled = await loki_bot._handle_duplicate_link_guard(dupe)

        self.assertTrue(handled)
        dupe.delete.assert_awaited_once()
        chan_a.send.assert_awaited_once()

    # Exact duplicate in another channel is deleted and suppressed
    async def test_duplicate_other_channel_deleted(self):
        chan_a = FakeChannel(1)
        chan_b = FakeChannel(2)
        first = FakeMessage("https://youtu.be/crosschan", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_duplicate_link_guard(first)

        dupe = FakeMessage("https://youtu.be/crosschan", FakeAuthor(2, "other"), chan_b, self.guild, msg_id=2)
        handled = await loki_bot._handle_duplicate_link_guard(dupe)

        self.assertTrue(handled)
        dupe.delete.assert_awaited_once()
        chan_b.send.assert_awaited_once()

    # Duplicate posted by another user is detected
    async def test_duplicate_by_different_user(self):
        chan_a = FakeChannel(1)
        first = FakeMessage("https://youtu.be/anotherusr", FakeAuthor(1, "alice"), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_duplicate_link_guard(first)

        dupe = FakeMessage("https://youtu.be/anotherusr", FakeAuthor(2, "bob"), chan_a, self.guild, msg_id=2)
        handled = await loki_bot._handle_duplicate_link_guard(dupe)
        self.assertTrue(handled)
        warning_msg = chan_a.send.call_args_list[-1][0][0]
        self.assertIn("<@2>", warning_msg)  # warned the duplicate poster, not the original

    # Excluded channels are never touched by the guard
    async def test_excluded_channel_ignored(self):
        excluded = FakeChannel(555, name="staging-dump")
        loki_bot.DUPLICATE_LINK_EXCLUDED_CHANNEL_IDS = {555}
        first = FakeMessage("https://youtu.be/inexcluded", FakeAuthor(1), excluded, self.guild, msg_id=1)
        await loki_bot._handle_duplicate_link_guard(first)

        dupe = FakeMessage("https://youtu.be/inexcluded", FakeAuthor(2), excluded, self.guild, msg_id=2)
        handled = await loki_bot._handle_duplicate_link_guard(dupe)
        self.assertFalse(handled)
        excluded.send.assert_not_called()

    # Bot/webhook messages are ignored
    async def test_bot_author_ignored(self):
        chan_a = FakeChannel(1)
        msg = FakeMessage(
            "https://youtu.be/frombot", FakeAuthor(1, bot=True), chan_a, self.guild
        )
        handled = await loki_bot._handle_duplicate_link_guard(msg)
        self.assertFalse(handled)
        chan_a.send.assert_not_called()

    # Multiple links in one message: only the duplicate is flagged, the
    # genuinely-new link in the same message is unaffected.
    async def test_multi_link_message_only_flags_duplicate(self):
        chan_a = FakeChannel(1)
        first = FakeMessage("https://youtu.be/multilink1", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_duplicate_link_guard(first)

        mixed = FakeMessage(
            "two clips: https://youtu.be/multilink1 and https://youtu.be/multilink2",
            FakeAuthor(2), chan_a, self.guild, msg_id=2,
        )
        handled = await loki_bot._handle_duplicate_link_guard(mixed)

        self.assertTrue(handled)
        mixed.delete.assert_not_called()  # mixed content: message preserved
        chan_a.send.assert_awaited_once()  # exactly one warning

        # The new link (multilink2) must remain claimable as an original —
        # posting it again elsewhere should now be flagged.
        again = FakeMessage("https://youtu.be/multilink2", FakeAuthor(3), FakeChannel(9), self.guild, msg_id=3)
        handled_again = await loki_bot._handle_duplicate_link_guard(again)
        self.assertTrue(handled_again)

    # Mixed content (new + duplicate) preserves the message, still warns once
    async def test_mixed_content_preserves_message(self):
        chan_a = FakeChannel(1)
        first = FakeMessage("https://youtu.be/olddupe", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_duplicate_link_guard(first)

        mixed = FakeMessage(
            "also check https://youtu.be/olddupe and this new one https://youtu.be/brandnew99",
            FakeAuthor(2), chan_a, self.guild, msg_id=2,
        )
        handled = await loki_bot._handle_duplicate_link_guard(mixed)

        self.assertTrue(handled)
        mixed.delete.assert_not_called()
        chan_a.send.assert_awaited_once()

    # Missing delete permission does not retry, still suppresses + warns
    async def test_missing_delete_permission_handled_gracefully(self):
        chan_a = FakeChannel(1)
        first = FakeMessage("https://youtu.be/noperms", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_duplicate_link_guard(first)

        fake_resp = MagicMock(status=403, reason="Forbidden")
        forbidden = discord.Forbidden(fake_resp, "Missing Permissions")
        dupe = FakeMessage(
            "https://youtu.be/noperms", FakeAuthor(2), chan_a, self.guild, msg_id=2,
            delete_error=forbidden,
        )
        handled = await loki_bot._handle_duplicate_link_guard(dupe)

        self.assertTrue(handled)
        dupe.delete.assert_awaited_once()  # tried exactly once, no retry
        chan_a.send.assert_awaited_once()  # warning still sent

    # Warnings contain no URL, no channel mention
    async def test_warning_contains_no_url_or_channel_reference(self):
        chan_a = FakeChannel(424242)
        first = FakeMessage("https://youtu.be/nourl", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_duplicate_link_guard(first)

        dupe = FakeMessage("https://youtu.be/nourl", FakeAuthor(2), chan_a, self.guild, msg_id=2)
        await loki_bot._handle_duplicate_link_guard(dupe)

        warning_msg = chan_a.send.call_args[0][0]
        self.assertNotIn("http://", warning_msg)
        self.assertNotIn("https://", warning_msg)
        self.assertNotIn("<#", warning_msg)  # channel-mention syntax
        prompt_text = " ".join(m["content"] for m in self.llm_calls[-1])
        self.assertNotIn("<#", prompt_text)
        self.assertNotIn(str(chan_a.id), prompt_text)

    # No timeout, mute, ban, role, or moderator action occurs
    async def test_no_moderation_actions_taken(self):
        chan_a = FakeChannel(1)
        author = FakeAuthor(2)
        author.timeout = AsyncMock()
        author.ban = AsyncMock()
        author.kick = AsyncMock()
        author.add_roles = AsyncMock()
        author.remove_roles = AsyncMock()

        first = FakeMessage("https://youtu.be/nomod", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_duplicate_link_guard(first)

        dupe = FakeMessage("https://youtu.be/nomod", author, chan_a, self.guild, msg_id=2)
        await loki_bot._handle_duplicate_link_guard(dupe)

        author.timeout.assert_not_called()
        author.ban.assert_not_called()
        author.kick.assert_not_called()
        author.add_roles.assert_not_called()
        author.remove_roles.assert_not_called()

    # Non-social URLs are not intercepted by this guard (unaffected feature)
    async def test_non_social_url_not_intercepted(self):
        chan_a = FakeChannel(1)
        msg = FakeMessage(
            "here's a file https://example.com/some/file.zip", FakeAuthor(1), chan_a, self.guild
        )
        handled = await loki_bot._handle_duplicate_link_guard(msg)
        self.assertFalse(handled)
        chan_a.send.assert_not_called()

    # Escalation wording changes across repeated duplicates by the same user
    async def test_escalation_level_passed_to_warning_prompt(self):
        chan_a = FakeChannel(1)
        author = FakeAuthor(5, "repeat-offender")
        first = FakeMessage("https://youtu.be/esc1", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_duplicate_link_guard(first)

        d1 = FakeMessage("https://youtu.be/esc1", author, chan_a, self.guild, msg_id=2)
        await loki_bot._handle_duplicate_link_guard(d1)

        d2_msg = FakeMessage("https://youtu.be/esc1", author, chan_a, self.guild, msg_id=3)
        await loki_bot._handle_duplicate_link_guard(d2_msg)

        first_prompt = self.llm_calls[0][1]["content"]
        second_prompt = self.llm_calls[1][1]["content"]
        self.assertIn("first duplicate", first_prompt.lower())
        self.assertIn("second duplicate", second_prompt.lower())

    # Edited messages that introduce a duplicate link are caught once
    async def test_edited_message_introducing_duplicate_link(self):
        chan_a = FakeChannel(1)
        first = FakeMessage("https://youtu.be/editdupe", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_duplicate_link_guard(first)

        before = FakeMessage("no link yet", FakeAuthor(2), chan_a, self.guild, msg_id=2)
        after = FakeMessage("no link yet, actually: https://youtu.be/editdupe", FakeAuthor(2), chan_a, self.guild, msg_id=2)
        await loki_bot._handle_duplicate_link_guard_edit(before, after)

        chan_a.send.assert_awaited_once()

    # Edits that don't introduce a new link are ignored
    async def test_edited_message_without_new_link_ignored(self):
        chan_a = FakeChannel(1)
        before = FakeMessage("hello", FakeAuthor(2), chan_a, self.guild, msg_id=2)
        after = FakeMessage("hello world", FakeAuthor(2), chan_a, self.guild, msg_id=2)
        await loki_bot._handle_duplicate_link_guard_edit(before, after)
        chan_a.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()

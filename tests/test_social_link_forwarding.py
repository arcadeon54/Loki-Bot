"""
Focused tests for the Hell Yeah Films duplicate-link forwarding feature wired
into loki_bot.py's on_message pipeline (_handle_social_link_dedup et al).

Discord objects are duck-typed fakes/mocks — no live Discord connection.
MEMORY_DB_PATH is redirected to a temp file before importing loki_bot so
these tests never touch the production database.

Run:  venv/bin/python -m unittest tests.test_social_link_forwarding -v
"""
import asyncio
import os
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("MEMORY_DB_PATH", tempfile.mktemp(suffix=".db"))
os.environ.setdefault("OWNER_USER_ID", "111111111111111111")

import discord  # noqa: E402
import loki_bot  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


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
    def __init__(self, guild_id, text_channels=None):
        self.id = guild_id
        self.text_channels = text_channels or []


class FakeMessage:
    def __init__(self, content, author, channel, guild, msg_id=1, delete_error=None):
        self.content = content
        self.author = author
        self.channel = channel
        self.guild = guild
        self.id = msg_id
        self._delete_error = delete_error
        self.delete = AsyncMock(side_effect=delete_error)


class SocialLinkForwardingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Isolated sqlite state per test.
        import sqlite3
        import social_link_dedup as sld
        loki_bot.memory.conn = sqlite3.connect(":memory:")
        sld.ensure_schema(loki_bot.memory.conn)

        self.guild = FakeGuild(1000)
        self.hyf_channel = FakeChannel(9999, name="hell-yeah-films")

        loki_bot.HELL_YEAH_FILMS_CHANNEL_ID = self.hyf_channel.id
        loki_bot.DUPLICATE_LINK_DETECTION_ENABLED = True
        loki_bot.DUPLICATE_LINK_ESCALATION_WINDOW_DAYS = 30
        loki_bot.DUPLICATE_LINK_WARNING_DELETE_AFTER = 20
        loki_bot.DUPLICATE_LINK_CAPTION_MAX_CHARS = 100
        loki_bot.DUPLICATE_LINK_EXCLUDED_CHANNEL_IDS = set()

        loki_bot.bot.get_channel = MagicMock(return_value=self.hyf_channel)

        self.llm_calls = []

        async def fake_chat(msgs):
            self.llm_calls.append(msgs)
            return "That one already made the rounds, champ."

        loki_bot.llm.chat = fake_chat

    async def _pending_tasks(self):
        # Let any asyncio.create_task(...) forward calls actually run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # 1. First link accepted and forwarded
    async def test_first_link_forwarded(self):
        chan_a = FakeChannel(1)
        msg = FakeMessage(
            "check this out https://youtu.be/abc123", FakeAuthor(1), chan_a, self.guild
        )
        handled = await loki_bot._handle_social_link_dedup(msg)
        await self._pending_tasks()
        self.assertFalse(handled)  # doesn't short-circuit on_message
        self.hyf_channel.send.assert_awaited_once()
        self.assertIn("https://youtu.be/abc123", self.hyf_channel.send.call_args[0][0])
        msg.delete.assert_not_called()

    # 2. Exact duplicate in the same channel is deleted and suppressed
    async def test_duplicate_same_channel_deleted(self):
        chan_a = FakeChannel(1)
        first = FakeMessage("https://youtu.be/dupe111", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_social_link_dedup(first)
        await self._pending_tasks()
        self.hyf_channel.send.reset_mock()

        dupe = FakeMessage("https://youtu.be/dupe111", FakeAuthor(1), chan_a, self.guild, msg_id=2)
        handled = await loki_bot._handle_social_link_dedup(dupe)
        await self._pending_tasks()

        self.assertTrue(handled)
        dupe.delete.assert_awaited_once()
        self.hyf_channel.send.assert_not_called()  # not forwarded again
        chan_a.send.assert_awaited_once()  # warning sent

    # 3. Exact duplicate in another channel is deleted and suppressed
    async def test_duplicate_other_channel_deleted(self):
        chan_a = FakeChannel(1)
        chan_b = FakeChannel(2)
        first = FakeMessage("https://youtu.be/crosschan", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_social_link_dedup(first)
        await self._pending_tasks()
        self.hyf_channel.send.reset_mock()

        dupe = FakeMessage("https://youtu.be/crosschan", FakeAuthor(2, "other"), chan_b, self.guild, msg_id=2)
        handled = await loki_bot._handle_social_link_dedup(dupe)
        await self._pending_tasks()

        self.assertTrue(handled)
        dupe.delete.assert_awaited_once()
        self.hyf_channel.send.assert_not_called()

    # 4. Duplicate posted by another user is detected
    async def test_duplicate_by_different_user(self):
        chan_a = FakeChannel(1)
        first = FakeMessage("https://youtu.be/anotherusr", FakeAuthor(1, "alice"), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_social_link_dedup(first)
        await self._pending_tasks()

        dupe = FakeMessage("https://youtu.be/anotherusr", FakeAuthor(2, "bob"), chan_a, self.guild, msg_id=2)
        handled = await loki_bot._handle_social_link_dedup(dupe)
        self.assertTrue(handled)
        warning_msg = chan_a.send.call_args_list[-1][0][0]
        self.assertIn("<@2>", warning_msg)  # warned the duplicate poster, not the original

    # 10. Hell Yeah Films forwarded messages are ignored
    async def test_hell_yeah_films_channel_excluded(self):
        msg = FakeMessage(
            "https://youtu.be/insidehyf", FakeAuthor(1), self.hyf_channel, self.guild
        )
        handled = await loki_bot._handle_social_link_dedup(msg)
        self.assertFalse(handled)
        self.hyf_channel.send.assert_not_called()

    # 11. Bot/webhook messages are ignored (Loki's own self-check happens one
    # level up in on_message; this covers the author.bot guard in the handler)
    async def test_bot_author_ignored(self):
        chan_a = FakeChannel(1)
        msg = FakeMessage(
            "https://youtu.be/frombot", FakeAuthor(1, bot=True), chan_a, self.guild
        )
        handled = await loki_bot._handle_social_link_dedup(msg)
        await self._pending_tasks()
        self.assertFalse(handled)
        self.hyf_channel.send.assert_not_called()

    # 12. Mixed new + duplicate links: preserve message, forward only new link
    async def test_mixed_content_preserves_message_and_forwards_new_link(self):
        chan_a = FakeChannel(1)
        first = FakeMessage("https://youtu.be/olddupe", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_social_link_dedup(first)
        await self._pending_tasks()
        self.hyf_channel.send.reset_mock()

        mixed = FakeMessage(
            "also check https://youtu.be/olddupe and this new one https://youtu.be/brandnew99",
            FakeAuthor(2), chan_a, self.guild, msg_id=2,
        )
        handled = await loki_bot._handle_social_link_dedup(mixed)
        await self._pending_tasks()

        self.assertTrue(handled)
        mixed.delete.assert_not_called()  # message preserved
        self.hyf_channel.send.assert_awaited_once()
        self.assertIn("brandnew99", self.hyf_channel.send.call_args[0][0])
        chan_a.send.assert_awaited_once()  # exactly one warning

    # 13. Missing delete permission does not forward the duplicate, doesn't retry
    async def test_missing_delete_permission_handled_gracefully(self):
        chan_a = FakeChannel(1)
        first = FakeMessage("https://youtu.be/noperms", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_social_link_dedup(first)
        await self._pending_tasks()
        self.hyf_channel.send.reset_mock()

        fake_resp = MagicMock(status=403, reason="Forbidden")
        forbidden = discord.Forbidden(fake_resp, "Missing Permissions")
        dupe = FakeMessage(
            "https://youtu.be/noperms", FakeAuthor(2), chan_a, self.guild, msg_id=2,
            delete_error=forbidden,
        )
        handled = await loki_bot._handle_social_link_dedup(dupe)
        await self._pending_tasks()

        self.assertTrue(handled)
        dupe.delete.assert_awaited_once()  # tried exactly once, no retry
        self.hyf_channel.send.assert_not_called()  # duplicate never forwarded
        chan_a.send.assert_awaited_once()  # warning still sent

    # 18. Warnings contain no URL, no channel mention
    async def test_warning_contains_no_url_or_channel_reference(self):
        chan_a = FakeChannel(424242)
        first = FakeMessage("https://youtu.be/nourl", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_social_link_dedup(first)
        await self._pending_tasks()

        dupe = FakeMessage("https://youtu.be/nourl", FakeAuthor(2), chan_a, self.guild, msg_id=2)
        await loki_bot._handle_social_link_dedup(dupe)

        warning_msg = chan_a.send.call_args[0][0]
        self.assertNotIn("http://", warning_msg)
        self.assertNotIn("https://", warning_msg)
        self.assertNotIn("<#", warning_msg)  # channel-mention syntax
        # The LLM prompt itself must not have been told an original channel
        # (no Discord channel-mention syntax, no raw channel/message IDs).
        prompt_text = " ".join(m["content"] for m in self.llm_calls[-1])
        self.assertNotIn("<#", prompt_text)
        self.assertNotIn(str(chan_a.id), prompt_text)

    # 19. No timeout, mute, ban, role, or moderator action occurs
    async def test_no_moderation_actions_taken(self):
        chan_a = FakeChannel(1)
        author = FakeAuthor(2)
        author.timeout = AsyncMock()
        author.ban = AsyncMock()
        author.kick = AsyncMock()
        author.add_roles = AsyncMock()
        author.remove_roles = AsyncMock()

        first = FakeMessage("https://youtu.be/nomod", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_social_link_dedup(first)
        await self._pending_tasks()

        dupe = FakeMessage("https://youtu.be/nomod", author, chan_a, self.guild, msg_id=2)
        await loki_bot._handle_social_link_dedup(dupe)

        author.timeout.assert_not_called()
        author.ban.assert_not_called()
        author.kick.assert_not_called()
        author.add_roles.assert_not_called()
        author.remove_roles.assert_not_called()

    # 20. Existing non-duplicate (non-social) forwarding behavior is unaffected —
    # the new handler doesn't intercept messages with no supported social link.
    async def test_non_social_url_not_intercepted(self):
        chan_a = FakeChannel(1)
        msg = FakeMessage(
            "here's a file https://example.com/some/file.zip", FakeAuthor(1), chan_a, self.guild
        )
        handled = await loki_bot._handle_social_link_dedup(msg)
        self.assertFalse(handled)
        self.hyf_channel.send.assert_not_called()

    # Escalation wording changes across repeated duplicates by the same user
    async def test_escalation_level_passed_to_warning_prompt(self):
        chan_a = FakeChannel(1)
        author = FakeAuthor(5, "repeat-offender")
        first = FakeMessage("https://youtu.be/esc1", FakeAuthor(1), chan_a, self.guild, msg_id=1)
        await loki_bot._handle_social_link_dedup(first)
        await self._pending_tasks()

        d1 = FakeMessage("https://youtu.be/esc1", author, chan_a, self.guild, msg_id=2)
        await loki_bot._handle_social_link_dedup(d1)

        d2_msg = FakeMessage("https://youtu.be/esc1", author, chan_a, self.guild, msg_id=3)
        await loki_bot._handle_social_link_dedup(d2_msg)

        first_prompt = self.llm_calls[0][1]["content"]
        second_prompt = self.llm_calls[1][1]["content"]
        self.assertIn("first duplicate", first_prompt.lower())
        self.assertIn("second duplicate", second_prompt.lower())


if __name__ == "__main__":
    unittest.main()

"""
Focused tests for the four Boss presence notifications.

Home Assistant already sends these in Loki's voice; Loki's LLM rewriter was
turning them into narrated sentences that restated what the Boss already knew
("Boss, you are not home and the office has been checked out", "Boss, someone
has been detected in the office while you are at work"). These tests pin the
exact concise wording, prove the rewriter is bypassed, and prove unrelated
smart-home notifications still go through it untouched.

No network: Home Assistant state lookups are stubbed and the Groq call is
booby-trapped so any attempt to rewrite a presence message fails the test.

Run:  venv/bin/python -m unittest tests.test_presence_notifications -v
"""

import asyncio
import os
import unittest

os.environ.setdefault("OWNER_USER_ID", "111111111111111111")

import personality
import ha_integration as ha


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# Exactly what Home Assistant posts to /ha-notify (title is empty for these).
HA_LEAVE_HOME = "✌ - Peace out, Homie! I'll hold things down til you get back 💯"
HA_OFFICE_IN = "💼 - Office check-in - 💼"
HA_OFFICE_OUT = "💼 - Office check-out - 💼"
HA_ARRIVE_HOME = "🏠 - Welcome home, Boss - 🏠"

# The verbose phrasings that must never appear again.
BANNED = (
    "someone has been detected",
    "someone detected",
    "you are not home",
    "has been checked out",
    "holding things down until you return",
    "please review the office status",
    "a message has been received",
)


class Base(unittest.TestCase):
    def setUp(self):
        self.rob_state = "home"
        self.llm_calls = []

        async def fake_get_state(entity_id):
            return {"state": self.rob_state}

        async def trapped_get_all_states():
            # Reaching the rewriter for a presence message is the bug.
            self.llm_calls.append("get_all_states")
            return []

        self._real_get_state = ha.get_state
        self._real_get_all = ha.get_all_states
        ha.get_state = fake_get_state
        ha.get_all_states = trapped_get_all_states
        self._real_key = ha.GROQ_API_KEY
        ha.GROQ_API_KEY = ""      # any rewrite attempt is then obvious

    def tearDown(self):
        ha.get_state = self._real_get_state
        ha.get_all_states = self._real_get_all
        ha.GROQ_API_KEY = self._real_key

    def notify(self, message, title=""):
        return run(ha.get_smart_notification(title, message))

    def assertConcise(self, text):
        low = text.lower()
        for phrase in BANNED:
            self.assertNotIn(phrase, low, f"verbose phrasing returned: {text!r}")
        self.assertNotIn("**", text, "raw fallback formatting leaked through")


# ── The four transitions, exact wording ────────────────────────────────────
class ExactWordingTests(Base):
    def test_leave_home(self):
        out = self.notify(HA_LEAVE_HOME)
        self.assertEqual(out, "✌ - Peace out, Homie! I'll hold things down til you get back 💯")
        self.assertConcise(out)

    def test_office_check_in(self):
        out = self.notify(HA_OFFICE_IN)
        self.assertEqual(out, "💼 - Office check-in - 💼")
        self.assertConcise(out)

    def test_office_check_out(self):
        out = self.notify(HA_OFFICE_OUT)
        self.assertEqual(out, "💼 - Office check-out - 💼")
        self.assertConcise(out)

    def test_welcome_home_first_line_is_exact(self):
        out = self.notify(HA_ARRIVE_HOME)
        self.assertEqual(out.split("\n")[0], "🏠 - Welcome home, Boss - 🏠")
        self.assertConcise(out)

    def test_office_messages_do_not_mention_home_state(self):
        for msg in (HA_OFFICE_IN, HA_OFFICE_OUT):
            out = self.notify(msg)
            self.assertNotIn("home", out.lower(),
                             "office notifications must not narrate home state")

    def test_no_transition_is_a_sentence_about_itself(self):
        for msg in (HA_LEAVE_HOME, HA_OFFICE_IN, HA_OFFICE_OUT):
            out = self.notify(msg)
            self.assertNotIn("Boss,", out, "state changes must not be narrated")


# ── The rewriter is bypassed entirely ──────────────────────────────────────
class ReachedRewriter(Exception):
    """Raised by the trap the moment the LLM path is entered."""


class NoRewriteTests(Base):
    def _arm_rewriter_trap(self):
        """Make entering the rewriter unmistakable — and stop it before any
        network call, so the Groq key can be set without reaching Groq."""
        async def trap():
            raise ReachedRewriter()
        ha.get_all_states = trap
        ha.GROQ_API_KEY = "test-key-not-used"

    def test_presence_messages_never_reach_the_llm(self):
        self._arm_rewriter_trap()
        for msg in (HA_LEAVE_HOME, HA_OFFICE_IN, HA_OFFICE_OUT, HA_ARRIVE_HOME):
            try:
                self.notify(msg)
            except ReachedRewriter:
                self.fail(f"{msg!r} was sent to the rewriter")

    def test_missing_api_key_fallback_cannot_narrate_them_either(self):
        """The old `**{title}**\\n{message}` fallback ran before this fix."""
        ha.GROQ_API_KEY = ""
        out = self.notify(HA_OFFICE_OUT, title="Office")
        self.assertEqual(out, "💼 - Office check-out - 💼")

    def test_one_notification_per_transition(self):
        """One HA post produces exactly one message — no second translation."""
        for msg in (HA_LEAVE_HOME, HA_OFFICE_IN, HA_OFFICE_OUT):
            out = self.notify(msg)
            self.assertEqual(out.count("\n"), 0, f"{msg!r} produced extra lines")

    def test_unrelated_notifications_still_go_through_the_rewriter(self):
        """Scope check: this change must not touch other notifications."""
        self._arm_rewriter_trap()
        for msg in ("Roommate arrived home 🏠", "The place is empty 🏠",
                    "Rain expected on your commute"):
            with self.assertRaises(ReachedRewriter,
                                   msg=f"{msg!r} was wrongly treated as a Boss transition"):
                self.notify(msg, title="Something")


# ── Roommate information on arrival home ───────────────────────────────────
class RoommateInfoTests(Base):
    def test_rob_home_is_reported_with_the_welcome(self):
        self.rob_state = "home"
        out = self.notify(HA_ARRIVE_HOME)
        self.assertIn("🏠 - Welcome home, Boss - 🏠", out)
        self.assertIn("Rob", out)
        self.assertIn("home", out.split("\n")[1].lower())
        self.assertConcise(out)

    def test_rob_away_is_reported_with_the_welcome(self):
        self.rob_state = "not_home"
        out = self.notify(HA_ARRIVE_HOME)
        self.assertIn("🏠 - Welcome home, Boss - 🏠", out)
        self.assertIn("Rob", out)
        self.assertIn("top lock", out.lower(),
                      "the lock decision is why this information exists")

    def test_the_two_rob_states_are_distinguishable(self):
        self.rob_state = "home"
        home = self.notify(HA_ARRIVE_HOME)
        self.rob_state = "not_home"
        away = self.notify(HA_ARRIVE_HOME)
        self.assertNotEqual(home, away)

    def test_welcome_home_is_a_single_glance(self):
        self.rob_state = "home"
        out = self.notify(HA_ARRIVE_HOME)
        self.assertEqual(len(out.split("\n")), 2,
                         "welcome + roommate state, nothing more")

    def test_unknown_rob_state_drops_the_line_but_keeps_the_welcome(self):
        self.rob_state = "unknown"
        out = self.notify(HA_ARRIVE_HOME)
        self.assertEqual(out, "🏠 - Welcome home, Boss - 🏠")

    def test_unreachable_home_assistant_still_welcomes(self):
        async def broken(entity_id):
            raise RuntimeError("HA down")
        ha.get_state = broken
        out = self.notify(HA_ARRIVE_HOME)
        self.assertEqual(out, "🏠 - Welcome home, Boss - 🏠")


# ── The formatter itself ───────────────────────────────────────────────────
class FormatterTests(unittest.TestCase):
    def test_kinds_are_recognised(self):
        self.assertEqual(personality.presence_kind(HA_LEAVE_HOME), personality.LEAVE_HOME)
        self.assertEqual(personality.presence_kind(HA_OFFICE_IN), personality.OFFICE_IN)
        self.assertEqual(personality.presence_kind(HA_OFFICE_OUT), personality.OFFICE_OUT)
        self.assertEqual(personality.presence_kind(HA_ARRIVE_HOME), personality.ARRIVE_HOME)

    def test_check_in_and_check_out_are_never_confused(self):
        self.assertNotEqual(personality.presence_kind(HA_OFFICE_IN),
                            personality.presence_kind(HA_OFFICE_OUT))

    def test_unrelated_messages_are_not_presence(self):
        for msg in ("Roommate arrived home 🏠", "The place is empty 🏠",
                    "Ammiel is home. You are free to lock the top lock.",
                    "", None):
            self.assertIsNone(personality.presence_kind(msg), repr(msg))

    def test_roommate_line_states(self):
        self.assertIn("Rob", personality.roommate_line("home"))
        self.assertIn("Rob", personality.roommate_line("not_home"))
        self.assertEqual(personality.roommate_line("unknown"), "")
        self.assertEqual(personality.roommate_line(None), "")


if __name__ == "__main__":
    unittest.main()

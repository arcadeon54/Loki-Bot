"""
Focused tests for presence notifications — the Boss's own four transitions,
plus Rob's (roommate) arrival/departure.

Home Assistant sends the Boss's four already in Loki's voice; Loki's LLM
rewriter was turning them into narrated sentences that restated what the
Boss already knew ("Boss, you are not home and the office has been checked
out", "Boss, someone has been detected in the office while you are at
work"). Rob's presence isn't pre-voiced by HA at all — a plain factual
message like "Ammiel is home. You are free to lock the top lock." went
through the same rewriter and came back as "Boss, your roommate has left
the premises while you are still at home", restating the Boss's own
(already-known) presence on top of Rob's. These tests pin the exact concise
wording for both, prove the rewriter is bypassed for both, and prove
unrelated smart-home notifications still go through it untouched.

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
import presence_monitor


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

# Rob's presence notifications are NOT pre-voiced by HA — these are the kind
# of plain, factual text a person-entity automation actually sends. Multiple
# differently-worded raw messages are used deliberately: the reply must come
# from Rob's live state, not from parsing HA's exact phrasing, so any of
# these should consolidate to the same canonical output.
HA_ROB_LEFT_VARIANTS = (
    "Ammiel has left home.",
    "Ammiel is no longer home. Lock the top lock.",
    "Roommate has left the premises.",
)
HA_ROB_HOME_VARIANTS = (
    "Ammiel is home. You are free to lock the top lock.",
    "Roommate arrived home 🏠",
    "Rob is home.",
)

# The verbose phrasings that must never appear again.
BANNED = (
    "someone has been detected",
    "someone detected",
    "you are not home",
    "has been checked out",
    "holding things down until you return",
    "please review the office status",
    "a message has been received",
    "premises",
    "while you are still at home",
    "while you remain home",
    "you are still at the premises",
    "a person",
    "a household member",
)


class Base(unittest.TestCase):
    def setUp(self):
        self.rob_state = "home"
        self.boss_state = "home"
        # presence_monitor's last-polled state — the "previous" state a
        # generic, unnamed notification's direction is resolved against.
        # None means "no prior poll yet", matching a fresh boot.
        self.prev_boss = "home"
        self.prev_rob = "home"
        self.llm_calls = []

        async def fake_get_state(entity_id):
            if entity_id == ha.BOSS_ENTITY:
                return {"state": self.boss_state}
            return {"state": self.rob_state}

        async def trapped_get_all_states():
            # Reaching the rewriter for a presence message is the bug.
            self.llm_calls.append("get_all_states")
            return []

        def fake_last_known():
            return {"boss": self.prev_boss, "rob": self.prev_rob}

        self._real_get_state = ha.get_state
        self._real_get_all = ha.get_all_states
        ha.get_state = fake_get_state
        ha.get_all_states = trapped_get_all_states
        self._real_key = ha.GROQ_API_KEY
        ha.GROQ_API_KEY = ""      # any rewrite attempt is then obvious
        self._real_last_known = presence_monitor.last_known
        presence_monitor.last_known = fake_last_known

    def tearDown(self):
        ha.get_state = self._real_get_state
        ha.get_all_states = self._real_get_all
        ha.GROQ_API_KEY = self._real_key
        presence_monitor.last_known = self._real_last_known

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
        for msg in ("The place is empty 🏠", "Rain expected on your commute",
                    "Motion detected in the driveway"):
            with self.assertRaises(ReachedRewriter,
                                   msg=f"{msg!r} was wrongly treated as a presence transition"):
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


# ── Rob's own arrival/departure (Boss stays home throughout) ───────────────
class RoommatePresenceTests(Base):
    def test_rob_leaves_while_boss_is_home(self):
        """The exact completion criterion: Boss home + Rob leaves."""
        self.rob_state = "not_home"
        for msg in HA_ROB_LEFT_VARIANTS:
            out = self.notify(msg)
            self.assertEqual(out, "Rob stepped out.", repr(msg))

    def test_rob_arrives_while_boss_is_home(self):
        self.rob_state = "home"
        for msg in HA_ROB_HOME_VARIANTS:
            out = self.notify(msg)
            self.assertEqual(out, "Rob is home.", repr(msg))

    def test_differently_worded_ha_messages_consolidate_to_one_phrasing(self):
        """Direction comes from Rob's live state, not from parsing HA's
        wording — three different raw messages, one canonical reply."""
        self.rob_state = "not_home"
        outputs = {self.notify(msg) for msg in HA_ROB_LEFT_VARIANTS}
        self.assertEqual(outputs, {"Rob stepped out."})

    def test_message_never_restates_boss_own_presence(self):
        """The actual bug report: Boss already knows he's home; the message
        must not remind him."""
        self.rob_state = "not_home"
        out = self.notify(HA_ROB_LEFT_VARIANTS[0])
        self.assertConcise(out)
        for phrase in ("while you are", "you are still", "you remain"):
            self.assertNotIn(phrase, out.lower())

    def test_no_robotic_wording(self):
        self.rob_state = "not_home"
        out = self.notify(HA_ROB_LEFT_VARIANTS[0])
        self.assertNotIn("left the premises", out.lower())

    def test_message_is_one_line(self):
        self.rob_state = "home"
        out = self.notify(HA_ROB_HOME_VARIANTS[0])
        self.assertEqual(out.count("\n"), 0)

    def test_unknown_rob_state_relays_ha_text_rather_than_guessing(self):
        self.rob_state = "unknown"
        msg = HA_ROB_LEFT_VARIANTS[0]
        out = self.notify(msg)
        self.assertEqual(out, msg)

    def test_unreachable_home_assistant_relays_ha_text(self):
        async def broken(entity_id):
            raise RuntimeError("HA down")
        ha.get_state = broken
        msg = HA_ROB_LEFT_VARIANTS[0]
        out = self.notify(msg)
        self.assertEqual(out, msg)

    def test_never_reaches_the_llm(self):
        async def trap():
            raise ReachedRewriter()
        ha.get_all_states = trap
        ha.GROQ_API_KEY = "test-key-not-used"
        for msg in HA_ROB_LEFT_VARIANTS + HA_ROB_HOME_VARIANTS:
            try:
                self.notify(msg)
            except ReachedRewriter:
                self.fail(f"{msg!r} was sent to the rewriter")

    def test_does_not_increase_notification_count(self):
        """One HA post about Rob produces exactly one reply — no second
        message, no duplicate delivery."""
        self.rob_state = "home"
        out = self.notify(HA_ROB_HOME_VARIANTS[0])
        self.assertIsInstance(out, str)
        self.assertEqual(out.count("\n"), 0)


# ── Generic, unnamed presence detections ────────────────────────────────────
# "Boss, a person has been detected at home." — HA sending a presence event
# without naming which of the exactly-two monitored residents it's about.
# Resolved from state (presence_monitor's last poll vs. live now), never
# from wording, so the reply always names a specific resident.
HA_GENERIC_HOME = "A person has been detected at home."
HA_GENERIC_LEFT = "A person has left home."
HA_GENERIC_REPORTED_BUG = "Boss, a person has been detected at home."


class GenericPresenceTests(Base):
    def test_rob_arrives_while_boss_is_home(self):
        self.prev_rob, self.rob_state = "not_home", "home"
        self.prev_boss = self.boss_state = "home"
        out = self.notify(HA_GENERIC_HOME)
        self.assertEqual(out, "Rob is home.")

    def test_rob_arrives_while_boss_is_away(self):
        self.prev_rob, self.rob_state = "not_home", "home"
        self.prev_boss = self.boss_state = "not_home"
        out = self.notify(HA_GENERIC_HOME)
        self.assertEqual(out, "Rob is home.")

    def test_rob_leaves_while_boss_is_home(self):
        self.prev_rob, self.rob_state = "home", "not_home"
        self.prev_boss = self.boss_state = "home"
        out = self.notify(HA_GENERIC_LEFT)
        self.assertEqual(out, "Rob stepped out.")

    def test_rob_leaves_while_boss_is_away(self):
        self.prev_rob, self.rob_state = "home", "not_home"
        self.prev_boss = self.boss_state = "not_home"
        out = self.notify(HA_GENERIC_LEFT)
        self.assertEqual(out, "Rob stepped out.")

    def test_boss_own_arrival_via_generic_message_uses_preferred_wording(self):
        """My own arrival path: a generic message that's actually about the
        Boss must still use his existing preferred phrase, not a generic
        rewrite and not Rob's wording."""
        self.prev_boss, self.boss_state = "not_home", "home"
        self.prev_rob = self.rob_state = "home"
        out = self.notify(HA_GENERIC_HOME)
        self.assertEqual(out.split("\n")[0], "🏠 - Welcome home, Boss - 🏠")

    def test_boss_own_departure_via_generic_message_uses_preferred_wording(self):
        self.prev_boss, self.boss_state = "home", "not_home"
        self.prev_rob = self.rob_state = "home"
        out = self.notify(HA_GENERIC_LEFT)
        self.assertEqual(out, "✌ - Peace out, Homie! I'll hold things down til you get back 💯")

    def test_the_exact_reported_bug_message(self):
        """Regression pin for the literal message from the bug report."""
        self.prev_rob, self.rob_state = "not_home", "home"
        self.prev_boss = self.boss_state = "home"
        out = self.notify(HA_GENERIC_REPORTED_BUG)
        self.assertEqual(out, "Rob is home.")
        self.assertNotIn("a person", out.lower())

    def test_no_known_event_ever_says_a_person_or_someone(self):
        cases = [
            ("not_home", "home", "home", "home", HA_GENERIC_HOME),
            ("home", "not_home", "home", "home", HA_GENERIC_LEFT),
            ("home", "home", "not_home", "home", HA_GENERIC_HOME),
            ("home", "home", "home", "not_home", HA_GENERIC_LEFT),
        ]
        for prev_rob, rob_now, prev_boss, boss_now, msg in cases:
            self.prev_rob, self.rob_state = prev_rob, rob_now
            self.prev_boss, self.boss_state = prev_boss, boss_now
            out = self.notify(msg)
            self.assertConcise(out)
            for msg2 in (HA_ROB_LEFT_VARIANTS + HA_ROB_HOME_VARIANTS
                        + (HA_LEAVE_HOME, HA_OFFICE_IN, HA_OFFICE_OUT, HA_ARRIVE_HOME)):
                self.assertConcise(self.notify(msg2))

    def test_ambiguous_when_both_residents_changed_relays_rather_than_guesses(self):
        """Both changed between polls — genuinely ambiguous which one this
        specific notification is about. Relay rather than misattribute."""
        self.prev_rob, self.rob_state = "not_home", "home"
        self.prev_boss, self.boss_state = "not_home", "home"
        out = self.notify(HA_GENERIC_HOME)
        self.assertEqual(out, HA_GENERIC_HOME)

    def test_no_prior_state_relays_rather_than_guesses(self):
        """Fresh boot, presence_monitor hasn't polled yet — no baseline to
        diff against."""
        self.prev_rob = self.prev_boss = None
        self.rob_state = self.boss_state = "home"
        out = self.notify(HA_GENERIC_HOME)
        self.assertEqual(out, HA_GENERIC_HOME)

    def test_unreachable_home_assistant_relays_rather_than_guesses(self):
        async def broken(entity_id):
            raise RuntimeError("HA down")
        ha.get_state = broken
        self.prev_rob, self.prev_boss = "not_home", "home"
        out = self.notify(HA_GENERIC_HOME)
        self.assertEqual(out, HA_GENERIC_HOME)

    def test_never_reaches_the_llm(self):
        async def trap():
            raise ReachedRewriter()
        ha.get_all_states = trap
        ha.GROQ_API_KEY = "test-key-not-used"
        self.prev_rob, self.rob_state = "not_home", "home"
        for msg in (HA_GENERIC_HOME, HA_GENERIC_LEFT, HA_GENERIC_REPORTED_BUG):
            try:
                self.notify(msg)
            except ReachedRewriter:
                self.fail(f"{msg!r} was sent to the rewriter")

    def test_camera_style_someone_without_presence_shape_still_goes_to_rewriter(self):
        """Scope check: 'someone' alone, with no home/detected/arrived/left/
        away word nearby (e.g. a doorbell/camera event), must not be swept
        into presence handling — Loki doesn't track who rang a doorbell."""
        self._real_get_all = ha.get_all_states

        async def trap():
            raise ReachedRewriter()
        ha.get_all_states = trap
        ha.GROQ_API_KEY = "test-key-not-used"
        with self.assertRaises(ReachedRewriter):
            self.notify("Someone is at the front door.", title="Doorbell")


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
        for msg in ("The place is empty 🏠", "Rain expected on your commute",
                    "Motion detected in the driveway",
                    "There's a problem with the router",  # 'rob' as a substring, not a word
                    "", None):
            self.assertIsNone(personality.presence_kind(msg), repr(msg))

    def test_roommate_reference_is_recognised_as_presence(self):
        for msg in ("Ammiel is home. You are free to lock the top lock.",
                    "Roommate arrived home 🏠",
                    "Rob is no longer home."):
            self.assertEqual(personality.presence_kind(msg),
                             personality.ROOMMATE_PRESENCE, repr(msg))

    def test_roommate_line_states(self):
        self.assertIn("Rob", personality.roommate_line("home"))
        self.assertIn("Rob", personality.roommate_line("not_home"))
        self.assertEqual(personality.roommate_line("unknown"), "")
        self.assertEqual(personality.roommate_line(None), "")

    def test_roommate_presence_text_states(self):
        self.assertEqual(personality.roommate_presence_text("home"), "Rob is home.")
        self.assertEqual(personality.roommate_presence_text("not_home"), "Rob stepped out.")
        self.assertEqual(personality.roommate_presence_text("away"), "Rob stepped out.")
        self.assertEqual(personality.roommate_presence_text("unknown"), "")
        self.assertEqual(personality.roommate_presence_text(None), "")

    def test_generic_unnamed_presence_is_recognised(self):
        for msg in ("A person has been detected at home.",
                    "Boss, a person has been detected at home.",
                    "Someone has arrived home.",
                    "A household member has left home."):
            self.assertEqual(personality.presence_kind(msg),
                             personality.GENERIC_PRESENCE, repr(msg))

    def test_generic_reference_without_presence_shape_is_not_presence(self):
        """'Someone'/'a person' alone, with no home/detected/arrived/left/
        away word, isn't enough — e.g. a doorbell camera event."""
        for msg in ("Someone is at the front door.",
                    "A person was seen on the porch camera."):
            self.assertIsNone(personality.presence_kind(msg), repr(msg))

    def test_named_reference_takes_priority_over_generic(self):
        """A message naming Rob is ROOMMATE_PRESENCE, not GENERIC_PRESENCE,
        even though it would also match the generic pattern."""
        msg = "Ammiel, a household member, has arrived home."
        self.assertEqual(personality.presence_kind(msg), personality.ROOMMATE_PRESENCE)


if __name__ == "__main__":
    unittest.main()

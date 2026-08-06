---
name: discord-telegram
description: >-
  Use when working on Loki's chat surfaces — Discord message handling,
  personas, the Telegram interface, the duplicate social-link guard, presence
  and Home Assistant notifications, or the tool permission model. Covers
  loki_bot.py, telegram_interface.py, personality.py, social_link_dedup.py,
  ha_integration.py. Read before changing anything a user sees.
---

# Discord / Telegram Surfaces

## Personas — all tone lives in `personality.py`

| Profile | Use |
|---|---|
| `DISCORD_PUBLIC` | Public channels — mischief, humour |
| `DISCORD_DM` / `TELEGRAM` | Serious |
| `HA_NOTIFICATION` | Brief, no humour |

**Nothing outside `personality.py` writes persona text.** If you are tempted to
inline a phrasing change somewhere else, that is the bug.

Loki is `Loki#5463`. Telegram is `@Leauxki_Bot`, always serious.

## Users and permissions

Boss (`OWNER_USER_ID`) · roommate Ammiel = "Rob" (crew) · everyone else.

`everyone` < `crew` < `boss`, enforced in `tools.execute()` and re-checked in
`tools.run_approved()`. **A missing `.env` must lock Loki down, not open it up** —
`tools.user_level()` once treated an unset `OWNER_USER_ID` as a match, making
any blank-id caller Boss. Never reintroduce an empty-string comparison there.

Telegram pairing is single-owner via `telegram_state.json`; deleting that file
re-opens first-come pairing, so treat it as a credential. Strangers are
rejected.

## Never send while testing

Discord and Telegram messages, and HA actions, are **real production side
effects** with real recipients. Mock the send path. There is no "test channel"
exemption.

## Duplicate social-link guard

`_handle_duplicate_link_guard` / `_handle_duplicate_link_guard_edit` in
`loki_bot.py`, backed by `social_link_dedup.py` (platform content-ID
canonicalization + an atomic SQLite claim in `loki_memory.db`, tables
`social_link_dupes` / `social_link_user_warnings`).

Behaviour: first occurrence is left alone; duplicates get deleted when safe plus
one escalating playful warning. **Never** moderation action, and no forwarding
to any channel.

Config: `DUPLICATE_LINK_*` env vars (see `.env.example`).

### The original bug, and the detour

The guard only recorded a URL as "seen" inside `run_download()`, which fires
only for `DOWNLOAD_CHANNEL_ID`, explicit trigger phrases ("save this" / "post
this"), or `AUTO_WATCH_CHANNEL_IDS` / `AUTO_DOWNLOAD_USER_ID`. A link shared
casually in an ordinary channel was never recorded, so reposting it was never
caught. **A data-flow bug, not a permissions bug** — `message_content` intent
was enabled and the handler was reachable.

An earlier session misdiagnosed this as a missing "Hell Yeah Films forwarding"
feature — a channel that never existed — and built a parallel subsystem instead
of repairing the real guard. **Verify the premise against live evidence before
building anything.**

Debugging: `journalctl -u loki | grep "Duplicate-link guard:"` shows the startup
config line; then check whether the channel is in
`DUPLICATE_LINK_EXCLUDED_CHANNEL_IDS`.

## Home Assistant notifications

HA automations POST `{title, message}` to Loki's `/ha-notify` webhook.
`ha_integration.get_smart_notification()` rewrites through Groq using
`personality.HA_NOTIFICATION`, then posts to the HA Discord channel with a
`*(timestamp)*` line.

**The trap:** HA already sends several messages fully formatted in Loki's voice —
`loki_someone_home` sends literally `🏠 - Welcome home, Boss - 🏠`. The rewriter
turned those into narrated sentences. The wording was never wrong in HA; it was
wrong in Loki.

Four Boss presence transitions now **bypass** the rewriter via
`personality.presence_kind()` / `presence_text()`, matched on message fragments:
`loki_someone_left`, `loki_arrived_office`, `loki_left_office`,
`loki_someone_home`. The passthrough sits **ahead** of the no-API-key
`**{title}**\n{message}` fallback so neither path can narrate them. Welcome-home
appends Rob's (`person.ammiel`) home/away state for the top-lock decision.

Unrelated notifications (roommate arrive/leave, nobody-home, rain, shopping)
still go through the rewriter deliberately.

**Before changing any HA notification wording**, check whether the text
originates in the HA automation or in Loki's rewriter:
`GET {HA_URL}/api/config/automation/config/{id}` shows the real payload.

Separate and untouched: `presence_monitor.py` lockout warnings (phone + Discord
DM) and the HA `top_lock_*` automations, which notify the phone only.

Tests: `tests/test_presence_notifications.py` (20).

## Telegram gaps

**UNFINISHED:** voice messages. `telegram_interface._handle()` reads text and
caption only; `voice`/`audio` payloads are silently dropped. Discord voice
transcription already exists (`transcribe_voice_message`) — wiring the Telegram
payload through it is the natural completion.

Media handling and `send_document` are done (`824e60b`).

## Testing that imports `loki_bot`

See `.agents/rules/validation-policy.md` — importing `loki_bot` in a test can
bind production DB paths and has destroyed live data. Neutralize the
side-effect imports first.

## Completion criteria

Tone changes live only in `personality.py` · no production message sent during
testing · permission level verified for any new tool · duplicate-guard changes
verified against the real handler path, not a parallel one.

## Source

`loki_bot.py` · `telegram_interface.py` · `personality.py` ·
`social_link_dedup.py` · `ha_integration.py` · `presence_monitor.py`

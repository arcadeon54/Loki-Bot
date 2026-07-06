# Loki — Integrations Guide (July 2026 personal-AI upgrade)

How each subsystem works, where it lives, and how to operate it. The chat
brain (Discord `loki_bot.py` / Telegram `telegram_interface.py`) reaches all
of it through the shared tool registry (`tools.py` + `assistant_tools.py`).

```
Discord ─┐                                     ┌─ Joplin sidecar (Data API :41184)
         ├─ LLMHandler.chat_with_tools ── tools ├─ ChromaDB :8100 (semantic index)
Telegram ┘        (one brain)                  ├─ Home Assistant (ha.ivn-group.cc, on the NAS)
                                               ├─ SearXNG :8083 (+ Tavily fallback)
background loops: jobsite_poll (5 min) ────────┤   work_tracker.py (90-min rule)
                  presence_poll (2 min) ───────┤   presence_monitor.py
                  memory_reindex (daily) ──────┘   semantic_memory.py
```

---

## 1. Joplin long-term memory

**Modules:** `joplin_integration.py` (Data API client), `semantic_memory.py`
(memory layer).

**Infrastructure:** `loki-joplin-api` container on dex247
(`/home/g2k247/docker/joplin-api/`). It's a headless joplin-cli client that
syncs with the Joplin Server (`notes.ivn-group.cc`) every 5 min and exposes
the Joplin **Data API** on `127.0.0.1:41184` (host networking; loopback only).
E2EE is handled with the master password cached in the client profile.

- Notes Loki writes sync to all your devices within ~5 min.
- Loki's namespace: `Loki/` (`Memories`, `Work Log`, `Lists`, `Inbox`) —
  created automatically. It can read/search **all** notebooks.
- **Memory model:** every remembered fact = one note in `Loki/Memories`
  (source of truth) + an embedding in ChromaDB collection `boss_memory`
  (semantic index). Edit or delete memory notes freely in Joplin — the daily
  `memory_reindex` (and every bot restart) syncs the index to match.
- **If you change the Joplin account password**, update BOTH
  `/home/g2k247/docker/joplin-api/.env` (`JOPLIN_SERVER_PASSWORD`) and restart
  the container: `cd ~/docker/joplin-api && docker compose restart`.
  The E2EE master password is separate; only changes if you re-encrypt.

Ops:
```bash
docker logs loki-joplin-api --tail 20         # sync/decrypt status
curl -s "http://127.0.0.1:41184/ping"         # API alive?
```

## 2. Telegram interface

**Module:** `telegram_interface.py` — raw Bot API long-polling on the shared
aiohttp session (no extra dependency). Same tools, memory, and LLM as Discord.

- **Token discovery:** `TELEGRAM_BOT_TOKEN` in `.env`, else any Joplin note
  mentioning "telegram" that contains a `1234567890:AA…` token. Invalid/absent
  token → logs a warning and stays dormant; nothing else is affected.
- **Pairing:** set `TELEGRAM_OWNER_ID` in `.env`, or just DM the bot first —
  the first human to message gets paired (persisted in `telegram_state.json`,
  announced to the Boss by Discord DM). Everyone else is refused.
- `-s` prefix = serious mode, same as Discord.

To (re)enable: create a bot with @BotFather → paste token into `.env` as
`TELEGRAM_BOT_TOKEN` → `sudo systemctl restart loki`.

## 3. Home Assistant

**Module:** `ha_integration.py` (pre-existing, extended). HA now runs on the
**NAS** (`192.168.1.63:8123`, public URL `https://ha.ivn-group.cc`).

- Chat tools: `home_status` (entity lookup/state), `home_control`
  (natural-language service calls — lights, switches, climate, automations,
  phone notifications).
- Inbound: HA still POSTs notifications to Loki's webhook on `:9100`
  (`/ha-notify`) → witty rewrite → Discord.
- Alarm set/cancel via `input_datetime.alarm_time` unchanged.

## 4. Work-hours tracking (the 90-minute rule)

**Module:** `work_tracker.py`, fed by `jobsite_poll` every 5 min following
`person.kavaris`.

> Stay ≥90 consecutive minutes at any non-home location → that's work, and
> the session **starts at arrival**, not at confirmation.
> Arrive 6:00 AM, leave 1:00 PM → **7h**, not 5.5h.

- Confirmation announces "on the clock since …" in the jobsite channel.
- Departure = zone `home`, or two consecutive polls >250 m away (GPS-jitter
  protection). Restart-safe: state persists to `work_tracker_state.json`.
- Every completed session → `work_sessions` table in `jobsite.db` **and** a
  row in Joplin `Loki/Work Log/Work Log — YYYY-MM`:
  `Date | Location | Start | End | Duration | Running Total`.
- Google Sheets export preserved (via HA `google_sheets.append_sheet`, with
  pay-period + earnings columns; the old inline version had a shadowed-import
  bug that made it silently fail).
- Location naming: HA zone name → saved job site (`jobsite_db`) → reverse
  geocode → coordinates. "Work Grind" calendar events still pre-name sites.
- Ask in chat: *"how many hours did I work this week?"* → `work_hours` tool.

Tunables (`.env`): `WORK_CONFIRM_MINUTES=90`, `WORK_PERSON_ENTITY`,
`WORK_HOURLY_RATE`, `WORK_MOVE_THRESHOLD_M`.

## 5. Presence / lockout notifications

**Module:** `presence_monitor.py`, polled every 2 min.

- Boss arrives home & Rob away → Boss's phone (+ Discord DM):
  *"Rob is not home. Do not lock him out."*
- Rob arrives home & Boss away → Rob's phone:
  *"Boss is not home. Do not lock him out."*
- Fires **only** when exactly one resident is home; 20-min cooldown against
  zone-edge GPS flapping; state survives restarts (`presence_state.json`).

Entities/services: `person.kavaris`→`notify.mobile_app_nokia_e23`,
`person.ammiel`→`notify.mobile_app_nothing_phone` (all overridable in `.env`).

## 6. Web search

`web_search` tool (everyone): SearXNG on dex247 (`127.0.0.1:8083`) first,
**Tavily API fallback** if SearXNG errors or returns nothing. The system
prompt pushes the model to search for anything current (weather, news,
prices, docs) instead of trusting training data.

## 7. Operations

```bash
sudo systemctl restart loki                    # restart the bot
tail -f /home/g2k247/loki-bot/loki_bot.log     # live logs
grep -E "online|DISABLED|dormant" loki_bot.log # subsystem states at startup
tail -f /home/g2k247/loki-bot/tool_calls.jsonl # audited tool calls
```

Every subsystem announces its state at startup and degrades gracefully:
a dead sidecar, unreachable HA, or missing token never stops the bot.

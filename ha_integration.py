import os
from dotenv import load_dotenv
load_dotenv()
import json
import asyncio
import logging
import re

import aiohttp
from aiohttp import web

import personality

log = logging.getLogger(__name__)

# Default = the NAS deployment via NPM (unicron, the old host, is decommissioned)
HA_URL              = os.getenv("HA_URL", "https://ha.ivn-group.cc")
HA_TOKEN            = os.getenv("HA_TOKEN", "")
HA_NOTIFY_CHANNEL_ID = int(os.getenv("HA_NOTIFY_CHANNEL_ID", "0"))
HA_WEBHOOK_PORT     = int(os.getenv("HA_WEBHOOK_PORT", "9100"))
# Same entity presence_monitor watches — the roommate whose home/away state
# rides along with the Boss's welcome-home notification.
ROOMMATE_ENTITY     = os.getenv("PRESENCE_ROOMMATE_ENTITY", "person.ammiel")


# --- Telegram mirror of HA notifications (raw text) to the owner's DM ---
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_MARK = "\U0001F6D2"  # loki_ha_notify titles starting with this ALSO DM Telegram

def _telegram_owner_id() -> int:
    v = os.getenv("TELEGRAM_OWNER_ID")
    if v:
        try: return int(v)
        except ValueError: pass
    try:
        with open(os.path.join(os.path.dirname(__file__), "telegram_state.json")) as f:
            return int(json.load(f).get("owner_id") or 0)
    except Exception:
        return 0

GROQ_API_KEY        = os.getenv("FALLBACK_LLM_API_KEY", "")
GROQ_MODEL          = os.getenv("FALLBACK_LLM_MODEL", "llama-3.3-70b-versatile")
GROQ_URL            = "https://api.groq.com/openai/v1/chat/completions"

_CONTROL_DOMAINS = {"light", "switch", "sensor", "binary_sensor",
                    "climate", "fan", "media_player", "camera", "person", "device_tracker"}

def _headers():
    return {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}


async def get_state(entity_id: str) -> dict | None:
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(f"{HA_URL}/api/states/{entity_id}",
                             headers=_headers(),
                             timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 200:
                    return await r.json()
        except Exception as e:
            log.error(f"HA get_state error: {e}")
    return None


async def get_all_states() -> list:
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(f"{HA_URL}/api/states",
                             headers=_headers(),
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.json()
        except Exception as e:
            log.error(f"HA get_all_states error: {e}")
    return []


async def call_service(domain: str, service: str, entity_id: str = None, extra: dict = None) -> bool:
    payload = {}
    if entity_id:
        payload["entity_id"] = entity_id
    if extra:
        payload.update(extra)
    async with aiohttp.ClientSession() as s:
        try:
            async with s.post(f"{HA_URL}/api/services/{domain}/{service}",
                              headers=_headers(), json=payload,
                              timeout=aiohttp.ClientTimeout(total=30)) as r:
                return r.status in (200, 201)
        except Exception as e:
            log.error(f"HA call_service error: {e}")
    return False


async def get_smart_notification(title: str, message: str) -> str:
    log.info(f"Processing smart notification: {title}")

    # The Boss's own presence transitions are already written in Loki's voice
    # by Home Assistant. They are delivered VERBATIM — rewriting them is what
    # produced "Boss, you are not home and the office has been checked out".
    # This sits ahead of the no-API-key fallback too, so neither path can
    # narrate them again.
    kind = personality.presence_kind(message)
    if kind is not None:
        rob_state = None
        if kind == personality.ARRIVE_HOME:
            # Rob's home/away decides the top lock, so it rides along with the
            # welcome. Best-effort: an unreachable HA drops the line, never
            # the welcome itself.
            try:
                st = await get_state(ROOMMATE_ENTITY)
                rob_state = (st or {}).get("state")
            except Exception as e:
                log.warning(f"roommate state unavailable for welcome-home: {e}")
        log.info(f"Presence transition '{kind}' delivered verbatim (no rewrite)")
        return personality.presence_text(kind, rob_state=rob_state)

    if not GROQ_API_KEY:
        return f"**{title}**\n{message}"

    states = await get_all_states()
    presence = "unknown"
    if states:
        persons = [s for s in states if s["entity_id"] == "person.kavaris"]
        presence = ", ".join([f"{p['entity_id'].split('.')[1]} is {p['state']}" for p in persons])

    system_prompt = personality.HA_NOTIFICATION

    user_content = f"Title: {title}\nMessage: {message}\nPresence: {presence}"

    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.7
    }

    async with aiohttp.ClientSession() as s:
        try:
            async with s.post(GROQ_URL, headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, json=payload) as r:
                if r.status == 200:
                    resp = await r.json()
                    return resp["choices"][0]["message"]["content"]
        except Exception as e:
            log.error(f"Groq smart notification error: {e}")

    return f"**{title}**\n{message}"


async def ha_control(query: str, llm) -> str:
    if not HA_TOKEN:
        return "Home Assistant isn't configured yet — token missing."

    states = await get_all_states()
    if not states:
        return "Couldn't reach Home Assistant right now."

    entities = [s for s in states if s["entity_id"].split(".")[0] in _CONTROL_DOMAINS]

    lines = []
    for e in entities[:100]:
        name = e.get("attributes", {}).get("friendly_name", "")
        suffix = f" ({name})" if name and name != e["entity_id"] else ""
        lines.append(f"{e['entity_id']} = {e['state']}{suffix}")

    system = (
        "You are a smart home controller with access to Home Assistant. "
        "Given a user request and the entity list, respond with ONLY a JSON object — no markdown, no prose.\n\n"
        "For control actions:\n"
        '{"action":"service","domain":"light","service":"turn_on","entity_id":"light.bathroom_lamp","reply":"Turning on the bathroom lamp."}\n\n'
        "For state/sensor queries:\n"
        '{"action":"state","entity_id":"sensor.govee_thermometer_temperature","reply":"Temperature is {state}°F."}\n\n'
        "For multi-entity (e.g. all lights off):\n"
        '{"action":"service","domain":"light","service":"turn_off","entity_id":["light.bathroom_lamp","light.bedroom_lamp"],"reply":"Lights off."}\n\n'
        "For unknown/unsupported:\n"
        '{"action":"none","reply":"I can\'t do that through Home Assistant."}\n\n'
        "Entity list:\n" + "\n".join(lines)
    )

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": query},
    ]

    raw = await llm.chat(messages)

    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return raw.strip()
        parsed = json.loads(m.group())

        action = parsed.get("action")
        reply_tmpl = parsed.get("reply", "Done.")

        if action == "service":
            entity_id = parsed.get("entity_id")
            success = await call_service(parsed["domain"], parsed["service"], entity_id, parsed.get("extra"))
            return reply_tmpl if success else "Couldn't reach Home Assistant."

        elif action == "state":
            state_data = await get_state(parsed["entity_id"])
            if state_data:
                return reply_tmpl.replace("{state}", state_data["state"])
            return "Couldn't get that state from Home Assistant."

        else:
            return reply_tmpl

    except Exception as e:
        log.error(f"HA control parse error: {e} | raw: {raw[:300]}")
        return "Ran into an issue parsing that request."


async def _mirror_to_telegram(title: str, message: str):
    """Best-effort raw DM of an HA notification to the owner's Telegram. Isolated
    from the inbound command loop; never raises into the webhook."""
    token, chat_id = TELEGRAM_BOT_TOKEN, _telegram_owner_id()
    if not token or not chat_id:
        return
    text = f"{title}\n{message}".strip()
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text},
                timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status != 200:
                    log.warning(f"Telegram mirror HTTP {r.status}")
    except Exception as e:
        log.error(f"Telegram HA-notify mirror failed: {e}")


def _make_webhook_app(bot):
    async def handle_notify(request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.Response(status=400, text="bad json")

        msg   = data.get("message", "").strip()
        title = data.get("title", "").strip()
        if not msg:
            return web.Response(status=400, text="no message")

        channel = bot.get_channel(HA_NOTIFY_CHANNEL_ID)
        if channel:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            ts = datetime.now(ZoneInfo("America/New_York")).strftime("%-I:%M %p, %b %-d %Y")
            
            async def send_smart():
                text = await get_smart_notification(title, msg)
                await channel.send(f"{text}\n*({ts})*")
            
            asyncio.create_task(send_smart())

        # Mirror title-marked (shopping) notifications to Telegram, raw + best-effort.
        if title.startswith(TELEGRAM_MARK):
            asyncio.create_task(_mirror_to_telegram(title, msg))

        return web.Response(status=200, text="ok")

    app = web.Application()
    app.router.add_post("/ha-notify", handle_notify)
    return app


async def start_webhook_server(bot):
    if not HA_NOTIFY_CHANNEL_ID:
        log.info("HA webhook disabled — HA_NOTIFY_CHANNEL_ID not set")
        return
    app = _make_webhook_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", HA_WEBHOOK_PORT)
    await site.start()
    log.info(f"HA webhook server listening on :{HA_WEBHOOK_PORT}")


async def set_alarm(target_dt) -> bool:
    """Set input_datetime.alarm_time to trigger the morning alarm script."""
    payload = {
        "entity_id": "input_datetime.alarm_time",
        "datetime": target_dt.strftime("%Y-%m-%d %H:%M:%S"),
    }
    async with aiohttp.ClientSession() as s:
        try:
            async with s.post(
                f"{HA_URL}/api/services/input_datetime/set_datetime",
                headers=_headers(), json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                return r.status in (200, 201)
        except Exception as e:
            log.error(f"HA set_alarm error: {e}")
    return False


async def cancel_alarm() -> bool:
    """Clear the alarm by pushing it 365 days out."""
    import datetime as _dt
    far_future = (_dt.datetime.now() + _dt.timedelta(days=365)).strftime("%Y-%m-%d 00:00:00")
    payload = {"entity_id": "input_datetime.alarm_time", "datetime": far_future}
    async with aiohttp.ClientSession() as s:
        try:
            async with s.post(
                f"{HA_URL}/api/services/input_datetime/set_datetime",
                headers=_headers(), json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                return r.status in (200, 201)
        except Exception as e:
            log.error(f"HA cancel_alarm error: {e}")
    return False

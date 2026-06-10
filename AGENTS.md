# Loki Bot — Project Context

Discord bot for a small friend group ("The Break Room"). Runs as a systemd service on this machine.

## Key facts
- **Service:** `sudo systemctl restart loki` — logs at `~/loki-bot/loki_bot.log`
- **Main file:** `~/loki-bot/loki_bot.py` (~5500 lines, Python 3.12, discord.py)
- **Virtualenv:** `~/loki-bot/venv/`
- **Config:** `~/loki-bot/.env`
- **LLM:** OpenAI `gpt-5.1` (primary), Groq `llama-3.3-70b-versatile` (fallback) — do not swap these
- **Image recognition:** Google Gemini
- **Memory:** SQLite `loki_memory.db`, ChromaDB RAG at localhost:8100
- **Personality file:** `~/loki-bot/loki_learned.md` — frozen, do not auto-update

## Download chain
Falls back through: direct image → Threads → fxTwitter → fbdownloader → Pinterest → Cobalt (localhost:9000) → tikwm → snapinsta → yt-dlp → gallery-dl.
Cookies: `~/loki-bot/cookies/{instagram,reddit,youtube}.txt`
Downloads land in `/home/g2k247/downloads/{user}/{YYYY-MM-DD}/vid-N.ext`
Large files served via `https://media.ivn-group.cc` (nginx container on port 8082).

## DM downloads
Trigger: user DMs Loki a download keyword + URL.
Flow: known platform → download chain → Nextcloud upload → share link. Unknown → JDownloader.
Nextcloud at `http://192.168.1.247:8082`, JD container output at `/home/g2k247/downloads/jdownloader/`.

## Infrastructure used by this bot
- Cobalt: localhost:9000
- ChromaDB: localhost:8100
- JDownloader: Docker container (jlesage/jdownloader-2), web UI at :5800
- Media server: nginx container on port 8082

## What NOT to change without asking
- LLM provider order (OpenAI primary, Groq fallback — settled after testing)
- `loki_learned.md` — frozen intentionally
- Cookie files — they expire and are managed manually

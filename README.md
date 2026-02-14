# Loki Bot — AI Discord Bot

A conversational AI Discord bot with persistent memory, image recognition, voice chat, per-server personalities, and support for ChatGPT or local LLMs.

## Features

- **Conversational AI** — Responds to mentions, name triggers, and replies with context-aware conversation
- **Persistent Memory** — SQLite-backed conversation history that survives restarts
- **Image/GIF Recognition** — Analyzes images and GIFs using Google Gemini (free tier)
- **Voice Chat** — Joins voice channels and speaks with TTS (edge-tts)
- **Per-Server Personalities** — Each server admin can set a custom personality prompt
- **Serious Mode** — Prefix a message with `-s` to get a straight answer without the persona
- **Summarize** — `/summarize` to get an AI summary of recent conversation
- **ChatGPT or Local LLM** — Works with OpenAI API or any local LLM with an OpenAI-compatible endpoint (LM Studio, Ollama, etc.)

## Slash Commands

| Command | Description | Permission |
|---------|-------------|------------|
| `/summarize` | Summarize the last ~20 messages | Everyone |
| `/loki_join` | Bot joins your voice channel | Everyone |
| `/loki_leave` | Bot leaves the voice channel | Everyone |
| `/loki_say [text]` | Bot speaks text in voice chat | Everyone |
| `/loki_speak [question]` | Ask a question and hear the answer in voice | Everyone |
| `/loki_reset` | Clear bot memory for this channel | Admin only |
| `/set_personality [prompt]` | Set a custom personality for this server | Admin only |
| `/view_personality` | View the current personality prompt | Admin only |
| `/reset_personality` | Reset to the default personality | Admin only |

## Requirements

- Linux (Ubuntu/Debian recommended)
- Python 3.10+
- ffmpeg (for voice features)
- A Discord bot token
- An OpenAI API key **or** a local LLM with an OpenAI-compatible API
- (Optional) A Google Gemini API key for image recognition (free)

## Quick Install

```bash
git clone https://github.com/arcadeon54/Loki-Bot.git
cd Loki-Bot
bash install.sh
```

The install script will:
1. Install system dependencies (Python, ffmpeg, etc.)
2. Create a Python virtual environment
3. Install Python packages
4. Set up the systemd service

## Configuration

After running the installer:

1. **Edit the `.env` file** with your tokens:
   ```bash
   nano .env
   ```

2. **Required settings:**
   - `DISCORD_TOKEN` — Your Discord bot token ([get one here](https://discord.com/developers/applications))
   - `OPENAI_API_KEY` — Your OpenAI API key ([get one here](https://platform.openai.com/api-keys)), **or** set `LLM_PROVIDER=local` and configure `LOCAL_LLM_URL`

3. **Optional settings:**
   - `GEMINI_API_KEY` — For image/GIF recognition ([free key here](https://aistudio.google.com/app/apikey))
   - `SYSTEM_PROMPT` — The default personality prompt (server admins can override per-server)
   - `OPENAI_MODEL` — Change the model (default: `gpt-4o`)
   - `CONTEXT_MESSAGE_COUNT` — How many past messages to include as context (default: 50)

## Running the Bot

**Test manually first:**
```bash
source venv/bin/activate
python loki_bot.py
```

**Run as a service (auto-starts on boot):**
```bash
sudo systemctl enable loki
sudo systemctl start loki
```

**Check status:**
```bash
sudo systemctl status loki
```

**View live logs:**
```bash
sudo journalctl -u loki -f
```

## Discord Bot Setup

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** and give it a name
3. Go to **Bot** tab:
   - Click **Reset Token** and copy it to your `.env` file
   - Enable **Message Content Intent** under Privileged Gateway Intents
   - Enable **Server Members Intent**
4. Go to **OAuth2** tab:
   - Under **Scopes**, select `bot` and `applications.commands`
   - Under **Bot Permissions**, select: Send Messages, Read Message History, Connect, Speak, Use Slash Commands, Attach Files, Embed Links
   - Copy the generated URL and open it to invite the bot to your server

## Per-Server Personalities

The bot supports custom personalities per server. The `.env` `SYSTEM_PROMPT` is the default for all servers. Any server admin can override it:

- `/set_personality You are a pirate captain who speaks in nautical terms...` — Sets a custom personality
- `/view_personality` — See what's currently active
- `/reset_personality` — Go back to the default

Personalities are stored in the database and survive restarts.

## License

MIT

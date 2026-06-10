# Loki Bot — AI Discord Bot

A conversational AI Discord bot with persistent memory, image recognition, voice chat, per-server personalities, and support for ChatGPT or local LLMs.

## Features

- **Conversational AI** — Responds to mentions, name triggers, and replies with context-aware conversation
- **Persistent Memory** — SQLite-backed conversation history that survives restarts
- **History Search (RAG)** — "remember when…" questions search the full server history via ChromaDB. Messages are embedded individually (local sentence-transformers, no API cost) and grouped into conversation chunks; matches return the whole exchange with a jump link. Run `ingest_history.py` to index history (incremental — cron it), `eval_rag.py` to measure retrieval quality. Config: `CHROMADB_HOST`/`CHROMADB_PORT` (default localhost:8100), `RAG_EMBED_MODEL` (default `BAAI/bge-small-en-v1.5`), `RAG_MAX_DISTANCE` relevance cutoff, `RAG_MAX_CHUNKS` context cap (default 5)
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

- Python 3.10+
- ffmpeg (for voice features)
- A Discord bot token
- An OpenAI API key **or** a local LLM with an OpenAI-compatible API
- (Optional) A Google Gemini API key for image recognition (free)

---

## Install on Linux

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

### Configuration (Linux)

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

### Running the Bot (Linux)

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

**Check status / view logs:**
```bash
sudo systemctl status loki
sudo journalctl -u loki -f
```

---

## Install on Windows

### Prerequisites

1. **Python 3.10+** — Download from [python.org](https://www.python.org/downloads/)
   - **Important:** Check **"Add Python to PATH"** during installation
2. **ffmpeg** (required for voice features) — Install using one of these methods:
   - `winget install Gyan.FFmpeg` (Windows 10/11 with App Installer)
   - `choco install ffmpeg` (if you have [Chocolatey](https://chocolatey.org/))
   - Or download manually from [ffmpeg.org](https://ffmpeg.org/download.html) and add to PATH
3. **Git** (optional) — [git-scm.com](https://git-scm.com/download/win), or just download the ZIP from GitHub

### Quick Install (Windows)

**Option A — With Git:**
```cmd
git clone https://github.com/arcadeon54/Loki-Bot.git
cd Loki-Bot
install_windows.bat
```

**Option B — Without Git:**
1. Download the ZIP from the green **Code** button on GitHub
2. Extract it to a folder (e.g., `C:\Loki-Bot`)
3. Double-click `install_windows.bat`

The install script will:
1. Check that Python and ffmpeg are available
2. Create a Python virtual environment
3. Install Python packages
4. Create a `.env` template file

### Configuration (Windows)

1. **Edit the `.env` file** with your tokens:
   ```cmd
   notepad .env
   ```

2. **Required settings:**
   - `DISCORD_TOKEN` — Your Discord bot token ([get one here](https://discord.com/developers/applications))
   - `OPENAI_API_KEY` — Your OpenAI API key ([get one here](https://platform.openai.com/api-keys)), **or** set `LLM_PROVIDER=local` and configure `LOCAL_LLM_URL`

3. **Optional settings:**
   - `GEMINI_API_KEY` — For image/GIF recognition ([free key here](https://aistudio.google.com/app/apikey))
   - `SYSTEM_PROMPT` — The default personality prompt (server admins can override per-server)
   - `OPENAI_MODEL` — Change the model (default: `gpt-4o`)
   - `CONTEXT_MESSAGE_COUNT` — How many past messages to include as context (default: 50)

### Running the Bot (Windows)

**Test manually first:**
```cmd
venv\Scripts\activate
python loki_bot.py
```

**Run on startup with Task Scheduler:**

1. Open **Task Scheduler** (search for it in the Start menu)
2. Click **Create Basic Task**
3. Name it `Loki Bot` and click Next
4. Trigger: **When the computer starts** → Next
5. Action: **Start a program** → Next
6. Program/script: Browse to `pythonw.exe` inside your venv:
   ```
   C:\Loki-Bot\venv\Scripts\pythonw.exe
   ```
7. Add arguments:
   ```
   loki_bot.py
   ```
8. Start in:
   ```
   C:\Loki-Bot
   ```
9. Check **Open the Properties dialog** → Finish
10. In Properties, check **Run whether user is logged on or not**

**Alternatively, create a simple start script** — save this as `start_loki.bat` in your Loki-Bot folder:
```bat
@echo off
cd /d "%~dp0"
call venv\Scripts\activate
python loki_bot.py
```
Double-click it to start the bot, or add it to your Startup folder (`shell:startup`).

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

## Member Identity Mapping

The bot includes each user's Discord ID in messages sent to the LLM, formatted as `[DisplayName (ID:123456789)]: message`. This allows the bot to recognize users even when they change their Discord nickname.

To take advantage of this, include an identity map in your server's personality prompt using `/set_personality`. Add a block like this:

```
CRITICAL IDENTITY RULE: Each user message is formatted as [DisplayName (ID:number)].
Use the ID number to identify who is talking, NOT the display name — people change
their nicknames constantly. Here are the real identities:
ID:123456789 = Alice.
ID:987654321 = Bob.
When you see a message from ID:123456789, that is ALWAYS Alice regardless of display name.
When you see a message from ID:987654321, that is ALWAYS Bob regardless of display name.
Always address them by their real name, not their display name.
```

### How to Get a User's Discord ID

1. Open Discord **Settings > Advanced** and enable **Developer Mode**
2. Right-click any user and select **Copy User ID**
3. Add their ID and real name to your personality prompt as shown above

This is especially useful when your personality prompt references specific people by name — the bot will always know who's who, even if someone's display name is completely different from their real name.

## License

MIT

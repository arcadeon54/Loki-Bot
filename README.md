# Project: L.O.K.I. [Learning Oriented Kinetic Intelligence]

A feature-rich, personality-driven AI Discord bot with persistent memory, multi-LLM support, voice capabilities, media downloads, and deep conversation recall.

---

## Features

### Conversational AI
- **Intent Classification** — Automatically classifies messages (chat, emotional, question, command, etc.) and routes to the appropriate LLM
- **Emotion Detection** — Detects emotional tone per message for context-aware responses
- **Smart Model Routing** — Routes cheap/simple tasks (intent classification, quick lookups) to a fast/cheap LLM, and complex tasks to a primary LLM
- **Serious Mode** — Prefix any message with `-s` to get a straight, persona-free answer
- **Correction Detection** — Learns from corrections and adjusts responses accordingly

### Multi-LLM Support
- **OpenAI** — Primary LLM provider (GPT-4o or any OpenAI-compatible model)
- **Groq** — Fallback/cheap LLM for intent classification and simple tasks
- **Local LLM** — Support for any OpenAI-compatible local endpoint (LM Studio, Ollama, text-generation-webui)

### Memory & Learning
- **Persistent Memory** — SQLite-backed conversation history that survives restarts
- **Deep Memory Search** — ChromaDB RAG integration for searching across full conversation history
- **Self-Learning Personality** — Automatically writes personality learnings to a file every 6 hours
- **Relationship Memory** — Per-user personality notes updated every 10 interactions
- **Per-Server Personalities** — Each server admin can set a custom personality prompt via `/set_personality`

### Voice Capabilities
- **Voice Message Transcription** — Automatically transcribes Discord voice messages using Groq Whisper
- **Voice Message Responses** — Responds with audio when triggered via voice message (voice-in = voice-out)
- **TTS via ElevenLabs** — High-quality text-to-speech with configurable voice
- **edge-tts Fallback** — Free, unlimited TTS fallback when ElevenLabs is unavailable
- **Voice Chat** — Join voice channels and speak with `/loki_join`, `/loki_say`, `/loki_speak`

### Image & GIF Recognition
- **Google Gemini Vision** — Analyzes images and GIFs attached to messages (free tier)
- **Context-Aware** — Incorporates image descriptions into conversation context

### Media Downloads
Downloads media from **10+ platforms** with an intelligent fallback chain:
- **TikTok** — Watermark-free via Cobalt, with TikWM fallback
- **Instagram** — Reels, posts, and stories via Cobalt, SnapInsta, and yt-dlp
- **Twitter/X** — Via FxTwitter API and Cobalt
- **YouTube** — Via Cobalt and yt-dlp
- **Reddit** — Direct images, Cobalt, yt-dlp with cookies, gallery-dl
- **Facebook** — Via FBDownloader scrapers
- **Threads** — Via SaveThreads/ThreadSave scrapers
- **Pinterest** — Via PinterestDownloader scraper
- **Direct Image URLs** — Automatic detection and download with cookie support

**Additional download features:**
- **Self-hosted Cobalt** — Watermark-free downloads from multiple platforms
- **Media File Server** — Automatic fallback to a file server URL for files too large for Discord upload
- **Automatic Compression** — Attempts 480p then 360p compression before file server fallback
- **Auto-Download** — Optionally auto-download TikTok/Instagram links from a specific user
- **Natural Language Trigger** — Say "post this [url]" to trigger a download
- **`/download` Command** — Manual download trigger

### Conversation Features
- **"What Did I Miss?"** — Natural language detection for catch-up summaries
- **Unprompted Interjections** — Mood-aware interjections with drift (chill/active/hyped/late_night)
- **Open Thread Detection** — Runs periodically to follow up on unresolved topics
- **Tea Flagging** — Tracks drama/gossip moments with reactions and keywords
- **@Mention Support** — Member directory injected into LLM context for accurate user identification

### Web Search
- **Tavily Integration** — Real-time web search for factual questions, automatically triggered by question intent

### Natural Language Reminders
- Supports "in X minutes/hours", "at 5pm", "tomorrow at X", "tonight at X"
- Stored in SQLite, checked every minute
- Fires with user ping

### Wit & Quips
- Snarky/roast quotes with configurable frequency
- Rate-limited to prevent overuse
- Only fires in non-serious conversations

### Bot-to-Bot Coordination
- Shared state file for coordinating with other bots
- Prevents response conflicts and enables cooperative behavior

### Claude Code Integration
- `/cc` slash command for running Claude Code queries
- Configurable binary path, workspace, and timeout

### Channel Summarization
- `/summarize` — AI-powered summary of recent channel conversation

---

## Slash Commands

| Command | Description | Permission |
|---------|-------------|------------|
| `/summarize` | Summarize recent conversation | Everyone |
| `/download [url]` | Download media from a URL | Everyone |
| `/cc [query]` | Run a Claude Code query | Everyone |
| `/loki_join` | Bot joins your voice channel | Everyone |
| `/loki_leave` | Bot leaves the voice channel | Everyone |
| `/loki_say [text]` | Bot speaks text in voice chat | Everyone |
| `/loki_speak [question]` | Ask a question and hear the answer | Everyone |
| `/loki_reset` | Clear bot memory for this channel | Admin |
| `/set_personality [prompt]` | Set a custom personality for this server | Admin |
| `/view_personality` | View the current personality prompt | Admin |
| `/reset_personality` | Reset to the default personality | Admin |

---

## Requirements

- Python 3.10+
- ffmpeg (for voice features)
- A Discord bot token
- An OpenAI API key **or** a local LLM with an OpenAI-compatible API

### Optional
- Google Gemini API key (free) — for image/GIF recognition
- Groq API key (free) — for cheap model routing and voice transcription
- ElevenLabs API key (free tier) — for high-quality TTS
- Tavily API key (free tier) — for web search
- ChromaDB instance — for deep memory search / RAG
- Self-hosted Cobalt instance — for watermark-free media downloads
- nginx or similar — for serving large media files

---

## Installation

### Linux

```bash
git clone https://github.com/arcadeon54/Loki-Bot.git
cd Loki-Bot
bash install.sh
```

The install script will:
1. Install system dependencies (Python, ffmpeg, libsodium, etc.)
2. Create a Python virtual environment
3. Install Python packages from `requirements.txt`
4. Set up the systemd service

#### Configuration

1. Edit the `.env` file with your tokens:
   ```bash
   nano .env
   ```

2. **Required settings:**
   - `DISCORD_TOKEN` — Your Discord bot token ([get one here](https://discord.com/developers/applications))
   - `OPENAI_API_KEY` — Your OpenAI API key ([get one here](https://platform.openai.com/api-keys)), **or** set `LLM_PROVIDER=local` and configure `LOCAL_LLM_URL`

3. **Recommended settings:**
   - `GEMINI_API_KEY` — For image/GIF recognition ([free key](https://aistudio.google.com/app/apikey))
   - `FALLBACK_LLM_API_KEY` — Groq API key for cheap routing and voice transcription ([free key](https://console.groq.com/))
   - `FALLBACK_LLM_URL` and `FALLBACK_LLM_MODEL` — Groq endpoint and model name

#### Running

**Test manually:**
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

### Windows

#### Prerequisites

1. **Python 3.10+** — Download from [python.org](https://www.python.org/downloads/)
   - Check **"Add Python to PATH"** during installation
2. **ffmpeg** — Install via one of:
   - `winget install Gyan.FFmpeg`
   - `choco install ffmpeg` (with [Chocolatey](https://chocolatey.org/))
   - Manual download from [ffmpeg.org](https://ffmpeg.org/download.html)
3. **Git** (optional) — [git-scm.com](https://git-scm.com/download/win)

#### Quick Install

**With Git:**
```cmd
git clone https://github.com/arcadeon54/Loki-Bot.git
cd Loki-Bot
install_windows.bat
```

**Without Git:**
1. Download the ZIP from the **Code** button on GitHub
2. Extract to a folder (e.g., `C:\Loki-Bot`)
3. Double-click `install_windows.bat`

The install script will:
1. Verify Python and ffmpeg are available
2. Create a Python virtual environment
3. Install Python packages
4. Create a `.env` template

#### Configuration

Edit `.env` with your tokens:
```cmd
notepad .env
```

See the Linux configuration section above for required and recommended settings.

#### Running

**Test manually:**
```cmd
venv\Scripts\activate
python loki_bot.py
```

**Run on startup** — use Task Scheduler or create a `start_loki.bat`:
```bat
@echo off
cd /d "%~dp0"
call venv\Scripts\activate
python loki_bot.py
```

---

### Docker (Optional)

The bot can be containerized with Docker. Create a `Dockerfile` and mount your `.env` file and any cookie files as volumes. The key requirements are Python 3.10+, ffmpeg, and libsodium.

---

## Discord Bot Setup

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications)
2. Click **New Application** and name it
3. Go to the **Bot** tab:
   - Click **Reset Token** and copy it to your `.env` file
   - Enable **Message Content Intent** under Privileged Gateway Intents
   - Enable **Server Members Intent**
4. Go to the **OAuth2** tab:
   - Under **Scopes**, select `bot` and `applications.commands`
   - Under **Bot Permissions**, select:
     - Send Messages
     - Read Message History
     - Connect
     - Speak
     - Use Slash Commands
     - Attach Files
     - Embed Links
     - Add Reactions
   - Copy the generated URL and open it to invite the bot to your server

---

## Optional Services Setup

### Cobalt (Watermark-Free Downloads)

[Cobalt](https://github.com/imputnet/cobalt) provides watermark-free downloads from TikTok, Instagram, YouTube, and more.

```bash
docker run -d --name cobalt -p 9000:9000 ghcr.io/imputnet/cobalt:latest
```

Set `COBALT_URL=http://localhost:9000` in your `.env`.

### ChromaDB (Deep Memory / RAG)

[ChromaDB](https://www.trychroma.com/) enables deep memory search across full conversation history.

```bash
docker run -d --name chromadb -p 8100:8000 chromadb/chroma
```

Set `CHROMADB_HOST=localhost` and `CHROMADB_PORT=8100` in your `.env`.

Use `ingest_history.py` to import existing conversation history into ChromaDB:
```bash
source venv/bin/activate
python ingest_history.py
```

### Media File Server

For files too large to upload to Discord, set up an nginx server pointing to your downloads directory and set `MEDIA_BASE_URL` to the public URL.

---

## Per-Server Personalities

The bot supports custom personalities per server. The `.env` `SYSTEM_PROMPT` is the default. Server admins can override it:

- `/set_personality You are a pirate captain who speaks in nautical terms...` — Set a custom personality
- `/view_personality` — View the current personality prompt
- `/reset_personality` — Reset to default

Personalities are stored in the database and persist across restarts.

---

## Member Identity Mapping

The bot includes each user's Discord ID in messages sent to the LLM, formatted as `[DisplayName (ID:123456789)]: message`. This allows the bot to recognize users even when they change nicknames.

To take advantage of this, include an identity map in your server's personality prompt:

```
CRITICAL IDENTITY RULE: Each user message is formatted as [DisplayName (ID:number)].
Use the ID number to identify who is talking, NOT the display name.
ID:123456789 = Alice.
ID:987654321 = Bob.
Always address them by their real name, not their display name.
```

### How to Get a User's Discord ID
1. Open Discord **Settings > Advanced** and enable **Developer Mode**
2. Right-click any user and select **Copy User ID**

---

## Project Structure

```
Loki-Bot/
├── loki_bot.py           # Main bot code
├── shared_state.py       # Bot-to-bot coordination module
├── rag_search.py         # ChromaDB RAG search module
├── ingest_history.py     # History ingestion for ChromaDB
├── install.sh            # Linux install script
├── install_windows.bat   # Windows install script
├── loki.service          # systemd service file
├── requirements.txt      # Python dependencies
├── .env.example          # Configuration template
├── .gitignore            # Git ignore rules
├── cookies/              # Platform cookies (not tracked)
│   ├── reddit.txt
│   └── instagram.txt
└── downloads/            # Downloaded media (not tracked)
```

---

## Cookie Setup (Optional)

Some platforms (Reddit, Instagram) require cookies for reliable downloads:

1. Create a `cookies/` directory in the bot folder
2. Export cookies from your browser in Netscape/Mozilla format
3. Save as `cookies/reddit.txt` and `cookies/instagram.txt`

Browser extensions like [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc) can export cookies in the correct format.

---

## License

MIT

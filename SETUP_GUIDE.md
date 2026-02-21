# 🐍 LOKI BOT — Complete Setup Guide (For Beginners)

This guide assumes you know absolutely nothing about Linux, coding, or bots.
Follow every step exactly and you'll have Loki running in no time.

---

## TABLE OF CONTENTS

1. [What You're Getting](#1-what-youre-getting)
2. [Step 1: Create a Discord Bot Account](#2-step-1-create-a-discord-bot-account)
3. [Step 2: Get Your Free API Keys](#3-step-2-get-your-free-api-keys)
4. [Step 3: Set Up Your Linux Server](#4-step-3-set-up-your-linux-server)
5. [Step 4: Upload the Bot Files](#5-step-4-upload-the-bot-files)
6. [Step 5: Run the Install Script](#6-step-5-run-the-install-script)
7. [Step 6: Configure Your Tokens](#7-step-6-configure-your-tokens)
8. [Step 7: Test the Bot](#8-step-7-test-the-bot)
9. [Step 8: Enable Auto-Start](#9-step-8-enable-auto-start)
10. [Using Local LLM Instead of ChatGPT](#10-using-local-llm-instead-of-chatgpt)
11. [All Bot Commands](#11-all-bot-commands)
12. [Troubleshooting](#12-troubleshooting)
13. [Linux Cheat Sheet](#13-linux-cheat-sheet)

---

## 1. WHAT YOU'RE GETTING

Loki is a Discord bot that:
- **Responds** when you say "Loki", "loki", "LOKI", "asshole", or "Asshole"
- **Remembers** past conversations (saved to a database file)
- **Sees** images and GIFs posted in chat (uses Google Gemini, free)
- **Speaks** in voice channels with a male voice
- **Summarizes** conversations on command
- **Auto-restarts** if it crashes or if the server reboots
- Has a **special response** for Sheridan when she uses the 👀 emoji

### FILES INCLUDED:
```
loki-bot/
├── loki_bot.py          ← The main bot code
├── .env.example         ← Template for your secret keys
├── requirements.txt     ← List of Python packages needed
├── loki.service         ← Auto-restart configuration
├── install.sh           ← Automated installer script
└── SETUP_GUIDE.md       ← This file
```

---

## 2. STEP 1: CREATE A DISCORD BOT ACCOUNT

You need to create a "bot application" on Discord's developer site.

### A. Create the Application

1. Open your web browser and go to: **https://discord.com/developers/applications**
2. Log in with your Discord account
3. Click the blue **"New Application"** button (top right)
4. Name it **"Loki"** and click **Create**

### B. Set Up the Bot

1. In the left sidebar, click **"Bot"**
2. You'll see a section called "Token" — click **"Reset Token"**
3. Click **"Yes, do it!"**
4. **COPY THE TOKEN** and save it somewhere safe (Notepad, etc.)
   - ⚠️ **You can only see this once!** If you lose it, you'll have to reset it again.
   - ⚠️ **NEVER share this token with anyone!**

5. Scroll down to **"Privileged Gateway Intents"** and turn ON all three:
   - ✅ **PRESENCE INTENT**
   - ✅ **SERVER MEMBERS INTENT**
   - ✅ **MESSAGE CONTENT INTENT**
6. Click **"Save Changes"**

### C. Invite the Bot to Your Server

1. In the left sidebar, click **"OAuth2"**
2. Scroll down to **"OAuth2 URL Generator"**
3. Under **SCOPES**, check:
   - ✅ `bot`
   - ✅ `applications.commands`
4. Under **BOT PERMISSIONS**, check:
   - ✅ `Send Messages`
   - ✅ `Send Messages in Threads`
   - ✅ `Read Message History`
   - ✅ `View Channels`
   - ✅ `Embed Links`
   - ✅ `Attach Files`
   - ✅ `Add Reactions`
   - ✅ `Connect` (for voice)
   - ✅ `Speak` (for voice)
   - ✅ `Use Voice Activity`

   Or just check **Administrator** for simplicity (gives all permissions).
5. Copy the **Generated URL** at the bottom
6. Paste it into your browser
7. Select your server from the dropdown and click **Authorize**

You should see "Loki" appear in your server (offline for now).

---

## 3. STEP 2: GET YOUR FREE API KEYS

### A. Google Gemini Key (FREE — for image recognition)

1. Go to: **https://aistudio.google.com/app/apikey**
2. Sign in with your Google account
3. Click **"Create API Key"**
4. Copy the key and save it

### B. OpenAI Key (PAID — for ChatGPT brain) — *Optional*

Only needed if you want to use ChatGPT. Skip if using a local LLM.

1. Go to: **https://platform.openai.com/api-keys**
2. Create an account or sign in
3. Click **"Create new secret key"**
4. Copy the key and save it
5. You'll need to add payment info (usage is pay-per-message, typically pennies)

---

## 4. STEP 3: SET UP YOUR LINUX SERVER

### If You're Using a Mini PC with Linux Already Installed:

#### A. Connect to Your Server

If you're sitting at the mini PC, just open the **Terminal** app.

If you're connecting remotely from another computer:
- On **Windows**: Download **PuTTY** or use **Windows Terminal**
- Type: `ssh yourusername@your-server-ip-address`
- Enter your password when prompted

#### B. Basic Linux Navigation (Crash Course)

Here's what you need to know:

| Command | What it does | Example |
|---------|-------------|---------|
| `ls` | List files in current folder | `ls` |
| `cd foldername` | Go into a folder | `cd loki-bot` |
| `cd ..` | Go back one folder | `cd ..` |
| `pwd` | Show current location | `pwd` |
| `nano filename` | Edit a file | `nano .env` |
| `cat filename` | Display a file's contents | `cat .env` |
| `mkdir foldername` | Create a new folder | `mkdir loki-bot` |
| `cp file1 file2` | Copy a file | `cp .env.example .env` |
| `rm filename` | Delete a file | `rm old_file.txt` |
| `sudo` | Run as administrator | `sudo apt update` |

---

## 5. STEP 4: UPLOAD THE BOT FILES

### Option A: Using SCP (from your Windows/Mac computer)

If the bot files are on your personal computer and the Linux server is elsewhere:

1. Download all the bot files to a folder on your computer
2. Open a terminal/command prompt on your computer
3. Run:
```bash
scp -r /path/to/loki-bot yourusername@your-server-ip:~/
```

### Option B: Using Git (download directly on the server)

If you put the files on GitHub:
```bash
cd ~
git clone https://github.com/yourusername/loki-bot.git
cd loki-bot
```

### Option C: Create Files Manually on the Server

1. Connect to your server
2. Run these commands one at a time:

```bash
# Go to your home folder
cd ~

# Create the bot folder
mkdir loki-bot
cd loki-bot
```

3. Create each file using nano (a text editor):

```bash
# Create the main bot file
nano loki_bot.py
# → Paste the contents of loki_bot.py
# → Press Ctrl+O to save, then Enter, then Ctrl+X to exit

# Create requirements.txt
nano requirements.txt
# → Paste the contents, save and exit

# Create .env.example
nano .env.example
# → Paste the contents, save and exit

# Create install.sh
nano install.sh
# → Paste the contents, save and exit

# Create loki.service
nano loki.service
# → Paste the contents, save and exit
```

---

## 6. STEP 5: RUN THE INSTALL SCRIPT

This script automatically installs everything you need.

```bash
# Make sure you're in the bot folder
cd ~/loki-bot

# Make the install script executable
chmod +x install.sh

# Run it
bash install.sh
```

The script will:
- Update your system
- Install Python 3, pip, ffmpeg, and other tools
- Create a Python virtual environment
- Install all Python packages
- Create your .env file
- Set up the auto-restart service

**If you see any errors**, check the [Troubleshooting](#12-troubleshooting) section.

---

## 7. STEP 6: CONFIGURE YOUR TOKENS

Now you need to put your secret keys into the .env file.

```bash
nano .env
```

You'll see a file that looks like this:
```
DISCORD_TOKEN=paste-your-discord-bot-token-here
LLM_PROVIDER=openai
OPENAI_API_KEY=paste-your-openai-api-key-here
...
```

**Replace each placeholder** with your actual keys:

1. **DISCORD_TOKEN**: Paste the bot token from Step 1B
2. **OPENAI_API_KEY**: Paste your OpenAI key from Step 2B
3. **GEMINI_API_KEY**: Paste your Gemini key from Step 2A
4. **SYSTEM_PROMPT**: This is already filled with Loki's personality — edit if you want!

**To save:** Press `Ctrl+O`, then `Enter`, then `Ctrl+X`

---

## 8. STEP 7: TEST THE BOT

Before enabling auto-start, test it manually to make sure everything works.

```bash
# Make sure you're in the bot folder
cd ~/loki-bot

# Activate the virtual environment
source venv/bin/activate

# Run the bot
python loki_bot.py
```

You should see:
```
2025-XX-XX [INFO] Memory database loaded from loki_memory.db
2025-XX-XX [INFO] Gemini vision initialized
2025-XX-XX [INFO] LLM: OpenAI  model=gpt-4o
2025-XX-XX [INFO] ✅  Loki is online as Loki#1234 (ID: 123456789)
2025-XX-XX [INFO] Synced 5 slash commands
```

Now go to your Discord server and type: **"Hey Loki"**

The bot should respond in character!

**To stop the bot:** Press `Ctrl+C` in the terminal.

---

## 9. STEP 8: ENABLE AUTO-START

Once the bot works, set it up to run forever and restart automatically.

```bash
# Enable the service (starts on boot)
sudo systemctl enable loki

# Start the service now
sudo systemctl start loki
```

### Useful Commands:

```bash
# Check if the bot is running
sudo systemctl status loki

# View live logs (like seeing the bot's thoughts)
sudo journalctl -u loki -f

# Restart the bot
sudo systemctl restart loki

# Stop the bot
sudo systemctl stop loki

# Disable auto-start
sudo systemctl disable loki
```

---

## 10. USING LOCAL LLM INSTEAD OF CHATGPT

If you want to use a locally-hosted LLM instead of paying for ChatGPT:

### Option A: LM Studio (Easiest)

1. Download LM Studio from: https://lmstudio.ai
2. Install it on your mini PC (or another computer on your network)
3. Download a model (e.g., Mistral 7B, Llama 3, etc.)
4. In LM Studio, go to the **"Local Server"** tab
5. Click **"Start Server"** — it will run on `http://localhost:1234`
6. Edit your `.env` file:

```
LLM_PROVIDER=local
LOCAL_LLM_URL=http://localhost:1234/v1
LOCAL_LLM_MODEL=local-model
```

### Option B: Ollama

1. Install Ollama:
```bash
curl -fsSL https://ollama.ai/install.sh | sh
```

2. Download a model:
```bash
ollama pull llama3
```

3. Ollama automatically runs a server. Edit your `.env`:
```
LLM_PROVIDER=local
LOCAL_LLM_URL=http://localhost:11434/v1
LOCAL_LLM_MODEL=llama3
```

---

## 11. ALL BOT COMMANDS

### Text Triggers (just type these in chat):
| Trigger | What happens |
|---------|-------------|
| Say "Loki" / "loki" / "LOKI" | Bot responds to you |
| Say "asshole" / "Asshole" | Bot responds (it's one of its names) |
| @Loki (mention the bot) | Bot responds |
| Reply to a Loki message | Bot responds |
| Sheridan uses 👀 | Special response just for her |

### Slash Commands (type / in Discord):
| Command | What it does |
|---------|-------------|
| `/summarize` | Summarizes the last ~20 messages |
| `/loki_join` | Loki joins your voice channel |
| `/loki_leave` | Loki leaves the voice channel |
| `/loki_say [text]` | Makes Loki speak specific text in voice |
| `/loki_speak [question]` | Ask Loki a question, hear the answer in voice |
| `/loki_reset` | Wipes Loki's memory for the current channel |

---

## 12. TROUBLESHOOTING

### "Command not found: python3"
```bash
sudo apt install python3 python3-pip python3-venv
```

### "No module named discord"
You forgot to activate the virtual environment:
```bash
cd ~/loki-bot
source venv/bin/activate
pip install -r requirements.txt
```

### "DISCORD_TOKEN not set"
You didn't edit the .env file yet:
```bash
nano .env
```
Make sure there are NO spaces around the `=` sign.

### Bot is online but doesn't respond
- Check that **MESSAGE CONTENT INTENT** is turned on (Step 1B, #5)
- Make sure you're saying one of the trigger words
- Check the logs: `sudo journalctl -u loki -f`

### Slash commands don't appear
- They can take up to an hour to sync globally
- Try restarting the bot
- Make sure `applications.commands` scope was selected when inviting

### Voice doesn't work
- Make sure ffmpeg is installed: `sudo apt install ffmpeg`
- Make sure PyNaCl is installed: `pip install PyNaCl`
- Make sure the bot has Connect and Speak permissions

### "Error: libsodium not found"
```bash
sudo apt install libsodium-dev
pip install PyNaCl --force-reinstall
```

### Bot crashes with memory errors
Your model might be too large. Try a smaller one or increase system swap:
```bash
sudo fallocate -l 4G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
```

### Image recognition not working
- Check your GEMINI_API_KEY is correct
- Free tier has rate limits (~15 requests/minute)
- Check logs for specific error messages

---

## 13. LINUX CHEAT SHEET

### File & Folder Commands
```bash
ls                  # List files
ls -la              # List ALL files (including hidden ones like .env)
cd ~/loki-bot       # Go to bot folder
pwd                 # Where am I?
cat .env            # Show file contents
nano .env           # Edit a file
cp file1 file2      # Copy a file
mv file1 file2      # Move/rename a file
rm filename         # Delete a file
mkdir foldername    # Create a folder
```

### System Commands
```bash
sudo apt update              # Update package lists
sudo apt upgrade -y          # Install updates
sudo reboot                  # Restart the server
top                          # Show running processes (q to quit)
df -h                        # Show disk space
free -h                      # Show memory usage
```

### Service Commands (for the bot)
```bash
sudo systemctl start loki    # Start the bot
sudo systemctl stop loki     # Stop the bot
sudo systemctl restart loki  # Restart the bot
sudo systemctl status loki   # Check if it's running
sudo systemctl enable loki   # Enable start on boot
sudo systemctl disable loki  # Disable start on boot
sudo journalctl -u loki -f   # Watch live logs
sudo journalctl -u loki --since "1 hour ago"  # Recent logs
```

### Python Virtual Environment
```bash
source venv/bin/activate     # Activate (must do before running bot manually)
deactivate                   # Deactivate
pip install package_name     # Install a Python package
pip list                     # Show installed packages
```

### Network
```bash
ip addr show                 # Show your IP address
ping google.com              # Test internet connection
curl ifconfig.me             # Show your public IP
```

---

## UPDATING THE BOT

If you edit the bot code later:

```bash
# If running as a service
sudo systemctl restart loki

# If running manually, Ctrl+C first, then:
source venv/bin/activate
python loki_bot.py
```

---

## EDITING LOKI'S PERSONALITY

To change how Loki talks, edit the `SYSTEM_PROMPT` in your `.env` file:

```bash
nano .env
```

Find the line starting with `SYSTEM_PROMPT=` and change the text after it.
Then restart the bot.

---

*That's it! If you get stuck, re-read the relevant section or check the logs.*
*Have fun with Loki!* 🐍

#!/bin/bash
set -euo pipefail
pkill -f 'python loki_bot.py' 2>/dev/null || true
sleep 1
cd /home/g2k247/loki-bot
exec /home/g2k247/loki-bot/venv/bin/python loki_bot.py >> /home/g2k247/loki-bot/loki_bot.log 2>&1

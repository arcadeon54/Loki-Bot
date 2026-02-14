#!/bin/bash
# =============================================================================
#  LOKI BOT — Automated Install Script for Linux
# =============================================================================
#  Run this with:   bash install.sh
# =============================================================================

set -e  # Exit on any error

echo "============================================"
echo "  🐍 Loki Bot — Automated Installer"
echo "============================================"
echo ""

# ─── 1. Update system packages ───────────────────────────────────────────────
echo "📦 Updating system packages..."
sudo apt update && sudo apt upgrade -y

# ─── 2. Install required system packages ─────────────────────────────────────
echo "📦 Installing Python, pip, ffmpeg, and other dependencies..."
sudo apt install -y python3 python3-pip python3-venv ffmpeg libffi-dev \
    libsodium-dev build-essential

# ─── 3. Create virtual environment ───────────────────────────────────────────
echo "🐍 Creating Python virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
source venv/bin/activate

# ─── 4. Install Python packages ──────────────────────────────────────────────
echo "📦 Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# ─── 5. Create .env from template if it doesn't exist ────────────────────────
if [ ! -f ".env" ]; then
    echo "📝 Creating .env file from template..."
    cp .env.example .env
    echo ""
    echo "⚠️  IMPORTANT: You need to edit the .env file with your tokens!"
    echo "   Run:  nano .env"
    echo ""
else
    echo "✅ .env file already exists — skipping"
fi

# ─── 6. Set up systemd service ───────────────────────────────────────────────
echo "⚙️  Setting up auto-start service..."
CURRENT_USER=$(whoami)
CURRENT_DIR=$(pwd)

# Create a copy with the correct username and paths
sed -e "s|YOUR_USERNAME|$CURRENT_USER|g" \
    -e "s|/home/$CURRENT_USER/loki-bot|$CURRENT_DIR|g" \
    loki.service > /tmp/loki.service

sudo cp /tmp/loki.service /etc/systemd/system/loki.service
sudo systemctl daemon-reload
echo "✅ Service installed (but not started yet — edit .env first!)"

echo ""
echo "============================================"
echo "  ✅ Installation Complete!"
echo "============================================"
echo ""
echo "NEXT STEPS:"
echo "  1. Edit the .env file with your tokens:"
echo "     nano .env"
echo ""
echo "  2. Test the bot manually first:"
echo "     source venv/bin/activate"
echo "     python loki_bot.py"
echo ""
echo "  3. Once it works, enable auto-start:"
echo "     sudo systemctl enable loki"
echo "     sudo systemctl start loki"
echo ""
echo "  4. Check the bot status:"
echo "     sudo systemctl status loki"
echo ""
echo "  5. View live logs:"
echo "     sudo journalctl -u loki -f"
echo ""

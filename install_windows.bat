@echo off
REM =============================================================================
REM  LOKI BOT — Automated Install Script for Windows
REM =============================================================================
REM  Run this by double-clicking or from Command Prompt: install_windows.bat
REM =============================================================================

echo ============================================
echo   Loki Bot — Windows Installer
echo ============================================
echo.

REM ─── 1. Check for Python ─────────────────────────────────────────────────────
echo Checking for Python...
python --version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python is not installed or not in your PATH.
    echo.
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo IMPORTANT: Check "Add Python to PATH" during installation!
    echo.
    pause
    exit /b 1
)
python --version
echo.

REM ─── 2. Check for ffmpeg ─────────────────────────────────────────────────────
echo Checking for ffmpeg...
ffmpeg -version >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo WARNING: ffmpeg is not installed. Voice features will not work.
    echo.
    echo To install ffmpeg, run one of the following:
    echo   winget install Gyan.FFmpeg
    echo   choco install ffmpeg
    echo Or download from https://ffmpeg.org/download.html
    echo.
    echo Continuing without ffmpeg...
    echo.
) else (
    echo ffmpeg found.
    echo.
)

REM ─── 3. Create virtual environment ───────────────────────────────────────────
echo Creating Python virtual environment...
if not exist "venv" (
    python -m venv venv
)
call venv\Scripts\activate.bat

REM ─── 4. Install Python packages ──────────────────────────────────────────────
echo Installing Python dependencies...
pip install --upgrade pip
pip install -r requirements.txt

REM ─── 5. Create .env from template if it doesn't exist ────────────────────────
if not exist ".env" (
    if exist ".env.example" (
        echo Creating .env file from template...
        copy .env.example .env
    ) else (
        echo Creating blank .env file...
        (
            echo DISCORD_TOKEN=your_discord_bot_token_here
            echo OPENAI_API_KEY=your_openai_api_key_here
            echo GEMINI_API_KEY=
            echo LLM_PROVIDER=openai
            echo OPENAI_MODEL=gpt-4o
            echo LOCAL_LLM_URL=http://localhost:1234/v1
            echo LOCAL_LLM_MODEL=local-model
            echo SYSTEM_PROMPT=You are Loki, the God of Mischief.
            echo CONTEXT_MESSAGE_COUNT=50
        ) > .env
    )
    echo.
    echo !! IMPORTANT: You need to edit the .env file with your tokens!
    echo    Run:  notepad .env
    echo.
) else (
    echo .env file already exists — skipping
)

echo.
echo ============================================
echo   Installation Complete!
echo ============================================
echo.
echo NEXT STEPS:
echo   1. Edit the .env file with your tokens:
echo      notepad .env
echo.
echo   2. Test the bot:
echo      venv\Scripts\activate
echo      python loki_bot.py
echo.
echo   3. To run on startup, see the README for
echo      Task Scheduler instructions.
echo.
pause

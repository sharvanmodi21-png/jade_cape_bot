@echo off
REM Jade Cape Crypto Bot Launcher
REM This script runs the trading bot with proper environment

cd /d "%~dp0"
echo Starting Jade Cape Crypto Trading Bot...
echo.

REM Ensure pip is available
python -m ensurepip --upgrade >nul 2>&1

REM Check if .env file exists, if not copy from example
if not exist .env (
    if exist .env.example (
        copy .env.example .env >nul
        echo Created .env file from .env.example
        echo Please edit .env with your actual Binance API credentials
        echo.
    )
)

REM Install required packages non‑interactive
echo Installing required packages...
python -m pip install --quiet -r requirements.txt >nul 2>&1

REM Run the bot
echo Launching bot in %TRADING_MODE% mode...
python jade_cape_bot.py

pause
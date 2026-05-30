# Jade Cape Crypto Trading Bot

A Python-based trading bot that implements the Jade Cape Liquidity & Volatility Playbook for cryptocurrency intraday trading.

## Features

- Implements the complete Jade Cape strategy for crypto markets
- Paper trading mode for safe testing
- Live trading capability with Binance API
- Automated scanning every 15 minutes during NY session (13:00-16:00 UTC)
- Risk management with configurable position sizing
- Comprehensive logging and error handling

## Strategy Overview

The bot identifies liquidity raids (stop hunts) followed by reversals using:
- Session timing (NY Session: 13:00-16:00 UTC)
- Institutional order flow patterns
- Liquidity zones (PDH, PDL, Asian Session High/Low, London Session High/Low)
- Confirmation signals (FVG, MSS, Turtle Soup, Breaker Block)

## Setup Instructions

### 1. Prerequisites
- Python 3.8+
- Binance account (for API keys)
- Required Python packages (installed automatically)

### 2. Installation
1. Clone or copy this repository to your local machine
2. Navigate to the `jade_cape_bot` directory

### 3. Configuration
1. Copy `.env.example` to `.env`:
   ```bash
   copy .env.example .env
   ```
2. Edit `.env` file and add your Binance API credentials:
   ```
   BINANCE_API_KEY=your_actual_api_key_here
   BINANCE_API_SECRET=your_actual_api_secret_here
   ```
3. Configure trading parameters:
   - `TRADING_MODE=PAPER` (use PAPER for testing, LIVE for real trading)
   - `SYMBOL=BTCUSDT` (trading pair)
   - `RISK_PERCENT=1.0` (risk per trade as % of account)
   - `MAX_TRADES_PER_DAY=3`
   - Session times in UTC (default: 13:00-16:00)

### 4. Install Dependencies
The bot will automatically install required packages on first run, or you can manually install:
```bash
pip install -r requirements.txt
```

### 5. Running the Bot
Double-click `run_bot.bat` or run from command line:
```bash
python jade_cape_bot.py
```

## Important Notes

### Security
- **Never commit your `.env` file** to version control
- API keys provide access to your Binance account
- Start with PAPER mode to test the strategy
- When switching to LIVE mode, start with small position sizes

### Paper Trading vs Live Trading
- **PAPER mode**: Simulates trades without placing real orders
- **LIVE mode**: Places actual orders on Binance
- Always test thoroughly in PAPER mode before going live

### Risk Management
- The bot implements fixed fractional position sizing
- Daily trade limits prevent overtrading
- Stop losses are placed beyond liquidity raid levels
- Never risk more than configured percentage per trade

## Files in This Directory

- `jade_cape_bot.py` - Main bot implementation
- `requirements.txt` - Python dependencies
- `.env.example` - Template for environment variables
- `run_bot.bat` - Windows batch file to launch the bot
- `README.md` - This file

## Strategy Parameters (Based on Jade Cape Model)

- **Primary Session**: NY Session (13:00-16:00 UTC)
- **Instruments**: BTC/USDT (primary), ETH/USDT (secondary)
- **Timeframes**: 
  - Daily: Bias determination
  - 4H: Liquidity zone identification
  - 1H: Setup tracking
  - 15m: Setup confirmation
  - 5m: Entry timing
- **Risk**: 0.5-1% per trade, max 3 trades/day, 2% daily loss limit

## Disclaimer

THIS BOT IS FOR EDUCATIONAL PURPOSES ONLY. 
CRYPTOCURRENCY TRADING INVOLVES SIGNIFICANT RISK OF LOSS.
PAST PERFORMANCE DOES NOT GUARANTEE FUTURE RESULTS.
THE AUTHOR IS NOT RESPONSIBLE FOR ANY LOSSES INCURRED WHILE USING THIS BOT.
ALWAYS TRADE RESPONSIBLY AND CONSULT WITH A FINANCIAL ADVISOR.
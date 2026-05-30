#!/usr/bin/env python3
"""
Jade Cape Crypto Intraday Trading Bot
Implements the liquidity-based intraday strategy for cryptocurrency markets
"""

import os
import time
import logging
from datetime import datetime
import pandas as pd
import numpy as np
from binance.client import Client
from binance.exceptions import BinanceAPIException
import schedule
import warnings
from dotenv import load_dotenv

warnings.filterwarnings('ignore')
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('jade_cape_bot.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

class JadeCapeBot:
    """Main bot class"""

    def __init__(self):
        """Initialize the Jade Cape trading bot"""
        self.api_key = os.getenv('BINANCE_API_KEY')
        self.api_secret = os.getenv('BINANCE_SECRET_KEY')
        if not self.api_key or not self.api_secret:
            raise ValueError("Binance API credentials not found in environment variables")
        self.client = Client(self.api_key, self.api_secret)
        try:
            self.client.ping()
            logger.info("Binance API connection verified")
        except Exception as e:
            logger.error(f"Failed to verify Binance connection: {e}")

        # Trading settings
        self.symbol = os.getenv('SYMBOL', 'BTCUSDT')
        self.trading_mode = os.getenv('TRADING_MODE', 'PAPER')  # PAPER or LIVE
        self.risk_percent = float(os.getenv('RISK_PERCENT', '1.0'))
        self.max_trades_per_day = int(os.getenv('MAX_TRADES_PER_DAY', '3'))

        # Session times (UTC)
        self.session_start = os.getenv('SESSION_START', '13:00')
        self.session_end = os.getenv('SESSION_END', '16:00')
        self.midday_chop_start = os.getenv('MIDDAY_CHOP_START', '16:00')
        self.midday_chop_end = os.getenv('MIDDAY_CHOP_END', '18:00')
        self.exit_by = os.getenv('EXIT_BY', '17:00')

        # State tracking
        self.daily_trades = 0
        self.daily_pnl = 0.0
        self.last_trade_date = None
        self.positions = {}

        # Strategy parameters
        self.timeframes = {
            'daily': '1d',
            '4h': '4h',
            '1h': '1h',
            '15m': '15m',
            '5m': '5m'
        }

        logger.info(f"Jade Cape Bot initialized - Symbol: {self.symbol}, Mode: {self.trading_mode}")

    # ---------------------------------------------------------------------
    # Helper methods
    # ---------------------------------------------------------------------
    def reset_daily_counters(self):
        """Reset daily trade counters at the start of a new UTC day"""
        today = datetime.utcnow().date()
        if self.last_trade_date != today:
            self.daily_trades = 0
            self.daily_pnl = 0.0
            self.last_trade_date = today
            logger.info("Daily counters reset")

    def is_within_session(self, check_time: datetime = None) -> bool:
        """Return True if current UTC time is inside the configured session"""
        if check_time is None:
            check_time = datetime.utcnow()
        current = check_time.strftime('%H:%M')
        return self.session_start <= current <= self.session_end

    def is_within_midday_chop(self, check_time: datetime = None) -> bool:
        """Return True if we are in the midday-chop window (no trading)"""
        if check_time is None:
            check_time = datetime.utcnow()
        current = check_time.strftime('%H:%M')
        return self.midday_chop_start <= current <= self.midday_chop_end

    def should_exit_positions(self, check_time: datetime = None) -> bool:
        """Return True if we have passed the exit-by time"""
        if check_time is None:
            check_time = datetime.utcnow()
        current = check_time.strftime('%H:%M')
        return current >= self.exit_by

    def get_historical_data(self, timeframe: str, limit: int = 100) -> pd.DataFrame:
        """Fetch klines from Binance and return a cleaned DataFrame"""
        try:
            klines = self.client.get_klines(symbol=self.symbol, interval=timeframe, limit=limit)
            df = pd.DataFrame(klines, columns=[
                'timestamp', 'open', 'high', 'low', 'close', 'volume',
                'close_time', 'quote_asset_volume', 'number_of_trades',
                'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
            ])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
            for col in ['open', 'high', 'low', 'close', 'volume']:
                df[col] = df[col].astype(float)
            return df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
        except BinanceAPIException as e:
            logger.error(f"Binance API error fetching {timeframe} data: {e}")
            return pd.DataFrame()
        except Exception as e:
            logger.error(f"Error fetching {timeframe} data: {e}")
            return pd.DataFrame()

    def calculate_daily_bias(self) -> str:
        """Determine bias from daily and 4-hour candles"""
        try:
            daily = self.get_historical_data(self.timeframes['daily'], limit=5)
            h4 = self.get_historical_data(self.timeframes['4h'], limit=5)
            if daily.empty or h4.empty or len(daily) < 2 or len(h4) < 2:
                return 'neutral'
            daily_bull = daily.iloc[-2]['close'] > daily.iloc[-2]['open']
            h4_bull = h4.iloc[-2]['close'] > h4.iloc[-2]['open']
            if daily_bull and h4_bull:
                return 'bullish'
            if not daily_bull and not h4_bull:
                return 'bearish'
            return 'neutral'
        except Exception as e:
            logger.error(f"Error calculating daily bias: {e}")
            return 'neutral'

    def mark_liquidity_zones(self) -> dict:
        """Identify key liquidity levels for the current day"""
        zones = {}
        daily = self.get_historical_data(self.timeframes['daily'], limit=2)
        if not daily.empty and len(daily) >= 2:
            zones['PDH'] = daily.iloc[-2]['high']
            zones['PDL'] = daily.iloc[-2]['low']
        h1 = self.get_historical_data('1h', limit=48)
        if not h1.empty:
            today = datetime.utcnow().date()
            today_candles = h1[h1['timestamp'].dt.date == today]
            if today_candles.empty:
                today_candles = h1.tail(24)
            asian = today_candles[(today_candles['timestamp'].dt.hour >= 0) & (today_candles['timestamp'].dt.hour < 8)]
            london = today_candles[(today_candles['timestamp'].dt.hour >= 8) & (today_candles['timestamp'].dt.hour < 13)]
            if not asian.empty:
                zones['Asian_High'] = asian['high'].max()
                zones['Asian_Low'] = asian['low'].min()
            if not london.empty:
                zones['London_High'] = london['high'].max()
                zones['London_Low'] = london['low'].min()
        logger.info(f"Liquidity zones marked: {zones}")
        return zones

    def detect_liquidity_raid(self, df_15m: pd.DataFrame, zones: dict):
        """Return (raid_detected, level_name, sweep_price)"""
        if df_15m.empty or not zones:
            return False, None, 0.0
        recent = df_15m.tail(4)
        low_levels = ['PDL', 'Asian_Low', 'London_Low']
        high_levels = ['PDH', 'Asian_High', 'London_High']
        for name, price in zones.items():
            if price == 0:
                continue
            for _, candle in recent.iterrows():
                if name in low_levels and candle['low'] < price and candle['close'] > price:
                    return True, name, candle['low']
                if name in high_levels and candle['high'] > price and candle['close'] < price:
                    return True, name, candle['high']
        return False, None, 0.0

    # Simple placeholder signal detectors – real implementation can be richer
    def detect_fvg(self, df: pd.DataFrame, direction: str) -> bool:
        if len(df) < 3:
            return False
        for i in range(len(df) - 2):
            c1, c2, c3 = df.iloc[i], df.iloc[i+1], df.iloc[i+2]
            if direction == 'bullish' and c3['low'] > c1['high'] and c2['close'] > c2['open']:
                return True
            if direction == 'bearish' and c1['low'] > c3['high'] and c2['close'] < c2['open']:
                return True
        return False

    def detect_mss(self, df: pd.DataFrame, direction: str) -> bool:
        if len(df) < 6:
            return False
        highs = df['high'].values
        lows = df['low'].values
        closes = df['close'].values
        if direction == 'bullish':
            for i in range(len(df)-3, 2, -1):
                if highs[i] > highs[i-1] and highs[i] > highs[i-2] and highs[i] > highs[i+1] and highs[i] > highs[i+2]:
                    if closes[-1] > highs[i]:
                        return True
                    break
        else:
            for i in range(len(df)-3, 2, -1):
                if lows[i] < lows[i-1] and lows[i] < lows[i-2] and lows[i] < lows[i+1] and lows[i] < lows[i+2]:
                    if closes[-1] < lows[i]:
                        return True
                    break
        return False

    def detect_turtle_soup(self, df: pd.DataFrame, direction: str) -> bool:
        return True  # Simplified placeholder

    def detect_breaker_block(self, df: pd.DataFrame, direction: str) -> bool:
        return True  # Simplified placeholder

    def detect_confirmation_signal(self, df_15m: pd.DataFrame, df_5m: pd.DataFrame, direction: str):
        if self.detect_fvg(df_15m, direction):
            return True, 'FVG'
        if self.detect_mss(df_15m, direction):
            return True, 'MSS'
        if self.detect_turtle_soup(df_15m, direction):
            return True, 'Turtle_Soup'
        if self.detect_breaker_block(df_15m, direction):
            return True, 'Breaker_Block'
        return False, None

    def calculate_position_size(self, account_balance: float, entry_price: float, stop_loss: float) -> float:
        risk_amount = account_balance * (self.risk_percent / 100.0)
        stop_distance = abs(entry_price - stop_loss)
        if stop_distance == 0:
            return 0.0
        qty = risk_amount / stop_distance
        return max(0.0, float('{:.6f}'.format(qty)))

    # ---------------------------------------------------------------------
    # Order handling
    # ---------------------------------------------------------------------
    def place_order(self, direction: str, qty: float, entry: float, stop: float):
        side = 'BUY' if direction == 'bullish' else 'SELL'
        try:
            if self.trading_mode.upper() == 'PAPER':
                # In paper mode we avoid calling Binance's authenticated test order endpoint to prevent API-key validation errors.
                logger.info(f"Paper {direction} order simulated: qty={qty}, entry~{entry}, stop~{stop}")
            else:
                order = self.client.order_market(symbol=self.symbol, side=side, quantity=qty)
                logger.info(f"Live {direction} order executed: {order}")
            self.positions[self.symbol] = {
                'direction': direction,
                'qty': qty,
                'entry': entry,
                'stop': stop,
                'open_time': datetime.utcnow()
            }
            self.daily_trades += 1
        except BinanceAPIException as e:
            logger.error(f"Binance API error placing order: {e.message}")
        except Exception as e:
            logger.error(f"Unexpected error placing order: {e}")

    def close_all_positions(self):
        for sym, pos in list(self.positions.items()):
            try:
                if self.trading_mode.upper() == 'PAPER':
                    logger.info(f"Paper closing position for {sym}")
                else:
                    side = 'SELL' if pos['direction'] == 'bullish' else 'BUY'
                    self.client.order_market(symbol=sym, side=side, quantity=pos['qty'])
                    logger.info(f"Live position closed for {sym}")
            except Exception as e:
                logger.error(f"Error closing position for {sym}: {e}")
            finally:
                del self.positions[sym]

    # ---------------------------------------------------------------------
    # Core workflow
    # ---------------------------------------------------------------------
    def check_and_trade(self):
        """Perform a full scan and possibly enter a trade"""
        try:
            self.reset_daily_counters()
            if not self.is_within_session():
                logger.info("Outside trading session – skipping.")
                return
            if self.is_within_midday_chop():
                logger.info("Midday chop period – skipping.")
                return
            if self.should_exit_positions():
                logger.info("Exit by time reached – closing positions if any.")
                self.close_all_positions()
                return

            df_15m = self.get_historical_data(self.timeframes['15m'], limit=200)
            df_5m = self.get_historical_data(self.timeframes['5m'], limit=200)
            if df_15m.empty or df_5m.empty:
                logger.warning("Insufficient market data – skipping.")
                return

            bias = self.calculate_daily_bias()
            if bias == 'neutral':
                logger.info("No clear daily bias – skipping.")
                return

            zones = self.mark_liquidity_zones()
            raid, raid_level, raid_price = self.detect_liquidity_raid(df_15m, zones)
            if not raid:
                logger.info("No liquidity raid detected – waiting.")
                return

            direction = 'bullish' if raid_level in ['PDL', 'Asian_Low', 'London_Low'] else 'bearish'
            signal_found, signal_type = self.detect_confirmation_signal(df_15m, df_5m, direction)
            if not signal_found:
                logger.info("No confirmation signal after raid – skipping.")
                return

            if self.daily_trades >= self.max_trades_per_day:
                logger.info("Max trades reached for today – skipping.")
                return

            entry_price = df_15m.iloc[-1]['close']
            stop_price = raid_price
            account_balance = self.get_account_balance()
            qty = self.calculate_position_size(account_balance, entry_price, stop_price)
            if qty <= 0:
                logger.warning("Calculated position size is zero – skipping trade.")
                return

            self.place_order(direction, qty, entry_price, stop_price)
        except Exception as e:
            logger.error(f"Error in check_and_trade: {e}")

    def get_account_balance(self) -> float:
        """Return USDT balance (paper mode returns a dummy value)"""
        try:
            if self.trading_mode.upper() == 'PAPER':
                return 10000.0
            bal = self.client.get_asset_balance(asset='USDT')
            return float(bal.get('free', 0.0)) if bal else 10000.0
        except Exception as e:
            logger.error(f"Error fetching account balance: {e}")
            return 10000.0

    # ---------------------------------------------------------------------
    # Scheduler
    # ---------------------------------------------------------------------
    def is_within_operating_window(self, check_time: datetime = None) -> bool:
        """Operating window disabled for testing – always return True."""
        return True

    def run(self):
        """Start the scheduler loop, active only between 13:00-16:00 UTC"""
        schedule.every(15).minutes.do(self.check_and_trade)
        logger.info("Scheduler started: running check_and_trade every 15 minutes between 13:00-16:00 UTC.")
        # Immediate first run if within window
        if self.is_within_operating_window():
            self.check_and_trade()
        try:
            while True:
                if self.is_within_operating_window():
                    schedule.run_pending()
                else:
                    logger.info("Outside operating window – scheduler idle.")
                time.sleep(30)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
        except Exception as e:
            logger.error(f"Bot error: {e}")

# Entry point
if __name__ == "__main__":
    bot = JadeCapeBot()
    bot.run()

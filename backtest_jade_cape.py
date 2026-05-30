#!/usr/bin/env python3
"""
Backtest script for Jade Cape Bot strategy over the past 1 year.
Generates a CSV (compatible with Excel) with trade results.
"""

import os
import time
import logging
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from binance.client import Client
from binance.exceptions import BinanceAPIException

# Configure minimal logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Helper functions (mirroring JadeCapeBot logic)
# ---------------------------------------------------------------------

def get_client():
    api_key = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_API_SECRET')
    if not api_key or not api_secret:
        raise ValueError('Binance API credentials not set in environment')
    return Client(api_key, api_secret)

def fetch_klines(client, symbol, interval, start_str, end_str=None, limit=1000):
    """Fetch klines using Binance's get_historical_klines helper.
    Returns a DataFrame.
    """
    try:
        klines = client.get_historical_klines(symbol, interval, start_str, end_str, limit=limit)
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
        logger.error(f'Binance API error fetching {interval} data: {e}')
        return pd.DataFrame()
    except Exception as e:
        logger.error(f'Error fetching {interval} data: {e}')
        return pd.DataFrame()

def calculate_daily_bias(daily_df, h4_df):
    if daily_df.empty or h4_df.empty or len(daily_df) < 2 or len(h4_df) < 2:
        return 'neutral'
    daily_bull = daily_df.iloc[-2]['close'] > daily_df.iloc[-2]['open']
    h4_bull = h4_df.iloc[-2]['close'] > h4_df.iloc[-2]['open']
    if daily_bull and h4_bull:
        return 'bullish'
    if not daily_bull and not h4_bull:
        return 'bearish'
    return 'neutral'

def mark_liquidity_zones(daily_df, h1_df):
    zones = {}
    if not daily_df.empty and len(daily_df) >= 2:
        zones['PDH'] = daily_df.iloc[-2]['high']
        zones['PDL'] = daily_df.iloc[-2]['low']
    if not h1_df.empty:
        today = datetime.utcnow().date()
        today_candles = h1_df[h1_df['timestamp'].dt.date == today]
        if today_candles.empty:
            today_candles = h1_df.tail(24)
        asian = today_candles[(today_candles['timestamp'].dt.hour >= 0) & (today_candles['timestamp'].dt.hour < 8)]
        london = today_candles[(today_candles['timestamp'].dt.hour >= 8) & (today_candles['timestamp'].dt.hour < 13)]
        if not asian.empty:
            zones['Asian_High'] = asian['high'].max()
            zones['Asian_Low'] = asian['low'].min()
        if not london.empty:
            zones['London_High'] = london['high'].max()
            zones['London_Low'] = london['low'].min()
    return zones

def detect_liquidity_raid(df_15m, zones):
    if df_15m.empty or not zones:
        return False, None, 0.0
    recent = df_15m.tail(4)
    low_levels = ['PDL', 'Asian_Low', 'London_Low']
    high_levels = ['PDH', 'Asian_High', 'London_High']
    for name, price in zones.items():
        if price == 0:
            continue
        for _, row in recent.iterrows():
            if name in low_levels and row['low'] < price and row['close'] > price:
                return True, name, row['low']
            if name in high_levels and row['high'] > price and row['close'] < price:
                return True, name, row['high']
    return False, None, 0.0

def detect_fvg(df, direction):
    if len(df) < 3:
        return False
    for i in range(len(df)-2):
        c1, c2, c3 = df.iloc[i], df.iloc[i+1], df.iloc[i+2]
        if direction == 'bullish' and c3['low'] > c1['high'] and c2['close'] > c2['open']:
            return True
        if direction == 'bearish' and c1['low'] > c3['high'] and c2['close'] < c2['open']:
            return True
    return False

def detect_mss(df, direction):
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

def detect_confirmation(df_15m, df_5m, direction):
    if detect_fvg(df_15m, direction):
        return True, 'FVG'
    if detect_mss(df_15m, direction):
        return True, 'MSS'
    # placeholders for other signals
    return False, None

def calculate_position_size(balance, entry, stop, risk_percent=1.0):
    risk_amount = balance * (risk_percent/100.0)
    stop_dist = abs(entry - stop)
    if stop_dist == 0:
        return 0.0
    qty = risk_amount / stop_dist
    return max(0.0, float('{:.6f}'.format(qty)))

# ---------------------------------------------------------------------
# Main backtest routine
# ---------------------------------------------------------------------

def main():
    client = get_client()
    symbol = os.getenv('SYMBOL', 'BTCUSDT')
    # Define time range: last 365 days.
    end_dt = datetime.utcnow()
    start_dt = end_dt - timedelta(days=365)
    # Binance expects strings like '1 Jan, 2025'
    start_str = start_dt.strftime('%d %b %Y %H:%M:%S')
    end_str = end_dt.strftime('%d %b %Y %H:%M:%S')

    logger.info('Fetching daily data for bias calculation')
    daily_df = fetch_klines(client, symbol, '1d', start_str, end_str)
    logger.info('Fetching 4h data for bias calculation')
    h4_df = fetch_klines(client, symbol, '4h', start_str, end_str)
    logger.info('Fetching 1h data for liquidity zones')
    h1_df = fetch_klines(client, symbol, '1h', start_str, end_str)
    logger.info('Fetching 15m data for trade detection')
    df_15m = fetch_klines(client, symbol, '15m', start_str, end_str)
    logger.info('Fetching 5m data for signal detection')
    df_5m = fetch_klines(client, symbol, '5m', start_str, end_str)

    # Ensure data is sorted
    daily_df.sort_values('timestamp', inplace=True)
    h4_df.sort_values('timestamp', inplace=True)
    h1_df.sort_values('timestamp', inplace=True)
    df_15m.sort_values('timestamp', inplace=True)
    df_5m.sort_values('timestamp', inplace=True)

    # Simulate day by day – we will step through each day after the first two days.
    results = []
    balance = 10000.0  # paper balance
    daily_trades = 0
    max_trades_per_day = 3
    risk_percent = float(os.getenv('RISK_PERCENT', '1.0'))

    # Group 15m data by day for iteration
    df_15m['date'] = df_15m['timestamp'].dt.date
    df_5m['date'] = df_5m['timestamp'].dt.dt.date if hasattr(df_5m['timestamp'].dt, 'dt') else df_5m['timestamp'].dt.date

    unique_dates = sorted(df_15m['date'].unique())
    for current_date in unique_dates:
        # reset daily trades counter at start of each day
        daily_trades = 0
        # Extract slices up to current day (exclusive) for bias & zones
        daily_slice = daily_df[daily_df['timestamp'].dt.date < current_date]
        h4_slice = h4_df[h4_df['timestamp'].dt.date < current_date]
        bias = calculate_daily_bias(daily_slice, h4_slice)
        if bias == 'neutral':
            continue
        # Zones use daily slice and 1h slice up to current day
        h1_slice = h1_df[h1_df['timestamp'].dt.date < current_date]
        zones = mark_liquidity_zones(daily_slice, h1_slice)
        # 15m slice for the day
        day_15m = df_15m[df_15m['date'] == current_date]
        day_5m = df_5m[df_5m['date'] == current_date]
        if day_15m.empty or day_5m.empty:
            continue
        raid, raid_level, raid_price = detect_liquidity_raid(day_15m, zones)
        if not raid:
            continue
        direction = 'bullish' if raid_level in ['PDL', 'Asian_Low', 'London_Low'] else 'bearish'
        signal, sig_type = detect_confirmation(day_15m, day_5m, direction)
        if not signal:
            continue
        if daily_trades >= max_trades_per_day:
            continue
        entry_price = day_15m.iloc[-1]['close']
        stop_price = raid_price
        qty = calculate_position_size(balance, entry_price, stop_price, risk_percent)
        if qty <= 0:
            continue
        # Simulate exit at next day close (very naive)
        next_day = current_date + timedelta(days=1)
        next_close = df_15m[df_15m['date'] == next_day]
        exit_price = next_close.iloc[-1]['close'] if not next_close.empty else entry_price
        pnl = (exit_price - entry_price) * qty if direction == 'bullish' else (entry_price - exit_price) * qty
        balance += pnl
        daily_trades += 1
        results.append({
            'date': current_date,
            'side': direction.upper(),
            'entry_price': entry_price,
            'stop_price': stop_price,
            'qty': qty,
            'exit_price': exit_price,
            'pnl': pnl,
            'balance': balance,
            'signal_type': sig_type
        })

    # Write results to CSV
    out_path = os.path.join(os.getcwd(), 'backtest_results.csv')
    pd.DataFrame(results).to_csv(out_path, index=False)
    logger.info(f'Backtest completed. Results saved to {out_path}')

if __name__ == '__main__':
    main()

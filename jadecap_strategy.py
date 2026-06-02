#!/usr/bin/env python3
"""
jadecap_strategy.py
-------------------
Pure strategy logic for the JadeCap Liquidity & Volatility Model.

This module is deliberately execution-agnostic: it consumes OHLC DataFrames and
returns decisions. The live bot and the backtester both call into it, so the
exact same rules are tested and traded. No network, no orders here.

Implements (per the playbook):
  - Structural daily bias
  - Session liquidity zones (PDH/PDL, Asian, London) from COMPLETED sessions
  - Liquidity raid (sweep) detection
  - Ordered state machine: RAID -> CONFIRMATION -> ENTRY  (no entry during raid)
  - Confirmations tied to the raid: FVG, MSS, Turtle Soup, Breaker Block
  - Structural stop beyond the sweep extreme
  - Targets: T1/T2 from opposite-session liquidity / nearby FVG
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from enum import Enum
from typing import Optional
import pandas as pd
import numpy as np


# ----------------------------------------------------------------------------- 
# Config
# ----------------------------------------------------------------------------- 
@dataclass
class StrategyConfig:
    # Session windows are stored as UTC (time) objects. Defaults map the playbook's
    # NY 9:30-11:30 EST window to UTC (EST = UTC-5 standard; adjust for DST as needed).
    ny_session_start: time = time(13, 0)    # 13:00 UTC
    ny_session_end:   time = time(16, 0)    # 16:00 UTC
    exit_by:          time = time(17, 0)    # flatten intraday by here

    asian_start: time = time(0, 0)
    asian_end:   time = time(8, 0)
    london_start: time = time(8, 0)
    london_end:   time = time(13, 0)

    raid_lookback_15m: int = 4
    raid_lookback_5m: int = 6
    confirmation_window: int = 6         # bars after a raid to wait for confirmation
    bias_swing_lookback: int = 10        # candles used to read structural bias
    stop_buffer_pct: float = 0.001       # 0.1% beyond the sweep extreme
    risk_percent: float = 0.5            # % of account risked per trade
    rr_t1: float = 1.0                   # T1 at 1R
    rr_t2: float = 2.0                   # T2 at 2R
    t1_partial: float = 0.5              # take 50% off at T1
    max_trades_per_day: int = 3
    # crypto is a non-native adaptation; flag it loudly
    instrument_is_crypto: bool = True


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


@dataclass
class Setup:
    """A fully-formed trade setup ready to execute."""
    direction: Direction
    entry: float
    stop: float
    t1: float
    t2: float
    raid_level_name: str
    confirmation: str
    timestamp: datetime


# ----------------------------------------------------------------------------- 
# Bias
# ----------------------------------------------------------------------------- 
def structural_bias(daily: pd.DataFrame, lookback: int) -> Optional[Direction]:
    """Read bias from swing structure, not single-candle colour.

    Bullish if the recent series is making higher highs AND higher lows.
    Bearish if lower highs AND lower lows. Otherwise None (no clear bias -> skip).
    """
    if daily is None or len(daily) < lookback + 1:
        return None
    seg = daily.iloc[-(lookback + 1):]
    highs = seg["high"].values
    lows = seg["low"].values
    # Compare the most recent half vs the prior half
    mid = len(seg) // 2
    recent_hh = highs[mid:].max() > highs[:mid].max()
    recent_hl = lows[mid:].min() > lows[:mid].min()
    recent_lh = highs[mid:].max() < highs[:mid].max()
    recent_ll = lows[mid:].min() < lows[:mid].min()
    if recent_hh and recent_hl:
        return Direction.BULLISH
    if recent_lh and recent_ll:
        return Direction.BEARISH
    return None


# ----------------------------------------------------------------------------- 
# Liquidity zones (from COMPLETED sessions only)
# ----------------------------------------------------------------------------- 
def _session_slice(df: pd.DataFrame, day, start: time, end: time) -> pd.DataFrame:
    mask = (df["timestamp"].dt.date == day) & \
           (df["timestamp"].dt.time >= start) & (df["timestamp"].dt.time < end)
    return df[mask]


def mark_liquidity_zones(daily: pd.DataFrame, intraday_1h: pd.DataFrame,
                         as_of: datetime, cfg: StrategyConfig) -> dict:
    """Build zones using only data that is complete as of `as_of`.

    PDH/PDL come from the previous completed daily candle.
    Asian/London come from today's completed sessions (both end before the NY
    window opens, so they are complete by trade time).
    """
    zones: dict = {}
    if daily is not None and len(daily) >= 2:
        prev = daily.iloc[-2]   # previous completed day
        zones["PDH"] = float(prev["high"])
        zones["PDL"] = float(prev["low"])

    if intraday_1h is not None and not intraday_1h.empty:
        day = as_of.date()
        asian = _session_slice(intraday_1h, day, cfg.asian_start, cfg.asian_end)
        london = _session_slice(intraday_1h, day, cfg.london_start, cfg.london_end)
        if not asian.empty:
            zones["Asian_High"] = float(asian["high"].max())
            zones["Asian_Low"] = float(asian["low"].min())
        if not london.empty:
            zones["London_High"] = float(london["high"].max())
            zones["London_Low"] = float(london["low"].min())
    return zones


LOW_LEVELS = {"PDL", "Asian_Low", "London_Low"}
HIGH_LEVELS = {"PDH", "Asian_High", "London_High"}


# ----------------------------------------------------------------------------- 
# Raid detection
# ----------------------------------------------------------------------------- 
@dataclass
class Raid:
    level_name: str
    sweep_price: float        # the extreme of the sweep candle
    direction: Direction      # expected trade direction after the raid
    index: int                # position of the raid candle in the df


def detect_raid(df: pd.DataFrame, zones: dict, lookback: int) -> Optional[Raid]:
    """A raid = price pierces a level then closes back across it (failed breakout).

    Below a low-level: low < level AND close > level  -> bullish (reversal up).
    Above a high-level: high > level AND close < level -> bearish (reversal down).
    Returns the most recent qualifying raid in the lookback window.
    """
    if df.empty or not zones:
        return None
    recent = df.tail(lookback)
    best: Optional[Raid] = None
    for pos, (_, candle) in enumerate(recent.iterrows()):
        idx = len(df) - lookback + pos
        for name, price in zones.items():
            if not price:
                continue
            if name in LOW_LEVELS and candle["low"] < price and candle["close"] > price:
                best = Raid(name, float(candle["low"]), Direction.BULLISH, idx)
            elif name in HIGH_LEVELS and candle["high"] > price and candle["close"] < price:
                best = Raid(name, float(candle["high"]), Direction.BEARISH, idx)
    return best


# ----------------------------------------------------------------------------- 
# Confirmations — only valid AFTER the raid candle (idx > raid.index)
# ----------------------------------------------------------------------------- 
def detect_fvg(df: pd.DataFrame, direction: Direction, after_idx: int) -> Optional[float]:
    """Return the FVG entry level if a valid 3-candle gap forms after `after_idx`."""
    for i in range(after_idx + 1, len(df) - 2):
        c1, c2, c3 = df.iloc[i], df.iloc[i + 1], df.iloc[i + 2]
        if direction == Direction.BULLISH and c3["low"] > c1["high"] and c2["close"] > c2["open"]:
            return float(c1["high"])      # entry at the gap edge
        if direction == Direction.BEARISH and c1["low"] > c3["high"] and c2["close"] < c2["open"]:
            return float(c1["low"])
    return None


def detect_mss(df: pd.DataFrame, direction: Direction, after_idx: int) -> Optional[float]:
    """Market Structure Shift after the raid. Returns the latest close as entry."""
    seg = df.iloc[after_idx:]
    if len(seg) < 4:
        return None
    highs, lows, closes = seg["high"].values, seg["low"].values, seg["close"].values
    if direction == Direction.BULLISH:
        # break above a recent swing high
        for i in range(len(seg) - 2, 1, -1):
            if highs[i] > highs[i - 1] and highs[i] > highs[i + 1]:
                if closes[-1] > highs[i]:
                    return float(closes[-1])
                break
    else:
        for i in range(len(seg) - 2, 1, -1):
            if lows[i] < lows[i - 1] and lows[i] < lows[i + 1]:
                if closes[-1] < lows[i]:
                    return float(closes[-1])
                break
    return None


def detect_turtle_soup(df: pd.DataFrame, direction: Direction, raid: Raid) -> Optional[float]:
    """Genuine turtle soup: the raid candle pierced the level and the NEXT candle
    closed back strongly in the trade direction (sharp snap-back)."""
    nxt = raid.index + 1
    if nxt >= len(df):
        return None
    c = df.iloc[nxt]
    if direction == Direction.BULLISH and c["close"] > c["open"] and c["close"] > df.iloc[raid.index]["close"]:
        return float(c["close"])
    if direction == Direction.BEARISH and c["close"] < c["open"] and c["close"] < df.iloc[raid.index]["close"]:
        return float(c["close"])
    return None


def detect_breaker_block(df: pd.DataFrame, direction: Direction, after_idx: int) -> Optional[float]:
    """Breaker: a candle after the raid that breaks and closes beyond the prior
    opposing candle's body, signalling the flip. Conservative implementation."""
    seg = df.iloc[after_idx:]
    if len(seg) < 3:
        return None
    for i in range(1, len(seg) - 1):
        prev, cur = seg.iloc[i - 1], seg.iloc[i]
        if direction == Direction.BULLISH and prev["close"] < prev["open"] and cur["close"] > prev["high"]:
            return float(cur["close"])
        if direction == Direction.BEARISH and prev["close"] > prev["open"] and cur["close"] < prev["low"]:
            return float(cur["close"])
    return None


def find_confirmation(df: pd.DataFrame, raid: Raid):
    """Try each confirmation in order over the bars AFTER the raid up to the
    current (last) bar. Returns (name, entry_price) or (None, None).

    `raid.index` is the absolute index of the raid candle within `df`. The
    detectors scan from raid.index forward, so confirmation can only be found
    on bars that came after the raid — never on the raid bar itself.
    """
    d = raid.direction
    lvl = detect_fvg(df, d, raid.index)
    if lvl is not None:
        return "FVG", lvl
    lvl = detect_mss(df, d, raid.index)
    if lvl is not None:
        return "MSS", lvl
    lvl = detect_turtle_soup(df, d, raid)
    if lvl is not None:
        return "Turtle_Soup", lvl
    lvl = detect_breaker_block(df, d, raid.index)
    if lvl is not None:
        return "Breaker_Block", lvl
    return None, None


# ----------------------------------------------------------------------------- 
# Stateful raid -> confirmation -> entry machine
# ----------------------------------------------------------------------------- 
@dataclass
class StrategyState:
    """Carried across bars by the bot and the backtester.

    Holds a pending raid (if one has fired and we are now waiting for
    confirmation on subsequent bars). This is what makes the sequence ordered:
    a raid is recorded on one bar, and confirmation is looked for on LATER bars.
    """
    pending_raid: Optional[Raid] = None
    pending_zone_price: float = 0.0
    bars_since_raid: int = 0


def _make_setup_from(raid: Raid, entry: float, conf_name: str,
                     as_of: datetime, cfg: StrategyConfig) -> Optional[Setup]:
    """Assemble a Setup with stop/targets, validating entry is on the right side."""
    if raid.direction == Direction.BULLISH:
        stop = raid.sweep_price * (1 - cfg.stop_buffer_pct)
        if entry <= stop:
            return None
        risk = entry - stop
        t1 = entry + cfg.rr_t1 * risk
        t2 = entry + cfg.rr_t2 * risk
    else:
        stop = raid.sweep_price * (1 + cfg.stop_buffer_pct)
        if entry >= stop:
            return None
        risk = stop - entry
        t1 = entry - cfg.rr_t1 * risk
        t2 = entry - cfg.rr_t2 * risk
    if risk <= 0:
        return None
    return Setup(direction=raid.direction, entry=entry, stop=stop, t1=t1, t2=t2,
                 raid_level_name=raid.level_name, confirmation=conf_name, timestamp=as_of)


def process_bar(df_15m: pd.DataFrame, daily: pd.DataFrame, intraday_1h: pd.DataFrame,
                as_of: datetime, cfg: StrategyConfig, state: StrategyState) -> Optional[Setup]:
    """Stateful, ordered pipeline. Call once per bar.

    Step 1: if no pending raid, check for a NEW raid on the latest bar (matching bias).
            Record it and return None — we do NOT enter on the raid bar.
    Step 2: if a raid is pending, look for confirmation on this and subsequent bars.
            If found within the window -> build and return the Setup (entry now).
            If the window expires or price invalidates the raid -> clear it.
    """
    bias = structural_bias(daily, cfg.bias_swing_lookback)
    if bias is None:
        # no clear bias -> abandon any pending raid too
        state.pending_raid = None
        return None

    # --- Step 2: we already have a pending raid; hunt for confirmation ---
    if state.pending_raid is not None:
        state.bars_since_raid += 1
        raid = state.pending_raid

        # invalidate if price has blown back through the swept level the wrong way
        last = df_15m.iloc[-1]
        if raid.direction == Direction.BULLISH and last["close"] < raid.sweep_price:
            state.pending_raid = None
            return None
        if raid.direction == Direction.BEARISH and last["close"] > raid.sweep_price:
            state.pending_raid = None
            return None

        # timeout
        if state.bars_since_raid > cfg.confirmation_window:
            state.pending_raid = None
            return None

        # rebase raid.index into the current df (df grows by one bar each call)
        # the raid candle is `bars_since_raid` bars back from the last bar
        raid_idx = len(df_15m) - 1 - state.bars_since_raid
        if raid_idx < 0:
            state.pending_raid = None
            return None
        rebased = Raid(raid.level_name, raid.sweep_price, raid.direction, raid_idx)

        conf_name, entry = find_confirmation(df_15m, rebased)
        if conf_name is None:
            return None  # keep waiting

        setup = _make_setup_from(rebased, entry, conf_name, as_of, cfg)
        state.pending_raid = None  # consume the raid whether or not setup is valid
        return setup

    # --- Step 1: no pending raid; look for a new one on the latest bar ---
    zones = mark_liquidity_zones(daily, intraday_1h, as_of, cfg)
    if not zones:
        return None
    # only consider a raid on the most recent 1-2 bars so we genuinely wait afterward
    raid = detect_raid(df_15m, zones, lookback=2)
    if raid is None or raid.direction != bias:
        return None
    state.pending_raid = raid
    state.bars_since_raid = 0
    return None  # never enter on the raid bar — wait for next bars


# ----------------------------------------------------------------------------- 
# Stateless setup (kept for compatibility / single-shot checks)
# ----------------------------------------------------------------------------- 
def build_setup(df_15m: pd.DataFrame, daily: pd.DataFrame, intraday_1h: pd.DataFrame,
                as_of: datetime, cfg: StrategyConfig) -> Optional[Setup]:
    """Full pipeline. Returns a Setup only when ALL playbook criteria align."""
    # (1) bias
    bias = structural_bias(daily, cfg.bias_swing_lookback)
    if bias is None:
        return None

    # (2) zones
    zones = mark_liquidity_zones(daily, intraday_1h, as_of, cfg)
    if not zones:
        return None

    # (3) raid — must match bias direction
    raid = detect_raid(df_15m, zones, cfg.raid_lookback_15m)
    if raid is None or raid.direction != bias:
        return None

    # (4) confirmation AFTER the raid
    conf_name, entry = find_confirmation(df_15m, raid)
    if conf_name is None:
        return None

    # (5) stop beyond the sweep extreme, validated against entry side
    if raid.direction == Direction.BULLISH:
        stop = raid.sweep_price * (1 - cfg.stop_buffer_pct)
        if entry <= stop:                      # entry must be above stop
            return None
        risk = entry - stop
        t1 = entry + cfg.rr_t1 * risk
        t2 = entry + cfg.rr_t2 * risk
    else:
        stop = raid.sweep_price * (1 + cfg.stop_buffer_pct)
        if entry >= stop:
            return None
        risk = stop - entry
        t1 = entry - cfg.rr_t1 * risk
        t2 = entry - cfg.rr_t2 * risk

    if risk <= 0:
        return None

    return Setup(
        direction=raid.direction, entry=entry, stop=stop, t1=t1, t2=t2,
        raid_level_name=raid.level_name, confirmation=conf_name, timestamp=as_of,
    )


def position_size(account_balance: float, entry: float, stop: float,
                  cfg: StrategyConfig, lot_step: float = 1e-6, min_qty: float = 0.0) -> float:
    """Risk-based sizing with lot-step rounding (hook for Binance filters)."""
    risk_amount = account_balance * (cfg.risk_percent / 100.0)
    dist = abs(entry - stop)
    if dist <= 0:
        return 0.0
    qty = risk_amount / dist
    # round DOWN to lot step
    if lot_step > 0:
        qty = np.floor(qty / lot_step) * lot_step
    if qty < min_qty:
        return 0.0
    return float(round(qty, 8))

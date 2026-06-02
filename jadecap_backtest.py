#!/usr/bin/env python3
"""
jadecap_backtest.py
-------------------
Event-driven backtester for the JadeCap strategy. Uses the SAME engine the bot
uses (jadecap_strategy.build_setup), so what you test is what you'd trade.

Models:
  - commission (per side, % of notional)
  - slippage (% applied against you on entry and exit)
  - intraday-only: positions flattened at exit_by each day
  - T1 partial + remainder to T2 or stop; breakeven trail after T1

Reports: trades, win rate, profit factor, expectancy (R), avg win/loss,
max drawdown, total return.

Run with --synthetic to use generated data (no network needed), or point
--csv at a klines CSV with columns timestamp,open,high,low,close,volume.
"""

from __future__ import annotations
import argparse
from dataclasses import dataclass
from datetime import timedelta
import numpy as np
import pandas as pd

from jadecap_strategy import StrategyConfig, Direction, build_setup, process_bar, StrategyState


@dataclass
class BTConfig:
    commission_pct: float = 0.04     # 0.04% per side (taker-ish)
    slippage_pct: float = 0.02       # 0.02% against you each fill
    starting_equity: float = 10000.0


def _resample(df_15m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Build higher TF candles from 15m base data."""
    d = df_15m.set_index("timestamp")
    out = d.resample(rule, label="right", closed="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna().reset_index()
    return out


def run_backtest(df_15m: pd.DataFrame, scfg: StrategyConfig, btcfg: BTConfig):
    df_15m = df_15m.sort_values("timestamp").reset_index(drop=True)
    daily_all = _resample(df_15m, "1D")
    h1_all = _resample(df_15m, "1h")

    equity = btcfg.starting_equity
    equity_curve = [equity]
    trades = []
    open_pos = None
    trades_today = 0
    cur_day = None
    state = StrategyState()

    # warm-up so indicators have history
    start_i = 60
    for i in range(start_i, len(df_15m)):
        bar = df_15m.iloc[i]
        now = bar["timestamp"]

        if cur_day != now.date():
            cur_day = now.date()
            trades_today = 0

        # ---- manage an open position on THIS bar ----
        if open_pos:
            s = open_pos["setup"]
            qty = open_pos["qty"]
            hit_exit = None
            exit_price = None

            if s.direction == Direction.BULLISH:
                # stop first (conservative: assume worst order within bar)
                if bar["low"] <= s.stop:
                    hit_exit, exit_price = "stop", s.stop
                elif not open_pos["t1_done"] and bar["high"] >= s.t1:
                    # partial at T1, move stop to breakeven
                    _book_partial(open_pos, s.t1, scfg, btcfg, trades, now)
                    open_pos["t1_done"] = True
                    open_pos["setup"] = _to_breakeven(s)
                elif open_pos["t1_done"] and bar["high"] >= s.t2:
                    hit_exit, exit_price = "t2", s.t2
            else:
                if bar["high"] >= s.stop:
                    hit_exit, exit_price = "stop", s.stop
                elif not open_pos["t1_done"] and bar["low"] <= s.t1:
                    _book_partial(open_pos, s.t1, scfg, btcfg, trades, now)
                    open_pos["t1_done"] = True
                    open_pos["setup"] = _to_breakeven(s)
                elif open_pos["t1_done"] and bar["low"] <= s.t2:
                    hit_exit, exit_price = "t2", s.t2

            # intraday flatten
            if hit_exit is None and now.time() >= scfg.exit_by:
                hit_exit, exit_price = "time", bar["close"]

            if hit_exit:
                pnl = _close_remainder(open_pos, exit_price, hit_exit, scfg, btcfg, trades, now)
                equity += pnl
                equity_curve.append(equity)
                open_pos = None

        # ---- advance the strategy state machine every in-session bar ----
        in_session = scfg.ny_session_start <= now.time() <= scfg.ny_session_end
        if in_session:
            df_15m_slice = df_15m.iloc[: i + 1]
            daily = daily_all[daily_all["timestamp"] <= now]
            h1 = h1_all[h1_all["timestamp"] <= now]
            setup = process_bar(df_15m_slice, daily, h1, now.to_pydatetime(), scfg, state)

            # only ACT on a setup if we are flat and under the daily cap
            if setup is not None and open_pos is None and trades_today < scfg.max_trades_per_day:
                fill = setup.entry * (1 + btcfg.slippage_pct / 100) \
                    if setup.direction == Direction.BULLISH \
                    else setup.entry * (1 - btcfg.slippage_pct / 100)
                risk_amt = equity * (scfg.risk_percent / 100)
                dist = abs(fill - setup.stop)
                qty = risk_amt / dist if dist > 0 else 0
                if qty > 0:
                    open_pos = {"setup": setup, "qty": qty, "t1_done": False,
                                "init_risk": dist, "entry_fill": fill}
                    trades_today += 1

    return _summarize(trades, equity_curve, btcfg)


# ---- helpers for partial / remainder bookkeeping ----
def _to_breakeven(s):
    from dataclasses import replace
    return replace(s, stop=s.entry)


def _book_partial(pos, price, scfg, btcfg, trades, now):
    s = pos["setup"]
    part_qty = pos["qty"] * scfg.t1_partial
    fill = price * (1 - btcfg.slippage_pct / 100) if s.direction == Direction.BULLISH \
        else price * (1 + btcfg.slippage_pct / 100)
    gross = (fill - pos["entry_fill"]) * part_qty if s.direction == Direction.BULLISH \
        else (pos["entry_fill"] - fill) * part_qty
    comm = (pos["entry_fill"] + fill) * part_qty * (btcfg.commission_pct / 100)
    pnl = gross - comm
    pos["qty"] -= part_qty
    pos["partial_pnl"] = pos.get("partial_pnl", 0) + pnl
    trades.append({"time": now, "type": "T1_partial", "pnl": pnl,
                   "R": pnl / (pos["init_risk"] * part_qty) if pos["init_risk"] else 0})


def _close_remainder(pos, price, reason, scfg, btcfg, trades, now):
    s = pos["setup"]
    qty = pos["qty"]
    fill = price * (1 - btcfg.slippage_pct / 100) if s.direction == Direction.BULLISH \
        else price * (1 + btcfg.slippage_pct / 100)
    gross = (fill - pos["entry_fill"]) * qty if s.direction == Direction.BULLISH \
        else (pos["entry_fill"] - fill) * qty
    comm = (pos["entry_fill"] + fill) * qty * (btcfg.commission_pct / 100)
    pnl = gross - comm + pos.get("partial_pnl", 0)
    R = pnl / (pos["init_risk"] * (qty + (pos["qty"] if False else 0))) if pos["init_risk"] else 0
    # R relative to initial full risk
    full_risk = pos["init_risk"] * (qty / (1 - scfg.t1_partial)) if pos.get("t1_done") else pos["init_risk"] * qty
    trades.append({"time": now, "type": reason, "pnl": pnl,
                   "R": pnl / full_risk if full_risk else 0,
                   "direction": s.direction.value, "conf": s.confirmation})
    return pnl


def _summarize(trades, equity_curve, btcfg):
    closed = [t for t in trades if t["type"] not in ("T1_partial",)]
    # aggregate partials into their parent close for win/loss counting:
    pnls = [t["pnl"] for t in trades if t["type"] != "T1_partial"]
    # include partial pnl already folded into remainder via partial_pnl
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    eq = np.array(equity_curve)
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / peak
    summary = {
        "trades": n,
        "win_rate": (len(wins) / n * 100) if n else 0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0,
        "avg_win": (np.mean(wins) if wins else 0),
        "avg_loss": (np.mean(losses) if losses else 0),
        "expectancy_R": (np.mean([t["R"] for t in trades if t["type"] != "T1_partial"]) if n else 0),
        "max_drawdown_pct": (dd.max() * 100 if len(dd) else 0),
        "total_return_pct": ((eq[-1] / eq[0] - 1) * 100 if len(eq) else 0),
        "final_equity": float(eq[-1]) if len(eq) else btcfg.starting_equity,
    }
    return summary, trades


# ---- synthetic data so the harness runs without network ----
def make_synthetic(days=120, seed=7):
    rng = np.random.default_rng(seed)
    periods = days * 96  # 96 fifteen-min bars per day
    start = pd.Timestamp("2025-01-01", tz="UTC")
    idx = pd.date_range(start, periods=periods, freq="15min")
    # random walk with mild intraday seasonality
    ret = rng.normal(0, 0.0015, periods)
    price = 30000 * np.exp(np.cumsum(ret))
    high = price * (1 + np.abs(rng.normal(0, 0.0012, periods)))
    low = price * (1 - np.abs(rng.normal(0, 0.0012, periods)))
    openp = np.concatenate([[price[0]], price[:-1]])
    vol = np.abs(rng.normal(100, 20, periods))
    return pd.DataFrame({"timestamp": idx, "open": openp, "high": high,
                         "low": low, "close": price, "volume": vol})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="klines CSV: timestamp,open,high,low,close,volume")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--commission", type=float, default=0.04)
    ap.add_argument("--slippage", type=float, default=0.02)
    args = ap.parse_args()

    if args.csv:
        df = pd.read_csv(args.csv, parse_dates=["timestamp"])
        if df["timestamp"].dt.tz is None:
            df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
    else:
        df = make_synthetic()

    scfg = StrategyConfig()
    btcfg = BTConfig(commission_pct=args.commission, slippage_pct=args.slippage)
    summary, trades = run_backtest(df, scfg, btcfg)

    print("\n=== JadeCap Backtest Summary ===")
    for k, v in summary.items():
        if isinstance(v, float):
            print(f"{k:>20}: {v:,.2f}")
        else:
            print(f"{k:>20}: {v}")
    print(f"\n(commission {args.commission}%/side, slippage {args.slippage}%/fill)")
    if args.synthetic:
        print("NOTE: synthetic random-walk data — results show the harness works, "
              "NOT that the strategy has an edge. Use real klines via --csv.")

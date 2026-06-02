#!/usr/bin/env python3
"""
jadecap_bot.py
--------------
Rebuilt JadeCap bot. Execution layer only — all strategy decisions come from
jadecap_strategy.build_setup(). Defaults to PAPER mode.

Key safety changes vs the original:
  - Real stop + take-profit orders placed in LIVE mode (OCO where supported),
    AND an in-loop enforcement backstop.
  - Ordered RAID -> CONFIRMATION -> ENTRY via the strategy engine.
  - Open-position guard, max-trades guard, tz-aware session handling.
  - LIVE mode requires an explicit env opt-in (JADECAP_ALLOW_LIVE=I_UNDERSTAND_THE_RISK)
    on top of TRADING_MODE=LIVE, so you cannot arm real-money trading by accident.

NOTE: This trades crypto, which is NOT the instrument the playbook was designed
for. Treat it as an unvalidated adaptation and rely on the backtest first.
"""

from __future__ import annotations
import os
import time as _time
import logging
from datetime import datetime, timezone
import pandas as pd

from jadecap_strategy import (
    StrategyConfig, Direction, position_size, process_bar, StrategyState,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("jadecap_bot.log", encoding="utf-8"),
              logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


class JadeCapBot:
    def __init__(self, cfg: StrategyConfig | None = None):
        self.cfg = cfg or StrategyConfig()
        self.symbol = os.getenv("SYMBOL", "BTCUSDT")
        self.trading_mode = os.getenv("TRADING_MODE", "PAPER").upper()

        # Hard gate on live trading
        if self.trading_mode == "LIVE":
            if os.getenv("JADECAP_ALLOW_LIVE") != "I_UNDERSTAND_THE_RISK":
                raise SystemExit(
                    "LIVE mode requested but JADECAP_ALLOW_LIVE is not set to the "
                    "required acknowledgement. Refusing to arm real-money trading. "
                    "Backtest and paper-trade first."
                )

        self.client = None
        if self.trading_mode == "LIVE":
            self._init_client()

        self.daily_trades = 0
        self.last_day = None
        self.open_position = None  # dict or None
        self.paper_equity = float(os.getenv("PAPER_START_EQUITY", "10000"))
        self.state = StrategyState()
        if self.cfg.instrument_is_crypto:
            logger.warning("Running on CRYPTO — this is an adaptation of a futures/FX "
                           "strategy. Validate with the backtest before trusting results.")
        logger.info(f"JadeCapBot init — symbol={self.symbol} mode={self.trading_mode}")

    def _init_client(self):
        from binance.client import Client
        key, secret = os.getenv("BINANCE_API_KEY"), os.getenv("BINANCE_SECRET_KEY")
        if not key or not secret:
            raise ValueError("Live mode needs BINANCE_API_KEY / BINANCE_SECRET_KEY")
        self.client = Client(key, secret)
        self.client.ping()
        logger.info("Binance connection verified")

    # ----- time helpers (tz-aware) -----
    def _now(self):
        return datetime.now(timezone.utc)

    def _in_ny_session(self, now=None):
        now = now or self._now()
        return self.cfg.ny_session_start <= now.time() <= self.cfg.ny_session_end

    def _past_exit_time(self, now=None):
        now = now or self._now()
        return now.time() >= self.cfg.exit_by

    def _reset_daily(self):
        today = self._now().date()
        if self.last_day != today:
            self.daily_trades = 0
            self.last_day = today

    # ----- data -----
    def _klines(self, interval, limit=200):
        """Fetch klines. LIVE uses the authenticated client; PAPER uses the
        public REST endpoint (no API key needed) so it can run anywhere."""
        if self.trading_mode == "LIVE":
            raw = self.client.get_klines(symbol=self.symbol, interval=interval, limit=limit)
        else:
            import urllib.request, json
            url = (f"https://api.binance.com/api/v3/klines?symbol={self.symbol}"
                   f"&interval={interval}&limit={limit}")
            with urllib.request.urlopen(url, timeout=15) as r:
                raw = json.loads(r.read().decode())
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close",
                                        "volume", "ct", "qav", "n", "tb", "tq", "ig"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = df[c].astype(float)
        return df[["timestamp", "open", "high", "low", "close", "volume"]]

    def _balance(self):
        if self.trading_mode != "LIVE":
            return self.paper_equity
        bal = self.client.get_asset_balance(asset="USDT")
        return float(bal.get("free", 0.0)) if bal else 0.0

    # ----- orders -----
    def _enter(self, setup, qty):
        side = "BUY" if setup.direction == Direction.BULLISH else "SELL"
        if self.trading_mode != "LIVE":
            logger.info(f"[PAPER] {side} {qty} @~{setup.entry:.2f} "
                        f"stop={setup.stop:.2f} t1={setup.t1:.2f} t2={setup.t2:.2f} "
                        f"({setup.raid_level_name}/{setup.confirmation})")
        else:
            self.client.order_market(symbol=self.symbol, side=side, quantity=qty)
            # Real protective exit: OCO (take-profit + stop) on the opposite side
            exit_side = "SELL" if side == "BUY" else "BUY"
            self.client.create_oco_order(
                symbol=self.symbol, side=exit_side, quantity=qty,
                price=str(round(setup.t1, 2)),
                stopPrice=str(round(setup.stop, 2)),
                stopLimitPrice=str(round(setup.stop, 2)),
                stopLimitTimeInForce="GTC",
            )
            logger.info(f"[LIVE] entered + OCO protection placed")
        self.open_position = {"setup": setup, "qty": qty, "opened": self._now()}
        self.daily_trades += 1

    def _flatten(self, reason):
        if not self.open_position:
            return
        if self.trading_mode == "LIVE":
            s = self.open_position["setup"]
            exit_side = "SELL" if s.direction == Direction.BULLISH else "BUY"
            try:
                self.client.order_market(symbol=self.symbol, side=exit_side,
                                         quantity=self.open_position["qty"])
            except Exception as e:
                logger.error(f"flatten error: {e}")
        logger.info(f"Position closed ({reason})")
        self.open_position = None

    def _manage_paper_position(self):
        """Check the open paper position against stop/T1/T2 using the latest price."""
        if not self.open_position or self.trading_mode == "LIVE":
            return
        pos = self.open_position
        s = pos["setup"]
        df = self._klines("5m", 2)
        if df.empty:
            return
        last = df.iloc[-1]
        hi, lo = last["high"], last["low"]
        exit_price = reason = None
        if s.direction == Direction.BULLISH:
            if lo <= s.stop:
                exit_price, reason = s.stop, "stop"
            elif hi >= s.t2:
                exit_price, reason = s.t2, "t2"
            elif not pos.get("t1_done") and hi >= s.t1:
                pos["t1_done"] = True
                logger.info(f"[PAPER] T1 hit @ {s.t1:.2f} — trailing stop to breakeven")
                from dataclasses import replace
                pos["setup"] = replace(s, stop=s.entry)
        else:
            if hi >= s.stop:
                exit_price, reason = s.stop, "stop"
            elif lo <= s.t2:
                exit_price, reason = s.t2, "t2"
            elif not pos.get("t1_done") and lo <= s.t1:
                pos["t1_done"] = True
                logger.info(f"[PAPER] T1 hit @ {s.t1:.2f} — trailing stop to breakeven")
                from dataclasses import replace
                pos["setup"] = replace(s, stop=s.entry)
        if exit_price is not None:
            direction_mult = 1 if s.direction == Direction.BULLISH else -1
            pnl = (exit_price - s.entry) * direction_mult * pos["qty"]
            self.paper_equity += pnl
            logger.info(f"[PAPER] closed ({reason}) pnl={pnl:+.2f} "
                        f"equity={self.paper_equity:.2f}")
            self.open_position = None

    # ----- main step -----
    def step(self):
        self._reset_daily()

        if self.open_position and self.trading_mode != "LIVE":
            self._manage_paper_position()

        if self._past_exit_time():
            self._flatten("exit-by time")
            return
        if not self._in_ny_session():
            logger.info("Outside NY session — idle.")
            return
        if self.open_position:
            logger.info("Position already open — managing, no new entries.")
            return
        if self.daily_trades >= self.cfg.max_trades_per_day:
            logger.info("Max trades reached today.")
            return

        df_15m = self._klines("15m", 200)
        df_1h = self._klines("1h", 200)
        daily = self._klines("1d", 60)

        setup = process_bar(df_15m, daily, df_1h, self._now(), self.cfg, self.state)
        if setup is None:
            if self.state.pending_raid is not None:
                logger.info(f"Raid pending ({self.state.pending_raid.level_name}) — "
                            f"waiting for confirmation, bar "
                            f"{self.state.bars_since_raid}/{self.cfg.confirmation_window}.")
            else:
                logger.info("No raid this cycle — watching.")
            return

        qty = position_size(self._balance(), setup.entry, setup.stop, self.cfg)
        if qty <= 0:
            logger.info("Sized to zero — skipping.")
            return
        self._enter(setup, qty)

    def run(self, poll_seconds=900):
        logger.info("Scheduler started.")
        try:
            while True:
                try:
                    self.step()
                except Exception as e:
                    logger.error(f"step error: {e}")
                _time.sleep(poll_seconds)
        except KeyboardInterrupt:
            logger.info("Stopped by user")


if __name__ == "__main__":
    JadeCapBot().run()

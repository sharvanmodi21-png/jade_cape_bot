# JadeCap Bot — Rebuilt

Three files plus this readme:

- `FIX_LIST.md` — every problem found in the original `jade_cape_bot.py`, by severity.
- `jadecap_strategy.py` — pure strategy engine (no network/orders). The bot and the
  backtester both call this, so what you test is what you trade.
- `jadecap_bot.py` — execution layer. Defaults to PAPER. LIVE requires a deliberate opt-in.
- `jadecap_backtest.py` — event-driven backtester with commission + slippage.

## Quick start

```bash
pip install pandas numpy            # backtest only needs these
python3 jadecap_backtest.py --synthetic          # proves the harness runs
python3 jadecap_backtest.py --csv your_klines.csv --commission 0.05 --slippage 0.05
```

CSV needs columns: `timestamp,open,high,low,close,volume` at 15-minute resolution
(daily and 1h are resampled internally).

## What changed from the original

The original had two always-`True` confirmation stubs (so it entered on every raid),
no real stop or target, entered *during* the raid, and used single candle colours for
bias. The rebuild enforces an ordered RAID → CONFIRMATION → ENTRY sequence, ties all
four confirmations to the raid, places real OCO stop+target orders in live mode with an
in-loop backstop, sizes by risk with lot-step rounding, and reads bias from swing
structure. See `FIX_LIST.md` for the full list.

## Honest caveats — read these

1. **The synthetic backtest result is negative, and that's expected.** Synthetic data is
   a random walk; any strategy nets roughly negative after costs on it. It proves the
   plumbing works, not that the strategy has an edge. You must run real BTC/ETH klines.

2. **This is a futures/FX strategy bolted onto crypto.** The session-liquidity model
   (PDH/PDL, Asian/London sweeps, NY window) was built for markets with a daily close and
   session rhythm. Crypto trades 24/7 and respects these much less. The backtest is the
   only way to find out if it transfers — don't assume it does.

3. **No edge is demonstrated anywhere yet.** Unlike your JLaw system (validated over 695
   signals), there is zero performance evidence for this model on real data. Treat any
   live use as experimental and paper-trade first.

4. **Confirmations are simplified.** Real FVG/MSS/turtle-soup/breaker detection is
   genuinely discretionary in the playbook. The coded versions are reasonable
   approximations, but they will both miss real setups and fire on weak ones. Tune and
   re-test rather than trusting defaults.

5. **Live mode is intentionally gated.** `TRADING_MODE=LIVE` alone won't arm it; you also
   need `JADECAP_ALLOW_LIVE=I_UNDERSTAND_THE_RISK`. This is on purpose.

I'm not a financial advisor — this is a code rebuild and a testing tool, not a
recommendation to trade.

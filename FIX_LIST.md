# JadeCap Bot — Prioritized Fix List

Reviewed against the *JadeCap Intraday Liquidity & Volatility Model* playbook (April 2025).
Ordered by severity. P0 = could cause direct losses; P1 = strategy misimplemented; P2 = robustness/hygiene.

## P0 — Dangerous (fix before any live use)

1. **Stubbed confirmations always return True.**
   `detect_turtle_soup` and `detect_breaker_block` return `True` unconditionally, and the
   confirmation check is short-circuited so *every* raid passes. Criterion ④ is effectively
   disabled — the bot enters on every sweep.
   → Implement real detectors, or at minimum require a *genuine* FVG/MSS tied to the raid.

2. **No real stop-loss order in live mode.**
   `place_order` sends a bare market order; the stop lives only in a Python dict and is never
   monitored or enforced. A crash/restart leaves an unprotected position.
   → Place a real stop (stop-market / OCO) immediately after entry, and also enforce the stop
     in the monitoring loop as a backstop.

3. **No targets / partials — only a clock-based exit.**
   The only exit is `close_all_positions()` at `exit_by`. Playbook defines targets and partials.
   → Add T1/T2 (opposite session liquidity, FVG, HTF level), partial exits, and trail-to-breakeven.

## P1 — Strategy misimplemented

4. **Enters *during* the raid.** Playbook ③: do NOT enter during the raid; wait for confirmation.
   Code checks raid + confirmation on the same snapshot and enters on the current candle.
   → Enforce ordered state machine: RAID → (later, separate) CONFIRMATION → ENTRY.

5. **Entry price disconnected from setup.** Entry = latest 15m close, regardless of where the
   FVG/breaker actually is, and no check that entry is on the correct side of the stop.
   → Entry must be at the confirmation level (FVG edge / MSS candle), and validated vs stop.

6. **Daily bias = two candle colours.** Reduces structural bias to "prev daily green AND prev 4h
   green." → Use swing structure (higher-highs/higher-lows) over a lookback, not single candles.

7. **Wrong instrument class.** Strategy targets NQ/ES futures + FX during the NY session.
   This bot runs crypto 24/7 with arbitrary UTC slices. Session-liquidity behaviour is much
   weaker in crypto. → Either move to the intended instruments, or explicitly acknowledge crypto
   is an unvalidated adaptation and *backtest it* before trusting it.

8. **Session zone hours mismatched.** Asian 0–8 / London 8–13 UTC vs a 12:30–16:00 trade window;
   `.tail(24)` fallback silently mixes days. → Compute zones from completed prior sessions only.

9. **FVG scans entire window.** Returns True if *any* gap exists anywhere in 200 candles, not one
   formed after the sweep near the level. → Restrict FVG search to post-raid candles.

## P2 — Robustness / hygiene

10. `datetime.utcnow()` deprecated → use `datetime.now(timezone.utc)`.
11. String time compare breaks across midnight → compare `time` objects.
12. `is_within_operating_window` hardwired `return True` → make it real or remove.
13. No "is a position already open?" guard before entering a new one.
14. `max_trades_per_day` checked after full analysis; counter only bumps on fill → fine, but make
    the open-position guard explicit.
15. Position size ignores Binance lot-size/step/min-notional → orders rejected live.
16. No persistence of state across restarts (positions, daily counters).

## What the rebuild (b) actually implements

- Ordered RAID → CONFIRMATION → ENTRY state machine (fixes 1, 4, 9).
- Real FVG + MSS detection tied to the raid; turtle-soup and breaker as genuine (not stub) checks.
- Structural daily bias (fixes 6).
- Stop placed as a real order in live mode + enforced in the loop; T1/T2 + partial + breakeven
  trail (fixes 2, 3).
- Entry anchored to the confirmation level and validated vs stop (fixes 5).
- Session zones from completed sessions; tz-aware times (fixes 8, 10, 11).
- Open-position guard, max-trades guard (fixes 13, 14).
- Lot-size rounding hook (fixes 15).
- Crypto is treated as an explicit, clearly-labelled adaptation that MUST be backtested (7).

## What the backtest (c) measures

Win rate, profit factor, expectancy, max drawdown, avg win/loss, trade count — on historical
klines, with configurable commission + slippage, so you can see whether the fixed logic has any
edge *before* risking capital.

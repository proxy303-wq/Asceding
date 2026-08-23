# dhan-auto-trader - Capabilities & Limitations

As of now (this build). Honest list: what it can do, what it cannot do yet,
and what is deliberately disabled for safety.

## What it CAN do right now

**Trading**
- Trade NIFTY and BANKNIFTY index options (CE/PE) autonomously on DHAN.
- Three signal strategies + candlestick patterns:
  1. Trend + IV momentum (EMA/VWAP/RSI/ATR + IV-percentile filter)
  2. Opening-range / prior-day breakout (volume + OI confirmation)
  3. Candlestick reversals (hammer, engulfing, star, doji family, soldiers/crows)
  4. IV-contrarian short strangle (configurable, OFF by default - margin heavy)
- Paper mode with simulated fills on live quotes (default), live mode behind
  explicit confirmation (scripts/live_run.py, TRADING_MODE=live).
- BTST: holds solid late-day signals (14:00-15:00) overnight in MARGIN product;
  manages the exit next morning (exit at open unless >= 1R, force-exit 09:45).
- Theta-aware strike selection: prefers ATM/ITM strikes with least time decay
  (theta % per day), delta band 0.30-0.70, liquidity filter.
- Expiry policy: skips theta-trap expiries (<1 day left), prefers 2-5 days out.

**Risk management (5L budget defaults)**
- Rs 3,000 per-trade risk cap, Rs 5,000 daily loss halt, Rs 6,000 daily profit
  lock-in (5k/day target), max 6 trades/day, 3 concurrent positions,
  40% exposure cap, 4% drawdown halt, 2-loss streak breaker.
- Always-on broker-side SL + target (Super Orders), no entries after 15:00,
  intraday flatten 15:05, no trading outside market hours.
- Exit management: trailing stop (1R arm, 0.5R trail), breakeven after 0.8R,
  reversal-confirmation exits on 5m trend flips (2-min persistence).

**Data & infrastructure**
- WebSocket live feed (tick-rate LTP/OHLC/OI) with reconnect + REST fallback.
- Option chains with Greeks + IV, IV-percentile tracking, daily + intraday history.
- Authentication: 24h token, TOTP+pin auto-refresh, 12-month API key consent flow
  (scripts/dhan_consent.py), RenewToken.
- SQLite logging of signals, trades, equity curve, IV history.
- Telegram notifier: entries/exits/halts/daily summary + /status /positions
  /pause /resume commands.
- MCP server (11 tools) for AI assistants; FastAPI dashboard with equity curve,
  positions, signals, trades, expiry list and ATM+/-3 chain ladder.
- Offline backtest on synthetic regime data (pipeline validation, not edge proof).

## What it CANNOT do yet (honest limitations)

> Note: ML gate, real-data backtester, paper/live toggle and candlestick patterns
> are NEW and live in the build - they are listed here only with their caveats.

- **ML win-probability gate (new)** - gradient-boosted classifier trained on logged
  trades (16 features incl. RSI/EMA gaps/ATR/IV/trend-score/hour/rolling returns);
  blocks signals below the configured P(win) threshold once >= 30 labeled samples
  exist. Research note: the Downloads paper found LSTM best for pure price-series
  prediction on Indian stocks; the tabular gate uses boosting (best for tabular)
  with LSTM-style rolling-window features. A real LSTM forecaster is not yet built.
- **Real-data backtester (new)** - scripts/backtest_real.py replays actual DHAN 1m
  index history through the engine with per-strategy stats and P&L by entry hour.
  Premiums are modeled (Black-Scholes on realized vol), not broker prints.
- **Paper/Live toggle (new)** - dashboard button, MCP set_trading_mode, Telegram
  /paper /live; LIVE uses the real DHAN account balance for sizing; the switch
  applies only when no positions are open.
- **Candlestick patterns (new)** - 19 patterns in a dedicated strategy.
- **Stock BTST screener (new)** - screens liquid NIFTY-50 equities 15:00-15:20
  IST with live prices (trend + RSI + volume-surge + 52w-strength filters),
  deploys 50-60% of capital into the top pick immediately, holds overnight and
  exits next morning (SL 2.5% / target 5% / force-exit 09:50). Equities only -
  index options stay intraday.
- **12-month API key (new)** - auto-loaded from C:/Athena_X/dhan API KKEY.txt;
  the access token itself is still 24h (SEBI rule) but the key/secret make the
  consent flow the only credential management needed (TOTP+pin = fully automatic).
- **No LSTM forecaster yet** - the paper's best model for price series; planned as
  a next step if the tabular gate underperforms.
- **Partial fills / order slicing** - still not implemented.
- **No partial profit-taking** - positions exit fully (SL/target/reversal); no
  50%-at-1R scale-outs (on the roadmap).
- **No portfolio net-delta cap** - per-position risk only.
- **No news/event calendar filter** - events are not blacked out automatically.
- **Real-data fills are modeled** - the backtester's premiums are BS estimates;
  broker-level fills only exist in live mode.
- **No Kelly-style sizing** - position size is fixed-risk (Rs 3,000), not
  expectancy-scaled.
- **No stock options, futures, or equities** - index options only.
- **No overnight risk beyond BTST** - everything else is strictly intraday.
- **Live P&L is broker-reconciled** - dashboard numbers are estimates from
  positions/quotes; final numbers come from Dhan's order book.
- **WebSocket feed is unit-tested but not live-validated** - the binary parser
  matches the documented layouts; first live session should verify field-by-field.
- **Telegram commands are one-way control** - /pause /resume change the control
  file the loop reads; there is no two-way confirmation flow.
- **No mobile app** - dashboard is a web page; no push notifications beyond
  Telegram messages.
- **No strategy A/B / parallel paper instances** - one config runs at a time
  (possible manually by pointing at different YAMLs, not first-class).

## Deliberately disabled / not-yet-enabled
- Contrarian short strangle: OFF by default (margin-intensive; enable in live
  only after paper validation).
- use_delta_sl: OFF (spot->premium stop conversion; tighter but more stops).
- iv_rv_max: OFF (IV-vs-realized filter; enable once real-data backtest
  confirms a threshold).
- Daily profit lock-in: ON at Rs 6,000 (adjust to taste; 0 disables).

## Safety rules that always apply
1. Paper mode until you have reviewed 2-4 weeks of trade log + equity curve.
2. Live mode requires: static IP whitelisted, data plan active, and typing LIVE
   in scripts/live_run.py.
3. The bot never trades outside NSE hours; it never holds options to expiry;
   every trade has a broker-side stop.
4. No guarantee of profit. Capital at risk. The 5k/day figure is a target the
   risk rules aim at, not a promise.

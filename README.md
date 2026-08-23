# dhan-auto-trader - autonomous NIFTY / BANKNIFTY options portfolio manager

A personal portfolio manager that watches NIFTY & BANKNIFTY option chains, runs
price-action + quant-Greek strategies, manages risk on a Rs 5,00,000 budget, and
executes orders through the **DHAN (DhanHQ) API** - in paper mode by default,
with a live mode behind explicit confirmation. Ships with an **MCP server**
(AI-assistant control plane), a **web dashboard**, a **backtester**, and
full trade/equity logging in SQLite.

> WARNING - honest expectations
> - **No software can guarantee profits.** Options trading has high variance;
>   a 5k/day, 15 profitable days/month outcome is a *target*, not a promise.
> - The bot manages risk so one bad day cannot blow up the account
>   (per-trade risk cap, daily loss halt, exposure caps, time exits).
> - **Paper-trade for at least 2-4 weeks** and validate before going live.
> - This is a tool for you to operate, not a money printer. You are responsible
>   for every order placed in live mode.

> **What this project can and cannot do right now:** see [CAPABILITIES.md](CAPABILITIES.md).

## What it does

| Layer | What |
|---|---|
| Market data | **WebSocket live feed** (tick-rate LTP/OHLC/OI) + REST fallback, option chains (OI, IV, Greeks), expiry lists, 1m candle building, IV-percentile tracking |
| Analytics | Black-Scholes Greeks (delta/gamma/theta/vega), Newton-Raphson implied vol, expected-move, ATR/EMA/RSI/ADX/VWAP indicators |
| Strategies | 1) Trend+IV momentum 2) Opening-range/prior-day breakout 3) IV-contrarian short strangle (opt-in) - all with theta-aware ATM/ITM strike selection and expiry-policy fallback |
| Risk (5L budget) | Rs 3,000/trade risk cap, Rs 5,000 daily loss halt, 3 max positions, 40% max exposure, 6 trades/day, drawdown halt, no entries after 15:00, flatten by 15:05 |
| Execution | Super Orders (entry + SL + target), trailing stop, breakeven move, reversal-confirmation exits (ride winners), paper broker with slippage, kill-switch hook |
| Control plane | MCP server (9 tools) for any MCP client; FastAPI dashboard; SQLite logs |
| Alerts | Telegram notifier (entries, exits, daily summary, risk halts) + /status /positions /pause /resume commands |
| Auth | 24h token auto-renewal via TOTP+pin, 12-month API key/secret consent flow (scripts/dhan_consent.py), RenewToken |

## Why MCP is genuinely useful here

1. **Dhan ships an official MCP server** - `https://mcp.dhan.co/mcp` (OAuth; works with
   Claude Desktop/Code, Cursor, Codex, ChatGPT). Connect your Dhan account to an AI
   assistant and trade/manage portfolio via natural language. Dhan also publishes an
   Agent Skills pack (`github.com/dhan-oss/dhanhq-skills`).
2. **This project adds its own local MCP server** (`src/mcp_server.py`) exposing the
   manager to AI: portfolio summary, positions, signals, risk status, trade log,
   equity curve, paper-trade queueing and auto-trading pause/resume - an AI agent can
   safely see and control the system without touching raw APIs.

## Project layout

```
dhan-auto-trader/
  config.yaml               # strategy + risk parameters (everything tunable)
  .env.example              # DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN / TRADING_MODE
  src/
    config.py               # config loader + IST market-hours helpers
    bridge.py               # JSON state/control files: loop <-> MCP <-> dashboard
    analytics/              # indicators.py, greeks.py (BS + IV)
    market/                 # instruments (scrip master), candles, chain service
    broker/                 # base, dhan_live.py, paper.py
    strategies/             # momentum, breakout, contrarian
    engine/                 # risk, signal_engine, execution, loop (AutoTrader)
    db/store.py             # SQLite (signals, trades, equity, IV history)
    mcp_server.py           # MCP tools
    dashboard/              # FastAPI app + single-page dark dashboard
  scripts/                  # paper_run, live_run, backtest, scan_signals
  tests/                    # greeks + indicators unit tests
  data/                     # sqlite db, scrip master, state.json, iv_history
```

## Stock BTST screener (equities)

Between **15:00-15:20 IST** (market still open) the loop screens a liquid equity
universe (trend + RSI + volume-surge + 52-week-strength filters) using live
near-close prices, picks the top candidate and - if enabled - deploys
**50-60% of capital** into it immediately (CNC, equity), holding it overnight.
Next morning it exits: SL 2.5% below entry, target 5% above, force-sell 09:50.
Universe: built-in NIFTY-50 list or `data/stock_universe.csv`.
Index options remain strictly intraday unless flagged BTST.

## Paper <-> Live toggle

The dashboard has a **switch to LIVE / switch to PAPER** button (also MCP
`set_trading_mode`, Telegram `/live` and `/paper`). LIVE places real orders
against your actual DHAN account balance (risk sizing rebases to available
funds); PAPER uses the configured Rs 5L simulated capital. The switch applies
only when no positions are open, and every switch is logged loudly.

## Authentication (no more daily manual token)

Three ways to keep an access token fresh (token resolution order):

1. **TOTP + PIN (fully automatic)** - enable TOTP in Dhan Web (DhanHQ APIs -> Setup TOTP),
   then set `DHAN_PIN` and `DHAN_TOTP_SECRET` (the base32 secret from the QR).
   The bot generates a fresh 24h token itself whenever the current one is near expiry.
2. **API Key & Secret (12-month credentials)** - Dhan Web -> API key. Run
   `python scripts/dhan_consent.py` once per refresh: it opens the consent URL, you log
   in, paste the tokenId, and the token is saved to `data/dhan_token.txt` for auto-load.
3. **Plain token** - `DHAN_ACCESS_TOKEN` as before; `RenewToken` is attempted automatically
   if the token is still active but close to expiry.

## Live market feed (WebSocket)

The trader connects to `wss://api-feed.dhan.co` (Quote mode, RequestCode 17) and streams
tick-rate LTP/OHLC/volume/OI for the two indices plus any open option positions. Reconnects
with backoff and resubscribes automatically; REST polling stays on as a fallback so a feed
outage never stops the loop. Disable with `feed.use_websocket: false`.

## Setup (5-10 minutes)

1. **Install deps**: `pip install -r requirements.txt`
2. **DhanHQ credentials** (web.dhan.co - My Profile - DhanHQ APIs):
   - Copy `.env.example` to `.env`, fill `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN`.
   - **Data APIs need an active Dhan data subscription** (option chain, quotes, history).
   - **Order APIs require your public IP whitelisted** in DhanHQ settings
     (set it before live mode; changes take up to 7 days to propagate).
   - Tokens expire every 24h (SEBI rule) - regenerate or use the API-key/TOTP flow.
3. **Verify account**: run `python scripts/scan_signals.py` during market hours.

## Usage

```bash
# 0) sanity checks
python tests/test_greeks.py && python tests/test_indicators.py

# 1) offline validation on synthetic data (no credentials needed)
python scripts/backtest.py --days 60

# 2) paper trading (DEFAULT; simulated fills on live quotes)
python scripts/paper_run.py

# 3) one-shot signal scan (see what strategies would do right now)
python scripts/scan_signals.py

# 4) dashboard - two options:
#    a) FastAPI page  ->  python -m uvicorn src.dashboard.app:app --port 8177   # http://127.0.0.1:8177
#    b) Streamlit app (easier to host/deploy) ->  streamlit run src/dashboard/streamlit_app.py --server.port 8501

# 5) Telegram alerts fire automatically once TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set
#    (auto-loaded from C:/Athena_X/.env when present) and telegram.enabled=true in config.yaml

# 6) MCP server (stdio) - connect from Claude Desktop / Cursor / Codex:
#       claude mcp add dhan-trader -- python src/mcp_server.py
python -m src.mcp_server

# 7) LIVE mode - real orders on Dhan (confirmation prompt; IP whitelist required)
python scripts/live_run.py
```

### MCP tools
`get_portfolio_summary · get_open_positions · get_risk_status · get_recent_signals ·
get_trade_log · get_equity_curve · get_strategy_config · paper_trade · set_auto_trading`

Example prompts: *What is my equity and day P&L?*, *Show risk limits*,
*Queue a paper BUY NIFTY CE 24500*, *Pause auto-trading*.

## Strategies (price action + quant Greeks)

1. **Trend + IV momentum** (`momentum`): EMA(9/21) alignment on 1m & 5m + price vs VWAP +
   RSI 52-80 (bull) + price touching the EMA buy zone + ATR expansion + **IV percentile
   <= 70** (never overpay). Buys the ATM+1 call / ATM-1 put. SL = 30% of premium,
   target = 1.8R, time-exit 15:05. This is the trend-day earner.
2. **Opening-range breakout** (`breakout`): breaks of the first-15-minute range before
   11:30, confirmed by volume surge >= 1.5x and OI build-up on the breakout side, same IV filter.
3. **IV-contrarian short strangle** (`contrarian`, **off by default**): sells a 1.5-sigma
   OTM strangle when IV percentile >= 82 and ADX <= 22 (range-bound), per-leg 25% premium
   SL. Margin-intensive - enable only in live mode with real margin awareness. (Shape
   inspired by the included `straddlestrategy.algtst`/`trendingstrategy.algtst` configs).

Greeks are computed with Black-Scholes (NIFTY/BANKNIFTY index options are European) and
used for position selection, IV filters, and expected-move estimation; IV percentile is
tracked daily per underlying to time entries (buy cheap IV, avoid rich IV).

## Options quant: do IV + Greeks actually help?

**Yes - but as filters and risk math, not as signals by themselves.** Here is how
this system uses them:

- **IV percentile (timing)** - the single most useful option-specific input. It
  tracks daily ATM straddle IV per underlying and only lets momentum/breakout buy
  when IV is in the cheap half of its recent range (percentile <= 70). Buying
  options when IV is rich is how traders lose money even on correct direction.
- **Expected move (targets/sizing)** - 1-sigma expected move = spot x ATM IV x
  sqrt(DTE/365). Used to sanity-check that a 1.8R target is actually reachable and
  that stops sit inside realistic noise.
- **Delta (exposure + optional stops)** - every entry records delta/gamma/theta/vega/IV
  (shown in Telegram + dashboard). Optional `use_delta_sl` converts the spot-based
  stop into a premium stop via delta, so the option-level SL matches the spot stop
  you intended.
- **Theta (time discipline)** - entries are blocked when fewer than `min_dte` days
  remain (theta trap: the option bleeds value even if direction is right) and after
  `no_new_entries_after`. Time-exit at 15:05 keeps overnight theta decay off your book.
- **Gamma (stop awareness)** - stops are tightest near expiry; the min-DTE guard
  plus default 30% premium SL keeps a gamma spike from running a stop beyond budget.

Rule of thumb the engine encodes: trade direction with price action, time the entry
with IV, size and stop with delta/theta, and never fight the expiry clock.

## Risk rules baked in (Rs 5,00,000 budget)

- **Per trade**: risk cap Rs 3,000 (~0.6% of capital) -> qty = risk / (premium x SL%),
  rounded to lot size, premium-value capped.
- **Per day**: stop new entries after -Rs 5,000 (daily loss limit), optional lock-in at
  +Rs 7,500, max 6 trades, max 3 concurrent positions, 40% exposure cap, drawdown halt at 4%.
- **Always**: broker-side SL + target (Super Order), no entries after 15:00, flatten at
  15:05, no trading outside market hours.
- **Exit management** (config `risk.exit_mode`): after 1R profit the stop trails within
  0.5R of the peak; after 0.8R it moves to breakeven; winners are only sold on a
  2-minute-confirmed 5m trend reversal (SL handles losers). `target` mode keeps the old
  fixed 1.8R target; `trail_and_reversal` (default) uses a 2.5R backstop.
- **Anti-tilt**: after 2 consecutive losing trades, no new entries for the day
  (`max_consecutive_losses`).
- **Theta-aware selection**: entries use ATM/ITM strikes with the least theta bleed
  (`strike_select.mode: theta_optimized`), and the expiry is picked to skip
  theta-trap weeks (`feed.expiry_policy: dte_window`, prefer 2-5 days out).

## Telegram alerts & commands

Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` (the loader also reads them from
`C:/Athena_X/.env` automatically) and keep `telegram.enabled: true` in config.yaml.
On startup the bot sends a "connected" message; from then on you get:

- trade entries (option, qty, entry/SL/target + delta/gamma/theta/IV)
- trade exits with P&L and reason (SL / TARGET / TIME)
- daily summary after 15:35 (equity, day P&L, trades, win rate, month P&L)
- risk-halt alerts the moment a daily limit trips

Commands you can send the bot: `/status`, `/positions`, `/pause` (auto-trading
off), `/resume`, `/help`. Telegram is non-critical: if it fails, trading continues.

## Backtest

`scripts/backtest.py` replays the real strategy/risk/paper-execution stack on synthetic
regime data (trend/range days, vol clustering, pullbacks) so you can validate plumbing
without credentials. A 60-day sample run: 18 trades, 44% win rate, SL/TARGET/TIME exits,
max DD 2.4%. **Synthetic data != real results** - use it for wiring checks, then use
paper mode on real data.

## What was merged from PrOxy + research references

- **Lock-profit exit** (PrOxy's sweep insight): lock gains at 0.8% premium,
  trail from peak - the mechanism that flips 30% WR scalping into winners.
- **Conviction/confidence gate**: 0-100 signal score, optional min_conviction.
- **Barrier-based ML labeling + meta-labeling** (MLFinLab / López de Prado):
  first-touch target/stop defines labels; the ML gate filters primary signals.
- **Real-data backtest on PrOxy's CSVs** (scripts/backtest_csv.py) with
  Sharpe/Sortino/max-DD + per-hour analytics; PortfolioLab-style metrics.
- Head-to-head: default exits vs PrOxy-style scalping+lock vs conviction gate on
  identical 2-year NIFTY/BANKNIFTY data (see scripts/backtest_csv.py).

## Git + Streamlit Cloud deploy

The project is on GitHub (https://github.com/proxy303-wq/Asceding). To host the
dashboard on Streamlit Cloud:

1. **The trading loop must run on YOUR machine/VPS** - Streamlit Cloud is UI-only
   and cannot run a long-lived trading process (or hold your DHAN credentials).
2. **Sync live state to the repo** so the hosted app can read it (no credentials
   in the sync files - market state/equity/positions only):
   ```bash
   python scripts/sync_remote.py     # push data/sync -> sync/ -> origin/main
   # run this every 5-10 min (task scheduler/cron) during market hours
   ```
3. **Deploy**: push the repo, connect it on share.streamlit.io, entry point
   `src/dashboard/streamlit_app.py`. The app falls back to
   `remote_state_url` (jsDelivr CDN of sync/state.json, ~10 min cache) when no
   local state.json exists - so the hosted app shows the loop's live state.
4. Auto-refresh is every 5s inside the app; CDN cache adds ~10 min lag.

Local quick view (no deploy needed): `streamlit run src/dashboard/streamlit_app.py`

## Going live responsibly

1. Paper trade >= 2-4 weeks; review the SQLite trades + equity curve on the dashboard.
2. Whitelist your IP, confirm your Dhan data plan covers F&O data.
3. Start with TRADING_MODE=live, small risk caps, and monitor the first sessions closely.
4. Track results monthly: the 5k/month target on 5L is ~1%/month - achievable in good
   months with tight risk, **not guaranteed** every month.

## Notes / limitations

- Live P&L reconciliation is broker-side; the dashboard uses position/unrealized data.
- The contrarian strategy is deliberately disabled by default.
- Rate limits: quotes 1 req/s (batch LTP), option chain 1 per 3s per underlying, orders 10/s.
- Not financial advice. Capital at risk.

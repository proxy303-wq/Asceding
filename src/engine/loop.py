"""AutoTrader: the market-hours loop that ties data, signals, risk and execution together.

Runs every few seconds during NSE hours (09:15-15:30 IST):
  refresh index quotes -> build candles -> refresh option chains -> compute indicators
  -> run strategies -> risk-filter -> execute -> monitor exits -> record equity.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Optional

from .. import bridge
from ..broker.base import Quote
from ..broker.dhan_live import DhanClient, DhanLiveBroker
from ..broker.paper import PaperBroker
from ..config import ROOT, ist_now, load_config, market_is_open
from ..db.store import Store
from ..market.candles import CandleSeries, resample
from ..market.chain import ChainService
from ..market.instruments import InstrumentMaster, Underlying, atm_strike, dte, expiry_by_rank
from ..strategies.breakout import BreakoutStrategy
from ..strategies.candlestick import CandlestickStrategy
from ..strategies.contrarian import ContrarianStrategy
from ..telegram import TelegramCommander, TelegramNotifier
from ..strategies.base import Signal
from ..strategies.momentum import TrendMomentumStrategy
from .execution import ExecutionManager
from .risk import RiskManager
from .signal_engine import MarketState, SignalEngine

log = logging.getLogger("trader")


class AutoTrader:
    def __init__(self, config_path: Optional[str] = None):
        self.cfg = load_config(config_path)
        self.mode = self.cfg.get("mode", "paper")
        self.store = Store(self.cfg.get("db_path", str(ROOT / "data" / "trader.db")))
        self._setup_logging()
        self.master = InstrumentMaster()
        self.master.load()

        client_id = self.cfg.get("dhan_client_id", "")
        token = self.cfg.get("dhan_access_token", "")
        if not client_id or not token:
            log.error("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set. Copy .env.example to .env and fill in.")
            raise SystemExit("credentials missing")

        self.data = DhanClient(client_id, token)
        self.instruments = self._build_instruments()
        intervals = {u.name: u.strike_interval for u in self.instruments}
        self.chain_svc = ChainService(self.data, intervals)

        self.paper_broker = PaperBroker(self.cfg["capital"]["initial"], quote_provider=self._quote)
        self.live_broker = DhanLiveBroker(self.data)
        self.broker = self.paper_broker if self.mode == "paper" else self.live_broker
        self.risk = RiskManager(self.cfg, self.store,
                                lot_sizes={u.name: u.lot_size for u in self.instruments})

        strat_cfg = self.cfg.get("strategies", {})
        self.strategies = [
            TrendMomentumStrategy(self.cfg, strat_cfg.get("momentum", {})),
            BreakoutStrategy(self.cfg, strat_cfg.get("breakout", {})),
            CandlestickStrategy(self.cfg, strat_cfg.get("candlestick", {})),
            ContrarianStrategy(self.cfg, strat_cfg.get("contrarian", {})),
        ]
        self.signal_engine = SignalEngine(self.strategies, self.risk, self.cfg["instruments"], self.cfg)
        self.exec_mgr = ExecutionManager(self.broker, self.risk, self.store, self.mode)
        self.exec_mgr.set_quote_provider(self._quote)
        self.telegram = self._setup_telegram()
        self.exec_mgr.on_trade_closed = lambda d: self.telegram.trade_exited(d)
        self._halt_notified = False
        self._daily_sent: set = set()
        self._last_heartbeat = 0.0
        self._last_stale_alert = 0.0
        self._blackout_notified = ""
        self.ws = self._setup_websocket()
        self._ws_sids: set = set()

        # runtime state
        self.quotes: dict[str, Quote] = {}
        self.series: dict[str, CandleSeries] = {}
        self.daily: dict[str, CandleSeries] = {}
        self.last_chain_ts: dict[str, float] = {}
        self.last_vol: dict[str, float] = {}
        self._or_bounds: dict[str, tuple] = {}
        self._last_cycle = 0.0

    # ------------------------------------------------------------------
    def _setup_logging(self):
        level = getattr(logging, self.cfg.get("log_level", "INFO").upper(), logging.INFO)
        root = logging.getLogger()
        root.setLevel(level)
        if not root.handlers:
            h = logging.StreamHandler(sys.stdout)
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            root.addHandler(h)
        (ROOT / "logs").mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(str(ROOT / "logs" / "trader.log"), encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)

    def _build_instruments(self) -> list[Underlying]:
        out = []
        for u in self.cfg["instruments"]:
            name = u["underlying"]
            sid = str(u.get("security_id", ""))
            if not sid or sid == "0":
                resolved = self.master.index_security_id(name)
                if resolved:
                    sid = resolved
                    log.info("%s index security_id resolved from master: %s", name, sid)
            lot = int(u.get("lot_size", 0) or 0)
            master_lot = self.master.lot_size(name)
            if master_lot:
                lot = master_lot
            index_id = self.master.index_security_id(name) or ""
            if not index_id:
                index_id = {"NIFTY": "13", "BANKNIFTY": "25"}.get(name, "")
            out.append(Underlying(
                name=name, security_id=sid, index_id=index_id,
                segment=u.get("segment", "NSE_FNO"),
                expiry=u.get("expiry", "nearest"), lot_size=lot,
                strike_interval=int(u.get("strike_interval", 50)),
            ))
        return out

    # ------------------------------------------------------------------
    def _quote(self, security_id: str) -> Optional[Quote]:
        return self.quotes.get(security_id)

    def _index_quote_key(self, u: Underlying) -> str:
        return u.index_id or u.security_id

    def _refresh_quotes(self):
        idx_ids = [u.index_id or u.security_id for u in self.instruments]
        fno_ids = []
        eq_ids = []
        for tr in self.exec_mgr.open.values():
            if tr.segment == "EQ":
                eq_ids.append(tr.security_id)
            else:
                fno_ids.append(tr.security_id)
        if idx_ids:
            try:
                idx_ltps = self.data.ltp(list(set(idx_ids)), segment="IDX_I")
            except Exception as e:
                log.warning("index ltp batch failed: %s", e)
                idx_ltps = {}
            for sid, ltp in idx_ltps.items():
                self._set_quote(sid, ltp)
        if fno_ids:
            try:
                ltps = self.data.ltp(list(set(fno_ids)), segment="NSE_FNO")
            except Exception as e:
                log.warning("fno ltp batch failed: %s", e)
                ltps = {}
            for sid, ltp in ltps.items():
                self._set_quote(sid, ltp)
        if eq_ids:
            try:
                eq_ltps = self.data.ltp(list(set(eq_ids)), segment="NSE_EQ")
            except Exception as e:
                log.warning("eq ltp batch failed: %s", e)
                eq_ltps = {}
            for sid, ltp in eq_ltps.items():
                self._set_quote(sid, ltp)
        for sid, ltp in ltps.items():
            prev = self.quotes.get(sid)
            if prev is None:
                self.quotes[sid] = Quote(security_id=sid, symbol=sid, ltp=ltp, ts=time.time())
            else:
                prev.ltp = ltp
                prev.ts = time.time()
        # update option quotes for open trades with chain-derived prices when available
        for tr in self.exec_mgr.open.values():
            row = self._chain_row(tr)
            if row and row.ltp > 0:
                q = self.quotes.get(tr.security_id)
                if q is None:
                    self.quotes[tr.security_id] = Quote(security_id=tr.security_id, symbol=tr.symbol,
                                                        ltp=row.ltp, ts=time.time())
                else:
                    q.ltp = row.ltp
                    q.bid, q.ask = row.bid, row.ask
                    q.ts = time.time()

    def _chain_row(self, tr):
        chain = self.chain_svc._cache.get(f"{tr.underlying}:{tr.expiry}")
        if chain:
            return chain.get(tr.strike, tr.option_type)
        return None

    def _refresh_chains(self):
        from ..market.instruments import select_expiry
        now = time.time()
        feed = self.cfg.get("feed", {})
        for u in self.instruments:
            interval = float(feed.get("chain_interval_sec", 30))
            key = u.name
            if now - self.last_chain_ts.get(key, 0) < interval:
                continue
            expiries = self.chain_svc.refresh_expiries(u.name, u.security_id, u.segment)
            if not expiries:
                log.warning("no expiries for %s", u.name)
                continue
            policy = str(feed.get("expiry_policy", "dte_window"))
            expiry = select_expiry(
                expiries, policy,
                min_dte=float(feed.get("expiry_min_dte", 1.0)),
                prefer_min=float(feed.get("prefer_dte_min", 2)),
                prefer_max=float(feed.get("prefer_dte_max", 5)),
            )
            if not expiry:
                log.warning("no tradable expiry for %s", u.name)
                continue
            self.chain_svc.selected_expiry[u.name] = expiry
            if expiry != u.expiry:
                pass  # policy already applied; keep config as fallback name
            snap = self.chain_svc.fetch(u.name, u.security_id, u.segment, expiry,
                                        spot_override=self._index_spot(u))
            if snap is not None:
                self.last_chain_ts[key] = now

    def _index_spot(self, u: Underlying) -> float:
        q = self.quotes.get(u.index_id or u.security_id)
        return q.ltp if q else 0.0

    # ------------------------------------------------------------------
    def _seed_series(self, u: Underlying):
        feed = self.cfg.get("feed", {})
        days = int(feed.get("history_intraday_days", 5))
        today = ist_now().date()
        from_date = (today - timedelta(days=days)).isoformat()
        to_date = (today + timedelta(days=1)).isoformat()
        rows = []
        sid = u.index_id or u.security_id
        for seg, itype in (("IDX_I", "INDEX"),):
            try:
                rows = self.data.intraday_minute(sid, seg, itype, from_date, to_date, 1)
                if rows:
                    break
            except Exception as e:
                log.warning("intraday history %s via %s failed: %s", u.name, seg, e)
        s = CandleSeries(interval_sec=60)
        s.seed(rows)
        self.series[u.name] = s
        log.info("%s seeded %d 1m candles", u.name, len(s.candles))

        drow = []
        try:
            drow = self.data.historical_daily(sid, "IDX_I", "INDEX",
                                              (today - timedelta(days=120)).isoformat(),
                                              (today + timedelta(days=1)).isoformat())
        except Exception as e:
            log.warning("daily history %s failed: %s", u.name, e)
        d = CandleSeries(interval_sec=86400)
        d.seed(drow)
        self.daily[u.name] = d
        log.info("%s seeded %d daily candles", u.name, len(d.candles))

    def _update_candles(self, u: Underlying):
        q = self.quotes.get(u.security_id)
        if q is None or q.ltp <= 0:
            return
        ts = int(time.time())
        prev_vol = self.last_vol.get(u.security_id, q.volume)
        vol_delta = max(0.0, q.volume - prev_vol)
        self.last_vol[u.security_id] = q.volume
        self.series[u.name].append_tick(ts, q.ltp, vol_delta)

    def _or_bounds_for(self, u: Underlying) -> tuple:
        feed = self.cfg.get("feed", {})
        or_min = int(self.cfg.get("strategies", {}).get("breakout", {}).get("or_minutes", 15))
        s = self.series.get(u.name)
        if s is None or not s.candles:
            return (0.0, 0.0)
        now = ist_now()
        day_start = datetime.combine(now.date(), datetime.min.time().replace(hour=9, minute=15), tzinfo=now.tzinfo)
        # candles whose bucket is within [09:15, 09:15+or_min]
        limit_epoch = int((day_start + timedelta(minutes=or_min)).timestamp())
        highs, lows = [], []
        for c in s.candles:
            if day_start.timestamp() <= c.ts < limit_epoch:
                highs.append(c.high)
                lows.append(c.low)
        if not highs:
            return (0.0, 0.0)
        return (max(highs), min(lows))

    def _indicators(self, u: Underlying) -> dict:
        import numpy as np
        from ..analytics import indicators as ta
        out = {}
        s1 = self.series.get(u.name)
        if s1 is None:
            return out
        closes, highs, lows = s1.closes(), s1.highs(), s1.lows()
        vols = [c.volume for c in s1.candles]
        n = len(closes)
        if n < 30:
            return out
        out["ema_fast_1m"] = float(ta.ema(closes, 9)[-1])
        out["ema_slow_1m"] = float(ta.ema(closes, 21)[-1])
        out["rsi_1m"] = float(ta.rsi(closes, 14)[-1])
        a = ta.atr(highs, lows, closes, 14)
        out["atr_1m"] = float(a[-1])
        out["atr_ma_1m"] = float(np.nanmean(a[-20:]))
        out["vwap_1m"] = float(ta.vwap(closes, vols)[-1])
        out["vol_avg_1m"] = float(np.mean(vols[-20:])) if vols else 0.0
        s5 = CandleSeries(interval_sec=300)
        s5.seed([{"timestamp": c.ts, "open": c.open, "high": c.high, "low": c.low,
                  "close": c.close, "volume": c.volume} for c in resample(s1.candles, 5)])
        self.series.setdefault(u.name + ":5m", s5)
        c5, h5, l5 = s5.closes(), s5.highs(), s5.lows()
        if len(c5) >= 25:
            out["ema_fast_5m"] = float(ta.ema(c5, 9)[-1])
            out["ema_slow_5m"] = float(ta.ema(c5, 21)[-1])
            out["atr_5m"] = float(ta.atr(h5, l5, c5, 14)[-1])
            out["adx_5m"] = float(ta.adx(h5, l5, c5, 14)[-1])
        orh, orl = self._or_bounds_for(u)
        out["or_high"], out["or_low"] = orh, orl
        d = self.daily.get(u.name)
        if d and len(d.candles) >= 2:
            out["pd_high"] = d.candles[-2].high
            out["pd_low"] = d.candles[-2].low
        return out

    # ------------------------------------------------------------------
    # WebSocket live feed (DHAN market feed, Quote mode)
    # ------------------------------------------------------------------
    def _setup_websocket(self):
        if not self.cfg.get("feed", {}).get("use_websocket", True):
            log.info("websocket feed disabled in config")
            return None
        try:
            from ..market.feed import DhanFeedWebSocket
            ws = DhanFeedWebSocket(self.cfg.get("dhan_access_token", ""),
                                   self.cfg.get("dhan_client_id", ""),
                                   on_quote=self._ws_quote)
            ws.start()
            return ws
        except Exception as e:
            log.warning("websocket feed unavailable: %s", e)
            return None

    def _set_quote(self, sid: str, ltp: float):
        prev = self.quotes.get(sid)
        if prev is None:
            self.quotes[sid] = Quote(security_id=sid, symbol=sid, ltp=ltp, ts=time.time())
        else:
            prev.ltp = ltp
            prev.ts = time.time()

    def _ws_quote(self, sid: str, fields: dict):
        q = self.quotes.get(sid)
        if q is None:
            q = Quote(security_id=sid, symbol=sid, ltp=0.0)
            self.quotes[sid] = q
        if fields.get("ltp"):
            q.ltp = float(fields["ltp"])
        if fields.get("volume"):
            q.volume = float(fields["volume"])
        if fields.get("oi"):
            q.oi = float(fields["oi"])
        if fields.get("high"):
            q.high = float(fields["high"])
        if fields.get("low"):
            q.low = float(fields["low"])
        if fields.get("open"):
            q.open = float(fields["open"])
        if fields.get("prev_close"):
            q.prev_close = float(fields["prev_close"])
        q.ts = time.time()

    def _update_ws_subscriptions(self):
        if self.ws is None:
            return
        wanted = {("IDX_I", u.index_id or u.security_id) for u in self.instruments}
        wanted |= {("NSE_FNO", tr.security_id) for tr in self.exec_mgr.open.values()}
        if self.cfg.get("stock_btst", {}).get("enabled", True):
            try:
                from ..screener.stock_screener import StockScreener
                uni = StockScreener(self.data, self.master, self.cfg).universe()
                wanted |= {("NSE_EQ", sid) for sid in uni.values()}
            except Exception:
                pass
        if wanted != self._ws_sids:
            self._ws_sids = wanted
            self.ws.subscribe(sorted(wanted))

    # ------------------------------------------------------------------
    # Stock BTST screener (equities; 50-60% capital allocation)
    # ------------------------------------------------------------------
    def _screen_stock_btst(self):
        """Screen 15:00-15:20 IST with live prices; buy the pick immediately,
        hold overnight, exit next morning (classic BTST)."""
        sc = self.cfg.get("stock_btst", {})
        if not sc.get("enabled", True):
            return
        now = ist_now()
        hm = now.hour * 100 + now.minute
        if not (int(sc.get("screen_hm_start", 1500)) <= hm <= int(sc.get("screen_hm_end", 1520))):
            return
        day = now.date().isoformat()
        if self.store.get_state("stock_btst_screen_day", "") == day:
            return
        from ..screener.stock_screener import StockScreener
        screener = StockScreener(self.data, self.master, self.cfg)
        universe = screener.universe()
        # live bars from the WS/REST caches
        live = {}
        for sid in universe.values():
            q = self.quotes.get(sid)
            if q and q.ltp > 0:
                live[sid] = {"ltp": q.ltp, "open": q.open, "high": q.high,
                             "low": q.low, "volume": q.volume}
        missing = [sid for sid in universe.values() if sid not in live]
        if missing:
            try:
                ltps = self.data.ltp(missing, segment="NSE_EQ")
                for sid, ltp in ltps.items():
                    self._set_quote(sid, ltp)
                    live[sid] = {"ltp": ltp}
            except Exception as e:
                log.warning("stock ltp refresh failed: %s", e)
        picks = []
        try:
            picks = screener.screen(live=live)
        except Exception as e:
            log.warning("stock btst screen failed: %s", e)
            return
        self.store.set_state("stock_btst_screen_day", day)
        if not picks:
            log.info("stock BTST: no picks at %04d", hm)
            return
        pick = picks[0]
        q = self.quotes.get(pick.security_id)
        entry = q.ltp if q and q.ltp > 0 else pick.close
        if entry <= 0:
            return
        capital = (self.risk.initial_capital if self.mode == "live" else
                   float(self.cfg.get("capital", {}).get("initial", 500000)))
        alloc = max(50.0, min(60.0, float(sc.get("allocation_pct", 55))))
        qty = int(capital * alloc / 100.0 / entry)
        if qty <= 0:
            return
        sl = entry * (1 - float(sc.get("sl_pct", 2.5)) / 100.0)
        target = entry * (1 + float(sc.get("target_pct", 5.0)) / 100.0)
        ok, msg = self.exec_mgr.track_stock(pick.security_id, pick.symbol, qty, entry, sl, target)
        if ok:
            log.warning("STOCK BTST ENTERED %s qty=%d @ %.2f (SL %.2f TP %.2f) - hold overnight",
                        pick.symbol, qty, entry, sl, target)
            self.telegram.status(
                f"📌 STOCK BTST ENTERED {pick.symbol} qty {qty} @ ₹{entry:.2f} "
                f"(score {pick.score}, RSI {pick.rsi:.0f}) - sell next morning (SL ₹{sl:.2f} / TP ₹{target:.2f})")
        else:
            log.warning("stock btst entry failed: %s", msg)

    def _manage_stock_time_exit(self):
        """Next morning: force-sell stock BTST legs by exit_hm."""
        sc = self.cfg.get("stock_btst", {})
        exit_hm = int(sc.get("exit_hm", 950))
        now = ist_now()
        if now.hour * 100 + now.minute < exit_hm:
            return
        for sid, tr in list(self.exec_mgr.open.items()):
            if tr.segment == "EQ":
                log.info("stock BTST time exit %s at %04d", tr.symbol, exit_hm)
                self.exec_mgr.exit_security(sid, "STOCK_TIME_EXIT")

    # ------------------------------------------------------------------
    # Paper <-> Live mode switching (live uses the real DHAN account balance)
    # ------------------------------------------------------------------
    def _live_balance(self) -> float:
        try:
            funds = self.live_broker.get_funds()
            bal = funds.available_balance
            return bal if bal and bal > 0 else float(self.cfg.get("capital", {}).get("initial", 500000))
        except Exception as e:
            log.warning("fund fetch failed: %s", e)
            return float(self.cfg.get("capital", {}).get("initial", 500000))

    def _switch_mode(self, new_mode: str):
        if new_mode == self.mode:
            return
        old = self.mode
        self.mode = new_mode
        self.broker = self.live_broker if new_mode == "live" else self.paper_broker
        self.exec_mgr.broker = self.broker
        self.exec_mgr.mode = new_mode
        if hasattr(self.broker, "set_quote_provider"):
            self.broker.set_quote_provider(self._quote)
        if new_mode == "live":
            self.risk.update_capital(self._live_balance())
        else:
            self.risk.update_capital(float(self.cfg.get("capital", {}).get("initial", 500000)))
        bridge.set_control({"mode": new_mode})
        log.warning("!! MODE SWITCHED %s -> %s (live = real DHAN balance)", old, new_mode)
        self.telegram.status(f"trading mode switched to {new_mode.upper()}")

    def _apply_kill_switch(self):
        ctl = bridge.read_control()
        req = ctl.get("kill_switch_requested")
        if req is None:
            return
        bridge.set_control({"kill_switch_requested": None})
        if self.mode != "live":
            log.warning("kill switch requested but mode is paper - ignoring")
            return
        try:
            action = "ACTIVATE" if req else "DEACTIVATE"
            resp = self.live_broker.client.kill_switch(action)
            self.telegram.status(f"kill switch {action} -> {resp}")
            log.warning("kill switch %s requested via control", action)
        except Exception as e:
            log.error("kill switch failed: %s (needs flat positions + no pending orders)", e)
            self.telegram.error(f"kill switch failed: {e}")

    def _check_stale_quotes(self):
        if not market_is_open() or not self.telegram.enabled:
            return
        now = time.time()
        stale = all(q.ts < now - 90 for q in list(self.quotes.values())[:5]) if self.quotes else True
        if stale:
            if now - self._last_stale_alert > 600:
                self._last_stale_alert = now
                self.telegram.error("market open but no quote updates for 90s - check feed/token")

    def _check_event_blackout(self):
        eb = self.cfg.get("event_blackout", {})
        if not eb.get("enabled", False):
            return
        day = ist_now().date().isoformat()
        try:
            import json as _json
            p = bridge.DATA_DIR / "event_blackout.json"
            if not p.exists():
                return
            events = _json.loads(p.read_text(encoding="utf-8")).get("events", [])
        except Exception:
            return
        hit = [e for e in events if e.get("date") == day]
        if hit and not self._blackout_notified:
            self._blackout_notified = day
            label = hit[0].get("label", "event")
            self.telegram.status(f"🚫 EVENT BLACKOUT {day} ({label}) - no new entries today")
        if hit:
            self.risk.halted = True
            self.risk.halt_reason = f"event blackout: {hit[0].get('label', '')}"
        elif self._blackout_notified == day:
            self.risk.halted = False
            self.risk.halt_reason = ""

    def _apply_control_mode(self):
        ctl = bridge.read_control()
        wanted = str(ctl.get("mode", self.mode))
        if wanted in ("paper", "live") and wanted != self.mode:
            if self.exec_mgr.open:
                log.warning("mode switch to %s deferred - flatten open positions first", wanted)
            else:
                self._switch_mode(wanted)

    def _sync_live_capital(self):
        if self.mode == "live":
            self.risk.update_capital(self._live_balance())

    # ------------------------------------------------------------------
    # Telegram wiring
    # ------------------------------------------------------------------
    def _setup_telegram(self) -> TelegramNotifier:
        tg_cfg = self.cfg.get("telegram", {})
        if not tg_cfg.get("enabled", True):
            log.info("telegram disabled in config")
            return TelegramNotifier("", "")
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat = str(tg_cfg.get("chat_id") or os.getenv("TELEGRAM_CHAT_ID", ""))
        nt = TelegramNotifier(token, chat)
        if nt.enabled:
            nt.test_connection()
            self._setup_commander(nt)
        return nt

    def _setup_commander(self, nt: TelegramNotifier):
        def h_status() -> str:
            s = bridge.read_state()
            return (f"mode {self.mode} | market {'open' if s.get('market_open') else 'closed'}\n"
                    f"equity {s.get('equity', 0):,.0f} | day pnl {s.get('day_pnl', 0):+,.0f}\n"
                    f"trades {s.get('trades_today', 0)} | open {len(s.get('open_positions', []))}\n"
                    f"halted {s.get('halted', False)} {s.get('halt_reason', '')}")

        def h_positions() -> str:
            rows = self.exec_mgr.open_trades_snapshot()
            if not rows:
                return "no open positions"
            return "\n".join(
                f"{r['symbol']} {r['strike']} {r['option_type']} qty {r['qty']} "
                f"entry {r['entry_price']:.2f} pnl {r.get('unrealized', 0):+.2f}"
                for r in rows)

        def h_pause() -> str:
            bridge.set_control({"auto": False})
            return "auto-trading paused (manual orders still allowed)"

        def h_resume() -> str:
            bridge.set_control({"auto": True})
            return "auto-trading resumed"

        def h_paper() -> str:
            bridge.set_control({"mode": "paper"})
            return "mode switch to PAPER queued (applies when no open positions)"

        def h_live() -> str:
            bridge.set_control({"mode": "live"})
            return "mode switch to LIVE queued - REAL MONEY on your DHAN balance (applies when no open positions)"

        def h_kill() -> str:
            bridge.set_control({"kill_switch_requested": True})
            return "kill switch request queued (live only; needs flat positions)"

        def h_help() -> str:
            return "/status /positions /pause /resume /paper /live /killswitch /help"

        commander = TelegramCommander(nt, {
            "status": h_status, "positions": h_positions,
            "pause": h_pause, "resume": h_resume,
            "paper": h_paper, "live": h_live, "killswitch": h_kill, "help": h_help,
        })
        commander.start()
        self._commander = commander

    def _maybe_retrain_ml(self):
        ml_cfg = self.cfg.get("ml_gate", {})
        if not ml_cfg.get("enabled", True) or not ml_cfg.get("auto_retrain", True):
            return
        try:
            rows = self.store.ml_samples(labeled_only=True)
            if len(rows) < int(ml_cfg.get("min_train_samples", 30)):
                return
            trained = int(self.store.get_state("ml_trained_samples", 0))
            if len(rows) <= trained:
                return
            from ..ml.gate import MLGate
            gate = MLGate()
            if gate.train([{"features": r["features"], "label": r["label"]} for r in rows]):
                self.store.set_state("ml_trained_samples", len(rows))
                self.signal_engine.ml = gate
        except Exception as e:
            log.warning("ML retrain failed: %s", e)

    def _maybe_daily_summary(self):
        day = ist_now().date().isoformat()
        if day in self._daily_sent:
            return
        stats = self.risk.day_stats()
        unreal = sum(p.unrealized for p in self.broker.get_positions()) if self.mode == "paper" else 0.0
        cash = self.broker.cash if hasattr(self.broker, "cash") else 0.0
        month = ist_now().strftime("%Y-%m")
        month_trades = [t for t in self.store.all_trades(10000) if str(t["ts"]) and
                        __import__("datetime").datetime.fromtimestamp(t["ts"]).strftime("%Y-%m") == month]
        month_pnl = sum(t["pnl"] for t in month_trades)
        total = stats.get("trades", 0)
        wins = stats.get("wins", 0)
        self.telegram.daily_summary({
            "date": day, "equity": cash + unreal, "day_pnl": stats["pnl"] + unreal,
            "trades": total, "win_rate": round(100.0 * wins / total, 1) if total else 0.0,
            "month_pnl": month_pnl,
        })
        self._daily_sent.add(day)

    def _maybe_heartbeat(self):
        hb_min = float(self.cfg.get("telegram", {}).get("notify", {}).get("heartbeat_min", 0) or 0)
        if hb_min <= 0 or not self.telegram.enabled:
            return
        now = time.time()
        if now - self._last_heartbeat >= hb_min * 60:
            self._last_heartbeat = now
            s = bridge.read_state()
            self.telegram.status(
                f"alive | equity {s.get('equity', 0):,.0f} | day pnl {s.get('day_pnl', 0):+,.0f} "
                f"| open {len(s.get('open_positions', []))} | halted {s.get('halted', False)}")

    def _manage_btst(self):
        """Next-morning handling of BTST positions: exit at the open unless the
        position is already >= 1R (then hold till exit_hm), force-exit at exit_hm."""
        btst_cfg = self.cfg.get("btst", {})
        if not btst_cfg.get("enabled", True):
            return
        if not self.exec_mgr.open:
            return
        now = ist_now()
        hm = now.hour * 100 + now.minute
        exit_hm = int(btst_cfg.get("exit_hm", 945))
        hold_r = float(btst_cfg.get("hold_if_profit_r", 1.0))
        for sid, tr in list(self.exec_mgr.open.items()):
            if not tr.btst:
                continue
            if hm < 930:
                continue                      # let the open settle
            q = self.quotes.get(sid)
            ltp = q.ltp if q else 0.0
            r_val = tr.meta.get("r", 0.0)
            profit = (ltp - tr.entry_price) if tr.side == "BUY" else (tr.entry_price - ltp)
            if hm < exit_hm and r_val > 0 and profit >= hold_r * r_val:
                continue                      # strong hold: keep riding till exit_hm
            self.exec_mgr.exit_security(sid, "BTST_OPEN_EXIT" if hm < exit_hm else "BTST_EXIT")
            log.info("btst position %s closed at %04d", sid, hm)

    def _make_state(self, u: Underlying, chain) -> MarketState:
        return MarketState(
            underlying=u.name, spot=self._index_spot(u), ts=time.time(), chain=chain,
            series_1m=self.series.get(u.name), series_5m=self.series.get(u.name + ":5m"),
            daily=self.daily.get(u.name),
            iv_percentile=self.chain_svc.iv_percentile(u.name),
            indicators=self._indicators(u),
            underlying_cfg={"name": u.name, "strike_interval": u.strike_interval,
                            "lot_size": u.lot_size},
        )

    def _execute_manual(self, o: dict):
        u = next((x for x in self.instruments if x.name == o.get("underlying")), None)
        if u is None:
            bridge.mark_manual_order(o.get("id"), False, "unknown underlying")
            return
        expiry = o.get("expiry") or self._current_expiry(u)
        chain = self.chain_svc._cache.get(f"{u.name}:{expiry}") if expiry else None
        if chain is None:
            bridge.mark_manual_order(o.get("id"), False, "chain unavailable")
            return
        strike = float(o.get("strike") or chain.atm_strike(u.strike_interval))
        ot = str(o.get("option_type", "CE")).upper()
        row = chain.get(strike, ot)
        if row is None or not row.security_id:
            bridge.mark_manual_order(o.get("id"), False, "no option row for strike")
            return
        premium = row.ltp or 0.0
        if premium <= 0:
            bridge.mark_manual_order(o.get("id"), False, "no premium")
            return
        side = str(o.get("side", "BUY")).upper()
        if side == "BUY":
            sl_pct = float(o.get("sl_pct", self.risk.sl_pct))
            qty = int(o.get("qty", 0)) or self.risk.size_position(premium, u.name, u.lot_size, sl_pct)
            sl, tp = self.risk.sl_target_prices(premium, sl_pct)
            product = "INTRADAY"
        else:
            sl_pct = float(o.get("sl_pct", 25))
            qty = int(o.get("qty", 0)) or u.lot_size
            sl, tp = premium * (1 + sl_pct / 100.0), premium * (1 - float(o.get("target_pct", 35)) / 100.0)
            product = "MARGIN"
        sig = Signal(strategy="manual", side=side, option_type=ot, underlying=u.name,
                     expiry=expiry, strike=strike, reason="manual order from MCP/dashboard",
                     ts=time.time(), entry_price_hint=premium)
        from .signal_engine import TradeIntent
        intent = TradeIntent(signal=sig, security_id=row.security_id, premium_entry=premium,
                             qty=qty, sl_price=sl, target_price=tp, lot_size=u.lot_size,
                             product_type=product, requires_live=(side == "SELL"))
        ok, msg = self.exec_mgr.enter(intent)
        bridge.mark_manual_order(o.get("id"), ok, msg)

    # ------------------------------------------------------------------
    def run_cycle(self):
        if not market_is_open():
            if self.exec_mgr.open:
                log.warning("market closed with open positions -> flattening intraday legs")
                self.exec_mgr.exit_non_btst("MARKET_CLOSED")
            now = ist_now()
            if now.hour * 100 + now.minute >= 1535:
                self._maybe_daily_summary()
                self._maybe_retrain_ml()
            if now.hour * 100 + now.minute >= 1540:
                self._screen_stock_btst()
            return
        self._manage_btst()
        now = ist_now()
        hm = now.hour * 100 + now.minute
        # allow 5 min warm-up after open
        if hm < 920:
            return

        self._apply_control_mode()
        self._apply_kill_switch()
        self._check_event_blackout()
        self._sync_live_capital()
        self._check_stale_quotes()
        self._update_ws_subscriptions()
        self._refresh_quotes()
        self._refresh_chains()
        self._manage_stock_time_exit()

        states = []
        open_exposure = 0.0
        for u in self.instruments:
            self._update_candles(u)
            chain = self.chain_svc._cache.get(f"{u.name}:{self._current_expiry(u)}")
            states.append(self._make_state(u, chain))

        open_pos = len(self.exec_mgr.open)
        for tr in self.exec_mgr.open.values():
            row = self._chain_row(tr)
            ltp = row.ltp if row else self.quotes.get(tr.security_id, Quote("", "", 0)).ltp
            open_exposure += (ltp or tr.entry_price) * tr.qty

        cash = self.broker.cash if hasattr(self.broker, "cash") else 0.0
        unreal = sum(tr.meta.get("unrealized", 0.0) for tr in self.exec_mgr.open.values())
        if self.mode == "paper":
            try:
                unreal = sum(p.unrealized for p in self.broker.get_positions())
            except Exception:
                pass
        equity = cash + unreal
        self.risk.set_day_start_equity(equity)
        self.risk.record_equity(cash, unreal)

        control = bridge.read_control()
        auto = bool(control.get("auto", True))
        for o in bridge.poll_manual_orders():
            self._execute_manual(o)

        intents = self.signal_engine.run(states, open_pos, open_exposure, equity) if auto else []
        if not auto and intents:
            log.info("auto-trading paused via control file; %d signal(s) suppressed", len(intents))
        for intent in intents:
            ok, msg = self.exec_mgr.enter(intent)
            log.info("intent %s %s %.0f: %s %s", intent.signal.strategy, intent.signal.option_type,
                     intent.signal.strike, "ENTERED" if ok else "SKIPPED", msg)
            if ok:
                self.store.record_signal(intent.signal.strategy, intent.signal.underlying,
                                         intent.signal.side, intent.signal.strike,
                                         intent.signal.option_type, intent.signal.expiry,
                                         intent.signal.reason, meta={"qty": intent.qty})
                if self.cfg.get("telegram", {}).get("notify", {}).get("entry", True):
                    self.telegram.trade_entered({
                        "symbol": intent.signal.underlying, "option_type": intent.signal.option_type,
                        "strike": intent.signal.strike, "strategy": intent.signal.strategy,
                        "qty": intent.qty, "entry_price": intent.premium_entry,
                        "sl_price": intent.sl_price, "target_price": intent.target_price,
                        "greeks": intent.signal.meta.get("greeks", {}),
                    })

        self.exec_mgr.monitor(indicators={st.underlying: st.indicators for st in states})
        self.exec_mgr.check_time_exit()
        chains_state = {}
        for st in states:
            if st.chain is not None:
                from ..market.selection import chain_ladder
                chains_state[st.underlying] = chain_ladder(
                    st.chain, float(st.underlying_cfg.get("strike_interval", 50)),
                    int(self.cfg.get("strike_select", {}).get("width", 3)))
        self.exec_mgr.check_day_halt(equity)
        if self.risk.halted and not self._halt_notified:
            self._halt_notified = True
            if self.cfg.get("telegram", {}).get("notify", {}).get("halt", True):
                self.telegram.risk_halt(self.risk.halt_reason)
        elif not self.risk.halted:
            self._halt_notified = False
        self._maybe_heartbeat()

        stats = self.risk.day_stats()
        wins = stats.get("wins", 0)
        total = stats.get("trades", 0)
        bridge.publish_state({
            "mode": self.mode,
            "market_open": market_is_open(),
            "auto": auto,
            "cash": cash,
            "equity": equity,
            "day_pnl": stats["pnl"] + unreal,
            "trades_today": total,
            "win_rate_today": round(100.0 * wins / total, 1) if total else 0.0,
            "halted": self.risk.halted,
            "halt_reason": self.risk.halt_reason,
            "limits": {
                "risk_per_trade": self.risk.risk_per_trade,
                "daily_loss_limit": self.risk.daily_loss_limit,
                "max_positions": self.risk.max_positions,
                "max_daily_trades": self.risk.max_daily_trades,
            },
            "open_positions": self.exec_mgr.open_trades_snapshot(),
            "recent_signals": self.store.recent_signals(10),
            "equity_curve": self.store.equity_curve(200)[-200:],
            "chains": chains_state,
            "expiries": self._expiries_state(),
        })
        bridge.publish_sync_files(bridge.read_state())
        log.info("[cycle] mode=%s equity=%.0f day_pnl=%.0f trades=%d open=%d | %s",
                 self.mode, equity, stats["pnl"] + unreal, stats["trades"],
                 len(self.exec_mgr.open),
                 ", ".join(f"{s.underlying} {s.option_type}{s.strike}" for s in states if s.chain))

    def _expiries_state(self) -> dict:
        from ..market.instruments import expiry_with_dte
        out = {}
        for u in self.instruments:
            exps = self.chain_svc.expiry_lists.get(u.name, [])
            rows = expiry_with_dte(exps)
            for r in rows:
                r["selected"] = (r["expiry"] == self.chain_svc.selected_expiry.get(u.name))
                chain = self.chain_svc._cache.get(f"{u.name}:{r['expiry']}")
                if chain is not None:
                    r["iv_atm"] = round(chain.iv_atm(u.strike_interval), 4) or None
            out[u.name] = rows[:8]
        return out

    def _current_expiry(self, u: Underlying) -> str:
        chain = None
        for key, snap in self.chain_svc._cache.items():
            if key.startswith(u.name + ":"):
                chain = snap
                break
        return chain.expiry if chain else ""

    def start(self, run_forever: bool = True):
        log.info("=== dhan-auto-trader starting (mode=%s) ===", self.mode)
        for u in self.instruments:
            log.info("underlying %s id=%s lot=%d interval=%d", u.name, u.security_id,
                     u.lot_size, u.strike_interval)
            self._seed_series(u)
        interval = max(3.0, float(self.cfg.get("feed", {}).get("quote_interval_sec", 5)))
        while run_forever:
            try:
                self.run_cycle()
            except Exception as e:
                log.exception("cycle error: %s", e)
            time.sleep(interval)


def main():
    AutoTrader().start()


if __name__ == "__main__":
    main()

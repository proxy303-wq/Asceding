"""DHAN (DhanHQ) live broker + market-data client, backed by the official dhanhq SDK."""
from __future__ import annotations

import logging
import time
from typing import Optional

from .base import Broker, ChainSnapshot, Funds, OptionRow, OrderResult, Position, Quote

log = logging.getLogger(__name__)

SEG = "NSE_FNO"


def _num(v, default=0.0):
    try:
        f = float(v)
        return f if f == f else default
    except (TypeError, ValueError):
        return default


def _get(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _normalize_ohlcv(data):
    """Convert DHAN parallel-array OHLCV responses ({'open':[...], ...}) into
    [{'timestamp','open','high','low','close','volume','open_interest'}] rows."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    ts = data.get("timestamp") or data.get("time") or []
    opens = data.get("open") or []
    highs = data.get("high") or []
    lows = data.get("low") or []
    closes = data.get("close") or []
    vols = data.get("volume") or data.get("vol") or []
    ois = data.get("open_interest") or []
    n = len(closes)
    rows = []
    for i in range(n):
        t = ts[i] if isinstance(ts, list) and i < len(ts) else 0
        if isinstance(t, (int, float)) and t > 1e12:   # millis -> seconds
            t = t / 1000.0
        rows.append({
            "timestamp": t,
            "open": opens[i] if i < len(opens) else 0.0,
            "high": highs[i] if i < len(highs) else 0.0,
            "low": lows[i] if i < len(lows) else 0.0,
            "close": closes[i],
            "volume": vols[i] if i < len(vols) else 0.0,
            "open_interest": ois[i] if i < len(ois) else 0.0,
        })
    return rows


class DhanClient:
    """Market data + account reads. Wraps dhanhq with rate-limit pacing."""

    def __init__(self, client_id: str, access_token: str, min_gap_s: float = 1.05):
        from dhanhq import DhanContext, dhanhq
        self._client_id = client_id
        self._access_token = access_token
        self._dhan = dhanhq(DhanContext(client_id, access_token))
        self._last_call = 0.0
        self.min_gap_s = min_gap_s

    def client_id(self) -> str:
        return self._client_id

    def access_token(self) -> str:
        return self._access_token

    def _pace(self):
        gap = time.time() - self._last_call
        if gap < self.min_gap_s:
            time.sleep(self.min_gap_s - gap)
        self._last_call = time.time()

    def _call(self, fn, *a, **kw):
        self._pace()
        try:
            resp = fn(*a, **kw)
            if isinstance(resp, dict) and resp.get("status") == "failure":
                raise RuntimeError("DHAN error: %s" % resp.get("errorMessage", resp))
            return resp
        except Exception as e:
            log.warning("dhan call %s failed: %s", getattr(fn, "__name__", "?"), e)
            raise

    # ---------- market data ----------
    def ltp(self, security_ids: list[str], segment: str = SEG) -> dict[str, float]:
        resp = self._call(self._dhan.ticker_data, {segment: [int(s) for s in security_ids]})
        while isinstance(resp, dict) and isinstance(resp.get("data"), dict):
            inner = resp["data"]
            seg_map = inner.get(segment)
            if isinstance(seg_map, dict):
                resp = seg_map
                break
            resp = inner
        out = {}
        for sid, item in (resp or {}).items():
            if isinstance(item, dict):
                ltp = _get(item, "last_price", "ltp", "LastTradedPrice", default=0.0)
                if ltp:
                    out[str(sid)] = float(ltp)
        return out

    def quote(self, security_id: str) -> Quote:
        resp = self._call(self._dhan.quote_data, {SEG: [int(security_id)]})
        item = None
        if isinstance(resp, dict):
            item = resp.get(str(security_id)) or resp.get(int(security_id)) or resp.get(security_id)
        item = item or {}
        return Quote(
            security_id=str(security_id),
            symbol=str(_get(item, "symbol", "tradingSymbol", default=security_id)),
            ltp=_num(_get(item, "last_price", "ltp", "LastTradedPrice", default=0.0)),
            open=_num(_get(item, "open", "Open", default=0.0)),
            high=_num(_get(item, "high", "High", default=0.0)),
            low=_num(_get(item, "low", "Low", default=0.0)),
            prev_close=_num(_get(item, "prev_close", "PreviousClose", default=0.0)),
            volume=_num(_get(item, "volume", "Volume", default=0.0)),
            oi=_num(_get(item, "open_interest", "OpenInterest", default=0.0)),
            bid=_num(_get(item, "bid_price", "BidPrice", default=0.0)),
            ask=_num(_get(item, "ask_price", "AskPrice", default=0.0)),
            ts=int(time.time()),
        )

    def option_chain(self, underlying_security_id: str, underlying_seg: str, expiry: str) -> ChainSnapshot:
        resp = self._call(self._dhan.option_chain, underlying_security_id, underlying_seg, expiry)
        return parse_chain(resp, expiry=expiry)

    def expiry_list(self, underlying_security_id: str, underlying_seg: str) -> list[str]:
        resp = self._call(self._dhan.expiry_list, underlying_security_id, underlying_seg)
        data = resp.get("data") if isinstance(resp, dict) else resp
        if isinstance(data, dict):            # nested {"data": [...]} shape
            data = data.get("data", [])
        out = []
        if isinstance(data, list):
            for e in data:
                if isinstance(e, dict):
                    e = _get(e, "expiry", "expiryDate", "EXPIRY", default=None)
                if e:
                    out.append(str(e)[:10])
        return out

    def historical_daily(self, security_id: str, exchange_segment: str, instrument_type: str,
                         from_date: str, to_date: str) -> list[dict]:
        """Daily OHLCV via direct REST (SDK wrapper proved unreliable here)."""
        import requests as _req
        self._pace()
        resp = _req.post("https://api.dhan.co/v2/charts/historical",
                         json={"securityId": int(security_id), "exchangeSegment": exchange_segment,
                               "instrument": instrument_type, "expiryCode": 0, "oi": "false",
                               "fromDate": from_date, "toDate": to_date},
                         headers={"access-token": self.access_token(), "client-id": self.client_id(),
                                  "Content-Type": "application/json"}, timeout=20)
        if resp.status_code != 200:
            raise RuntimeError(f"daily history HTTP {resp.status_code}: {resp.text[:200]}")
        j = resp.json()
        if isinstance(j, dict) and j.get("status") == "failure":
            raise RuntimeError(f"daily history failed: {j.get('errorMessage', j)[:200]}")
        data = j.get("data") if isinstance(j, dict) and isinstance(j.get("data"), (dict, list)) else j
        return _normalize_ohlcv(data)

    def intraday_minute(self, security_id: str, exchange_segment: str, instrument_type: str,
                        from_date: str, to_date: str, interval: int = 1) -> list[dict]:
        """Intraday OHLCV via direct REST (SDK wrapper proved unreliable here)."""
        import requests as _req
        self._pace()
        resp = _req.post("https://api.dhan.co/v2/charts/intraday",
                         json={"securityId": int(security_id), "exchangeSegment": exchange_segment,
                               "instrument": instrument_type, "interval": int(interval),
                               "fromDate": from_date, "toDate": to_date},
                         headers={"access-token": self.access_token(), "client-id": self.client_id(),
                                  "Content-Type": "application/json"}, timeout=25)
        if resp.status_code != 200:
            raise RuntimeError(f"intraday history HTTP {resp.status_code}: {resp.text[:200]}")
        j = resp.json()
        if isinstance(j, dict) and j.get("status") == "failure":
            raise RuntimeError(f"intraday history failed: {j.get('errorMessage', j)[:200]}")
        data = j.get("data") if isinstance(j, dict) and isinstance(j.get("data"), (dict, list)) else j
        return _normalize_ohlcv(data)

    # ---------- account ----------
    def get_positions_raw(self) -> list[dict]:
        resp = self._call(self._dhan.get_positions)
        return resp.get("data", []) if isinstance(resp, dict) else (resp or [])

    def get_funds_raw(self) -> dict:
        return self._call(self._dhan.get_fund_limits) or {}

    def kill_switch(self, action: str):
        return self._call(self._dhan.kill_switch, action)


def parse_chain(resp, expiry: str) -> ChainSnapshot:
    """Parse the DHAN option-chain response.

    Real shape (verified live): {"data": {"last_price": <spot>,
      "oc": {"17850.000000": {"ce": {...}, "pe": {...}}, ...}}}.
    Falls back to a list-of-rows shape if the API changes."""
    data = resp.get("data") if isinstance(resp, dict) else resp
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], dict):
        data = data["data"]                   # tolerate nested {"data": {...}}
    snap = ChainSnapshot(underlying="", expiry=expiry, spot=0.0, ts=int(time.time()))
    if isinstance(data, dict) and isinstance(data.get("oc"), dict):
        snap.spot = _num(data.get("last_price", 0.0))
        for strike_str, sides in data["oc"].items():
            if not isinstance(sides, dict):
                continue
            try:
                strike = float(strike_str)
            except (TypeError, ValueError):
                continue
            for ot_key, ot in (("ce", "CE"), ("pe", "PE")):
                r = sides.get(ot_key)
                if not isinstance(r, dict):
                    r = sides.get(ot_key.upper())
                if not isinstance(r, dict):
                    continue
                gr = r.get("greeks") or {}
                snap.rows[(strike, ot)] = OptionRow(
                    security_id=str(_get(r, "security_id", default="")),
                    symbol="", underlying=snap.underlying, expiry=expiry,
                    strike=strike, option_type=ot,
                    ltp=_num(r.get("last_price", 0)),
                    bid=_num(r.get("top_bid_price", 0)),
                    ask=_num(r.get("top_ask_price", 0)),
                    volume=_num(r.get("volume", 0)),
                    oi=_num(r.get("oi", 0)),
                    oi_change=_num(r.get("previous_oi", 0)) - _num(r.get("oi", 0)),
                    iv=_num(r.get("implied_volatility", 0)),
                    delta=_num(gr.get("delta", 0)), gamma=_num(gr.get("gamma", 0)),
                    theta=_num(gr.get("theta", 0)), vega=_num(gr.get("vega", 0)),
                    raw=r,
                )
        return snap
    # legacy list-of-rows fallback
    spot = _num(_get(resp, "spot", "underlying_spot", "spotPrice", default=0.0)) if isinstance(resp, dict) else 0.0
    snap.spot = spot
    if not isinstance(data, list):
        log.warning("unexpected chain payload: %s", str(resp)[:200])
        return snap
    for row in data:
        if not isinstance(row, dict):
            continue
        strike = _num(_get(row, "strikePrice", "strike_price", "strike", default=0.0))
        ot = str(_get(row, "optionType", "option_type", "OPTION_TYPE", default="")).upper()
        if ot in ("CALL", "CE"):
            ot = "CE"
        elif ot in ("PUT", "PE"):
            ot = "PE"
        if not strike or ot not in ("CE", "PE"):
            continue
        ltp = _num(_get(row, "ltp", "last_price", "LastTradedPrice", default=0.0))
        sid = str(_get(row, "security_id", "securityId", "SEM_SMST_SECURITY_ID", default=""))
        oi = _num(_get(row, "openInterest", "open_interest", "OI", default=0.0))
        snap.rows[(strike, ot)] = OptionRow(
            security_id=sid,
            symbol=str(_get(row, "symbol", "tradingSymbol", default="")),
            underlying=underlying,
            expiry=expiry,
            strike=strike,
            option_type=ot,
            ltp=ltp,
            bid=_num(_get(row, "bidPrice", "bid_price", default=0.0)),
            ask=_num(_get(row, "askPrice", "ask_price", default=0.0)),
            volume=_num(_get(row, "volume", "Volume", default=0.0)),
            oi=oi,
            oi_change=_num(_get(row, "oiChange", "oi_change", "openInterestChange", default=0.0)),
            iv=_num(_get(row, "iv", "IV", "impliedVolatility", default=0.0)),
            delta=_num(_get(row, "delta", "Delta", default=0.0)),
            gamma=_num(_get(row, "gamma", "Gamma", default=0.0)),
            theta=_num(_get(row, "theta", "Theta", default=0.0)),
            vega=_num(_get(row, "vega", "Vega", default=0.0)),
            raw=row,
        )
    return snap


class DhanLiveBroker(Broker):
    """Real-money execution against DHAN."""

    def __init__(self, client: DhanClient):
        self.client = client

    def place_order(self, security_id, transaction_type, quantity, order_type="LIMIT", price=0.0,
                    trigger_price=0.0, product_type="INTRADAY", exchange_segment=SEG,
                    validity="DAY", tag=""):
        try:
            resp = self.client._call(
                self.client._dhan.place_order,
                security_id=security_id, exchange_segment=exchange_segment,
                transaction_type=transaction_type, quantity=int(quantity),
                order_type=order_type, product_type=product_type, price=float(price),
                trigger_price=float(trigger_price), validity=validity, tag=tag,
            )
            oid = _get(resp, "orderId", "order_id", default="")
            status = _get(resp, "orderStatus", "order_status", default="")
            if not oid and resp.get("status") == "failure":
                return OrderResult.fail(str(resp.get("errorMessage", resp)), resp)
            return OrderResult(order_id=str(oid), status=str(status), raw=resp)
        except Exception as e:
            log.exception("place_order failed")
            return OrderResult.fail(str(e))

    def place_super_order(self, security_id, transaction_type, quantity, order_type="LIMIT", price=0.0,
                          target_price=0.0, stop_loss_price=0.0, product_type="INTRADAY",
                          exchange_segment=SEG, tag=""):
        try:
            resp = self.client._call(
                self.client._dhan.place_super_order,
                security_id=security_id, exchange_segment=exchange_segment,
                transaction_type=transaction_type, quantity=int(quantity),
                order_type=order_type, product_type=product_type, price=float(price),
                targetPrice=float(target_price), stopLossPrice=float(stop_loss_price),
                tag=tag,
            )
            oid = _get(resp, "orderId", "order_id", default="")
            status = _get(resp, "orderStatus", "order_status", default="")
            if not oid and resp.get("status") == "failure":
                return OrderResult.fail(str(resp.get("errorMessage", resp)), resp)
            return OrderResult(order_id=str(oid), status=str(status), raw=resp)
        except Exception as e:
            log.exception("place_super_order failed")
            return OrderResult.fail(str(e))

    def modify_super_order(self, order_id: str, leg_name: str, price: float):
        """Move a Super Order leg (e.g. STOP_LOSS_LEG) - used for trailing stops."""
        try:
            resp = self.client._call(self.client._dhan.modify_super_order,
                                     order_id, leg_name, float(price))
            return OrderResult(order_id=str(order_id), status=str(resp.get("orderStatus", "")), raw=resp)
        except Exception as e:
            log.warning("modify_super_order failed: %s", e)
            return OrderResult.fail(str(e))

    def cancel_order(self, order_id):
        try:
            resp = self.client._call(self.client._dhan.cancel_order, order_id)
            return OrderResult(order_id=str(order_id), status=str(resp.get("orderStatus", "")), raw=resp)
        except Exception as e:
            return OrderResult.fail(str(e))

    def get_positions(self) -> list[Position]:
        out = []
        for p in self.client.get_positions_raw():
            net = int(_num(p.get("netQty", 0)))
            realized = _num(p.get("realizedProfit", 0))
            if net == 0 and realized == 0:
                continue
            out.append(Position(
                security_id=str(p.get("securityId", "")),
                symbol=str(p.get("tradingSymbol", "")),
                exchange_segment=str(p.get("exchangeSegment", SEG)),
                net_qty=net,
                buy_avg=_num(p.get("buyAvg", 0)),
                sell_avg=_num(p.get("sellAvg", 0)),
                unrealized=_num(p.get("unrealizedProfit", 0)),
                realized=_num(p.get("realizedProfit", 0)),
                multiplier=int(_num(p.get("multiplier", 1), 1)),
                option_type=str(p.get("drvOptionType", "")),
                strike=_num(p.get("drvStrikePrice", 0)),
                expiry=str(p.get("drvExpiryDate", "")),
            ))
        return out

    def get_funds(self) -> Funds:
        f = self.client.get_funds_raw()
        if isinstance(f, dict) and isinstance(f.get("data"), dict):
            f = f["data"]
        return Funds(
            available_balance=_num(f.get("availabelBalance", 0)),
            utilized_amount=_num(f.get("utilizedAmount", 0)),
            withdrawable_balance=_num(f.get("withdrawableBalance", 0)),
            raw=f,
        )

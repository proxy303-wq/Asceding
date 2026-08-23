"""DHAN market-feed WebSocket client.

Endpoint:  wss://api-feed.dhan.co?version=2&token=<token>&clientId=<client>&authType=2

Subscription is JSON ({RequestCode: 17 -> Quote mode}) with up to 100 instruments
per message. Responses are binary, little-endian:

  8-byte header:  byte0 = response code | int16 message length | byte3 = exchange
                  segment (IDX_I=0, NSE_EQ=1, NSE_FNO=2, BSE_EQ=4) | int32 securityId
  code 2 Ticker : float32 LTP + int32 LTT
  code 4 Quote  : float32 LTP, int16 LTQ, int32 LTT, float32 ATP, int32 volume,
                  int32 sellQty, int32 buyQty, float32 open, float32 close,
                  float32 high, float32 low
  code 5 OI     : int32 open interest
  code 6 Prev   : float32 prev close + int32 prev OI
  code 8 Full   : quote + OI + high/low OI + day OHLC + 5x20 depth

The server pings every ~10s (auto-ponged by the websockets lib); the connection is
dropped if we stay silent >40s. Reconnects with backoff and resubscribes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import threading
import time
from typing import Callable, Optional

import websockets

log = logging.getLogger(__name__)

WS_URL = "wss://api-feed.dhan.co"

TICKER, INDEX, QUOTE, OI, PREV_CLOSE, MARKET_STATUS, FULL, DISCONNECT = 2, 1, 4, 5, 6, 7, 8, 50

# little-endian header: code(u8) length(i16) seg(u8) securityId(i32)
HEADER = struct.Struct("<BhBi")
QUOTE_FMT = struct.Struct("<fhifiiiffff")        # LTP LTQ LTT ATP vol sell buy open close high low (42B)
FULL_FMT = struct.Struct("<fhifiiiiiiffff")      # + OI, highOI, lowOI (58B)


def parse_packet(raw: bytes):
    """Parse one feed packet -> (code, seg, security_id, fields) or None for junk."""
    if raw is None or len(raw) < 8:
        return None
    code, _length, seg, sid = HEADER.unpack_from(raw, 0)
    payload = raw[8:]
    if code == DISCONNECT:
        reason = struct.unpack("<h", payload[:2])[0] if len(payload) >= 2 else 0
        return (code, seg, sid, {"reason": reason})
    if code == TICKER and len(payload) >= 8:
        ltp, ltt = struct.unpack("<fi", payload[:8])
        return (code, seg, sid, {"ltp": ltp, "ltt": ltt})
    if code == QUOTE and len(payload) >= QUOTE_FMT.size:
        (ltp, ltq, ltt, atp, volume, sell_qty, buy_qty, open_, close_, high_, low_) = QUOTE_FMT.unpack(payload[:QUOTE_FMT.size])
        return (code, seg, sid, {"ltp": ltp, "ltq": ltq, "ltt": ltt, "atp": atp, "volume": volume,
                                 "sell_qty": sell_qty, "buy_qty": buy_qty, "open": open_,
                                 "close": close_, "high": high_, "low": low_})
    if code == OI and len(payload) >= 4:
        return (code, seg, sid, {"oi": struct.unpack("<i", payload[:4])[0]})
    if code == PREV_CLOSE and len(payload) >= 8:
        prev_close, prev_oi = struct.unpack("<fi", payload[:8])
        return (code, seg, sid, {"prev_close": prev_close, "prev_oi": prev_oi})
    if code == FULL and len(payload) >= FULL_FMT.size:
        (ltp, ltq, ltt, atp, volume, sell_qty, buy_qty, oi, high_oi, low_oi,
         open_, close_, high_, low_) = FULL_FMT.unpack(payload[:FULL_FMT.size])
        return (code, seg, sid, {"ltp": ltp, "ltq": ltq, "ltt": ltt, "atp": atp, "volume": volume,
                                 "oi": oi, "high_oi": high_oi, "low_oi": low_oi, "open": open_,
                                 "close": close_, "high": high_, "low": low_})
    return None


class DhanFeedWebSocket:
    """Background thread streaming live quotes. Non-critical: failures are logged
    and callers fall back to REST polling."""

    def __init__(self, access_token: str, client_id: str,
                 on_quote: Optional[Callable[[str, dict], None]] = None,
                 reconnect_delay_s: float = 5.0):
        self.access_token = access_token
        self.client_id = client_id
        self.on_quote = on_quote
        self.reconnect_delay_s = reconnect_delay_s
        self._wanted: set[tuple[str, str]] = set()   # (segment, security_id)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.connected = False
        self.last_tick = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="dhan-feed-ws", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def subscribe(self, securities: list[tuple[str, str]]):
        with self._lock:
            self._wanted = set(securities)

    @property
    def healthy(self) -> bool:
        return self.connected and (time.time() - self.last_tick < 15.0)

    # ------------------------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                asyncio.run(self._stream())
            except Exception as e:
                log.warning("feed ws error: %s", e)
            self._stop.wait(self.reconnect_delay_s)

    async def _stream(self):
        url = (f"{WS_URL}?version=2&token={self.access_token}"
               f"&clientId={self.client_id}&authType=2")
        async with websockets.connect(url, ping_interval=None, max_size=None) as ws:
            self.connected = True
            log.info("feed ws connected")
            try:
                await self._send_subscription(ws)
                async for raw in ws:
                    self.last_tick = time.time()
                    packet = parse_packet(raw)
                    if packet is None:
                        continue
                    code, _seg, sid, fields = packet
                    if code == DISCONNECT:
                        log.warning("feed ws disconnect packet: %s", fields)
                        break
                    if self.on_quote and code in (TICKER, QUOTE, OI, PREV_CLOSE, FULL) and fields:
                        self.on_quote(str(sid), fields)
            finally:
                self.connected = False
                log.warning("feed ws closed")

    async def _send_subscription(self, ws):
        with self._lock:
            want = list(self._wanted)
        for i in range(0, len(want), 100):
            chunk = want[i:i + 100]
            msg = {
                "RequestCode": 17,   # Quote mode: LTP + OHLC + volume (+ OI packets)
                "InstrumentCount": len(chunk),
                "InstrumentList": [{"ExchangeSegment": seg, "SecurityId": sid} for seg, sid in chunk],
            }
            await ws.send(json.dumps(msg))
        log.info("feed ws subscribed to %d instruments", len(want))

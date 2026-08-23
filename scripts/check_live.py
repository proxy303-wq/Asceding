"""Live-account dry-run: verify everything the bot needs BEFORE going live.

Read-only by default (token, profile, funds, positions, order book, chain fetch).
With --place-test-order it places and cancels ONE tiny equity order after you
confirm - the safest possible real-touch to prove order APIs work from this IP.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker.auth import token_is_expired, token_expiry  # noqa: E402
from src.broker.dhan_live import DhanClient, DhanLiveBroker  # noqa: E402
from src.config import load_config  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--place-test-order", action="store_true",
                    help="place + cancel one tiny real equity order (NSE_EQ) after confirmation")
    args = ap.parse_args()

    cfg = load_config()
    cid = cfg.get("dhan_client_id", "")
    token = cfg.get("dhan_access_token", "")
    if not cid or not token:
        print("FATAL: no client id / access token (set DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN or TOTP).")
        sys.exit(1)

    from datetime import datetime
    exp = token_expiry(token)
    print(f"token exp: {datetime.fromtimestamp(exp).isoformat()} "
          f"({'OK' if not token_is_expired(token) else 'EXPIRED'})")
    if token_is_expired(token):
        print("token expired - enable DHAN_PIN + DHAN_TOTP_SECRET for auto-refresh, "
              "or run scripts/dhan_consent.py")
        sys.exit(1)

    client = DhanClient(cid, token)
    broker = DhanLiveBroker(client)
    ok = True

    print("\n[1] funds")
    try:
        f = broker.get_funds()
        print(f"    available: {f.available_balance:,.2f}  utilized: {f.utilized_amount:,.2f}")
    except Exception as e:
        ok = False; print(f"    FAIL: {e}")

    print("[2] positions")
    try:
        pos = broker.get_positions()
        print(f"    {len(pos)} open position(s)")
    except Exception as e:
        ok = False; print(f"    FAIL: {e}")

    print("[3] order book")
    try:
        ob = client._call(client._dhan.get_order_list)
        rows = ob.get("data", []) if isinstance(ob, dict) else (ob or [])
        print(f"    {len(rows)} order(s) today")
    except Exception as e:
        ok = False; print(f"    FAIL: {e}")

    time.sleep(2)
    print("[4] option chain (NIFTY)")
    try:
        from src.market.instruments import InstrumentMaster
        m = InstrumentMaster()
        m.load()
        nif = cfg["instruments"][0]
        sid = nif["security_id"]                       # F&O chain underlying id (26000)
        index_id = m.index_security_id("NIFTY") or "13"
        exps = client.expiry_list(sid, nif["segment"])
        snap = client.option_chain(sid, nif["segment"], exps[0])
        print(f"    expiries: {exps[:3]}  chain rows: {len(snap.rows)}  spot: {snap.spot}")
        # verify spot sanity vs index LTP
        time.sleep(1.2)
        ltps = client.ltp([index_id], segment="IDX_I")
        print(f"    index ltp check: {ltps.get(index_id)} (chain spot {snap.spot})")
    except Exception as e:
        ok = False; print(f"    FAIL: {e}")

    print("[5] historical data (intraday 1m)")
    try:
        from src.market.instruments import InstrumentMaster
        _m = InstrumentMaster(); _m.load()
        _idx = _m.index_security_id("NIFTY") or "13"
        rows = client.intraday_minute(_idx, "IDX_I", "INDEX",
                                      "2026-08-01", "2026-08-02", 1)
        print(f"    fetched {len(rows)} 1m rows for 2026-08-01")
    except Exception as e:
        ok = False; print(f"    FAIL: {e} (data plan active?)")

    if args.place_test_order:
        print("\n[6] test order (REAL, tiny)")
        ans = input("Place 1 share of SBIN (NSE_EQ, CNC) and cancel it? type YES: ").strip()
        if ans != "YES":
            print("    skipped")
        else:
            try:
                sid = None
                from src.market.instruments import InstrumentMaster
                m = InstrumentMaster()
                if m.load():
                    sid = m.stock_security_id("SBIN")
                if not sid:
                    print("    could not resolve SBIN security id - skipped")
                else:
                    res = broker.place_order(security_id=sid, transaction_type="BUY",
                                             quantity=1, order_type="LIMIT",
                                             price=1.0, product_type="CNC",
                                             exchange_segment="NSE_EQ")
                    print(f"    order id: {res.order_id} status: {res.status}")
                    if res.ok and res.order_id:
                        time.sleep(2)
                        c = broker.cancel_order(res.order_id)
                        print(f"    cancelled: {c.status}")
            except Exception as e:
                ok = False; print(f"    FAIL: {e}")

    print("\n" + ("ALL CHECKS OK - ready for live (paper-trade first!)" if ok
                  else "SOME CHECKS FAILED - fix before going live"))


if __name__ == "__main__":
    main()

"""Offline tests for the DHAN feed binary parser (byte layouts from the docs)."""
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from src.market.feed import FULL_FMT, QUOTE_FMT, parse_packet  # noqa: E402

HEADER = struct.Struct("<BhBi")


def hdr(code, seg, sid, length=0):
    return HEADER.pack(code, length, seg, sid)


def test_quote_packet():
    payload = struct.pack("<fhifiiiffff", 23500.5, 12, 1234567, 23499.9,
                          123456, 100, 150, 23400.0, 23450.0, 23520.0, 23380.0)
    raw = hdr(4, 2, 49081, QUOTE_FMT.size) + payload
    parsed = parse_packet(raw)
    assert parsed is not None
    code, seg, sid, f = parsed
    assert (code, seg, sid) == (4, 2, 49081)
    assert abs(f["ltp"] - 23500.5) < 1e-4
    assert f["volume"] == 123456
    assert abs(f["high"] - 23520.0) < 1e-4 and abs(f["low"] - 23380.0) < 1e-4


def test_oi_packet():
    raw = hdr(5, 2, 49081, 4) + struct.pack("<i", 50000)
    parsed = parse_packet(raw)
    assert parsed and parsed[0] == 5 and parsed[3]["oi"] == 50000


def test_ticker_packet():
    raw = hdr(2, 2, 49081, 8) + struct.pack("<fi", 23501.25, 1234568)
    parsed = parse_packet(raw)
    assert parsed and parsed[0] == 2
    assert abs(parsed[3]["ltp"] - 23501.25) < 1e-4


def test_prev_close_packet():
    raw = hdr(6, 0, 26000, 8) + struct.pack("<fi", 23200.0, 40000)
    parsed = parse_packet(raw)
    assert parsed and parsed[3]["prev_close"] == 23200.0 and parsed[3]["prev_oi"] == 40000


def test_full_packet():
    payload = struct.pack("<fhifiiiiiiffff", 23510.0, 5, 1234569, 23505.0,
                          200000, 90, 110, 80000, 90000, 70000,
                          23400.0, 23450.0, 23520.0, 23380.0)
    raw = hdr(8, 2, 49081, FULL_FMT.size) + payload
    parsed = parse_packet(raw)
    assert parsed and parsed[0] == 8
    f = parsed[3]
    assert f["oi"] == 80000 and f["high_oi"] == 90000 and f["low_oi"] == 70000


def test_disconnect_packet():
    raw = hdr(50, 2, 0, 2) + struct.pack("<h", 805)
    parsed = parse_packet(raw)
    assert parsed and parsed[0] == 50 and parsed[3]["reason"] == 805


def test_junk_returns_none():
    assert parse_packet(b"\x00") is None
    assert parse_packet(b"") is None


if __name__ == "__main__":
    for fn in [test_quote_packet, test_oi_packet, test_ticker_packet,
               test_prev_close_packet, test_full_packet, test_disconnect_packet,
               test_junk_returns_none]:
        fn()
        print("ok " + fn.__name__)
    print("ALL FEED PARSER TESTS PASSED")

"""Offline tests for the Telegram module (no network: send() is captured)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class CapturingNotifier:
    """Stand-in that captures formatted messages instead of hitting the API."""

    def __init__(self):
        self.messages = []

    def send(self, message):
        self.messages.append(message)
        return True


def build_notifier_with_capture():
    from telegram import TelegramNotifier
    nt = TelegramNotifier("test:token", "12345")
    nt.session = None  # ensure no network is attempted
    cap = CapturingNotifier()
    nt.send = cap.send
    return nt, cap


def test_disabled_when_no_creds():
    from telegram import TelegramNotifier
    nt = TelegramNotifier("", "")
    assert nt.enabled is False
    assert nt.send("hello") is False


def test_trade_entered_message_has_greeks():
    nt, cap = build_notifier_with_capture()
    nt.trade_entered({
        "symbol": "NIFTY", "option_type": "CE", "strike": 24500,
        "strategy": "momentum", "qty": 75, "entry_price": 310.5,
        "sl_price": 217.35, "target_price": 483.75,
        "greeks": {"delta": 0.52, "gamma": 0.0003, "theta": -4.1, "iv": 0.135},
    })
    assert cap.messages, "message should be produced"
    msg = cap.messages[0]
    assert "TRADE ENTERED" in msg
    assert "24500" in msg
    assert "0.52" in msg and "13.5%" in msg
    assert "momentum" in msg


def test_trade_exited_message_pnl_sign():
    nt, cap = build_notifier_with_capture()
    nt.trade_exited({"symbol": "NIFTY", "strike": 24500, "exit_reason": "TARGET_HIT",
                     "entry_price": 310.5, "exit_price": 483.75, "pnl": 12993.75})
    msg = cap.messages[0]
    assert "TARGET_HIT" in msg
    assert "+12,993.75" in msg or "12,993.75" in msg
    nt.trade_exited({"symbol": "NIFTY", "strike": 24500, "exit_reason": "SL_HIT",
                     "entry_price": 310.5, "exit_price": 217.35, "pnl": -6986.25})
    assert "-6,986.25" in cap.messages[1]


def test_daily_summary_message():
    nt, cap = build_notifier_with_capture()
    nt.daily_summary({"date": "2026-08-18", "equity": 498000, "day_pnl": -2000,
                      "trades": 3, "win_rate": 33.3, "month_pnl": 12000})
    msg = cap.messages[0]
    assert "DAILY SUMMARY" in msg
    assert "498,000" in msg and "33.3" in msg


def test_escape_prevents_html_injection():
    nt, cap = build_notifier_with_capture()
    nt.status("<script>alert(1)</script> & more")
    msg = cap.messages[0]
    assert "<script>" not in msg
    assert "&lt;script&gt;" in msg


def test_split_long_message():
    from telegram import TelegramNotifier
    chunks = TelegramNotifier._split_message("x" * 10000)
    assert len(chunks) == 3
    assert all(len(c) <= 3900 for c in chunks)


if __name__ == "__main__":
    for fn in [test_disabled_when_no_creds, test_trade_entered_message_has_greeks,
               test_trade_exited_message_pnl_sign, test_daily_summary_message,
               test_escape_prevents_html_injection, test_split_long_message]:
        fn()
        print("ok " + fn.__name__)
    print("ALL TELEGRAM TESTS PASSED")

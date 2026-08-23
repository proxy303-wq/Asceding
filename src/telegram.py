"""Telegram notifier + command polling for the portfolio manager.

Deliberately NON-CRITICAL: a Telegram failure must never stop the trading loop,
change a trade decision, or interfere with broker execution. All network errors
are swallowed and logged; sending is best-effort with retries.

Commands (/status, /positions, /pause, /resume, /help) are answered via long
polling in a daemon thread, only for the configured chat id.
"""
from __future__ import annotations

import html
import logging
import threading
import time
from typing import Callable, Optional

import requests

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 3900
REQUEST_TIMEOUT = 5
MAX_RETRIES = 2
RETRY_DELAY_SECONDS = 0.5


class TelegramNotifier:
    def __init__(self, bot_token: str = "", chat_id: str = ""):
        self.bot_token = str(bot_token).strip()
        self.chat_id = str(chat_id).strip()
        self.enabled = bool(self.bot_token and self.chat_id)
        self.session = requests.Session()
        if not self.enabled:
            log.warning("Telegram notifier DISABLED (missing bot token or chat id)")

    # --------------------------------------------------------------
    def send(self, message: str) -> bool:
        """Send a message. Returns True only on Telegram confirmation."""
        if not self.enabled or not message:
            return False
        url = f"{TELEGRAM_API}/bot{self.bot_token}/sendMessage"
        chunks = self._split_message(str(message))
        for chunk in chunks:
            sent = False
            data = {"chat_id": self.chat_id, "text": chunk, "parse_mode": "HTML",
                    "disable_web_page_preview": True}
            for attempt in range(MAX_RETRIES + 1):
                try:
                    resp = self.session.post(url, json=data, timeout=REQUEST_TIMEOUT)
                    if resp.status_code == 200:
                        try:
                            if resp.json().get("ok") is True:
                                sent = True
                                break
                        except ValueError:
                            pass
                    if resp.status_code in {429, 500, 502, 503, 504} and attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                        continue
                    log.warning("telegram HTTP %s", resp.status_code)
                    break
                except requests.RequestException as exc:
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                        continue
                    log.warning("telegram network error: %s", exc)
                    break
                except Exception as exc:
                    log.warning("telegram error: %s", exc)
                    break
            if not sent:
                return False
        return True

    def test_connection(self) -> bool:
        return self.send("✅ <b>dhan-auto-trader connected</b>")

    @staticmethod
    def _escape(value) -> str:
        return html.escape(str(value))

    @staticmethod
    def _split_message(message: str) -> list[str]:
        text = str(message)
        if len(text) <= MAX_MESSAGE_LENGTH:
            return [text]
        return [text[i:i + MAX_MESSAGE_LENGTH] for i in range(0, len(text), MAX_MESSAGE_LENGTH)]

    # --------------------------------------------------------------
    # message builders
    # --------------------------------------------------------------
    def trade_entered(self, t: dict) -> bool:
        g = t.get("greeks", {})
        msg = (
            "📊 <b>TRADE ENTERED</b>\n\n"
            f"🎯 {self._escape(t.get('symbol', ''))} {self._escape(t.get('option_type', ''))} "
            f"{self._escape(t.get('strike', ''))}\n"
            f"📦 Qty: {t.get('qty')}  ·  Strategy: <b>{self._escape(t.get('strategy', ''))}</b>\n"
            f"💰 Entry: ₹{float(t.get('entry_price', 0)):,.2f}\n"
            f"🎯 Target: ₹{float(t.get('target_price', 0)):,.2f}  "
            f"🛑 SL: ₹{float(t.get('sl_price', 0)):,.2f}\n"
            f"Δ {g.get('delta', 0):.2f}  Γ {g.get('gamma', 0):.4f}  "
            f"Θ/day {g.get('theta', 0):.2f}  IV {g.get('iv', 0)*100:.1f}%"
        )
        if t.get("btst"):
            msg += "\n🌙 <b>BTST</b> - held overnight (MARGIN), exits next morning"
        return self.send(msg)

    def trade_exited(self, t: dict) -> bool:
        pnl = float(t.get('pnl', 0))
        emoji = "✅" if pnl >= 0 else "❌"
        msg = (
            f"{emoji} <b>TRADE EXITED</b>  "
            f"{self._escape(t.get('symbol', ''))} {self._escape(t.get('strike', ''))}\n"
            f"Reason: {self._escape(t.get('exit_reason', ''))}\n"
            f"P&L: <b>₹{pnl:+,.2f}</b>  (entry ₹{float(t.get('entry_price', 0)):,.2f} "
            f"→ exit ₹{float(t.get('exit_price', 0)):,.2f})"
        )
        return self.send(msg)

    def daily_summary(self, s: dict) -> bool:
        msg = (
            "📈 <b>DAILY SUMMARY</b>\n\n"
            f"📅 {self._escape(s.get('date', ''))}\n"
            f"💰 Equity: ₹{float(s.get('equity', 0)):,.0f}\n"
            f"📊 Day P&L: <b>₹{float(s.get('day_pnl', 0)):+,.0f}</b>\n"
            f"🏆 Trades: {s.get('trades', 0)}  (win {s.get('win_rate', 0)}%)\n"
            f"🎯 Month P&L: ₹{float(s.get('month_pnl', 0)):+,.0f}"
        )
        return self.send(msg)

    def risk_halt(self, reason: str) -> bool:
        return self.send(f"⛔ <b>RISK HALT</b>\n\n{self._escape(reason)}")

    def status(self, text: str) -> bool:
        return self.send("ℹ️ <b>STATUS</b>\n\n" + self._escape(text))

    def error(self, text: str) -> bool:
        return self.send("❌ <b>ERROR</b>\n\n" + self._escape(str(text))[:1000])


class TelegramCommander:
    """Long-polls Telegram for commands addressed to the configured chat."""

    def __init__(self, notifier: TelegramNotifier, handlers: dict[str, Callable[[], str]],
                 poll_interval_s: float = 3.0):
        self.notifier = notifier
        self.handlers = handlers
        self.poll_interval_s = poll_interval_s
        self._offset = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if not self.notifier.enabled:
            log.info("commander not started (notifier disabled)")
            return
        self._thread = threading.Thread(target=self._loop, name="telegram-commander",
                                        daemon=True)
        self._thread.start()
        log.info("telegram commander started")

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception as exc:
                log.warning("telegram poll error: %s", exc)
            self._stop.wait(self.poll_interval_s)

    def _poll_once(self):
        url = f"{TELEGRAM_API}/bot{self.notifier.bot_token}/getUpdates"
        params = {"offset": self._offset, "timeout": 25, "allowed_updates": ["message"]}
        try:
            resp = self.notifier.session.get(url, params=params, timeout=REQUEST_TIMEOUT + 20)
        except requests.RequestException:
            return
        if resp.status_code != 200:
            return
        try:
            updates = resp.json().get("result", [])
        except ValueError:
            return
        for upd in updates:
            self._offset = max(self._offset, int(upd.get("update_id", 0)) + 1)
            msg = upd.get("message", {})
            chat = str(msg.get("chat", {}).get("id", ""))
            if chat != self.notifier.chat_id:
                continue
            text = (msg.get("text") or "").strip()
            if not text.startswith("/"):
                continue
            cmd = text.split()[0].lower().lstrip("/").split("@")[0]
            handler = self.handlers.get(cmd)
            if handler:
                try:
                    reply = handler()
                    self.notifier.send(reply)
                except Exception as exc:
                    self.notifier.error(f"command {cmd}: {exc}")
            else:
                self.notifier.send("Unknown command. Try /help")

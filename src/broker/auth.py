"""DHAN authentication helpers: 24h tokens, RenewToken, API-key consent flow, TOTP.

Token resolution order used by the trader:
  1. DHAN_ACCESS_TOKEN in env (if not expired)
  2. DHAN_PIN + DHAN_TOTP_SECRET  -> programmatic TOTP+pin token (fully automated, 24h)
  3. DHAN_API_KEY + DHAN_SECRET    -> interactive OAuth consent flow (12-month credentials;
     run scripts/dhan_consent.py once per refresh to obtain a tokenId)

TOTP follows RFC 6238 with a base32 secret (same format as your authenticator app).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import struct
import time
import urllib.parse

import requests

log = logging.getLogger(__name__)

AUTH_BASE = "https://auth.dhan.co"
API_BASE = "https://api.dhan.co/v2"


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------
def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def token_expiry(token: str) -> float:
    """Return epoch seconds when a Dhan JWT expires (0 if unparseable)."""
    try:
        payload = token.split(".")[1]
        data = json.loads(_b64url_decode(payload))
        return float(data.get("exp", 0))
    except Exception:
        return 0.0


def token_is_expired(token: str, margin_s: float = 3600) -> bool:
    exp = token_expiry(token)
    return exp <= 0 or exp - time.time() < margin_s


# ---------------------------------------------------------------------------
# TOTP (RFC 6238, HMAC-SHA1, 30s window, base32 secret)
# ---------------------------------------------------------------------------
_B32_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def _b32decode(s: str) -> bytes:
    s = s.upper().strip().replace(" ", "").replace("-", "")
    bits = 0
    value = 0
    out = bytearray()
    for ch in s:
        idx = _B32_ALPHABET.find(ch)
        if idx < 0:
            continue
        value = (value << 5) | idx
        bits += 5
        if bits >= 8:
            bits -= 8
            out.append((value >> bits) & 0xFF)
    return bytes(out)


def totp(secret: str, for_time: float | None = None, digits: int = 6,
         period: int = 30, algorithm=hashlib.sha1) -> str:
    """RFC 6238 TOTP code for a base32 secret (as shown by authenticator apps)."""
    if for_time is None:
        for_time = time.time()
    counter = int(for_time // period)
    key = _b32decode(secret)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, algorithm).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


# ---------------------------------------------------------------------------
# Token flows
# ---------------------------------------------------------------------------
def generate_access_token(client_id: str, pin: str, totp_code: str) -> dict:
    """POST /app/generateAccessToken - programmatic 24h token (TOTP enabled required)."""
    url = f"{AUTH_BASE}/app/generateAccessToken"
    resp = requests.post(url, params={
        "dhanClientId": client_id, "pin": pin, "totp": totp_code,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()


def auto_token_from_totp(client_id: str, pin: str, totp_secret: str) -> str | None:
    """Generate a fresh access token using TOTP secret + pin. Returns token or None."""
    try:
        code = totp(totp_secret)
        data = generate_access_token(client_id, pin, code)
        token = data.get("accessToken", "")
        if token:
            log.info("access token generated via TOTP (exp %s)", data.get("expiryTime", "?"))
            return token
        log.warning("token generation returned no token: %s", data)
    except Exception as e:
        log.warning("TOTP token generation failed: %s", e)
    return None


def renew_token(client_id: str, token: str) -> str | None:
    """GET /v2/RenewToken - extend an ACTIVE token by 24h."""
    try:
        resp = requests.get(f"{API_BASE}/RenewToken",
                            headers={"access-token": token, "dhanClientId": client_id}, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            tok = data.get("accessToken", "")
            if tok:
                log.info("token renewed (exp %s)", data.get("expiryTime", "?"))
                return tok
        log.warning("renew failed: HTTP %s %s", resp.status_code, resp.text[:200])
    except Exception as e:
        log.warning("renew error: %s", e)
    return None


def api_key_consent(api_key: str, secret: str) -> dict:
    """POST /partner/generate-consent -> {consentId}. Browser login URL follows."""
    resp = requests.post(f"{AUTH_BASE}/partner/generate-consent",
                         headers={"partner_id": api_key, "partner_secret": secret}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def consume_consent(api_key: str, secret: str, token_id: str) -> dict:
    """POST /partner/consume-consent?tokenId=... -> {accessToken, expiryTime} (12-mo credentials)."""
    resp = requests.post(f"{AUTH_BASE}/partner/consume-consent",
                         params={"tokenId": token_id},
                         headers={"partner_id": api_key, "partner_secret": secret}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def consent_login_url(consent_id: str) -> str:
    return f"{AUTH_BASE}/login/consentApp-login?consentAppId={urllib.parse.quote(consent_id)}"


def resolve_token(client_id: str, access_token: str = "", pin: str = "",
                  totp_secret: str = "", api_key: str = "", api_secret: str = "") -> str:
    """Best-effort token resolution. Returns a usable token or ''."""
    if access_token and not token_is_expired(access_token):
        return access_token
    if access_token:
        renewed = renew_token(client_id, access_token)
        if renewed and not token_is_expired(renewed):
            return renewed
    if pin and totp_secret:
        tok = auto_token_from_totp(client_id, pin, totp_secret)
        if tok:
            return tok
    if api_key and api_secret:
        log.warning(
            "Access token expired and TOTP not configured. 12-month API key found - "
            "run once interactively:\n  python scripts/dhan_consent.py\n"
            "then the token is auto-saved to data/dhan_token.txt. "
            "For fully automatic refresh, set DHAN_PIN + DHAN_TOTP_SECRET.")
    return ""

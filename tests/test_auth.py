"""Tests for the DHAN auth helpers (TOTP RFC 6238, JWT expiry)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from broker import auth  # noqa: E402


def test_totp_rfc6238_sha1_vector():
    # RFC 6238 Appendix B test vector: ASCII secret "12345678901234567890",
    # T=59, 8 digits -> 94287082. Our base32 input decodes to those same bytes.
    secret_b32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert auth.totp(secret_b32, for_time=59, digits=8) == "94287082"


def test_totp_always_six_digits():
    secret = "JBSWY3DPEHPK3PXP"
    for t in range(0, 120, 7):
        code = auth.totp(secret, for_time=t)
        assert len(code) == 6 and code.isdigit()


def test_token_expiry_decode():
    import base64, json
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": 1787538842}).encode()).rstrip(b"=").decode()
    token = f"header.{payload}.sig"
    assert auth.token_expiry(token) == 1787538842.0
    assert auth.token_is_expired(token, margin_s=0) is False


def test_token_is_expired_for_garbage():
    assert auth.token_is_expired("not.a.jwt") is True


if __name__ == "__main__":
    for fn in [test_totp_rfc6238_sha1_vector, test_totp_always_six_digits,
               test_token_expiry_decode, test_token_is_expired_for_garbage]:
        fn()
        print("ok " + fn.__name__)
    print("ALL AUTH TESTS PASSED")

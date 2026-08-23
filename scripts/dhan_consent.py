"""Interactive DHAN API-key consent flow (12-month credentials).

Prints a login URL; after you log in and get redirected, paste the tokenId
from the redirect URL. The resulting access token is written to
data/dhan_token.txt (auto-loaded if DHAN_ACCESS_TOKEN is not set).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.broker import auth  # noqa: E402
from src.config import load_config  # noqa: E402


def main():
    cfg = load_config()
    api_key = cfg.get("dhan_api_key", "")
    api_secret = cfg.get("dhan_api_secret", "")
    if not api_key or not api_secret:
        print("API key/secret not found. Expected in C:/Athena_X/dhan API KKEY.txt or DHAN_API_KEY/DHAN_API_SECRET.")
        sys.exit(1)
    print(f"using API key: {api_key}")
    consent = auth.api_key_consent(api_key, api_secret)
    consent_id = consent.get("consentId") or consent.get("consent_id") or ""
    if not consent_id:
        print("consent failed:", consent)
        sys.exit(1)
    print("1. Open this URL in a browser and log in with your Dhan account:")
    print("   ", auth.consent_login_url(consent_id))
    print("2. After login you are redirected to a URL containing ?tokenId=...")
    token_id = input("   Paste the full redirect URL or just the tokenId: ").strip()
    if "tokenId=" in token_id:
        token_id = token_id.split("tokenId=")[-1].split("&")[0]
    data = auth.consume_consent(api_key, api_secret, token_id)
    token = data.get("accessToken", "")
    if not token:
        print("consume-consent failed:", data)
        sys.exit(1)
    out = Path("data/dhan_token.txt")
    out.parent.mkdir(exist_ok=True)
    out.write_text(token, encoding="utf-8")
    print("Token saved to", out, "- expires", data.get("expiryTime", "?"))


if __name__ == "__main__":
    main()

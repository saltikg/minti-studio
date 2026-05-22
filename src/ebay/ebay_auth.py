import os, time, requests
from typing import Optional
from dotenv import load_dotenv, find_dotenv
import base64, json

# ✅ .env'yi proje kökünden güvenli biçimde yükle
load_dotenv("/home/ubuntu/blog-factory/.env")

# 🔒 Bu uygulama için garantili çalışan public scope:
DEFAULT_SCOPES = "https://api.ebay.com/oauth/api_scope"
EBAY_SCOPES = (os.getenv("EBAY_SCOPES", DEFAULT_SCOPES)).strip()

# cache
_token: Optional[str] = None
_expiry: float = 0.0
_token_scopes: Optional[str] = None

def _assert_creds():
    if not os.getenv("EBAY_CLIENT_ID") or not os.getenv("EBAY_CLIENT_SECRET"):
        raise RuntimeError("EBAY_CLIENT_ID / EBAY_CLIENT_SECRET bulunamadı. .env doğru mu?")

def _request_token(scopes: str) -> requests.Response:
    """Her çağrıda env'den okumayı garantiler"""
    client_id = os.getenv("EBAY_CLIENT_ID")
    client_secret = os.getenv("EBAY_CLIENT_SECRET")
    return requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": scopes},
        auth=(client_id, client_secret),
        timeout=20
    )

def get_token(scopes: Optional[str] = None, force_refresh: bool = False) -> str:
    """Client Credentials ile token alır; cache'ler; süresi dolunca yeniler."""
    global _token, _expiry, _token_scopes
    _assert_creds()

    want_scopes = (scopes or EBAY_SCOPES).strip()
    now = time.time()

    if (not force_refresh) and _token and _token_scopes == want_scopes and now < _expiry - 60:
        return _token

    resp = _request_token(want_scopes)
    if resp.status_code == 200:
        data = resp.json()
        _token = data["access_token"]
        _expiry = now + float(data.get("expires_in", 0))
        _token_scopes = want_scopes
        return _token

    try:
        body = resp.json()
    except Exception:
        body = {"raw": resp.text}
    raise RuntimeError(f"eBay OAuth token alınamadı. status={resp.status_code}, body={body}")

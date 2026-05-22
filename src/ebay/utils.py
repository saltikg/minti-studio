import os
import requests
from base64 import b64encode
from dotenv import load_dotenv

# .env dosyasını yükle
load_dotenv()

CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")

def get_app_token(scope: str = "https://api.ebay.com/oauth/api_scope") -> str:
    """
    Ebay OAuth2 app token alır.
    Varsayılan scope: buy/browse için yeterlidir.
    """
    url = "https://api.ebay.com/identity/v1/oauth2/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": "Basic " + b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode(),
    }
    data = {
        "grant_type": "client_credentials",
        "scope": scope,
    }
    r = requests.post(url, headers=headers, data=data)
    r.raise_for_status()
    token = r.json()["access_token"]
    return token

if __name__ == "__main__":
    token = get_app_token()
    print("✅ Access Token:", token[:50] + "...")  # kısaltarak gösteriyoruz

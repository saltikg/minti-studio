import os
import requests
from dotenv import load_dotenv
from base64 import b64encode

load_dotenv()

CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")

def get_app_token():
    url = "https://api.ebay.com/identity/v1/oauth2/token"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": "Basic " + b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode(),
    }
    data = {
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }
    response = requests.post(url, headers=headers, data=data)
    if response.status_code == 200:
        token_data = response.json()
        print("✅ Access Token:")
        print(token_data["access_token"])
        print("\n⏳ Expires in (seconds):", token_data["expires_in"])
        return token_data["access_token"]
    else:
        print("❌ Error:", response.status_code, response.text)
        return None

if __name__ == "__main__":
    get_app_token()

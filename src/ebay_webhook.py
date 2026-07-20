from fastapi import FastAPI, Request
import os, hashlib
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()
VERIFICATION_TOKEN = os.getenv("EBAY_VERIFICATION_TOKEN")
ENDPOINT = "https://mintistudio.com/ebay/webhook"

@app.get("/ebay/webhook")
async def ebay_webhook_validate(challenge_code: str = None):
    if challenge_code:
        # SHA256 hash: challengeCode + verificationToken + endpoint
        m = hashlib.sha256()
        m.update(challenge_code.encode("utf-8"))
        m.update(VERIFICATION_TOKEN.encode("utf-8"))
        m.update(ENDPOINT.encode("utf-8"))
        response_hash = m.hexdigest()
        return {"challengeResponse": response_hash}
    return {"status": "ready"}

@app.post("/ebay/webhook")
async def ebay_webhook(request: Request):
    data = await request.json()
    print("Webhook event:", data)

    # Normal event geldiğinde token doğrulaması
    if data.get("verificationToken") != VERIFICATION_TOKEN:
        return {"error": "Invalid verification token"}

    return {"status": "ok"}

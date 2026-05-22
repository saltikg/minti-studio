from flask import Blueprint, request, jsonify
from datetime import datetime
import json

ebay_bp = Blueprint("ebay_bp", __name__)

@ebay_bp.route("/ebay/webhook", methods=["POST"])
def ebay_webhook():
    payload = request.get_json(silent=True) or {}
    try:
        with open("/home/ubuntu/blog-factory/logs/ebay_webhook.log", "a") as f:
            f.write(json.dumps({
                "ts": datetime.utcnow().isoformat(),
                "payload": payload
            }) + "\n")
    except Exception as e:
        # log yazamazsak da sorun değil, eBay'e yine 200 dönelim
        pass
    return jsonify({"status":"ok"}), 200

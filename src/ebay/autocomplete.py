import requests
import json
import re
from typing import List

def clean_response(text: str) -> str:
    """
    eBay autosug cevabını temizler: ( {...} ) veya /**/json({...})
    """
    t = text.strip()
    # /**/json( ... ) veya ( ... )
    t = re.sub(r'^[\s/\*]*json\(', '', t)   # baştaki /**/json( veya json( kaldır
    if t.startswith("(") and t.endswith(")"):
        t = t[1:-1]
    if t.endswith(")"):
        t = t[:-1]
    return t.strip()

def get_autocomplete(seed: str) -> List[str]:
    """
    eBay autocomplete API’den verilen seed için popüler keywordleri getirir.
    """
    url = "https://autosug.ebay.com/autosug"
    params = {"kwd": seed, "sId": 0, "pt": "gh", "callback": "json"}
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64) Chrome/120.0",
        "Accept": "*/*"
    }
    r = requests.get(url, params=params, headers=headers, timeout=10)
    r.raise_for_status()

    raw = clean_response(r.text)
    try:
        data = json.loads(raw)
        return data.get("res", {}).get("sug", [])
    except Exception as e:
        print("⚠️ Parse error:", e)
        print("Cleaned response snippet:", raw[:200])
        return []

if __name__ == "__main__":
    seeds = ["hallow", "pumpkin", "witch", "scary"]
    for s in seeds:
        print(f"\n🔮 Seed: {s}")
        kws = get_autocomplete(s)
        if not kws:
            print(" (no suggestions)")
        else:
            for kw in kws:
                print(" -", kw)

# app/utils_content.py
import math, re, json, unicodedata
from datetime import datetime, timezone

def _md_to_html_or_text(md_to_html, s: str) -> str:
    if not s: return ""
    return md_to_html(s) if md_to_html else s

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^a-z0-9\s]", "", s)
    return s

def strip_leading_title_from_md(md_text: str, title: str) -> str:
    if not md_text or not title: return md_text
    lines = md_text.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip(): i += 1
    if i >= len(lines): return md_text
    first = lines[i].strip()
    title_norm = _norm(title)
    m = re.match(r"^\s*#{1,6}\s+(.*)$", first)
    if m and _norm(m.group(1)) == title_norm:
        j = i + 1
        if j < len(lines) and not lines[j].strip(): j += 1
        return "\n".join(lines[:i] + lines[j:])
    if _norm(first) == title_norm:
        j = i + 1
        if j < len(lines) and re.match(r"^\s*(=+|-+)\s*$", lines[j]):
            k = j + 1
            if k < len(lines) and not lines[k].strip(): k += 1
            return "\n".join(lines[:i] + lines[k:])
    return md_text

def _eta_as_days(val):
    if val is None: return None
    if isinstance(val, (int, float)): return int(val)
    if isinstance(val, datetime):
        now = datetime.now(timezone.utc) if val.tzinfo else datetime.utcnow()
        return max(0, (val - now).days)
    if isinstance(val, str):
        s = val.strip()
        if s.isdigit(): return int(s)
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc) if dt.tzinfo else datetime.utcnow()
            return max(0, (dt - now).days)
        except Exception:
            return None
    return None

def _product_card_html_from_ebay(p):
    title = p.get("title") or "Product"
    img   = p.get("image") or "/static/img/placeholder.png"
    price = p.get("price")
    url   = p.get("url") or "#"
    price_txt = f"${price:,.2f}" if isinstance(price, (int,float)) else (str(price) if price else "")
    return f"""<div style="border:1px solid #eee;padding:10px;margin:15px 5px;text-align:center;max-width:200px;display:inline-block;vertical-align:top;box-shadow:0 2px 4px rgba(0,0,0,0.1);border-radius:5px;">
  <img src="{img}" alt="{title}" style="max-width:100%;height:150px;object-fit:cover;display:block;margin:0 auto 10px;border-radius:3px;">
  <h4 style="font-size:0.9em;margin:0 0 10px;line-height:1.2;font-weight:normal;height:4.5em;overflow:hidden;">{title}</h4>
  <p style="font-weight:bold;color:#e53935;font-size:1.1em;margin:0 0 10px;">{price_txt}</p>
  <a href="{url}" target="_blank" rel="noopener sponsored" style="display:inline-block;padding:8px 12px;background-color:#3665f3;color:#fff;text-decoration:none;border-radius:20px;font-size:0.9em;font-weight:bold;">View on eBay</a>
</div>"""

def _inject_product_cards_into_text(txt: str, ebay_products: list):
    if not txt: return txt
    cards = [_product_card_html_from_ebay(p) for p in (ebay_products or [])[:5]]
    for i, html in enumerate(cards, start=1):
        for pat in (rf"\{{\{{\s*PRODUCT_CARD_{i}\s*\}}\}}", rf"\{{\s*PRODUCT_CARD_{i}\s*\}}"):
            if re.search(pat, txt):
                txt = re.sub(pat, html, txt, count=1)
                break
        else:
            txt += f"\n\n{html}\n"
    return txt

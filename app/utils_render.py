# app/utils_render.py
import json
from html import escape
from typing import List, Dict, Any, Optional

def _coerce_list(val) -> List[dict]:
    """list/dict ise aynen döndür, string ise JSON parse dener; olmazsa []"""
    if val is None:
        return []
    if isinstance(val, list):
        return val
    if isinstance(val, str):
        try:
            data = json.loads(val)
            return data if isinstance(data, list) else []
        except Exception:
            return []
    return []

def render_buyers_guide(items: Any) -> Optional[str]:
    """[{title, desc}] → semantik ve temiz HTML. JSON yoksa None döner."""
    rows = _coerce_list(items)
    if not rows:
        return None

    li_html = []
    for i, it in enumerate(rows, 1):
        t = (it.get("title") or "").strip()
        d = (it.get("desc")  or "").strip()
        if not t and not d:
            continue
        # başlıktaki gereksiz numara/markdown kalıntılarını nazikçe temizle
        if t.startswith(("**", "*")) and t.endswith(("**", "*")):
            t = t.strip("* ").strip()
        t = t.lstrip("0123456789). -–").strip()
        block = f"""
        <li>
          <div class="bg-item">
            <div class="bg-title"><strong>{escape(t)}</strong></div>
            <div class="bg-desc">{d}</div>
          </div>
        </li>
        """
        li_html.append(block)

    return f"""
<section class="buyers-guide" aria-label="Buyer’s Guide">
  <h2>Buyer&apos;s Guide</h2>
  <ol class="bg-list">
    {''.join(li_html)}
  </ol>
</section>
""".strip()

def render_faq(items: Any) -> Optional[str]:
    """[{q, a}] → HTML. JSON yoksa None döner."""
    rows = _coerce_list(items)
    if not rows:
        return None

    acc = []
    for i, it in enumerate(rows, 1):
        q = (it.get("q") or "").strip()
        a = (it.get("a") or "").strip()
        if not q and not a:
            continue
        # baştan **1. ...** gibi işaretleri temizle
        q = q.strip("* ").lstrip("0123456789). -–").strip()
        acc.append(f"""
        <details class="faq-item">
          <summary><strong>{escape(q)}</strong></summary>
          <div class="faq-answer">{a}</div>
        </details>
        """)

    return f"""
<section class="faq" aria-label="FAQ">
  <h2>FAQ</h2>
  {''.join(acc)}
</section>
""".strip()

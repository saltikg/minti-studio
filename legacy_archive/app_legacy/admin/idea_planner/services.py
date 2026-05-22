from datetime import date
from openai import OpenAI
import os

from app.db import connect_ro, connect_rw

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

client = OpenAI(api_key=OPENAI_API_KEY)


def get_db(read_only=True):
    return connect_ro() if read_only else connect_rw()


# ---------------------------------------------------------------------
# 🔹 1️⃣ Trend Collector
# ---------------------------------------------------------------------
def trend_collector():
    con = get_db(read_only=False)
    data = [
        ("Taylor Swift outfit", "Google", "+980%", str(date.today())),
        ("Red Panda plush toy", "eBay", "+420%", str(date.today())),
        ("Winter candle scents", "YouTube", "+310%", str(date.today())),
    ]
    con.executemany(
        "INSERT INTO trend_feed_snapshot (topic, source, change_pct, date) VALUES (?, ?, ?, ?)",
        data
    )
    con.commit()
    con.close()
    return [dict(topic=r[0], source=r[1], change=r[2], date=r[3]) for r in data]


# ---------------------------------------------------------------------
# 🔹 2️⃣ Context Hazırlığı
# ---------------------------------------------------------------------
def planner_context():
    con = get_db()
    seasons = con.execute("SELECT name, theme, start_date, end_date FROM seasons ORDER BY start_date").fetchall()
    trends = con.execute("""
        SELECT topic, source, change_pct 
        FROM trend_feed_snapshot 
        ORDER BY date DESC LIMIT 20
    """).fetchall()
    brands = con.execute("SELECT category, brand FROM sd_brand_categories LIMIT 20").fetchall()
    con.close()

    return {
        "seasons": seasons,
        "trends": trends,
        "brands": brands,
    }


# ---------------------------------------------------------------------
# 🔹 3️⃣ LLM Çağrısı (LangChain / OpenAI)
# ---------------------------------------------------------------------
def planner_llm(context):
    prompt = f"""
    You are an e-commerce content planner.
    Based on the following context, generate 3 blog ideas for today's content plan.
    Context:
    - Seasons: {context['seasons']}
    - Trends: {context['trends']}
    - Brands: {context['brands']}

    Return as JSON list with fields:
    [{{"title": "...", "category": "...", "brand": "...", "season": "...", "reasoning": "...", "publish_date": "...", "risk": 0.0}}]
    """

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "system", "content": "You are a content planner assistant."},
                  {"role": "user", "content": prompt}],
        temperature=0.7
    )

    import json
    try:
        return json.loads(response.choices[0].message.content)
    except Exception:
        return [{"title": "Fallback Example", "category": "General", "brand": "Example", "season": "none", "reasoning": "Example reasoning", "publish_date": str(date.today()), "risk": 0.5}]


# ---------------------------------------------------------------------
# 🔹 4️⃣ Kaydet DB’ye
# ---------------------------------------------------------------------
def planner_storage(ideas):
    con = get_db(read_only=False)
    for idea in ideas:
        con.execute("""
            INSERT INTO content_plan (title, category, brand, season, reasoning, publish_date, risk, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, [
            idea.get("title"), idea.get("category"), idea.get("brand"),
            idea.get("season"), idea.get("reasoning"),
            idea.get("publish_date"), idea.get("risk")
        ])
    con.commit()
    con.close()
    return {"message": f"{len(ideas)} fikir başarıyla kaydedildi."}

from pytrends.request import TrendReq
import sys

pytrends = TrendReq(hl="en-US", tz=360)

def top_related_queries(keyword, cat_id=0, geo="", timeframe="today 12-m", top_k=10):
    try:
        pytrends.build_payload([keyword], cat=cat_id, timeframe=timeframe, geo=geo)
        related = pytrends.related_queries()

        if keyword in related and related[keyword]["top"] is not None:
            df = related[keyword]["top"].head(top_k)[["query", "value"]]
            print("\n🔥 Related Queries:")
            print(df.to_string(index=False))
            return True
        else:
            return False
    except Exception as e:
        print(f"⚠️ Error: {e}")
        return False

def fallback_interest_over_time(keyword, cat_id=0, geo="", timeframe="today 12-m"):
    try:
        pytrends.build_payload([keyword], cat=cat_id, timeframe=timeframe, geo=geo)
        data = pytrends.interest_over_time()
        if not data.empty:
            print("\n📈 Interest Over Time (last values):")
            print(data[[keyword]].tail(10).to_string())
        else:
            print("⚠️ No trend data available.")
    except Exception as e:
        print(f"⚠️ Error in fallback: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python trends_tester.py <keyword> <category_id>")
        sys.exit(1)

    kw = sys.argv[1]
    cat_id = int(sys.argv[2])

    print(f"\n🔎 Testing keyword='{kw}' | category_id={cat_id}")
    has_related = top_related_queries(kw, cat_id)

    if not has_related:
        print("\n⚠️ No related queries found, trying fallback...")
        fallback_interest_over_time(kw, cat_id)

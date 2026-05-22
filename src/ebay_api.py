import os
import requests
import csv
from dotenv import load_dotenv
from base64 import b64encode

load_dotenv()

CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")

# -------------------- AUTH --------------------
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
    response.raise_for_status()
    return response.json()["access_token"]

# -------------------- TAXONOMY --------------------
def get_default_category_tree(token):
    url = "https://api.ebay.com/commerce/taxonomy/v1/get_default_category_tree_id?marketplace_id=EBAY_US"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json()

def search_category(token, tree_id="0", keyword="halloween"):
    url = f"https://api.ebay.com/commerce/taxonomy/v1/category_tree/{tree_id}/get_category_suggestions?q={keyword}"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json()

# -------------------- BROWSE --------------------
def get_items_by_category(token, category_id, limit=10):
    url = f"https://api.ebay.com/buy/browse/v1/item_summary/search?category_ids={category_id}&limit={limit}"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers)
    r.raise_for_status()
    return r.json()

# -------------------- SAVE TO CSV --------------------
def save_items_to_csv(items, filename="ebay_results.csv"):
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["title", "price", "currency", "url"])
        for i in items.get("itemSummaries", []):
            writer.writerow([
                i.get("title", ""),
                i.get("price", {}).get("value", ""),
                i.get("price", {}).get("currency", ""),
                i.get("itemWebUrl", "")
            ])
    print(f"💾 Results saved to {filename}")

# -------------------- MAIN --------------------
if __name__ == "__main__":
    token = get_app_token()

    # 1. Default category tree id al
    default_tree = get_default_category_tree(token)
    tree_id = default_tree["categoryTreeId"]
    print("✅ Default category tree:", default_tree)

    # 2. Keyword ile kategorileri bul
    keyword = input("\nAramak istediğin kategori kelimesi (örn: halloween): ") or "halloween"
    suggestions = search_category(token, tree_id, keyword)

    if not suggestions.get("categorySuggestions"):
        print("❌ Hiç kategori bulunamadı.")
        exit()

    print("\n📂 Category suggestions:")
    for idx, s in enumerate(suggestions["categorySuggestions"], start=1):
        cat = s["category"]
        print(f"{idx}. {cat['categoryName']} (ID={cat['categoryId']})")

    # 3. Kullanıcıya kategori seçtir
    choice = int(input("\nKategori seç (numara): "))
    chosen_cat = suggestions["categorySuggestions"][choice-1]["category"]
    print(f"\n👉 Seçilen kategori: {chosen_cat['categoryName']} (ID={chosen_cat['categoryId']})")

    # 4. Ürünleri getir
    items = get_items_by_category(token, chosen_cat["categoryId"], limit=10)
    print("\n🛒 Sample items:")
    for i in items.get("itemSummaries", []):
        print(f"- {i['title']} | {i['price']['value']} {i['price']['currency']} | {i['itemWebUrl']}")

    # 5. CSV’ye kaydet
    save_items_to_csv(items)

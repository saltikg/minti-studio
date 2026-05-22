from .autocomplete import get_autocomplete
from .browse import get_top_products

BASE_SEEDS = ["hallow", "pumpkin", "witch", "scary"]

def expand_keywords(seeds):
    """
    İlk seedlerden autocomplete sonuçlarını toplar,
    niş keywordleri yeni seed listesine ekler.
    """
    seen = set()
    new_seeds = []

    for s in seeds:
        suggestions = get_autocomplete(s)
        for kw in suggestions:
            seen.add(kw)
            # Ana kelimelerden başlıyorsa atla
            if kw.startswith(("halloween", "pumpkin", "witch", "scary")):
                # sadece çok kelimeli olanlar niş olarak eklenebilir
                if len(kw.split()) > 2:
                    new_seeds.append(kw)
            else:
                # diğer özel kelimeleri ekle
                if len(kw.split()) > 1:
                    new_seeds.append(kw)
    return sorted(set(new_seeds)), sorted(seen)

if __name__ == "__main__":
    print("🔎 Expanding keywords...")
    extra_seeds, all_keywords = expand_keywords(BASE_SEEDS)

    print("\n✨ Autocomplete ALL keywords:")
    for kw in all_keywords:
        print(" -", kw)

    print("\n🌱 New niche seeds to explore:")
    for kw in extra_seeds:
        print(" -", kw)

    # Deneme: ilk 2 niş keyword için ürün çek
    for kw in extra_seeds[:2]:
        print(f"\n🛒 Top products for: {kw}")
        products = get_top_products(kw, limit=5)
        for p in products:
            print("-", p["title"], "|", p["price"], "|", p["url"])

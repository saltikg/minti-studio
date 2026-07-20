import requests
from bs4 import BeautifulSoup
from datetime import datetime
from pathlib import Path

SITEMAP_URL = "https://mintistudio.com/sitemap.xml"
OUTPUT_FILE = Path("/home/ubuntu/blog-factory/sitemap_clean.xml")

def check_url_status(url):
    try:
        r = requests.get(url, allow_redirects=True, timeout=15, stream=True)
        status = r.status_code
        # Bazı CDN'ler 403 veriyor ama sayfa içerik doluysa kabul edelim
        if 200 <= status < 300:
            return True
        # redirect (301/302) varsa, hedefe ulaştıysa yine kabul edelim
        if status in (301, 302) and 'Location' in r.headers:
            redirected = r.headers['Location']
            if redirected.startswith("https://mintistudio.com"):
                return True
        return False
    except requests.RequestException:
        return False

def clean_sitemap():
    print(f"🔍 Reading sitemap: {SITEMAP_URL}")
    xml = requests.get(SITEMAP_URL, timeout=20).text
    soup = BeautifulSoup(xml, "xml")
    urls = soup.find_all("url")
    print(f"Found {len(urls)} URLs.")

    valid_urls = []
    for u in urls:
        loc = u.find("loc").text.strip()
        if check_url_status(loc):
            valid_urls.append(u)
            print(f"✅ OK {loc}")
        else:
            print(f"❌ INVALID {loc}")

    now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    new_xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    new_xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    for u in valid_urls:
        loc = u.find("loc").text
        lastmod = u.find("lastmod").text if u.find("lastmod") else now
        new_xml += f"  <url>\n    <loc>{loc}</loc>\n    <lastmod>{lastmod}</lastmod>\n  </url>\n"
    new_xml += "</urlset>"

    OUTPUT_FILE.write_text(new_xml, encoding="utf-8")
    print(f"✅ Clean sitemap written to {OUTPUT_FILE}")
    print(f"Total kept: {len(valid_urls)} / {len(urls)}")

if __name__ == "__main__":
    clean_sitemap()

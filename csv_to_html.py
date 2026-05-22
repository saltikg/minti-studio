import csv
import argparse
from pathlib import Path

def csv_to_html(csv_file: str, output_file: str):
    rows = []
    with open(csv_file, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    html_rows = []
    for r in rows:
        html_rows.append(f"""
        <tr>
            <td>{r['season']}</td>
            <td>{r['keyword']}</td>
            <td>{r['title']}</td>
            <td>{r['price']}</td>
            <td>{r['brand']}</td>
            <td><a href="{r['url']}" target="_blank">Link</a></td>
            <td><img src="{r['image']}" alt="img" width="80"></td>
        </tr>
        """)

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>eBay Product List</title>
        <style>
            body {{ font-family: Arial, sans-serif; }}
            table {{ border-collapse: collapse; width: 100%; }}
            th, td {{ border: 1px solid #ddd; padding: 8px; }}
            th {{ background-color: #f2f2f2; }}
            img {{ max-height: 80px; }}
        </style>
    </head>
    <body>
        <h1>eBay Product List from {csv_file}</h1>
        <table>
            <thead>
                <tr>
                    <th>Season</th>
                    <th>Keyword</th>
                    <th>Title</th>
                    <th>Price</th>
                    <th>Brand</th>
                    <th>URL</th>
                    <th>Image</th>
                </tr>
            </thead>
            <tbody>
                {''.join(html_rows)}
            </tbody>
        </table>
    </body>
    </html>
    """

    Path(output_file).write_text(html_content, encoding='utf-8')
    print(f"✅ HTML file generated: {output_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, help="Input CSV file")
    parser.add_argument("--output", required=True, help="Output HTML file path")
    args = parser.parse_args()

    csv_to_html(args.csv, args.output)

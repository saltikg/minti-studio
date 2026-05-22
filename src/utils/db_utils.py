import duckdb
from typing import Optional, List, Dict


class DuckDBHelper:
    """
    Centralized DuckDB helper for the Minti orchestration system.
    Handles season lookup, content duplication checks, and product retrieval.
    """

    def __init__(self, db_path: str = "/home/ubuntu/blog-factory/warehouse/blog_factory.duckdb"):
        self.db_path = db_path
        self.con = duckdb.connect(database=db_path, read_only=False)

    # -----------------------
    # 📅 SEASON HELPERS
    # -----------------------

    def get_current_season(self) -> Optional[str]:
        """
        Returns the most recently created season name.
        """
        query = """
            SELECT season_name
            FROM seasons
            ORDER BY created_at DESC
            LIMIT 1
        """
        result = self.con.execute(query).fetchone()
        return result[0] if result else None

    def get_recent_seasons(self, limit: int = 3) -> List[str]:
        query = f"""
            SELECT season_name
            FROM seasons
            ORDER BY created_at DESC
            LIMIT {limit}
        """
        return [row[0] for row in self.con.execute(query).fetchall()]

    # -----------------------
    # 🧠 CONTENT VALIDATION
    # -----------------------

    def check_existing_content(self, keyword: str, season: Optional[str] = None) -> bool:
        """
        Checks if a blog already exists for a given keyword (trend/category).
        Joins blog_posts and trend_ideas on idea_id.
        """
        query = f"""
            SELECT COUNT(*)
            FROM blog_posts b
            JOIN trend_ideas t
              ON CAST(b.idea_id AS BIGINT) = t.idea_id
            WHERE (
                t.category_slug ILIKE '%{keyword}%'
                OR t.title ILIKE '%{keyword}%'
            )
              AND (b.status IS NULL OR b.status != 'draft')
        """
        result = self.con.execute(query).fetchone()[0]
        return result > 0

    # -----------------------
    # 🛍️ PRODUCT RETRIEVAL
    # -----------------------

    def get_products_by_category(self, category: str, limit: int = 5) -> List[Dict]:
        """
        Retrieves products related to a given category by joining:
        products + product_media + idea_products.
        Returns product_title, brand, price, image_url.
        """
        query = f"""
            SELECT 
                p.product_title AS title,
                p.brand,
                p.price,
                p.category_slug,
                m.image_url
            FROM products p
            LEFT JOIN product_media m 
              ON p.parent_asin = m.parent_asin
            LEFT JOIN idea_products i 
              ON p.parent_asin = i.parent_asin
            WHERE p.category_slug ILIKE '%{category}%'
            ORDER BY RANDOM()
            LIMIT {limit}
        """
        df = self.con.execute(query).fetchdf()
        return df.to_dict(orient="records")

    # -----------------------
    # 🧹 CLEANUP
    # -----------------------

    def close(self):
        if self.con:
            self.con.close()

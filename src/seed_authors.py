import duckdb
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "warehouse", "blog_factory.duckdb")

AUTHORS_DATA = [
    {
        "author_id": "steph-bazzle",
        "author_name": "Steph Bazzle",
        "author_avatar_url": "/static/authors/steph-bazzle.png",
        "author_bio": "Steph is a former consumer goods analyst who now dedicates her time to testing and reviewing products that make everyday life better. When she's not benchmarking kitchen gadgets or testing smart home devices, she's usually hiking with her two golden retrievers."
    },
    {
        "author_id": "alex-chen",
        "author_name": "Alex Chen",
        "author_avatar_url": "/static/authors/alex-chen.png",
        "author_bio": "Alex has a background in mechanical engineering and a passion for DIY projects. He loves taking things apart to see how they work, making him our resident expert on tools, home improvement, and automotive gear. He lives in a self-renovated barn with his family."
    },
    {
        "author_id": "maria-garcia",
        "author_name": "Maria Garcia",
        "author_avatar_url": "/static/authors/maria-garcia.png",
        "author_bio": "With a degree in kinesiology and over a decade of experience as a personal trainer, Maria is our go-to source for all things health, fitness, and outdoors. She believes the right gear can be a powerful motivator for an active lifestyle."
    },
    {
        "author_id": "david-lee",
        "author_name": "David Lee",
        "author_avatar_url": "/static/authors/david-lee.png",
        "author_bio": "David is a tech journalist who has been covering the consumer electronics beat for over 15 years. From the latest smartphones to cutting-edge gaming rigs, he provides insightful, no-nonsense reviews to help you navigate the fast-paced world of technology."
    },
    {
        "author_id": "emily-sato",
        "author_name": "Emily Sato",
        "author_avatar_url": "/static/authors/emily-sato.png",
        "author_bio": "Emily is a professional chef and food blogger with a love for beautiful, functional kitchenware. Her reviews are informed by countless hours of professional use, focusing on performance, durability, and value for the home cook."
    },
    {
        "author_id": "ben-carter",
        "author_name": "Ben Carter",
        "author_avatar_url": "/static/authors/ben-carter.png",
        "author_bio": "As a father of three, Ben understands the importance of finding products that are safe, durable, and genuinely useful for a busy family. He specializes in toys, baby gear, and family-friendly home goods."
    },
    {
        "author_id": "chloe-kim",
        "author_name": "Chloe Kim",
        "author_avatar_url": "/static/authors/chloe-kim.png",
        "author_bio": "Chloe is a landscape designer and avid gardener. Her expertise lies in outdoor tools, garden equipment, and patio furniture. She helps readers create beautiful and functional outdoor living spaces."
    },
    {
        "author_id": "james-owen",
        "author_name": "James Owen",
        "author_avatar_url": "/static/authors/james-owen.png",
        "author_bio": "James is a sound engineer and audiophile who is obsessed with finding the best audio equipment on the market. From high-fidelity headphones to home theater systems, his reviews are meticulous and technically detailed."
    },
    {
        "author_id": "sophia-rodriguez",
        "author_name": "Sophia Rodriguez",
        "author_avatar_url": "/static/authors/sophia-rodriguez.png",
        "author_bio": "Sophia is a professional organizer and home stylist. She reviews storage solutions, home decor, and cleaning products with an eye for both form and function, helping people create calm and organized living environments."
    },
    {
        "author_id": "liam-wilson",
        "author_name": "Liam Wilson",
        "author_avatar_url": "/static/authors/liam-wilson.png",
        "author_bio": "An avid traveler and photographer, Liam tests and reviews travel gear, cameras, and accessories. He focuses on portability, durability, and performance for adventurers on the go."
    }
]

def seed_authors():
    con = duckdb.connect(DB_PATH)
    try:
        # Create table with the new author_bio column
        con.execute("""
        CREATE TABLE IF NOT EXISTS authors (
            author_id         VARCHAR PRIMARY KEY,
            display_name      VARCHAR NOT NULL,
            avatar_url        VARCHAR,
            author_bio        TEXT
        )
        """)

        # Add the new column if it doesn't exist
        try:
            con.execute("ALTER TABLE authors ADD COLUMN IF NOT EXISTS author_bio TEXT;")
            print("✅ Added 'author_bio' column to authors table.")
        except duckdb.CatalogException:
            pass # Column already exists

        # Upsert authors
        for author in AUTHORS_DATA:
            con.execute("""
                INSERT INTO authors (author_id, display_name, avatar_url, author_bio)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (author_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    avatar_url   = EXCLUDED.avatar_url,
                    author_bio = EXCLUDED.author_bio
            """, [author['author_id'], author['author_name'], author['author_avatar_url'], author['author_bio']])
        
        print(f"✅ Successfully seeded {len(AUTHORS_DATA)} authors into the database.")

    finally:
        con.close()

if __name__ == "__main__":
    seed_authors()
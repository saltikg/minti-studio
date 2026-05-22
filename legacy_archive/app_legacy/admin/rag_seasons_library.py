# app/admin/rag_seasons_library.py

CANONICAL_FAMILIES = [
    "valentine", "easter", "mothers-day", "fathers-day",
    "back-to-school", "halloween", "thanksgiving",
    "black-friday", "cyber-week", "christmas",
    "new-year", "small-business-saturday", "super-saturday",
    "green-monday", "boxing-day", "hanukkah"
]


EVERGREEN_RULES = [
    {
        "name": "valentine",
        "window": "Feb 1 to Feb 14",
        "locale": "US",
        "seeds": ["valentine gifts", "flowers", "chocolate", "jewelry", "date night"],
        "themes": ["gift", "romance", "budget"],
        "types": ["deal", "guide", "list"]
    },
    {
        "name": "easter",
        "window": "Moveable feast in March or April",
        "locale": "US",
        "seeds": ["easter eggs", "baskets", "decor", "brunch"],
        "themes": ["family", "decor", "craft"],
        "types": ["deal", "roundup", "how-to"]
    },
    {
        "name": "mothers-day",
        "window": "Second Sunday of May",
        "locale": "US",
        "seeds": ["gifts", "spa", "flowers", "jewelry", "personalized"],
        "themes": ["gift", "sentimental"],
        "types": ["deal", "guide", "list"]
    },
    {
        "name": "fathers-day",
        "window": "Third Sunday of June",
        "locale": "US",
        "seeds": ["gadgets", "grilling", "tools", "sports", "wallet"],
        "themes": ["gift", "outdoor"],
        "types": ["deal", "guide", "roundup"]
    },
    {
        "name": "back-to-school",
        "window": "July 15 to September 15",
        "locale": "US",
        "seeds": ["backpacks", "laptops", "notebooks", "planners", "dorm"],
        "themes": ["study", "budget"],
        "types": ["deal", "guide", "list"]
    },
    {
        "name": "halloween",
        "window": "Oct 1 to Oct 31",
        "locale": "US",
        "seeds": ["costumes", "decor", "pumpkins", "party", "candy"],
        "themes": ["spooky", "diy"],
        "types": ["deal", "roundup", "how-to"]
    },
    {
        "name": "thanksgiving",
        "window": "Fourth Thursday of November",
        "locale": "US",
        "seeds": ["turkey", "cookware", "table decor", "family"],
        "themes": ["family", "kitchen"],
        "types": ["guide", "list", "how-to"]
    },
    {
        "name": "black-friday",
        "window": "Day after Thanksgiving",
        "locale": "US",
        "seeds": ["doorbusters", "tv deals", "laptops", "toys"],
        "themes": ["deal", "electronics"],
        "types": ["deal", "roundup", "tracker"]
    },
    {
        "name": "cyber-week",
        "window": "Black Friday to Cyber Monday",
        "locale": "US",
        "seeds": ["online deals", "promo codes", "bundles", "returns"],
        "themes": ["deal", "online"],
        "types": ["deal", "roundup", "tracker"]
    },
    {
        "name": "christmas",
        "window": "Dec 1 to Dec 26",
        "locale": "US",
        "seeds": ["gift guide", "stocking stuffers", "tree decor", "ugly sweaters"],
        "themes": ["gift", "decor", "family"],
        "types": ["deal", "guide", "how-to"]
    },
    {
         "name": "new-year",
         "window": "Dec 26 to Jan 3",
         "locale": "US",
         "seeds": ["party", "resolutions", "fitness", "planner"],
         "themes": ["celebration", "fresh-start"],
         "types": ["roundup", "guide", "deal"]
     },
]

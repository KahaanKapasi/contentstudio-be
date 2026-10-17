"""Footonomy workspace: football-wide Discovery. DATA ONLY: edit the constants below.

The base football-wide feeds live in `rss.DEFAULT_FEEDS`; EXTRA_FEEDS are added on top of them.

Contract (checked by tests/test_workspaces.py, live-checked by `python scripts/check_sources.py footonomy`):
- EXTRA_FEEDS: tuple of (unique_name, http(s) RSS/Atom url) that return entries with the honest
  `ContentStudio/1.0 (RSS reader)` User-Agent and do not duplicate a name in rss.DEFAULT_FEEDS.
  Keep DEFAULT_FEEDS + EXTRA_FEEDS at <= 20 so a scrape stays fast.
- X_QUERY: an X API v2 recent-search query (<= 480 characters). Each post read costs money.
- TOPIC_FOCUS: one paragraph injected into the Gemini topic prompt.
"""

LABEL = "Footonomy"
DESCRIPTION = "Football-wide: top leagues, transfers, tactics, tournaments and global storylines."

EXTRA_FEEDS: tuple[tuple[str, str], ...] = (
    ("transfermarkt_news", "https://www.transfermarkt.com/rss/news"),
    ("football365", "https://www.football365.com/rss"),
    ("google_news_soccer_transfers", "https://news.google.com/rss/search?q=soccer+transfer+news+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("google_news_world_cup", "https://news.google.com/rss/search?q=World+Cup+football+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("google_news_champions_league", "https://news.google.com/rss/search?q=Champions+League+when:1d&hl=en-US&gl=US&ceid=US:en"),
    ("guardian_womens_football", "https://www.theguardian.com/football/womensfootball/rss"),
    ("football_italia", "https://www.football-italia.net/feed"),
    ("google_news_soccer_viral", "https://news.google.com/rss/search?q=soccer+viral+when:1d&hl=en-US&gl=US&ceid=US:en"),
)

X_QUERY = "(from:FabrizioRomano OR from:DiMarzio OR from:OptaJoe OR from:ChampionsLeague OR from:FIFAWorldCup OR from:ESPNFC) -is:retweet"

TOPIC_FOCUS = (
    "Footonomy, a football-wide content brand covering the whole sport: transfers and confirmed deals, tactics and "
    "coaching ideas, international and tournament football (World Cup, Champions League, national teams), women's "
    "and youth football, and viral moments with cross-club appeal. Prefer stories a fan of any club would click on "
    "over club-specific minutiae."
)

"""Madridonomy workspace: Real Madrid-only Discovery. DATA ONLY: edit the constants below.

Contract (checked by tests/test_workspaces.py, live-checked by `python scripts/check_sources.py madridonomy`):
- FEEDS: tuple of (unique_name, http(s) RSS/Atom url). Every feed must return entries when fetched with the
  honest `ContentStudio/1.0 (RSS reader)` User-Agent. Prefer sites behind well-known Madrid X accounts
  (their RSS is free; the X API charges per post read). Keep the total at <= 14 so a scrape stays fast.
- X_QUERY: an X API v2 recent-search query (<= 480 characters), e.g. `(from:a OR from:b) -is:retweet`.
  Each post read costs money, so keep it focused on the best accounts/keywords.
- TOPIC_FOCUS: one paragraph injected into the Gemini topic prompt to keep every topic Real Madrid-relevant.
"""

LABEL = "Madridonomy"
DESCRIPTION = "Real Madrid only: club news, matches, transfers, tactics and storylines."

FEEDS: tuple[tuple[str, str], ...] = (
    ("madridxtra", "https://madridxtra.com/rss.xml"),
    ("madrid_universal", "https://madriduniversal.com/feed"),
    ("managing_madrid", "https://www.managingmadrid.com/rss/index.xml"),
    ("marca_real_madrid", "https://e00-marca.uecdn.es/rss/futbol/real-madrid.xml"),
    ("marca_en_real_madrid", "https://www.marca.com/en/rss/googlenews/organization/real-madrid.xml"),
    (
        "as_real_madrid",
        "https://news.google.com/rss/search?q=site:as.com+%22Real+Madrid%22+when:2d&hl=es&gl=ES&ceid=ES:es",
    ),
    (
        "mundo_deportivo_real_madrid",
        "https://news.google.com/rss/search?q=site:mundodeportivo.com+%22Real+Madrid%22+when:2d&hl=es&gl=ES&ceid=ES:es",
    ),
    (
        "defensa_central_real_madrid",
        "https://news.google.com/rss/search?q=site:defensacentral.com+%22Real+Madrid%22+when:7d&hl=es&gl=ES&ceid=ES:es",
    ),
    ("reddit_realmadrid", "https://www.reddit.com/r/realmadrid/top/.rss?t=day"),
    ("realmadrid_youtube", "https://www.youtube.com/feeds/videos.xml?channel_id=UCWV3obpZVGgJ3j9FVhEjF2Q"),
    (
        "realmadrid_official_news",
        "https://news.google.com/rss/search?q=site:realmadrid.com+when:7d&hl=en-US&gl=US&ceid=US:en",
    ),
    (
        "google_news_real_madrid_en",
        "https://news.google.com/rss/search?q=%22Real+Madrid%22+when:1d&hl=en-US&gl=US&ceid=US:en",
    ),
    (
        "google_news_real_madrid_fichajes_es",
        "https://news.google.com/rss/search?q=%22Real+Madrid%22+fichajes+when:1d&hl=es&gl=ES&ceid=ES:es",
    ),
    (
        "google_news_real_madrid_analysis_en",
        "https://news.google.com/rss/search?q=%22Real+Madrid%22+tactics+analysis+when:2d&hl=en-GB&gl=GB&ceid=GB:en",
    ),
)

X_QUERY = (
    "(from:MadridXtra OR from:themadridzone OR from:managingmadrid OR from:marca OR from:marcainenglish "
    "OR from:diarioas OR from:realmadrid) -is:retweet"
)

TOPIC_FOCUS = (
    "Madridonomy, a Real Madrid-focused football content brand. Every topic must be about Real Madrid: players and "
    "academy talent (including Castilla), the coach and squad, transfer sagas and rumour checks, contract and "
    "finance stories, club business and the Bernabeu, tactical breakdowns of Real Madrid matches, dressing-room "
    "and off-pitch storylines, and La Liga / Champions League nights featuring the club. Prefer angles that work "
    "as short video. Ignore items that have nothing to do with Real Madrid."
)

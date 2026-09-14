import feedparser

from app.services.scraping.base import RawItem, SourceResult

# Seed list — exact scraping source list is an explicitly open item in
# 01_Content_Discovery_Scraping.md. These are stable, well-known football RSS
# feeds chosen as reasonable defaults; add/remove via this list.
DEFAULT_FEEDS = [
    ("bbc_sport_football", "http://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("sky_sports_football", "https://www.skysports.com/rss/12040"),
    ("espn_fc", "https://www.espn.com/espn/rss/soccer/news"),
]


def fetch_feed(name: str, url: str, limit: int = 20) -> SourceResult:
    try:
        parsed = feedparser.parse(url)
        if parsed.bozo and not parsed.entries:
            return SourceResult(source_name=name, items=[], ok=False, error=str(parsed.bozo_exception))

        items = []
        for entry in parsed.entries[:limit]:
            text_parts = [entry.get("title", ""), entry.get("summary", "")]
            items.append(
                RawItem(
                    source=f"news_rss:{name}",
                    source_url=entry.get("link"),
                    raw_text="\n".join(p for p in text_parts if p),
                )
            )
        return SourceResult(source_name=name, items=items, ok=True)
    except Exception as exc:  # noqa: BLE001 — any single feed failing must not halt the pipeline
        return SourceResult(source_name=name, items=[], ok=False, error=str(exc))


def fetch_all_feeds(feeds: list[tuple[str, str]] | None = None) -> list[SourceResult]:
    feeds = feeds if feeds is not None else DEFAULT_FEEDS
    return [fetch_feed(name, url) for name, url in feeds]

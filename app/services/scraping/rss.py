from concurrent.futures import ThreadPoolExecutor

import feedparser
import httpx

from app.services.scraping.base import RawItem, SourceResult

# Seed list — the exact source list is an open item in
# 01_Content_Discovery_Scraping.md. First three were the original seeds; the
# rest were each verified to return entries live. Add/remove freely.
DEFAULT_FEEDS = [
    ("bbc_sport_football", "http://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("sky_sports_football", "https://www.skysports.com/rss/12040"),
    ("espn_fc", "https://www.espn.com/espn/rss/soccer/news"),
    ("guardian_football", "https://www.theguardian.com/football/rss"),
    ("marca_real_madrid", "https://e00-marca.uecdn.es/rss/futbol/real-madrid.xml"),
    ("marca_laliga", "https://e00-marca.uecdn.es/rss/futbol/primera-division.xml"),
    ("talksport_football", "https://talksport.com/football/feed/"),
]

REQUEST_TIMEOUT_SECONDS = 12
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 ContentStudio/1.0"
)


def _download(url: str) -> bytes:
    # feedparser's built-in fetcher has no timeout (one hung feed would hang the
    # whole scrape) and several publishers reset its default User-Agent.
    response = httpx.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"},
        follow_redirects=True,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.content


def fetch_feed(name: str, url: str, limit: int = 20) -> SourceResult:
    try:
        parsed = feedparser.parse(_download(url))
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
    with ThreadPoolExecutor(max_workers=min(len(feeds), 8) or 1) as pool:
        return list(pool.map(lambda f: fetch_feed(*f), feeds))

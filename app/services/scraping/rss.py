from concurrent.futures import ThreadPoolExecutor

import feedparser
import httpx

from app.services.scraping.base import RawItem, SourceResult

# Verified live to return entries (see docs / README). Add/remove freely, keep it to ~12 so a scrape stays fast.
# Browser-like User-Agents trigger bot challenges on ESPN (HTTP 202, empty body) and talkSPORT (an HTML page),
# so we identify honestly as a feed reader instead.
DEFAULT_FEEDS = [
    ("bbc_sport_football", "http://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("sky_sports_football", "https://www.skysports.com/rss/12040"),
    ("espn_fc", "https://www.espn.com/espn/rss/soccer/news"),
    ("guardian_football", "https://www.theguardian.com/football/rss"),
    ("talksport_football", "https://talksport.com/football/feed/"),
    ("90min", "https://www.90min.com/posts.rss"),
    ("mirror_football", "https://www.mirror.co.uk/sport/football/?service=rss"),
    ("athletic_football", "https://www.nytimes.com/athletic/rss/football/"),
    ("fourfourtwo", "https://www.fourfourtwo.com/feeds/all"),
    ("marca_real_madrid", "https://e00-marca.uecdn.es/rss/futbol/real-madrid.xml"),
    ("marca_laliga", "https://e00-marca.uecdn.es/rss/futbol/primera-division.xml"),
    ("as_futbol", "https://as.com/rss/futbol/primera.xml"),
]

REQUEST_TIMEOUT_SECONDS = 12
USER_AGENT = "ContentStudio/1.0 (RSS reader)"

# url -> {"etag", "last_modified", "items"}: per-process conditional-GET cache so unchanged feeds cost one tiny 304.
_cache: dict[str, dict] = {}


def _download(url: str) -> tuple[bytes | None, dict]:
    """Returns (body, validators). body is None when the server answered 304 Not Modified."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml, */*"}
    cached = _cache.get(url)
    if cached:
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]
    # feedparser's built-in fetcher has no timeout (one hung feed would hang the whole scrape).
    response = httpx.get(url, headers=headers, follow_redirects=True, timeout=REQUEST_TIMEOUT_SECONDS)
    if response.status_code == 304 and cached:
        return None, {}
    response.raise_for_status()
    return response.content, {"etag": response.headers.get("etag"), "last_modified": response.headers.get("last-modified")}


def fetch_feed(name: str, url: str, limit: int = 20) -> SourceResult:
    try:
        downloaded = _download(url)
        if isinstance(downloaded, tuple):
            body, validators = downloaded
        else:  # tolerate a plain-bytes downloader
            body, validators = downloaded, {}
        if body is None:
            return SourceResult(source_name=name, items=list(_cache[url]["items"]), ok=True, not_modified=True)
        parsed = feedparser.parse(body)
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
        if validators.get("etag") or validators.get("last_modified"):
            _cache[url] = {**validators, "items": items}
        return SourceResult(source_name=name, items=items, ok=True)
    except Exception as exc:  # noqa: BLE001 — any single feed failing must not halt the pipeline
        return SourceResult(source_name=name, items=[], ok=False, error=str(exc))


def fetch_all_feeds(feeds: list[tuple[str, str]] | None = None) -> list[SourceResult]:
    feeds = feeds if feeds is not None else DEFAULT_FEEDS
    with ThreadPoolExecutor(max_workers=min(len(feeds), 12) or 1) as pool:
        return list(pool.map(lambda f: fetch_feed(*f), feeds))

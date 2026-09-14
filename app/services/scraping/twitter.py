"""X/Twitter source adapter for the general Discovery Engine (distinct from the
match-day scraping used by the Posts Studio — see services/scraping/match_day.py).

Uses the official X API v2 recent-search endpoint (requires X_BEARER_TOKEN).
API tier/cost was flagged as an open item in 01_Content_Discovery_Scraping.md —
this returns a clean "not configured" result rather than failing the whole
pipeline when no token is set.
"""

import httpx

from app.config import settings
from app.services.scraping.base import RawItem, SourceResult

SEARCH_URL = "https://api.twitter.com/2/tweets/search/recent"


def fetch_topic(query: str = "football OR soccer -is:retweet lang:en", limit: int = 20) -> SourceResult:
    if not settings.x_bearer_token:
        return SourceResult(source_name="twitter", items=[], ok=False, error="X_BEARER_TOKEN not configured")

    try:
        resp = httpx.get(
            SEARCH_URL,
            headers={"Authorization": f"Bearer {settings.x_bearer_token}"},
            params={"query": query, "max_results": min(max(limit, 10), 100)},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        items = [
            RawItem(source="twitter", source_url=f"https://twitter.com/i/web/status/{t['id']}", raw_text=t["text"])
            for t in data.get("data", [])
        ]
        return SourceResult(source_name="twitter", items=items, ok=True)
    except Exception as exc:  # noqa: BLE001
        return SourceResult(source_name="twitter", items=[], ok=False, error=str(exc))

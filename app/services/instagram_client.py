"""Instagram Graph API — carousel publishing + basic account metrics.

Per 04_Posts_Carousel_Studio.md: carousel publishing is stable Graph API
functionality, no App Review needed for publishing to your own account.
Requires IG_BUSINESS_ACCOUNT_ID + IG_ACCESS_TOKEN in .env (Professional
account with a linked Page — confirm PPA status before relying on this,
per the doc's open item).
"""

import time

import httpx

from app.config import settings
from app.services import ig_token

# Facebook-Login tokens (EAA...) use graph.facebook.com; Instagram-Login tokens
# (IGAA...) only work on graph.instagram.com — set IG_GRAPH_BASE accordingly.
GRAPH_BASE = settings.ig_graph_base.rstrip("/")


class InstagramNotConfigured(RuntimeError):
    pass


def _require_config():
    if not settings.ig_access_token or not settings.ig_business_account_id:
        raise InstagramNotConfigured("IG_ACCESS_TOKEN / IG_BUSINESS_ACCOUNT_ID not set in .env")
    ig_token.refresh_if_due()


def get_account_metrics() -> dict:
    _require_config()
    resp = httpx.get(
        f"{GRAPH_BASE}/{settings.ig_business_account_id}",
        params={
            "fields": "followers_count,media_count",
            "access_token": ig_token.current_token(),
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


INSIGHT_METRICS = ("reach", "total_interactions", "accounts_engaged")
INSIGHT_WINDOW_DAYS = 30  # the insights endpoint rejects/ignores wider since/until windows


def get_account_insights(days: int = INSIGHT_WINDOW_DAYS) -> dict:
    """Account-level insights summed over the last `days` days (max 30).

    Returns {"reach", "total_interactions", "accounts_engaged"} (ints; a metric Instagram doesn't
    return is None). `reach` is unique accounts reached over the whole window (not a sum of daily
    values); `total_interactions` is likes+comments+saves+shares+replies.
    """
    _require_config()
    until = int(time.time())
    since = until - min(days, INSIGHT_WINDOW_DAYS) * 86400
    resp = httpx.get(
        f"{GRAPH_BASE}/{settings.ig_business_account_id}/insights",
        params={
            "metric": ",".join(INSIGHT_METRICS),
            "period": "day",
            "metric_type": "total_value",
            "since": since,
            "until": until,
            "access_token": ig_token.current_token(),
        },
        timeout=20,
    )
    _raise_for_graph_error(resp)
    out: dict = {m: None for m in INSIGHT_METRICS}
    for item in resp.json().get("data", []):
        value = (item.get("total_value") or {}).get("value")
        if item.get("name") in out and value is not None:
            out[item["name"]] = int(value)
    return out


class InstagramPublishError(RuntimeError):
    pass


def _graph_error_message(resp: httpx.Response) -> str:
    try:
        err = resp.json().get("error") or {}
        if err.get("message"):
            return f"{err['message']} (code {err.get('code')})"
    except Exception:
        pass
    return f"HTTP {resp.status_code}"


def _raise_for_graph_error(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        raise InstagramPublishError(f"Instagram API: {_graph_error_message(resp)}")


# Reels containers are processed asynchronously; poll the container until FINISHED.
REEL_POLL_INTERVAL_S = 5
REEL_POLL_TIMEOUT_S = 300


def publish_reel(
    video_url: str,
    caption: str,
    share_to_feed: bool = True,
    cover_url: str | None = None,
) -> dict:
    """Publishes a Reel from a PUBLIC video URL. Returns {"media_id", "permalink"}.

    POST /{ig-id}/media (media_type=REELS) -> poll GET /{container}?fields=status_code,status
    until FINISHED -> POST /{ig-id}/media_publish -> GET /{media}?fields=permalink.
    Raises InstagramPublishError on API errors, container ERROR/EXPIRED, or timeout.
    """
    _require_config()
    base = f"{GRAPH_BASE}/{settings.ig_business_account_id}"
    data = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "share_to_feed": "true" if share_to_feed else "false",
        "access_token": ig_token.current_token(),
    }
    if cover_url:
        data["cover_url"] = cover_url
    container = httpx.post(f"{base}/media", data=data, timeout=60)
    _raise_for_graph_error(container)
    creation_id = container.json()["id"]

    deadline = time.monotonic() + REEL_POLL_TIMEOUT_S
    while True:
        status = httpx.get(
            f"{GRAPH_BASE}/{creation_id}",
            params={"fields": "status_code,status", "access_token": ig_token.current_token()},
            timeout=20,
        )
        _raise_for_graph_error(status)
        body = status.json()
        code = body.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise InstagramPublishError(f"Instagram could not process the video ({code}): {body.get('status') or 'no details'}")
        if time.monotonic() >= deadline:
            raise InstagramPublishError(f"Timed out after {REEL_POLL_TIMEOUT_S} s waiting for Instagram to process the video (last status: {code}).")
        time.sleep(REEL_POLL_INTERVAL_S)

    publish = httpx.post(
        f"{base}/media_publish",
        data={"creation_id": creation_id, "access_token": ig_token.current_token()},
        timeout=60,
    )
    _raise_for_graph_error(publish)
    media_id = publish.json()["id"]

    permalink = None
    try:  # the post is already live; a failed permalink lookup must not fail the publish
        link = httpx.get(
            f"{GRAPH_BASE}/{media_id}",
            params={"fields": "permalink", "access_token": ig_token.current_token()},
            timeout=20,
        )
        if link.status_code < 400:
            permalink = link.json().get("permalink")
    except Exception:
        pass
    return {"media_id": media_id, "permalink": permalink}


def publish_single_image(image_url: str, caption: str) -> str:
    """Publishes a single image post. Returns the published media id."""
    _require_config()
    container = httpx.post(
        f"{GRAPH_BASE}/{settings.ig_business_account_id}/media",
        data={"image_url": image_url, "caption": caption, "access_token": ig_token.current_token()},
        timeout=30,
    )
    container.raise_for_status()
    creation_id = container.json()["id"]

    publish = httpx.post(
        f"{GRAPH_BASE}/{settings.ig_business_account_id}/media_publish",
        data={"creation_id": creation_id, "access_token": ig_token.current_token()},
        timeout=30,
    )
    publish.raise_for_status()
    return publish.json()["id"]


def publish_carousel(image_urls: list[str], caption: str) -> str:
    """Publishes a multi-image carousel. Returns the published media id."""
    _require_config()
    child_ids = []
    for url in image_urls:
        child = httpx.post(
            f"{GRAPH_BASE}/{settings.ig_business_account_id}/media",
            data={"image_url": url, "is_carousel_item": "true", "access_token": ig_token.current_token()},
            timeout=30,
        )
        child.raise_for_status()
        child_ids.append(child.json()["id"])

    container = httpx.post(
        f"{GRAPH_BASE}/{settings.ig_business_account_id}/media",
        data={
            "media_type": "CAROUSEL",
            "children": ",".join(child_ids),
            "caption": caption,
            "access_token": ig_token.current_token(),
        },
        timeout=30,
    )
    container.raise_for_status()
    creation_id = container.json()["id"]

    publish = httpx.post(
        f"{GRAPH_BASE}/{settings.ig_business_account_id}/media_publish",
        data={"creation_id": creation_id, "access_token": ig_token.current_token()},
        timeout=30,
    )
    publish.raise_for_status()
    return publish.json()["id"]

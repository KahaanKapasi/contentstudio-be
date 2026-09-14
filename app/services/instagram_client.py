"""Instagram Graph API — carousel publishing + basic account metrics.

Per 04_Posts_Carousel_Studio.md: carousel publishing is stable Graph API
functionality, no App Review needed for publishing to your own account.
Requires IG_BUSINESS_ACCOUNT_ID + IG_ACCESS_TOKEN in .env (Professional
account with a linked Page — confirm PPA status before relying on this,
per the doc's open item).
"""

import httpx

from app.config import settings

GRAPH_BASE = "https://graph.facebook.com/v21.0"


class InstagramNotConfigured(RuntimeError):
    pass


def _require_config():
    if not settings.ig_access_token or not settings.ig_business_account_id:
        raise InstagramNotConfigured("IG_ACCESS_TOKEN / IG_BUSINESS_ACCOUNT_ID not set in .env")


def get_account_metrics() -> dict:
    _require_config()
    resp = httpx.get(
        f"{GRAPH_BASE}/{settings.ig_business_account_id}",
        params={
            "fields": "followers_count,media_count",
            "access_token": settings.ig_access_token,
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def publish_single_image(image_url: str, caption: str) -> str:
    """Publishes a single image post. Returns the published media id."""
    _require_config()
    container = httpx.post(
        f"{GRAPH_BASE}/{settings.ig_business_account_id}/media",
        data={"image_url": image_url, "caption": caption, "access_token": settings.ig_access_token},
        timeout=30,
    )
    container.raise_for_status()
    creation_id = container.json()["id"]

    publish = httpx.post(
        f"{GRAPH_BASE}/{settings.ig_business_account_id}/media_publish",
        data={"creation_id": creation_id, "access_token": settings.ig_access_token},
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
            data={"image_url": url, "is_carousel_item": "true", "access_token": settings.ig_access_token},
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
            "access_token": settings.ig_access_token,
        },
        timeout=30,
    )
    container.raise_for_status()
    creation_id = container.json()["id"]

    publish = httpx.post(
        f"{GRAPH_BASE}/{settings.ig_business_account_id}/media_publish",
        data={"creation_id": creation_id, "access_token": settings.ig_access_token},
        timeout=30,
    )
    publish.raise_for_status()
    return publish.json()["id"]

"""Client for the Footonomy admin API.

Footonomy serves /admin/** only on a private port and only with a bearer token. Errors raised here are
sanitized on purpose: they never contain the URL or the token, so they are safe to show in the UI.
"""

from typing import Any

import httpx

from app.config import settings


class FootonomyNotConfigured(RuntimeError):
    pass


class FootonomyError(RuntimeError):
    def __init__(self, message: str, http_status: int = 502):
        super().__init__(message)
        self.http_status = http_status


def request(method: str, path: str, *, params: dict | None = None, json: Any = None) -> Any:
    if not settings.footonomy_admin_token:
        raise FootonomyNotConfigured("FOOTONOMY_ADMIN_TOKEN not set in .env")
    url = settings.footonomy_admin_url.rstrip("/") + path
    try:
        resp = httpx.request(
            method,
            url,
            params=params,
            json=json,
            headers={"Authorization": f"Bearer {settings.footonomy_admin_token}"},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise FootonomyError("Footonomy admin API is unreachable. Is the app running and the admin port reachable?") from exc

    if resp.status_code == 401:
        # Not passed through as 401: Studio's own UI treats 401 as "wrong Studio password".
        raise FootonomyError("Footonomy rejected the admin token. Check FOOTONOMY_ADMIN_TOKEN against the app's ADMIN_TOKEN.")
    if resp.status_code == 404 and path.startswith("/admin") and not resp.content:
        raise FootonomyError("Footonomy admin API not found. Is ADMIN_TOKEN set on the app and FOOTONOMY_ADMIN_URL the admin port?")
    if resp.status_code >= 500:
        raise FootonomyError("Footonomy returned a server error.")
    if resp.status_code >= 400:
        message = "Request rejected by Footonomy."
        try:
            message = resp.json().get("message") or message
        except ValueError:
            pass
        raise FootonomyError(message, http_status=resp.status_code)
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()

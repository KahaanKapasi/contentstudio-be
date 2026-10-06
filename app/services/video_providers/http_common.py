"""Shared bits for the httpx-based providers (Higgsfield, Muapi)."""

from pathlib import Path

import httpx

from app.services.video_providers import ProviderError, TransientProviderError

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):
        detail = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    return str(detail or body)[:200]


def request(client: httpx.Client, method: str, url: str, *, label: str, polling: bool = False, **kwargs) -> dict:
    """One JSON call. Polling treats network errors and 5xx as transient; submits do not."""
    try:
        resp = client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        if polling:
            raise TransientProviderError(str(exc)) from exc
        raise ProviderError(f"Could not reach {label}. Please try again.") from exc
    if resp.status_code >= 500 and polling:
        raise TransientProviderError(f"{label} returned {resp.status_code}")
    if resp.status_code >= 400:
        raise ProviderError(_status_message(label, resp.status_code, error_detail(resp)))
    try:
        data = resp.json()
    except ValueError as exc:
        raise ProviderError(f"{label} returned an unreadable response.") from exc
    return data if isinstance(data, dict) else {}


def _status_message(label: str, status: int, detail: str) -> str:
    hints = {401: "check the API credentials", 403: "insufficient credits or access", 404: "model or request not found"}
    hint = hints.get(status)
    return f"{label} rejected the request ({status}{', ' + hint if hint else ''}): {detail}"


def find_video_url(data: dict) -> str | None:
    """Pull the output video URL out of the response shapes these APIs use."""
    video = data.get("video")
    candidates = [
        video.get("url") if isinstance(video, dict) else video,
        _first_url(data.get("videos")),
        _first_url(data.get("outputs")),
        (data.get("output") or {}).get("url") if isinstance(data.get("output"), dict) else data.get("output"),
        (data.get("result") or {}).get("url") if isinstance(data.get("result"), dict) else None,
        data.get("url"),
    ]
    for c in candidates:
        if isinstance(c, str) and c.startswith("http"):
            return c
    return None


def _first_url(items) -> str | None:
    if not isinstance(items, list) or not items:
        return None
    first = items[0]
    return first.get("url") if isinstance(first, dict) else first


def download_url(url: str, dest: Path) -> None:
    """Stream to `dest` via a .part file so a half-written mp4 is never served."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=httpx.Timeout(120.0, connect=15.0)) as resp:
            if resp.status_code >= 400:
                raise ProviderError(f"Could not download the finished video (HTTP {resp.status_code}).")
            with part.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 16):
                    fh.write(chunk)
    except httpx.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise ProviderError("Could not download the finished video. Please try again.") from exc
    except Exception:
        part.unlink(missing_ok=True)
        raise
    part.replace(dest)

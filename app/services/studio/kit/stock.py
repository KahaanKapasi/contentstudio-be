"""Stock footage search + download (Pexels, optional Pixabay), following MoneyPrinterTurbo's rules: filter by
orientation and minimum duration, choose the smallest rendition that still covers the target frame, and
de-duplicate clips across search terms. Search results are cached for 24 h so retries and re-plans are free.
"""

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from itertools import zip_longest
from pathlib import Path

import httpx

from app.config import CACHE_DIR as _CACHE_ROOT
from app.config import settings
from app.services.studio.kit import ffmpeg

CACHE_DIR = _CACHE_ROOT  # tests redirect this
CACHE_TTL_S = 24 * 3600
MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_ORIENTATION = {"9:16": "portrait", "16:9": "landscape", "1:1": "square"}


class StockError(RuntimeError):
    """Search/download failed; the message is safe to show the user."""


@dataclass
class StockClip:
    id: str
    source: str  # pexels | pixabay
    duration: float
    width: int
    height: int
    url: str  # direct file URL of the chosen rendition
    page: str = ""
    thumb: str | None = None


def missing_keys() -> list[str]:
    return [] if settings.pexels_api_key else ["PEXELS_API_KEY"]


def pick_rendition(files: list[dict], target: tuple[int, int]) -> dict | None:
    """Smallest mp4 rendition whose width and height both cover the target; otherwise the largest one."""
    mp4s = [f for f in files if f.get("link") and (f.get("file_type") or "video/mp4") == "video/mp4" and f.get("width") and f.get("height")]
    if not mp4s:
        return None
    covering = [f for f in mp4s if f["width"] >= target[0] and f["height"] >= target[1]]
    if covering:
        return min(covering, key=lambda f: f["width"] * f["height"])
    return max(mp4s, key=lambda f: f["width"] * f["height"])


def search(query: str, aspect: str, min_duration: float, *, per_page: int = 15) -> list[StockClip]:
    """Pexels first, Pixabay when Pexels has no key or no usable result and PIXABAY_API_KEY is set."""
    clips: list[StockClip] = []
    if settings.pexels_api_key:
        clips = _search_pexels(query, aspect, min_duration, per_page)
    if not clips and settings.pixabay_api_key:
        clips = _search_pixabay(query, aspect, min_duration, per_page)
    if not clips and not settings.pexels_api_key and not settings.pixabay_api_key:
        raise StockError("PEXELS_API_KEY is not set in .env")
    return clips


def find_clips(terms: list[str], aspect: str, min_duration: float, *, per_term: int = 6) -> list[StockClip]:
    """Interleave results across terms (so the video opens on the first term but varies), de-duplicated by id."""
    per = [search(t, aspect, min_duration)[:per_term] for t in terms]
    seen: set[str] = set()
    out: list[StockClip] = []
    for group in zip_longest(*per):
        for clip in group:
            if clip is not None and f"{clip.source}:{clip.id}" not in seen:
                seen.add(f"{clip.source}:{clip.id}")
                out.append(clip)
    return out


def _cached(key: str, fetch) -> list[StockClip]:
    path = CACHE_DIR / "stock" / f"{hashlib.sha1(key.encode()).hexdigest()}.json"
    try:
        blob = json.loads(path.read_text())
        if time.time() - blob["t"] < CACHE_TTL_S:
            return [StockClip(**c) for c in blob["clips"]]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    clips = fetch()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"t": time.time(), "clips": [asdict(c) for c in clips]}))
    except OSError:
        pass
    return clips


def _get_json(url: str, *, headers: dict | None = None, params: dict, label: str) -> dict:
    try:
        resp = httpx.get(url, headers=headers, params=params, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise StockError(f"Could not reach {label}. Please try again.") from exc
    if resp.status_code in (401, 403):
        raise StockError(f"{label} rejected the API key.")
    if resp.status_code == 429:
        raise StockError(f"{label} rate limit reached. Try again in a few minutes.")
    if resp.status_code >= 400:
        raise StockError(f"{label} search failed (HTTP {resp.status_code}).")
    try:
        data = resp.json()
    except ValueError as exc:
        raise StockError(f"{label} returned an unreadable response.") from exc
    return data if isinstance(data, dict) else {}


def _search_pexels(query: str, aspect: str, min_duration: float, per_page: int) -> list[StockClip]:
    target = ffmpeg.target_size(aspect)

    def fetch() -> list[StockClip]:
        data = _get_json(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": settings.pexels_api_key},
            params={"query": query, "per_page": per_page, "orientation": _ORIENTATION[aspect]},
            label="Pexels",
        )
        clips = []
        for v in data.get("videos") or []:
            if (v.get("duration") or 0) < min_duration:
                continue
            chosen = pick_rendition(v.get("video_files") or [], target)
            if chosen:
                clips.append(
                    StockClip(str(v["id"]), "pexels", float(v["duration"]), chosen["width"], chosen["height"], chosen["link"], v.get("url", ""), v.get("image"))
                )
        return clips

    return _cached(f"pexels|{query.lower()}|{aspect}|{min_duration:g}|{per_page}", fetch)


def _search_pixabay(query: str, aspect: str, min_duration: float, per_page: int) -> list[StockClip]:
    target = ffmpeg.target_size(aspect)
    portrait, landscape = target[1] > target[0], target[0] > target[1]

    def fetch() -> list[StockClip]:
        data = _get_json(
            "https://pixabay.com/api/videos/",
            params={"key": settings.pixabay_api_key, "q": query, "per_page": max(per_page, 3)},
            label="Pixabay",
        )
        clips = []
        for hit in data.get("hits") or []:
            if (hit.get("duration") or 0) < min_duration:
                continue
            files = [
                {"link": r.get("url"), "width": r.get("width"), "height": r.get("height"), "file_type": "video/mp4"}
                for r in (hit.get("videos") or {}).values()
            ]
            chosen = pick_rendition(files, target)
            if not chosen:
                continue
            w, h = chosen["width"], chosen["height"]
            if (portrait and h <= w) or (landscape and w <= h):
                continue
            clips.append(StockClip(str(hit["id"]), "pixabay", float(hit["duration"]), w, h, chosen["link"], hit.get("pageURL", "")))
        return clips

    return _cached(f"pixabay|{query.lower()}|{aspect}|{min_duration:g}|{per_page}", fetch)


def download(clip: StockClip, dest: Path, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> Path:
    """Stream to `dest` (via .part), aborting past `max_bytes`."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        with httpx.stream("GET", clip.url, follow_redirects=True, timeout=httpx.Timeout(60.0, connect=10.0)) as resp:
            if resp.status_code >= 400:
                raise StockError(f"Could not download a stock clip (HTTP {resp.status_code}).")
            size = 0
            with part.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 16):
                    size += len(chunk)
                    if size > max_bytes:
                        raise StockError("A stock clip was larger than the allowed size and was skipped.")
                    fh.write(chunk)
    except httpx.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise StockError("Could not download a stock clip. Please try again.") from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(dest)
    return dest

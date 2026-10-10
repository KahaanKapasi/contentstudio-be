"""Versioned price table for everything that costs money. Data only, plus tiny lookup helpers.

One entry per billable unit. Entries with the same `id` but different `effective_from` dates are
date-effective versions of the same price (Gemini text/TTS/grounding double on 2027-01-01).
Unknown prices (Higgsfield credits, most Muapi models) carry a wide low/high range and
confidence="unknown" instead of a made-up point value. These are estimates, never billing truth.

`PRICE_OVERRIDES_JSON` (env) is a JSON map id -> usd that replaces the point price (and clears any range).
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date

from app.config import settings

log = logging.getLogger(__name__)

GEMINI_URL = "https://ai.google.dev/gemini-api/docs/pricing"
X_URL = "https://docs.x.com/x-api/getting-started/pricing"
MUAPI_URL = "https://muapi.ai/pricing"
HIGGSFIELD_URL = "https://docs.higgsfield.ai"
VERIFIED = "2026-10-09"
HIKE = date(2027, 1, 1)  # Gemini text / TTS / grounding price change


@dataclass(frozen=True)
class Price:
    id: str
    service: str  # gemini | veo | muapi | higgsfield | x | cloudinary | stock | instagram | edge-tts
    unit: str  # "1M tokens", "image", "second", "request", "resource", "credit"
    usd: float | None  # point price; None when only a range is known
    source_url: str
    verified_on: str
    confidence: str  # official | third-party | unknown
    low_usd: float | None = None
    high_usd: float | None = None
    effective_from: str | None = None  # ISO date; None = always
    note: str | None = None


def _p(id, service, unit, usd, url, confidence="official", *, low=None, high=None, eff=None, note=None, verified=VERIFIED):
    return Price(id, service, unit, usd, url, verified, confidence, low, high, eff, note)


_G = GEMINI_URL
PRICES: tuple[Price, ...] = (
    # --- Gemini text (flash). Input / output (output includes thinking tokens). ---
    _p("gemini.flash.text.in", "gemini", "1M tokens", 0.75, _G),
    _p("gemini.flash.text.in", "gemini", "1M tokens", 1.50, _G, eff="2027-01-01", note="Price increase announced for 2027-01-01."),
    _p("gemini.flash.text.out", "gemini", "1M tokens", 3.75, _G, note="Includes thinking tokens."),
    _p("gemini.flash.text.out", "gemini", "1M tokens", 7.50, _G, eff="2027-01-01", note="Includes thinking tokens."),
    _p("gemini.grounding", "gemini", "request", 0.014, _G, note="First 5,000 grounded requests per month are free."),
    _p("gemini.grounding", "gemini", "request", 0.028, _G, eff="2027-01-01", note="Assumed to double with the other Gemini prices."),
    # --- Gemini image (page id gemini-3.1-flash-image; image tokens $60 / 1M) ---
    _p("gemini.image.out", "gemini", "image", 0.067, _G, note="1K image."),
    _p("gemini.image.out.2k", "gemini", "image", 0.101, _G, note="2K image."),
    _p("gemini.image.out.512", "gemini", "image", 0.045, _G, note="512px image."),
    _p("gemini.image.out.tokens", "gemini", "1M tokens", 60.0, _G, note="Image output tokens; a 1K image is ~1,120 tokens."),
    _p("gemini.image.in", "gemini", "1M tokens", 0.50, _G),
    # --- Gemini TTS (25 audio tokens per second of speech) ---
    _p("gemini.tts.3_8.in", "gemini", "1M tokens", 0.50, _G),
    _p("gemini.tts.3_8.in", "gemini", "1M tokens", 1.00, _G, eff="2027-01-01"),
    _p("gemini.tts.3_8.out", "gemini", "1M tokens", 9.00, _G),
    _p("gemini.tts.3_8.out", "gemini", "1M tokens", 18.00, _G, eff="2027-01-01"),
    _p("gemini.tts.2_5.in", "gemini", "1M tokens", 0.50, _G),
    _p("gemini.tts.2_5.out", "gemini", "1M tokens", 10.00, _G),
    # --- Veo 3.1 (per second of video) ---
    _p("veo.3_1.standard.720p", "veo", "second", 0.40, _G),
    _p("veo.3_1.standard.1080p", "veo", "second", 0.40, _G),
    _p("veo.3_1.standard.4k", "veo", "second", 0.60, _G),
    _p("veo.3_1.fast.720p", "veo", "second", 0.10, _G),
    _p("veo.3_1.fast.1080p", "veo", "second", 0.12, _G),
    _p("veo.3_1.fast.4k", "veo", "second", 0.30, _G),
    _p("veo.3_1.lite.720p", "veo", "second", 0.05, _G),
    _p("veo.3_1.lite.1080p", "veo", "second", 0.08, _G),
    # --- Muapi / Higgsfield ---
    _p("muapi.kling_v3_std_motion_control", "muapi", "second", 0.10, "https://muapi.ai/playground/kling-v3.0-std-motion-control", note="Official page, undated."),
    _p("muapi.default", "muapi", "second", None, MUAPI_URL, "unknown", low=0.05, high=0.40, note="Per-model prices are not published in an API; range is a guess."),
    _p("higgsfield.default", "higgsfield", "second", None, HIGGSFIELD_URL, "unknown", low=0.04, high=0.30, note="Billed in credits, visible only in the console; range is a guess."),
    # --- X API (pay per use) ---
    _p("x.post_read", "x", "resource", 0.005, X_URL, note="Official page, undated."),
    _p("x.user_lookup", "x", "resource", 0.010, X_URL),
    _p("x.post_create", "x", "request", 0.015, X_URL),
    _p("x.post_create_url", "x", "request", 0.200, X_URL, note="Posts that contain a URL."),
    # --- Free / near-free services ---
    _p("cloudinary", "cloudinary", "credit", 0.0, "https://cloudinary.com/pricing", note="Free plan: 25 credits/month. 1 credit = 1k transformations, 1 GB storage or 1 GB bandwidth."),
    _p("stock.pexels_pixabay", "stock", "request", 0.0, "https://www.pexels.com/api/documentation/", note="Free; attribution required."),
    _p("instagram.graph", "instagram", "call", 0.0, "https://developers.facebook.com/docs/instagram-platform", "third-party", note="No fee stated; rate limited. Unverified.", verified="2026-10-09"),
    _p("edge-tts", "edge-tts", "request", 0.0, "https://pypi.org/project/edge-tts/", "third-party", note="Unofficial community client."),
)

_BY_ID: dict[str, list[Price]] = {}
for _price in PRICES:
    _BY_ID.setdefault(_price.id, []).append(_price)


def _overrides() -> dict[str, float]:
    raw = (getattr(settings, "price_overrides_json", "") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {str(k): float(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        log.warning("PRICE_OVERRIDES_JSON is not a JSON map of id -> usd; ignoring it")
        return {}


def _on(on: date | str | None) -> date:
    if on is None:
        return date.today()
    return date.fromisoformat(on) if isinstance(on, str) else on


def get(price_id: str, on: date | str | None = None) -> Price:
    """The entry for `price_id` effective on `on` (default today), with overrides applied."""
    entries = _BY_ID.get(price_id)
    if not entries:
        raise KeyError(f"Unknown price id '{price_id}'")
    day = _on(on)
    live = [e for e in entries if e.effective_from is None or date.fromisoformat(e.effective_from) <= day]
    entry = max(live or entries, key=lambda e: e.effective_from or "")
    override = _overrides().get(price_id)
    if override is not None:
        entry = Price(**{**asdict(entry), "usd": override, "low_usd": None, "high_usd": None, "note": "Overridden by PRICE_OVERRIDES_JSON."})
    return entry


def usd(price_id: str, on: date | str | None = None) -> float:
    """Point price (midpoint for range-only entries)."""
    e = get(price_id, on)
    if e.usd is not None:
        return e.usd
    return ((e.low_usd or 0.0) + (e.high_usd or 0.0)) / 2


def bounds(price_id: str, on: date | str | None = None) -> tuple[float, float]:
    e = get(price_id, on)
    if e.usd is not None:
        return e.usd, e.usd
    return e.low_usd or 0.0, e.high_usd or 0.0


def table(on: date | str | None = None) -> list[dict]:
    """Every price as it applies on `on`, plus upcoming dated changes, for GET /api/costs/prices."""
    day = _on(on)
    rows = []
    for price_id, entries in _BY_ID.items():
        current = get(price_id, day)
        row = asdict(current)
        later = sorted((e for e in entries if e.effective_from and date.fromisoformat(e.effective_from) > day), key=lambda e: e.effective_from)
        row["upcoming"] = [{"effective_from": e.effective_from, "usd": e.usd} for e in later]
        rows.append(row)
    return rows

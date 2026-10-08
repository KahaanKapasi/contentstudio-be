"""Cost estimators: `estimate(action, params) -> Estimate` (docs/11_Cost_Awareness.md).

Pure and fast: no network, no DB. Token ranges come from the known prompt size of each action
(prompt builders in services/*) and output bounds; thinking tokens (billed as output) widen the high end.
These are estimates from the versioned price table in prices.py, never billing truth.
"""

from __future__ import annotations

import re
from datetime import date

from app.config import settings
from app.services.costs import prices
from app.services.video_providers import catalog

WORDS_TO_TOKENS = 1.35
TTS_TOKENS_PER_SECOND = 25
STUDIO_WIDEN = (0.75, 1.25)  # engines return point values: widen +-25%

ACTIONS = (
    "discovery.scrape",
    "articles.generate",
    "articles.regenerate",
    "video.titles",
    "video.scripts",
    "video.prompt_improve",
    "video.generation",
    "studio.project",
    "posts.render_background",
    "posts.render_preview",
    "posts.upload_to_host",
    "posts.publish",
    "posts.match_scrape",
    "dashboard.instagram_refresh",
    "dashboard.twitter_refresh",
    "dashboard.twitter_suggestions",
    "dashboard.twitter_post",
)

# Which service a ledger row is attributed to in the spend summary (video.generation uses the provider).
SERVICE_BY_ACTION = {
    "discovery.scrape": "gemini",
    "articles.generate": "gemini",
    "articles.regenerate": "gemini",
    "video.titles": "gemini",
    "video.scripts": "gemini",
    "video.prompt_improve": "gemini",
    "video.generation": "video",
    "studio.project": "studio",
    "posts.match_scrape": "x",
    "dashboard.twitter_refresh": "x",
    "dashboard.twitter_suggestions": "gemini",
    "dashboard.twitter_post": "x",
}


class UnknownAction(ValueError):
    pass


def _i(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class _Builder:
    def __init__(self, action: str, on: date):
        self.action = action
        self.on = on
        self.rows: list[dict] = []
        self.notes: list[str] = []
        self._tin = [0.0, 0.0]
        self._tout = [0.0, 0.0]
        self._grounded = [0, 0]

    # -- generic rows --
    def row(self, item, price_id, qty_low, qty_high, unit=None):
        p = prices.get(price_id, self.on)
        lo_p, hi_p = prices.bounds(price_id, self.on)
        self.rows.append(
            {
                "item": item,
                "qty_low": round(qty_low, 6),
                "qty_high": round(qty_high, 6),
                "unit": unit or p.unit,
                "unit_usd": p.usd if p.usd is not None else round((lo_p + hi_p) / 2, 6),
                "unit_usd_low": lo_p,
                "unit_usd_high": hi_p,
                "low_usd": round(qty_low * lo_p, 6),
                "high_usd": round(qty_high * hi_p, 6),
                "price_id": price_id,
                "confidence": p.confidence,
                "verified_on": p.verified_on,
                "source_url": p.source_url,
            }
        )

    def fixed_row(self, item, low, high, confidence="official", unit="estimate", note=None):
        self.rows.append(
            {
                "item": item, "qty_low": 1, "qty_high": 1, "unit": unit, "unit_usd": high,
                "unit_usd_low": low, "unit_usd_high": high, "low_usd": round(low, 6), "high_usd": round(high, 6),
                "price_id": None, "confidence": confidence, "verified_on": prices.VERIFIED, "source_url": prices.GEMINI_URL,
            }
        )
        if note:
            self.notes.append(note)

    # -- Gemini text: accumulate then emit two rows --
    def gemini_text(self, in_tokens, out_tokens, think=(0, 0), calls=1):
        self._tin[0] += in_tokens[0] * calls
        self._tin[1] += in_tokens[1] * calls
        self._tout[0] += out_tokens[0] * calls
        self._tout[1] += (out_tokens[1] + think[1]) * calls
        return self

    def grounding(self, low, high):
        self._grounded = [self._grounded[0] + low, self._grounded[1] + high]

    def tts(self, seconds_low, seconds_high):
        model = "2_5" if "2.5" in (settings.gemini_tts_model or "") else "3_8"
        self.row("Gemini speech: text in", f"gemini.tts.{model}.in", 100 / 1e6, 500 / 1e6, "1M tokens")
        self.row("Gemini speech: audio out", f"gemini.tts.{model}.out", seconds_low * TTS_TOKENS_PER_SECOND / 1e6,
                 seconds_high * TTS_TOKENS_PER_SECOND / 1e6, "1M tokens")

    def finish(self, free_notes: list[str] | None = None) -> dict:
        if self._tin[1] or self._tout[1]:
            self.rows.insert(0, self._tok_row("Gemini text: input", "gemini.flash.text.in", self._tin))
            self.rows.insert(1, self._tok_row("Gemini text: output + thinking", "gemini.flash.text.out", self._tout))
        if self._grounded[1]:
            self.row("Google Search grounding", "gemini.grounding", self._grounded[0], self._grounded[1])
            self.notes.append("The first 5,000 grounded requests per month are free.")
        if free_notes:
            self.notes.extend(free_notes)
        low = round(sum(r["low_usd"] for r in self.rows), 6)
        high = round(sum(r["high_usd"] for r in self.rows), 6)
        unknown_high = sum(r["high_usd"] for r in self.rows if r["confidence"] == "unknown")
        if high <= 0:
            confidence = "official"
        elif unknown_high >= high / 2:
            confidence = "unknown"
        elif any(r["confidence"] != "official" for r in self.rows):
            confidence = "mixed"
        else:
            confidence = "official"
        verified = sorted({r["verified_on"] for r in self.rows if r.get("verified_on")})
        return {
            "action": self.action,
            "low_usd": low,
            "high_usd": high,
            "currency": "USD",
            "free": high <= 0,
            "confidence": confidence,
            "breakdown": self.rows,
            "notes": self.notes,
            "prices_verified_on": verified[0] if verified else None,
            "as_of": self.on.isoformat(),
        }

    def _tok_row(self, item, price_id, tokens):
        p = prices.get(price_id, self.on)
        return {
            "item": item, "qty_low": round(tokens[0] / 1e6, 6), "qty_high": round(tokens[1] / 1e6, 6), "unit": "1M tokens",
            "unit_usd": p.usd, "unit_usd_low": p.usd, "unit_usd_high": p.usd,
            "low_usd": round(tokens[0] / 1e6 * p.usd, 6), "high_usd": round(tokens[1] / 1e6 * p.usd, 6),
            "price_id": price_id, "confidence": p.confidence, "verified_on": p.verified_on, "source_url": p.source_url,
        }


# --- per-action estimators ---


def _discovery_scrape(b: _Builder, p: dict):
    n = _i(p.get("n_items"), None)
    lo_n, hi_n = (n, n) if n is not None else (20, 60)  # generate_topics caps at 60 items of <=400 chars (~115 tokens)
    b.gemini_text((300 + lo_n * 40, 300 + hi_n * 115), (900, 1600), think=(0, 2000))
    include_x = p.get("include_x", bool(settings.x_bearer_token))
    if include_x:
        reads = _i(p.get("x_reads"), None)
        b.row("X recent-search reads", "x.post_read", reads if reads is not None else 10, reads if reads is not None else 20)
    b.notes.append("RSS feeds and Instagram scraping are free.")


def _article(b: _Builder, p: dict):
    src_lo, src_hi = (_i(p.get("source_items"), 0),) * 2 if "source_items" in p else (0, 8)  # ~75 tokens per source line
    body = (int(500 * WORDS_TO_TOKENS), int(1800 * WORDS_TO_TOKENS))
    b.gemini_text((200 + src_lo * 75, 200 + src_hi * 75), body, think=(0, 1500))  # draft
    b.gemini_text((250 + body[0], 250 + body[1]), body, think=(0, 1500))  # humanize rewrite
    b.gemini_text((150 + 300, 150 + 750), (120, 300), think=(0, 1500))  # SEO metadata
    b.notes.append("Three Gemini calls: draft, humanize, SEO metadata.")
    if b.action == "articles.regenerate":
        b.notes.append("Replaces the current draft with a new one.")


def _video_titles(b: _Builder, p: dict):
    b.gemini_text((250, 400), (200, 450), think=(0, 1000))


def _video_scripts(b: _Builder, p: dict):
    b.gemini_text((150, 300), (int(460 * WORDS_TO_TOKENS), int(820 * WORDS_TO_TOKENS)), think=(0, 1500))


def _prompt_improve(b: _Builder, p: dict):
    if p.get("research"):
        b.gemini_text((200, 4000), (180, 450), think=(0, 1000))  # search results count as input
        b.grounding(1, 3)
    else:
        b.gemini_text((200, 350), (120, 300), think=(0, 1000))


_VEO_NAME = re.compile(r"veo-3\.1-(lite|fast|standard|)", re.I)


def _veo_price_id(model: str, resolution: str) -> str | None:
    m = _VEO_NAME.match(model or "")
    if not m:
        return None
    tier = m.group(1) or "standard"
    pid = f"veo.3_1.{tier}.{resolution}"
    try:
        prices.get(pid)
    except KeyError:
        return None
    return pid


def _video_generation(b: _Builder, p: dict):
    provider, model = str(p.get("provider") or "veo"), str(p.get("model") or "")
    resolution = str(p.get("resolution") or "720p")
    seconds = _i(p.get("duration_seconds"), 8)
    try:
        spec = catalog.get_model(provider, model or catalog.DEFAULT_MODELS.get(provider, ""))
    except catalog.CatalogError as exc:
        raise ValueError(str(exc)) from exc
    label = f"{spec.label} {resolution}, {seconds}s"
    known = (spec.price_per_second_usd or {}).get(resolution)
    pid = _veo_price_id(spec.id, resolution) if provider == "veo" else None
    if pid:
        b.row(label, pid, seconds, seconds)
    elif known is not None:
        b.fixed_row(label, known * seconds, known * seconds, note="Price from the provider catalog.")
    else:
        b.row(label, f"{provider}.default" if provider in ("muapi", "higgsfield") else "muapi.default", seconds, seconds)
        b.notes.append("This provider does not publish a per-second price; the range is a guess. Check your provider console.")
    b.notes.append("Estimate assumes the clip succeeds; Veo outputs are deleted by Google after ~2 days.")


def _posts_match_scrape(b: _Builder, p: dict):
    b.row("X recent-search reads", "x.post_read", 10, 40)
    b.gemini_text((300 + 10 * 70, 300 + 40 * 70), (350, 700), think=(0, 1500))


def _twitter_refresh(b: _Builder, p: dict):
    b.row("X user lookup", "x.user_lookup", 1, 1)


def _twitter_suggestions(b: _Builder, p: dict):
    b.gemini_text((300, 650), (300, 700), think=(0, 1500))


def _twitter_post(b: _Builder, p: dict):
    has_url = p.get("has_url")
    if has_url is None and isinstance(p.get("text"), str):
        has_url = bool(re.search(r"https?://|\bwww\.", p["text"]))
    if has_url is True:
        b.row("X post (contains a URL)", "x.post_create_url", 1, 1)
    elif has_url is False:
        b.row("X post", "x.post_create", 1, 1)
    else:
        low, high = prices.bounds("x.post_create", b.on)[0], prices.bounds("x.post_create_url", b.on)[1]
        b.rows.append({**_range_row("X post (URL unknown)", "x.post_create", low, high, b.on)})
        b.notes.append("Posts that contain a URL cost far more ($0.20).")


def _range_row(item, price_id, low, high, on):
    pr = prices.get(price_id, on)
    return {"item": item, "qty_low": 1, "qty_high": 1, "unit": "request", "unit_usd": high, "unit_usd_low": low, "unit_usd_high": high,
            "low_usd": low, "high_usd": high, "price_id": price_id, "confidence": pr.confidence, "verified_on": pr.verified_on,
            "source_url": pr.source_url}


# --- Video Studio ---


def _studio_project(b: _Builder, p: dict):
    from app.services.studio import registry

    engine = registry.engines().get(str(p.get("engine") or ""))
    if engine is None:
        raise ValueError("Unknown engine. See GET /api/studio/engines.")
    recipe = engine.recipe(str(p.get("recipe") or "") or None)
    if recipe is None:
        raise ValueError(f"'recipe' is required for engine '{engine.id}': one of {', '.join(r.id for r in engine.recipes)}.")
    params = {f.name: f.default for f in recipe.fields if f.default is not None}
    params.update(p.get("params") or {})
    point = p.get("estimated_usd")
    after_plan = point is not None  # an approved plan already spent its text calls
    is_scenefilm = hasattr(engine, "preplan_estimate")

    if p.get("plan_only"):
        # Plan-first run: only the planning call (script/shots) is paid now; rendering is estimated at approval.
        if recipe.id != "dance":  # dance only checks the uploads while planning
            b.gemini_text((500, 3000), (500, 3000), think=(0, 3000))
            if params.get("research") or recipe.id == "podcast":
                b.grounding(1, 3)
        b.notes.append("Planning only. The render cost is shown when you approve the plan.")
        return

    if recipe.id == "dance":
        secs = p.get("trend_seconds")
        lo, hi = (float(secs), float(secs)) if secs else (3.0, 30.0)
        b.row("Muapi Kling motion control", "muapi.kling_v3_std_motion_control", lo, hi)
        b.notes.append("Cost scales with the trend video length (up to 30 s)." if not secs else "Based on the trend video length.")
        return

    if point is not None:
        point = float(point)
        b.fixed_row(f"{engine.label} / {recipe.label} (images + clips)", point * STUDIO_WIDEN[0], point * STUDIO_WIDEN[1],
                    note="Plan-based figure from the engine, shown as a +-25% range.")
    elif is_scenefilm:
        pre = engine.preplan_estimate(recipe, params)
        b.row("Gemini images", "gemini.image.out", *pre["images"])
        secs = pre["clip_seconds"]
        if pre["provider"]:
            spec = catalog.get_model(pre["provider"], pre["model"])
            per = (spec.price_per_second_usd or {}).get(pre["resolution"])
            pid = _veo_price_id(spec.id, pre["resolution"]) if pre["provider"] == "veo" else None
            label = f"{spec.label} {pre['resolution']} clips"
            if pid:
                b.row(label, pid, *secs)
            elif per is not None:
                b.fixed_row(label, per * secs[0], per * secs[1])
            else:
                b.row(label, "muapi.default", *secs)
                b.notes.append("Muapi bills in credits; the clip price is unknown and shown as a wide range.")
        else:
            b.notes.append("Camera-move stills, no AI video clips.")
    else:
        try:
            value = engine.estimate_cost(recipe, params, {})
        except Exception:
            value = None
        if value:
            b.fixed_row(f"{engine.label} / {recipe.label} (images + clips)", value * STUDIO_WIDEN[0], value * STUDIO_WIDEN[1],
                        note="Engine estimate, shown as a +-25% range.")

    if not after_plan:
        b.gemini_text((500, 3000), (500, 3000), think=(0, 3000))
        if params.get("research") or recipe.id == "podcast":
            b.grounding(1, 3)
    if params.get("tts_provider") == "gemini":
        d = params.get("duration")
        lo, hi = (float(d), float(d)) if d else (30.0, 75.0)
        b.tts(lo, hi)
    elif not b.rows:
        b.notes.append("Free voices and local rendering.")


_FREE = {
    "posts.render_background": "Rendered on this machine (CPU).",
    "posts.render_preview": "Rendered on this machine (CPU).",
    "posts.upload_to_host": "Cloudinary free tier (25 credits/month); each upload uses a little of it.",
    "posts.publish": "Includes the Cloudinary upload (free tier, 25 credits/month) and the Instagram Graph API publish (no per-post fee, rate limited).",
    "dashboard.instagram_refresh": "Instagram Graph API has no per-call fee (rate limited).",
}

_PAID = {
    "discovery.scrape": _discovery_scrape,
    "articles.generate": _article,
    "articles.regenerate": _article,
    "video.titles": _video_titles,
    "video.scripts": _video_scripts,
    "video.prompt_improve": _prompt_improve,
    "video.generation": _video_generation,
    "studio.project": _studio_project,
    "posts.match_scrape": _posts_match_scrape,
    "dashboard.twitter_refresh": _twitter_refresh,
    "dashboard.twitter_suggestions": _twitter_suggestions,
    "dashboard.twitter_post": _twitter_post,
}


def estimate(action: str, params: dict | None = None, on: date | str | None = None) -> dict:
    """Estimate for `action`. Raises UnknownAction for unknown ids and ValueError for bad params."""
    if action not in ACTIONS:
        raise UnknownAction(f"Unknown action '{action}'. Known: {', '.join(ACTIONS)}.")
    day = on if isinstance(on, date) else (date.fromisoformat(on) if on else date.today())
    b = _Builder(action, day)
    fn = _PAID.get(action)
    if fn:
        fn(b, params or {})
    return b.finish([_FREE[action]] if action in _FREE else None)

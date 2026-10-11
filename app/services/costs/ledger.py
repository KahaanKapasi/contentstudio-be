"""Cost ledger: `log_event` after a paid action succeeds, `summary` for the Dashboard.

Logging is failure-safe by design: it opens its own DB session and swallows every error, so a ledger
problem can never break (or roll back) the action that was just performed.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import CostEvent
from app.services.costs import estimate as est
from app.services.costs import prices
from app.services.gemini_client import track_usage

log = logging.getLogger(__name__)


def usage_cost_usd(records: list[dict], on: date | None = None) -> float | None:
    """Actual Gemini text cost from recorded usage_metadata token counts (None when nothing was recorded)."""
    if not records:
        return None
    p_in = prices.usd("gemini.flash.text.in", on)
    p_out = prices.usd("gemini.flash.text.out", on)
    total = sum(r.get("prompt_tokens", 0) / 1e6 * p_in + r.get("output_tokens", 0) / 1e6 * p_out for r in records)
    grounded = sum(1 for r in records if r.get("grounded"))
    total += grounded * prices.usd("gemini.grounding", on)
    return round(total, 6)


def log_event(
    action: str,
    params: dict | None = None,
    *,
    ref_type: str | None = None,
    ref_id: int | None = None,
    usage: list[dict] | None = None,
    actual_usd: float | None = None,
    details: dict | None = None,
) -> None:
    try:
        low = high = None
        try:
            e = est.estimate(action, params or {})
            low, high = e["low_usd"], e["high_usd"]
        except Exception:
            log.debug("cost estimate failed for %s", action, exc_info=True)
        if actual_usd is None:
            actual_usd = usage_cost_usd(usage or [])
        info = dict(details or {})
        if usage:
            info["tokens"] = {
                "calls": len(usage),
                "prompt": sum(r.get("prompt_tokens", 0) for r in usage),
                "output": sum(r.get("output_tokens", 0) for r in usage),
            }
        with SessionLocal() as db:
            db.add(
                CostEvent(
                    action=action, ref_type=ref_type, ref_id=ref_id,
                    estimated_low_usd=low, estimated_high_usd=high, actual_usd=actual_usd,
                    details=json.dumps(info) if info else None,
                )
            )
            db.commit()
    except Exception:
        log.warning("could not write cost event for %s", action, exc_info=True)


@contextmanager
def track_paid(action: str, params: dict | None = None, **kwargs):
    """`with ledger.track_paid("articles.generate") as usage:` — like gemini_client.track_usage, but if the block
    fails after Gemini calls were already made (tokens spent), that spend is still logged as a failed event."""
    with track_usage() as usage:
        try:
            yield usage
        except Exception as exc:
            if usage:
                log_event(action, params, usage=usage, details={"failed": True, "error": str(exc)[:200]}, **kwargs)
            raise


def _service(ev: CostEvent) -> str:
    service = est.SERVICE_BY_ACTION.get(ev.action, "other")
    if service in ("video", "studio"):
        try:
            provider = json.loads(ev.details or "{}").get("provider")
        except ValueError:
            provider = None
        if service == "video":
            return provider or "video"
    return service


def _usd(ev: CostEvent) -> tuple[float, bool]:
    """(usd, is_estimate): actual where known, else the midpoint of the estimate range."""
    if ev.actual_usd is not None:
        # Studio projects can include price guesses (unpriced Muapi/Higgsfield clips); they say so in details.
        return ev.actual_usd, bool(ev.details and '"has_estimate": true' in ev.details)
    lo, hi = ev.estimated_low_usd or 0.0, ev.estimated_high_usd or 0.0
    return (lo + hi) / 2, True


def summary(db, days: int = 30) -> dict:
    days = max(1, min(int(days), 366))
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=days - 1)
    since = datetime.combine(start, datetime.min.time())
    rows = db.query(CostEvent).filter(CostEvent.created_at >= since).all()

    by_service: dict[str, float] = defaultdict(float)
    by_action: dict[str, dict] = {}
    daily: dict[str, float] = {(start + timedelta(days=i)).isoformat(): 0.0 for i in range(days)}
    total = estimated = 0.0
    for ev in rows:
        usd, is_est = _usd(ev)
        total += usd
        estimated += usd if is_est else 0.0
        by_service[_service(ev)] += usd
        a = by_action.setdefault(ev.action, {"action": ev.action, "count": 0, "usd": 0.0, "estimated_count": 0})
        a["count"] += 1
        a["usd"] += usd
        a["estimated_count"] += 1 if is_est else 0
        key = ev.created_at.date().isoformat()
        if key in daily:
            daily[key] += usd
    return {
        "days": days,
        "total_usd": round(total, 6),
        "estimated_usd": round(estimated, 6),  # portion of the total that is an estimate midpoint, not a measured amount
        "event_count": len(rows),
        "by_service": sorted(({"service": k, "usd": round(v, 6)} for k, v in by_service.items()), key=lambda r: -r["usd"]),
        "by_action": sorted(({**a, "usd": round(a["usd"], 6)} for a in by_action.values()), key=lambda r: -r["usd"]),
        "daily": [{"date": d, "usd": round(v, 6)} for d, v in daily.items()],
    }


# --- cost log (global tracker) ---

ACTION_LABELS = {
    "discovery.scrape": "Discovery scrape",
    "articles.generate": "Article generated",
    "articles.regenerate": "Article regenerated",
    "video.titles": "Video titles generated",
    "video.scripts": "Video scripts generated",
    "video.prompt_improve": "Video prompt improved",
    "video.generation": "Video generated",
    "studio.project": "Studio project",
    "posts.render_background": "Post background rendered",
    "posts.render_preview": "Post preview rendered",
    "posts.upload_to_host": "Image uploaded to host",
    "posts.publish": "Post published to Instagram",
    "instagram.publish_reel": "Reel published to Instagram",
    "posts.match_scrape": "Match-day scrape",
    "dashboard.instagram_refresh": "Instagram metrics refreshed",
    "dashboard.twitter_refresh": "X metrics refreshed",
    "dashboard.twitter_suggestions": "X post suggestions generated",
    "dashboard.twitter_post": "Posted to X",
}


def _details(ev: CostEvent) -> dict:
    try:
        value = json.loads(ev.details or "{}")
        return value if isinstance(value, dict) else {}
    except ValueError:
        return {}


def _pretty(value: str | None) -> str:
    text = (value or "").replace("_", " ").replace("-", " ").strip()
    return text[:1].upper() + text[1:] if text else ""


def label_for(action: str, details: dict | None = None) -> str:
    """Human text for a cost event, e.g. 'Article generated' or 'Studio project: 3D explainer (failed)'."""
    d = details or {}
    base = ACTION_LABELS.get(action) or _pretty(action.split(".")[-1]) or action
    if action == "studio.project":
        extra = _pretty(d.get("recipe") or d.get("engine"))
        base = f"Studio project: {extra}" if extra else base
    elif action == "video.generation":
        extra = " ".join(str(x) for x in (d.get("provider"), d.get("model")) if x)
        base = f"Video generated: {extra}" if extra else base
    if d.get("failed") or d.get("status") == "failed":
        base += " (failed)"
    elif action == "discovery.scrape" and d.get("status") == "skipped":
        base += " (nothing new)"
    elif action == "discovery.scrape" and d.get("status") == "error":
        base += " (failed)"
    return base


def _has_estimate_expr():
    return CostEvent.details.like('%"has_estimate": true%')


def _usd_row(actual, low, high, flagged) -> tuple[float, bool]:
    if actual is not None:
        return actual, bool(flagged)
    return ((low or 0.0) + (high or 0.0)) / 2, True


def _since(days: int, tz_offset_minutes: int = 0) -> datetime:
    """UTC (naive) start of the local calendar day `days - 1` days ago, for a client `tz_offset_minutes` east of UTC."""
    offset = timedelta(minutes=max(-14 * 60, min(int(tz_offset_minutes), 14 * 60)))
    local_today = (datetime.now(timezone.utc).replace(tzinfo=None) + offset).date()
    return datetime.combine(local_today - timedelta(days=max(days, 1) - 1), datetime.min.time()) - offset


def _all_rows(db):
    return db.query(
        CostEvent.id, CostEvent.created_at, CostEvent.actual_usd, CostEvent.estimated_low_usd,
        CostEvent.estimated_high_usd, _has_estimate_expr()
    ).order_by(CostEvent.id.asc()).all()


def totals(db, tz_offset_minutes: int = 0) -> dict:
    rows = _all_rows(db)
    s_today, s7, s30 = _since(1, tz_offset_minutes), _since(7, tz_offset_minutes), _since(30, tz_offset_minutes)
    out = {"all_time_usd": 0.0, "today_usd": 0.0, "last_7d_usd": 0.0, "last_30d_usd": 0.0}
    any_est = False
    for r in rows:
        usd, is_est = _usd_row(r[2], r[3], r[4], r[5])
        any_est = any_est or is_est
        out["all_time_usd"] += usd
        created = r[1].replace(tzinfo=None)
        if created >= s30:
            out["last_30d_usd"] += usd
            if created >= s7:
                out["last_7d_usd"] += usd
                if created >= s_today:
                    out["today_usd"] += usd
    out = {k: round(v, 6) for k, v in out.items()}
    out.update({"last_event_id": rows[-1][0] if rows else None, "count": len(rows), "any_estimates": any_est})
    return out


def events(db, limit: int = 100, before_id: int | None = None, days: int | None = None, tz_offset_minutes: int = 0) -> list[dict]:
    """Newest first. `running_total_usd` is the all-time cumulative spend as of that event."""
    limit = max(1, min(int(limit), 500))
    running: dict[int, float] = {}
    cumulative = 0.0
    for r in _all_rows(db):
        cumulative += _usd_row(r[2], r[3], r[4], r[5])[0]
        running[r[0]] = cumulative
    query = db.query(CostEvent)
    if before_id is not None:
        query = query.filter(CostEvent.id < before_id)
    if days:
        query = query.filter(CostEvent.created_at >= _since(days, tz_offset_minutes))
    out = []
    for ev in query.order_by(CostEvent.id.desc()).limit(limit).all():
        details = _details(ev)
        usd, is_est = _usd(ev)
        created = ev.created_at if ev.created_at.tzinfo else ev.created_at.replace(tzinfo=timezone.utc)
        out.append({
            "id": ev.id,
            "created_at": created.isoformat(),
            "action": ev.action,
            "label": label_for(ev.action, details),
            "actual_usd": ev.actual_usd,
            "est_low_usd": ev.estimated_low_usd,
            "est_high_usd": ev.estimated_high_usd,
            "usd": round(usd, 6),
            "is_estimate": is_est,
            "running_total_usd": round(running.get(ev.id, 0.0), 6),
        })
    return out

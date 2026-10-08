"""Cost ledger: `log_event` after a paid action succeeds, `summary` for the Dashboard.

Logging is failure-safe by design: it opens its own DB session and swallows every error, so a ledger
problem can never break (or roll back) the action that was just performed.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from app.database import SessionLocal
from app.models import CostEvent
from app.services.costs import estimate as est
from app.services.costs import prices

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
        return ev.actual_usd, False
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

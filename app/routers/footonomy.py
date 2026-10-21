"""Footonomy admin and analytics, shown inside Content Studio.

Everything here is a thin, validated pass-through to the Footonomy admin API, plus a snapshot table that
keeps a short history of the app's health numbers.
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import FootonomySnapshot
from app.services import footonomy_client
from app.services.footonomy_client import FootonomyError, FootonomyNotConfigured

router = APIRouter(prefix="/api/footonomy", tags=["footonomy"])

SNAPSHOT_MIN_INTERVAL = timedelta(minutes=5)
FLAG_KEY = Path(pattern=r"^[a-z_]{1,64}$")
MATCH_ID = Path(pattern=r"^[A-Za-z0-9._~-]{1,100}$")


class SuspendBody(BaseModel):
    reason: str | None = Field(default=None, max_length=255)


class FlagBody(BaseModel):
    enabled: bool
    text: str | None = Field(default=None, max_length=500)


def _call(method: str, path: str, **kwargs):
    try:
        return footonomy_client.request(method, path, **kwargs)
    except FootonomyNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except FootonomyError as exc:
        raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc


def _naive_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _dig(data: dict, *keys):
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


# ---- analytics ------------------------------------------------------------------------------


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    data = _call("GET", "/admin/overview")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    last = db.query(FootonomySnapshot).order_by(FootonomySnapshot.captured_at.desc()).first()
    if last is None or now - _naive_utc(last.captured_at) >= SNAPSHOT_MIN_INTERVAL:
        db.add(
            FootonomySnapshot(
                total_users=_dig(data, "users", "total"),
                new_users_7d=_dig(data, "users", "new7d"),
                follows_total=_dig(data, "follows", "total"),
                reactions_total=_dig(data, "reactions", "total"),
                requests_total=_dig(data, "system", "http", "requestsTotal"),
                errors_5xx_total=_dig(data, "system", "http", "errors5xxTotal"),
                p95_seconds=_dig(data, "system", "http", "p95Seconds"),
                cache_hit_ratio=_dig(data, "system", "upstream", "cacheHitRatio"),
                websocket_sessions=int(_dig(data, "system", "websocketSessions") or 0),
            )
        )
        db.commit()
    return data


@router.get("/history")
def history(hours: int = Query(72, ge=1, le=24 * 30), db: Session = Depends(get_db)):
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    rows = (
        db.query(FootonomySnapshot)
        .filter(FootonomySnapshot.captured_at >= since)
        .order_by(FootonomySnapshot.captured_at)
        .limit(2000)
        .all()
    )
    return [
        {
            "captured_at": r.captured_at.isoformat(),
            "total_users": r.total_users,
            "new_users_7d": r.new_users_7d,
            "follows_total": r.follows_total,
            "reactions_total": r.reactions_total,
            "requests_total": r.requests_total,
            "errors_5xx_total": r.errors_5xx_total,
            "p95_seconds": r.p95_seconds,
            "cache_hit_ratio": r.cache_hit_ratio,
            "websocket_sessions": r.websocket_sessions,
        }
        for r in rows
    ]


@router.get("/daily")
def daily(days: int = Query(30, ge=1, le=180)):
    return _call("GET", "/admin/analytics/daily", params={"days": days})


@router.get("/top-follows")
def top_follows(limit: int = Query(10, ge=1, le=50)):
    return _call("GET", "/admin/analytics/top-follows", params={"limit": limit})


@router.get("/reactions")
def reactions(limit: int = Query(10, ge=1, le=50)):
    return _call("GET", "/admin/analytics/reactions", params={"limit": limit})


# ---- users ----------------------------------------------------------------------------------


@router.get("/users")
def users(q: str = Query("", max_length=100), page: int = Query(0, ge=0), size: int = Query(25, ge=1, le=100)):
    return _call("GET", "/admin/users", params={"q": q, "page": page, "size": size})


@router.get("/users/{user_id}")
def user_detail(user_id: uuid.UUID):
    return _call("GET", f"/admin/users/{user_id}")


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: uuid.UUID):
    _call("DELETE", f"/admin/users/{user_id}")


@router.post("/users/{user_id}/suspend", status_code=204)
def suspend_user(user_id: uuid.UUID, body: SuspendBody):
    _call("POST", f"/admin/users/{user_id}/suspend", json={"reason": body.reason})


@router.post("/users/{user_id}/unsuspend", status_code=204)
def unsuspend_user(user_id: uuid.UUID):
    _call("POST", f"/admin/users/{user_id}/unsuspend")


# ---- switches, moderation, operations ---------------------------------------------------------


@router.get("/flags")
def flags():
    return _call("GET", "/admin/flags")


@router.put("/flags/{key}")
def set_flag(body: FlagBody, key: str = FLAG_KEY):
    return _call("PUT", f"/admin/flags/{key}", json={"enabled": body.enabled, "text": body.text})


@router.post("/matches/{match_id}/reactions/reset", status_code=204)
def reset_reactions(match_id: str = MATCH_ID):
    _call("POST", f"/admin/matches/{match_id}/reactions/reset")


@router.post("/cache/flush")
def flush_cache():
    return _call("POST", "/admin/cache/flush")


@router.get("/audit")
def audit(limit: int = Query(50, ge=1, le=200)):
    return _call("GET", "/admin/audit", params={"limit": limit})

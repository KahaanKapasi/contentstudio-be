"""Keeps the Instagram long-lived token alive.

Instagram-Login tokens (IGAA...) last 60 days and can be refreshed for another
60 once they are at least 24h old. Render's free tier sleeps (no scheduler) and
env vars can't be written from inside the app, so:
  * the live token is stored in the database (`app_secrets`), seeded from
    IG_ACCESS_TOKEN the first time it is seen (re-seeded if that env var changes);
  * it is refreshed lazily — on startup and before any Instagram call — whenever
    it is older than REFRESH_AFTER. As long as the app is used at least once every
    ~50 days the token never lapses.
"""

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import SessionLocal
from app.models import AppSecret

KEY = "instagram_token"
REFRESH_AFTER = timedelta(days=7)
MIN_AGE = timedelta(hours=24)  # Instagram rejects refreshes of tokens younger than this
DEFAULT_LIFETIME = timedelta(days=60)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _refreshable() -> bool:
    return "graph.instagram.com" in settings.ig_graph_base


def _load(db) -> dict | None:
    row = db.get(AppSecret, KEY)
    return json.loads(row.value) if row else None


def _save(db, state: dict) -> None:
    row = db.get(AppSecret, KEY)
    if row:
        row.value = json.dumps(state)
    else:
        db.add(AppSecret(key=KEY, value=json.dumps(state)))
    try:
        db.commit()
    except IntegrityError:
        # Startup refresh thread and a request can both seed the row at once; the loser updates it.
        db.rollback()
        row = db.get(AppSecret, KEY)
        row.value = json.dumps(state)
        db.commit()


def _sync(db) -> dict | None:
    env_token = settings.ig_access_token
    if not env_token:
        return None
    state = _load(db)
    if state is None or state.get("env_token") != env_token:
        state = {
            "token": env_token,
            "env_token": env_token,
            "issued_at": _now().isoformat(),
            "expires_in": int(DEFAULT_LIFETIME.total_seconds()),
            "last_error": None,
        }
        _save(db, state)
    return state


def current_token() -> str:
    with SessionLocal() as db:
        state = _sync(db)
        return state["token"] if state else ""


def status() -> dict:
    with SessionLocal() as db:
        state = _sync(db)
    if not state:
        return {"configured": False, "refreshable": False, "issued_at": None, "expires_at": None, "days_left": None, "last_error": None}
    issued = datetime.fromisoformat(state["issued_at"])
    expires = issued + timedelta(seconds=state.get("expires_in", DEFAULT_LIFETIME.total_seconds()))
    return {
        "configured": True,
        "refreshable": _refreshable(),
        "issued_at": issued.isoformat(),
        "expires_at": expires.isoformat(),
        "days_left": max(0, (expires - _now()).days),
        "last_error": state.get("last_error"),
    }


def refresh_if_due(force: bool = False) -> dict:
    """Refreshes the token when it is old enough (or `force`). Never raises:
    a failed refresh is recorded in `last_error` and shown by `status()`."""
    if not settings.ig_access_token or not _refreshable():
        return status()
    with SessionLocal() as db:
        state = _sync(db)
        age = _now() - datetime.fromisoformat(state["issued_at"])
        if age < MIN_AGE or (age < REFRESH_AFTER and not force):
            return status()
        host = urlparse(settings.ig_graph_base)
        try:
            resp = httpx.get(
                f"{host.scheme}://{host.netloc}/refresh_access_token",
                params={"grant_type": "ig_refresh_token", "access_token": state["token"]},
                timeout=15,
            )
            body = resp.json()
            if resp.status_code != 200 or "access_token" not in body:
                raise RuntimeError(body.get("error", {}).get("message") or f"HTTP {resp.status_code}")
            state.update(
                token=body["access_token"],
                issued_at=_now().isoformat(),
                expires_in=int(body.get("expires_in") or DEFAULT_LIFETIME.total_seconds()),
                last_error=None,
            )
        except Exception as exc:  # noqa: BLE001 — a failed refresh must not break Instagram calls
            state["last_error"] = f"{_now().isoformat()}: {str(exc)[:200]}"
        _save(db, state)
    return status()

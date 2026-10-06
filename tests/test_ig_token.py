from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import AppSecret
from app.services import ig_token


@pytest.fixture(autouse=True)
def ig_env(monkeypatch):
    monkeypatch.setattr(settings, "ig_access_token", "IGAA_env_token")
    monkeypatch.setattr(settings, "ig_graph_base", "https://graph.instagram.com/v21.0")
    with SessionLocal() as db:
        db.query(AppSecret).delete()
        db.commit()
    yield
    with SessionLocal() as db:
        db.query(AppSecret).delete()
        db.commit()


def _age_token(days: float):
    with SessionLocal() as db:
        state = ig_token._load(db)
        state["issued_at"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        ig_token._save(db, state)


class _Resp:
    def __init__(self, status_code, body):
        self.status_code, self._body = status_code, body

    def json(self):
        return self._body


def test_seeds_from_env_and_not_due_when_fresh(monkeypatch):
    calls = []
    monkeypatch.setattr(httpx, "get", lambda *a, **k: calls.append(1))
    assert ig_token.current_token() == "IGAA_env_token"
    st = ig_token.refresh_if_due()
    assert st["configured"] and st["days_left"] in (59, 60)
    assert calls == []


def test_refreshes_when_older_than_a_week(monkeypatch):
    ig_token.current_token()
    _age_token(8)
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen["url"], seen["params"] = url, params
        return _Resp(200, {"access_token": "IGAA_new", "token_type": "bearer", "expires_in": 5184000})

    monkeypatch.setattr(httpx, "get", fake_get)
    st = ig_token.refresh_if_due()
    assert seen["url"] == "https://graph.instagram.com/refresh_access_token"
    assert seen["params"] == {"grant_type": "ig_refresh_token", "access_token": "IGAA_env_token"}
    assert ig_token.current_token() == "IGAA_new"
    assert st["last_error"] is None and st["days_left"] >= 59


def test_failure_is_recorded_and_old_token_kept(monkeypatch):
    ig_token.current_token()
    _age_token(8)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(400, {"error": {"message": "Token expired"}}))
    st = ig_token.refresh_if_due()
    assert "Token expired" in st["last_error"]
    assert ig_token.current_token() == "IGAA_env_token"


def test_network_error_does_not_raise(monkeypatch):
    ig_token.current_token()
    _age_token(8)

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    assert ig_token.refresh_if_due()["last_error"]


def test_new_env_token_overrides_stored_one(monkeypatch):
    ig_token.current_token()
    _age_token(8)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, {"access_token": "IGAA_rotated", "expires_in": 100}))
    ig_token.refresh_if_due()
    assert ig_token.current_token() == "IGAA_rotated"
    monkeypatch.setattr(settings, "ig_access_token", "IGAA_brand_new_env")
    assert ig_token.current_token() == "IGAA_brand_new_env"


def test_facebook_login_tokens_are_never_refreshed(monkeypatch):
    monkeypatch.setattr(settings, "ig_graph_base", "https://graph.facebook.com/v21.0")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("must not call"))
    assert ig_token.refresh_if_due(force=True)["refreshable"] is False


def test_status_endpoint_hides_the_token(client):
    body = client.get("/api/dashboard/instagram/token").json()
    assert body["configured"] and "token" not in body and "IGAA" not in str(body)


def test_force_refresh_endpoint(client, monkeypatch):
    ig_token.current_token()
    _age_token(2)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, {"access_token": "IGAA_x", "expires_in": 5184000}))
    assert client.post("/api/dashboard/instagram/token/refresh").status_code == 200
    assert ig_token.current_token() == "IGAA_x"


def test_force_refresh_too_young_is_409(client):
    ig_token.current_token()
    assert client.post("/api/dashboard/instagram/token/refresh").status_code == 409


def test_force_refresh_upstream_error_is_502(client, monkeypatch):
    ig_token.current_token()
    _age_token(2)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(400, {"error": {"message": "bad"}}))
    assert client.post("/api/dashboard/instagram/token/refresh").status_code == 502


def test_force_refresh_503_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr(settings, "ig_access_token", "")
    assert client.post("/api/dashboard/instagram/token/refresh").status_code == 503

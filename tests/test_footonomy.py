from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.config import settings
from app.models import FootonomySnapshot
from app.services import footonomy_client

OVERVIEW = {
    "users": {"total": 120, "new7d": 9},
    "follows": {"total": 340},
    "reactions": {"total": 51},
    "system": {
        "http": {"requestsTotal": 1000, "errors5xxTotal": 3, "p95Seconds": 0.25},
        "upstream": {"cacheHitRatio": 0.93},
        "websocketSessions": 42.0,
    },
}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "footonomy_admin_token", "test-token")
    monkeypatch.setattr(settings, "footonomy_admin_url", "http://admin.test:8082/")


@pytest.fixture
def upstream(monkeypatch, configured):
    """Replaces the Footonomy client; records calls and returns whatever `responses` maps the path to."""

    class Fake:
        responses: dict = {}
        calls: list = []

    def fake_request(method, path, *, params=None, json=None):
        Fake.calls.append((method, path, params, json))
        value = Fake.responses.get((method, path), Fake.responses.get(path))
        if isinstance(value, Exception):
            raise value
        return value

    Fake.responses, Fake.calls = {}, []
    monkeypatch.setattr(footonomy_client, "request", fake_request)
    return Fake


def test_not_configured_returns_503(client):
    resp = client.get("/api/footonomy/overview")
    assert resp.status_code == 503
    assert "FOOTONOMY_ADMIN_TOKEN" in resp.json()["detail"]


def test_overview_is_passed_through_and_snapshotted(client, db, upstream):
    upstream.responses["/admin/overview"] = OVERVIEW
    resp = client.get("/api/footonomy/overview")
    assert resp.status_code == 200 and resp.json() == OVERVIEW

    snap = db.query(FootonomySnapshot).one()
    assert (snap.total_users, snap.follows_total, snap.requests_total) == (120, 340, 1000)
    assert snap.p95_seconds == 0.25 and snap.cache_hit_ratio == 0.93 and snap.websocket_sessions == 42


def test_snapshots_are_throttled_to_one_per_five_minutes(client, db, upstream):
    upstream.responses["/admin/overview"] = OVERVIEW
    client.get("/api/footonomy/overview")
    client.get("/api/footonomy/overview")
    assert db.query(FootonomySnapshot).count() == 1

    row = db.query(FootonomySnapshot).one()
    row.captured_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=6)
    db.commit()
    client.get("/api/footonomy/overview")
    assert db.query(FootonomySnapshot).count() == 2


def test_history_is_oldest_first_and_limited_to_the_window(client, db, upstream):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add_all(
        [
            FootonomySnapshot(captured_at=now - timedelta(hours=100), total_users=1),
            FootonomySnapshot(captured_at=now - timedelta(hours=2), total_users=2),
            FootonomySnapshot(captured_at=now - timedelta(hours=1), total_users=3),
        ]
    )
    db.commit()
    rows = client.get("/api/footonomy/history?hours=72").json()
    assert [r["total_users"] for r in rows] == [2, 3]


def test_upstream_failures_become_502_never_401(client, upstream):
    upstream.responses["/admin/overview"] = footonomy_client.FootonomyError("Footonomy rejected the admin token.")
    resp = client.get("/api/footonomy/overview")
    assert resp.status_code == 502  # a 401 here would make Studio's UI log you out


def test_upstream_client_errors_keep_their_status(client, upstream):
    upstream.responses[("DELETE", "/admin/users/8f14e45f-ceea-467a-9575-1e3c9e1d4a52")] = footonomy_client.FootonomyError(
        "User not found", http_status=404
    )
    resp = client.delete("/api/footonomy/users/8f14e45f-ceea-467a-9575-1e3c9e1d4a52")
    assert resp.status_code == 404 and resp.json()["detail"] == "User not found"


def test_user_actions_are_forwarded(client, upstream):
    uid = "8f14e45f-ceea-467a-9575-1e3c9e1d4a52"
    assert client.post(f"/api/footonomy/users/{uid}/suspend", json={"reason": "spam"}).status_code == 204
    assert client.post(f"/api/footonomy/users/{uid}/unsuspend").status_code == 204
    assert client.delete(f"/api/footonomy/users/{uid}").status_code == 204
    assert [(c[0], c[1]) for c in upstream.calls] == [
        ("POST", f"/admin/users/{uid}/suspend"),
        ("POST", f"/admin/users/{uid}/unsuspend"),
        ("DELETE", f"/admin/users/{uid}"),
    ]
    assert upstream.calls[0][3] == {"reason": "spam"}


def test_flags_are_forwarded(client, upstream):
    upstream.responses[("PUT", "/admin/flags/reactions")] = {"key": "reactions", "enabled": False, "text": None}
    resp = client.put("/api/footonomy/flags/reactions", json={"enabled": False})
    assert resp.status_code == 200 and resp.json()["enabled"] is False
    assert upstream.calls[-1][3] == {"enabled": False, "text": None}


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("delete", "/api/footonomy/users/not-a-uuid", None),
        ("post", "/api/footonomy/users/8f14e45f-ceea-467a-9575-1e3c9e1d4a52/suspend", {"reason": "x" * 300}),
        ("put", "/api/footonomy/flags/Bad-Key", {"enabled": True}),
        ("put", "/api/footonomy/flags/reactions", {"enabled": "maybe"}),
        ("put", "/api/footonomy/flags/maintenance_banner", {"enabled": True, "text": "x" * 501}),
        ("post", "/api/footonomy/matches/bad;id/reactions/reset", None),
    ],
)
def test_invalid_input_is_rejected_before_reaching_footonomy(client, upstream, method, path, body):
    resp = getattr(client, method)(path, **({"json": body} if body is not None else {}))
    assert resp.status_code == 422
    assert upstream.calls == []


# ---- the HTTP client itself ----------------------------------------------------------------------


def _patch_httpx(monkeypatch, status=200, json_body=None, raises=None):
    seen = {}

    def fake(method, url, **kwargs):
        seen.update(method=method, url=url, **kwargs)
        if raises:
            raise raises
        return httpx.Response(status, json=json_body, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx, "request", fake)
    return seen


def test_client_sends_bearer_token_and_normalises_url(configured, monkeypatch):
    seen = _patch_httpx(monkeypatch, json_body={"ok": True})
    assert footonomy_client.request("GET", "/admin/overview", params={"a": 1}) == {"ok": True}
    assert seen["url"] == "http://admin.test:8082/admin/overview"
    assert seen["headers"]["Authorization"] == "Bearer test-token"


def test_client_errors_never_leak_the_token_or_url(configured, monkeypatch):
    for kwargs in ({"status": 401}, {"status": 500}, {"raises": httpx.ConnectError("boom http://admin.test:8082")}):
        _patch_httpx(monkeypatch, **kwargs)
        with pytest.raises(footonomy_client.FootonomyError) as exc:
            footonomy_client.request("GET", "/admin/overview")
        assert "test-token" not in str(exc.value) and "admin.test" not in str(exc.value)
        assert exc.value.http_status == 502


def test_client_maps_204_to_none_and_passes_4xx_message(configured, monkeypatch):
    _patch_httpx(monkeypatch, status=204)
    assert footonomy_client.request("DELETE", "/admin/users/x") is None
    _patch_httpx(monkeypatch, status=400, json_body={"message": "Unknown flag: nope"})
    with pytest.raises(footonomy_client.FootonomyError) as exc:
        footonomy_client.request("PUT", "/admin/flags/nope")
    assert exc.value.http_status == 400 and str(exc.value) == "Unknown flag: nope"

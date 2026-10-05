import pytest

from app.config import settings
from app.main import seed_templates

FEATURE_ROUTE = "/api/discovery/topics"


@pytest.fixture
def password_required(monkeypatch):
    monkeypatch.setattr(settings, "local_access_password", "s3cret")


def test_everything_is_open_when_no_password_configured(client):
    assert client.get(FEATURE_ROUTE).status_code == 200
    assert client.get("/api/auth/check").json() == {"ok": True}
    assert client.get("/api/auth/status").json() == {"required": False}


def test_missing_header_is_rejected(client, password_required):
    resp = client.get(FEATURE_ROUTE)
    assert resp.status_code == 401


def test_wrong_password_is_rejected(client, password_required):
    resp = client.get(FEATURE_ROUTE, headers={"X-Access-Password": "nope"})
    assert resp.status_code == 401


def test_empty_password_header_is_rejected(client, password_required):
    resp = client.get(FEATURE_ROUTE, headers={"X-Access-Password": ""})
    assert resp.status_code == 401


def test_correct_password_is_accepted(client, password_required):
    resp = client.get(FEATURE_ROUTE, headers={"X-Access-Password": "s3cret"})
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.parametrize(
    "route",
    [
        "/api/articles",
        "/api/posts/templates",
        "/api/dashboard/kpi-baseline",
        "/api/video/topics",
    ],
)
def test_every_feature_router_is_gated(client, password_required, route):
    assert client.get(route).status_code == 401
    assert client.get(route, headers={"X-Access-Password": "s3cret"}).status_code == 200


def test_health_and_auth_status_stay_open(client, password_required):
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/auth/status").json() == {"required": True}


def test_auth_check_requires_the_password(client, password_required):
    assert client.get("/api/auth/check").status_code == 401
    ok = client.get("/api/auth/check", headers={"X-Access-Password": "s3cret"})
    assert ok.status_code == 200


def test_startup_seeds_three_templates_once(client):
    templates = client.get("/api/posts/templates").json()
    assert [t["layout_config"]["renderer"] for t in templates] == [
        "darkened_background",
        "transparency_overlay",
        "background_removed",
    ]
    seed_templates()
    assert len(client.get("/api/posts/templates").json()) == 3

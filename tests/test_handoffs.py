"""Send to phone: handoff records and the public read route."""

from datetime import timedelta

import pytest

from app.config import settings
from app.models import PhoneHandoff, utcnow
from app.routers import posts as posts_router
from app.services import media_hosting

URLS = [f"https://res.cloudinary.com/demo/image/upload/slide{i}.png" for i in range(1, 13)]


def create(client, **kw):
    return client.post("/api/handoffs", json={"title": "Real Madrid 3-1 Barcelona", "slide_urls": URLS, "caption": "Hala Madrid", **kw})


def test_create_returns_unguessable_id_and_seven_day_expiry(client):
    body = create(client).json()
    assert len(body["id"]) >= 20 and body["slide_urls"] == URLS and body["caption"] == "Hala Madrid"
    from datetime import datetime

    created, expires = datetime.fromisoformat(body["created_at"]), datetime.fromisoformat(body["expires_at"])
    assert expires - created == timedelta(days=7)
    assert create(client).json()["id"] != body["id"]


def test_public_read_needs_no_password_but_create_does(client, monkeypatch):
    monkeypatch.setattr(settings, "local_access_password", "secret")
    assert create(client).status_code == 401
    made = client.post(
        "/api/handoffs",
        json={"slide_urls": URLS[:2], "caption": ""},
        headers={"X-Access-Password": "secret"},
    ).json()
    read = client.get(f"/api/handoffs/{made['id']}")  # no header: this is the phone
    assert read.status_code == 200 and read.json()["slide_urls"] == URLS[:2]
    assert read.headers["cache-control"] == "no-store" and read.headers["x-robots-tag"] == "noindex"
    assert client.delete(f"/api/handoffs/{made['id']}").status_code == 401
    assert client.delete(f"/api/handoffs/{made['id']}", headers={"X-Access-Password": "secret"}).status_code == 200
    assert client.get(f"/api/handoffs/{made['id']}").status_code == 404


def test_unknown_and_expired_links_404(client, db):
    assert client.get("/api/handoffs/not-a-real-token-123").status_code == 404
    assert client.get("/api/handoffs/x").status_code == 404
    made = create(client).json()
    row = db.get(PhoneHandoff, made["id"])
    row.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    gone = client.get(f"/api/handoffs/{made['id']}")
    assert gone.status_code == 404 and "expired" in gone.json()["detail"]


def test_expired_rows_are_purged_on_next_create(client, db):
    old = create(client).json()
    row = db.get(PhoneHandoff, old["id"])
    row.expires_at = utcnow() - timedelta(days=1)
    db.commit()
    create(client)
    db.expire_all()
    assert db.get(PhoneHandoff, old["id"]) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"slide_urls": []},
        {"slide_urls": ["http://insecure.example/a.png"]},
        {"slide_urls": ["javascript:alert(1)"]},
        {"slide_urls": ["https://x.example/a.png"] * 41},
        {"slide_urls": ["https://x.example/a.png"], "caption": "c" * 5001},
    ],
)
def test_rejects_bad_payloads(client, payload):
    assert client.post("/api/handoffs", json=payload).status_code == 422


# --- hosting status + upload guard (the dialog says exactly what is missing) ---------------------------------------


def test_hosting_status_names_the_missing_variable(client):
    body = client.get("/api/posts/hosting-status").json()
    assert body["hosting"]["configured"] is False and body["hosting"]["missing"] == ["CLOUDINARY_URL"]
    assert body["hosting"]["max_image_bytes"] == 10 * 1024 * 1024
    assert body["instagram"] == {"configured": False, "missing": ["IG_ACCESS_TOKEN", "IG_BUSINESS_ACCOUNT_ID"]}


def test_hosting_status_flags_a_malformed_url_and_accepts_a_good_one(client, monkeypatch):
    monkeypatch.setattr(settings, "cloudinary_url", "https://nope")
    bad = client.get("/api/posts/hosting-status").json()["hosting"]
    assert bad["configured"] is False and "malformed" in bad["problem"]
    monkeypatch.setattr(settings, "cloudinary_url", "cloudinary://key:secret@cloudname")
    monkeypatch.setattr(settings, "ig_access_token", "t")
    monkeypatch.setattr(settings, "ig_business_account_id", "1")
    body = client.get("/api/posts/hosting-status").json()
    assert body["hosting"]["configured"] is True and body["instagram"]["configured"] is True


def test_upload_rejects_images_over_the_cloudinary_limit_before_calling_it(client, monkeypatch):
    monkeypatch.setattr(media_hosting, "upload_image", lambda data: pytest.fail("must not be called"))
    big = b"\0" * (10 * 1024 * 1024 + 1)
    resp = client.post("/api/posts/upload-to-host", files={"image": ("big.png", big, "image/png")})
    assert resp.status_code == 413 and "10 MB" in resp.json()["detail"]


def test_upload_maps_cloudinary_errors(client, monkeypatch):
    import cloudinary.exceptions

    def boom(data):
        raise cloudinary.exceptions.Error("File size too large. Got 11000000. Maximum is 10485760.")

    monkeypatch.setattr(media_hosting, "upload_image", boom)
    resp = client.post("/api/posts/upload-to-host", files={"image": ("a.png", b"x", "image/png")})
    assert resp.status_code == 413 and "Cloudinary rejected" in resp.json()["detail"]
    monkeypatch.setattr(media_hosting, "upload_image", lambda data: (_ for _ in ()).throw(cloudinary.exceptions.Error("bad key")))
    assert client.post("/api/posts/upload-to-host", files={"image": ("a.png", b"x", "image/png")}).status_code == 502


def test_upload_503_without_cloudinary_and_ok_with_it(client, monkeypatch):
    resp = client.post("/api/posts/upload-to-host", files={"image": ("a.png", b"x", "image/png")})
    assert resp.status_code == 503 and "CLOUDINARY_URL" in resp.json()["detail"]
    monkeypatch.setattr(media_hosting, "upload_image", lambda data: "https://res.cloudinary.com/demo/a.png")
    assert client.post("/api/posts/upload-to-host", files={"image": ("a.png", b"x", "image/png")}).json() == {"url": "https://res.cloudinary.com/demo/a.png"}

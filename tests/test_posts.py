import json

import pytest

from app.config import settings
from app.models import PostDraft
from app.routers import posts as posts_router
from app.services import instagram_client, match_day, media_hosting
from app.services.gemini_client import GeminiNotConfigured
from app.services.scraping.base import RawItem, SourceResult


def make_draft(db, **kw):
    draft = PostDraft(**kw)
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return draft


def test_create_draft_defaults(client):
    resp = client.post("/api/posts/drafts", json={})
    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "manual" and body["status"] == "draft" and body["final_image_config"] is None


def test_create_and_list_drafts(client):
    first = client.post("/api/posts/drafts", json={"final_text": "one", "template_id": 1}).json()
    second = client.post("/api/posts/drafts", json={"suggested_opinion_text": "two", "image_source_url": "http://i"}).json()
    listed = client.get("/api/posts/drafts").json()
    assert [d["id"] for d in listed] == [second["id"], first["id"]]
    assert listed[1]["final_text"] == "one" and listed[1]["template_id"] == 1
    assert listed[0]["image_source_url"] == "http://i"


def test_update_draft_round_trips_image_config_as_json(client, db):
    draft = make_draft(db)
    config = {"aspect_ratio": "4:5", "hosted_image_urls": ["http://a", "http://b"], "nested": {"k": [1, 2]}}
    resp = client.patch(f"/api/posts/drafts/{draft.id}", json={"final_image_config": config, "final_text": "cap", "status": "finalized"})
    assert resp.status_code == 200
    assert resp.json()["final_image_config"] == config
    assert resp.json()["final_text"] == "cap" and resp.json()["status"] == "finalized"
    db.expire_all()
    assert json.loads(db.get(PostDraft, draft.id).final_image_config) == config
    assert client.get("/api/posts/drafts").json()[0]["final_image_config"] == config


def test_update_draft_keeps_unset_fields(client, db):
    draft = make_draft(db, final_text="keep", final_image_config=json.dumps({"a": 1}))
    body = client.patch(f"/api/posts/drafts/{draft.id}", json={"status": "finalized"}).json()
    assert body["final_text"] == "keep" and body["final_image_config"] == {"a": 1}


def test_update_unknown_draft_is_404(client):
    assert client.patch("/api/posts/drafts/9", json={"final_text": "x"}).status_code == 404


def test_templates_endpoint_returns_parsed_layout_config(client):
    templates = client.get("/api/posts/templates").json()
    assert templates[0]["name"] == "Darkened background"
    assert isinstance(templates[0]["layout_config"], dict)


def test_match_scrape_persists_each_opinion_as_draft(client, monkeypatch):
    seen = {}

    def fake(team):
        seen["team"] = team
        return ["Opinion A", "Opinion B"]

    monkeypatch.setattr(match_day, "suggest_opinions", fake)
    resp = client.post("/api/posts/match-scrape", params={"team": "Barcelona"})
    assert resp.status_code == 200
    assert seen["team"] == "Barcelona"
    assert [(d["source"], d["suggested_opinion_text"], d["status"]) for d in resp.json()] == [
        ("match_scrape", "Opinion A", "draft"),
        ("match_scrape", "Opinion B", "draft"),
    ]
    assert len(client.get("/api/posts/drafts").json()) == 2


def test_match_scrape_defaults_to_real_madrid(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(match_day, "suggest_opinions", lambda team: seen.setdefault("team", team) and [])
    assert client.post("/api/posts/match-scrape").json() == []
    assert seen["team"] == "Real Madrid"


def test_match_scrape_503_when_gemini_not_configured(client, monkeypatch):
    def raise_not_configured(team):
        raise GeminiNotConfigured("no key")

    monkeypatch.setattr(match_day, "suggest_opinions", raise_not_configured)
    resp = client.post("/api/posts/match-scrape")
    assert resp.status_code == 503 and resp.json()["detail"] == "no key"


def test_suggest_opinions_builds_prompt_from_tweets(monkeypatch):
    result = SourceResult("twitter", [RawItem("twitter", "u", "great goal"), RawItem("twitter", "u", "var drama")], ok=True)
    captured = {}
    monkeypatch.setattr(match_day, "fetch_topic", lambda query, limit: captured.update(query=query) or result)
    monkeypatch.setattr(match_day, "generate_json", lambda prompt: captured.update(prompt=prompt) or ["op1"])
    assert match_day.suggest_opinions("Arsenal") == ["op1"]
    assert '"Arsenal"' in captured["query"]
    assert "- great goal" in captured["prompt"] and "- var drama" in captured["prompt"]


def test_suggest_opinions_returns_empty_without_tweets_and_skips_gemini(monkeypatch):
    monkeypatch.setattr(match_day, "fetch_topic", lambda query, limit: SourceResult("twitter", [], ok=False, error="off"))

    def fail(prompt):
        raise AssertionError("must not call Gemini")

    monkeypatch.setattr(match_day, "generate_json", fail)
    assert match_day.suggest_opinions() == []


def test_publish_unknown_draft_404(client):
    assert client.post("/api/posts/drafts/3/publish").status_code == 404


def test_publish_400_without_image_config(client, db):
    draft = make_draft(db)
    resp = client.post(f"/api/posts/drafts/{draft.id}/publish")
    assert resp.status_code == 400 and "final_image_config" in resp.json()["detail"]


@pytest.mark.parametrize("config", [{}, {"hosted_image_urls": []}, {"other": 1}])
def test_publish_400_without_hosted_image_urls(client, db, config):
    draft = make_draft(db, final_image_config=json.dumps(config))
    resp = client.post(f"/api/posts/drafts/{draft.id}/publish")
    assert resp.status_code == 400 and "hosted_image_urls" in resp.json()["detail"]


@pytest.mark.parametrize("urls", [["http://a"], ["http://a", "http://b"]])
def test_publish_503_when_instagram_unconfigured_and_draft_stays_unpublished(client, db, urls):
    draft = make_draft(db, final_text="cap", final_image_config=json.dumps({"hosted_image_urls": urls}))
    resp = client.post(f"/api/posts/drafts/{draft.id}/publish")
    assert resp.status_code == 503
    db.expire_all()
    assert db.get(PostDraft, draft.id).status == "draft"


def test_publish_single_image_marks_draft_published(client, db, monkeypatch):
    calls = []
    monkeypatch.setattr(posts_router, "publish_single_image", lambda url, caption: calls.append((url, caption)) or "m1")
    draft = make_draft(db, final_text="cap", final_image_config=json.dumps({"hosted_image_urls": ["http://a"]}))
    resp = client.post(f"/api/posts/drafts/{draft.id}/publish")
    assert resp.json() == {"media_id": "m1"}
    assert calls == [("http://a", "cap")]
    db.expire_all()
    assert db.get(PostDraft, draft.id).status == "published"


def test_publish_multiple_images_uses_carousel(client, db, monkeypatch):
    calls = []
    monkeypatch.setattr(posts_router, "publish_carousel", lambda urls, caption: calls.append((urls, caption)) or "c1")
    draft = make_draft(db, final_image_config=json.dumps({"hosted_image_urls": ["http://a", "http://b"]}))
    assert client.post(f"/api/posts/drafts/{draft.id}/publish").json() == {"media_id": "c1"}
    assert calls == [(["http://a", "http://b"], "")]


def test_instagram_client_requires_both_credentials(monkeypatch):
    monkeypatch.setattr(settings, "ig_access_token", "tok")
    with pytest.raises(instagram_client.InstagramNotConfigured):
        instagram_client.get_account_metrics()


def test_upload_to_host_503_when_cloudinary_unconfigured(client, image_bytes):
    resp = client.post("/api/posts/upload-to-host", files={"image": ("a.png", image_bytes(), "image/png")})
    assert resp.status_code == 503 and "CLOUDINARY_URL" in resp.json()["detail"]


def test_upload_to_host_returns_hosted_url(client, image_bytes, monkeypatch):
    monkeypatch.setattr(media_hosting, "upload_image", lambda data: "https://cdn/x.jpg")
    resp = client.post("/api/posts/upload-to-host", files={"image": ("a.png", image_bytes(), "image/png")})
    assert resp.json() == {"url": "https://cdn/x.jpg"}


def test_getty_search_is_always_501(client):
    resp = client.get("/api/posts/getty-search", params={"query": "bellingham"})
    assert resp.status_code == 501
    assert "bot-detection" in resp.json()["detail"]

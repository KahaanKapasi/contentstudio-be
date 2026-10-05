import json

import pytest

from app.models import Article, ScrapedItem, TopicSourceLink
from app.services import article_pipeline


@pytest.fixture
def mock_gemini(monkeypatch):
    calls = {"text": [], "json": []}

    def fake_text(prompt):
        calls["text"].append(prompt)
        return f"generated body {len(calls['text'])}"

    def fake_json(prompt):
        calls["json"].append(prompt)
        return {"title": "Vini's Big Night: A Hat-Trick!", "meta_description": "meta", "tags": ["madrid", "vini"]}

    monkeypatch.setattr(article_pipeline, "generate_text", fake_text)
    monkeypatch.setattr(article_pipeline, "generate_json", fake_json)
    return calls


def make_article(db, topic_id=None, **kw):
    article = Article(topic_id=topic_id, title=kw.pop("title", "Title"), body="body", tags=json.dumps(["a"]), **kw)
    db.add(article)
    db.commit()
    db.refresh(article)
    return article


def test_slugify():
    assert article_pipeline._slugify("Vini's Big Night: A Hat-Trick!") == "vini-s-big-night-a-hat-trick"
    assert article_pipeline._slugify("  --Hi--  ") == "hi"
    assert len(article_pipeline._slugify("x" * 200)) == 80


def test_generate_article_persists_draft_with_seo_fields(db, make_topic, mock_gemini):
    topic = make_topic("Original angle")
    article = article_pipeline.generate_article(db, topic.id)

    stored = db.get(Article, article.id)
    assert stored.topic_id == topic.id
    assert stored.title == "Vini's Big Night: A Hat-Trick!"
    assert stored.slug == "vini-s-big-night-a-hat-trick"
    assert json.loads(stored.tags) == ["madrid", "vini"]
    assert stored.meta_description == "meta"
    assert stored.status == "draft"
    assert stored.published_at is None
    assert stored.body == "generated body 2"
    assert "generated body 1" in mock_gemini["text"][1]


def test_generate_article_includes_linked_source_context(db, make_topic, mock_gemini):
    topic = make_topic()
    item = ScrapedItem(source="s", raw_text="UNIQUE-SCRAPED-SNIPPET")
    db.add(item)
    db.commit()
    db.add(TopicSourceLink(topic_id=topic.id, scraped_item_id=item.id))
    db.commit()
    article_pipeline.generate_article(db, topic.id)
    assert "UNIQUE-SCRAPED-SNIPPET" in mock_gemini["text"][0]


def test_generate_article_falls_back_to_topic_title_when_seo_title_missing(db, make_topic, monkeypatch):
    monkeypatch.setattr(article_pipeline, "generate_text", lambda p: "body")
    monkeypatch.setattr(article_pipeline, "generate_json", lambda p: {})
    article = article_pipeline.generate_article(db, make_topic("Plain Topic").id)
    assert (article.title, article.slug, article.tags) == ("Plain Topic", "plain-topic", "[]")


def test_generate_article_unknown_topic_raises_value_error(db):
    with pytest.raises(ValueError):
        article_pipeline.generate_article(db, 404)


def test_generate_endpoint_creates_article(client, make_topic, mock_gemini):
    topic = make_topic()
    resp = client.post("/api/articles/generate", json={"topic_id": topic.id})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "draft" and body["tags"] == ["madrid", "vini"] and body["topic_id"] == topic.id
    assert client.get(f"/api/articles/{body['id']}").json()["slug"] == body["slug"]


def test_generate_endpoint_503_without_gemini_key(client, make_topic):
    resp = client.post("/api/articles/generate", json={"topic_id": make_topic().id})
    assert resp.status_code == 503
    assert "GEMINI_API_KEY" in resp.json()["detail"]


def test_generate_endpoint_404_for_unknown_topic(client, mock_gemini):
    assert client.post("/api/articles/generate", json={"topic_id": 12345}).status_code == 404


def test_get_unknown_article_is_404(client):
    assert client.get("/api/articles/1").status_code == 404


def test_list_articles_newest_first(client, db):
    first = make_article(db, title="first")
    second = make_article(db, title="second")
    ids = [a["id"] for a in client.get("/api/articles").json()]
    assert ids == [second.id, first.id]


def test_patch_tags_round_trip_as_list(client, db):
    article = make_article(db)
    resp = client.patch(f"/api/articles/{article.id}", json={"tags": ["x", "y z"], "title": "New"})
    assert resp.status_code == 200
    assert resp.json()["tags"] == ["x", "y z"] and resp.json()["title"] == "New"
    assert client.get(f"/api/articles/{article.id}").json()["tags"] == ["x", "y z"]
    db.expire_all()
    assert json.loads(db.get(Article, article.id).tags) == ["x", "y z"]


def test_patch_leaves_unset_fields_untouched(client, db):
    article = make_article(db)
    body = client.patch(f"/api/articles/{article.id}", json={"slug": "s"}).json()
    assert body["slug"] == "s" and body["body"] == "body" and body["tags"] == ["a"]


def test_patch_status_published_sets_published_at_once(client, db):
    article = make_article(db)
    first = client.patch(f"/api/articles/{article.id}", json={"status": "published"}).json()
    assert first["status"] == "published" and first["published_at"]
    again = client.patch(f"/api/articles/{article.id}", json={"status": "published"}).json()
    assert again["published_at"] == first["published_at"]


def test_patch_unknown_article_is_404(client):
    assert client.patch("/api/articles/99", json={"title": "x"}).status_code == 404


def test_publish_endpoint_sets_status_and_timestamp(client, db):
    article = make_article(db)
    resp = client.post(f"/api/articles/{article.id}/publish")
    assert resp.status_code == 200
    assert resp.json()["status"] == "published" and resp.json()["published_at"]


def test_publish_keeps_existing_published_at(client, db):
    article = make_article(db)
    first = client.post(f"/api/articles/{article.id}/publish").json()["published_at"]
    assert client.post(f"/api/articles/{article.id}/publish").json()["published_at"] == first


def test_publish_unknown_article_is_404(client):
    assert client.post("/api/articles/5/publish").status_code == 404


def test_regenerate_replaces_old_row(client, db, make_topic, mock_gemini):
    topic = make_topic()
    old = make_article(db, topic_id=topic.id)
    resp = client.post(f"/api/articles/{old.id}/regenerate")
    assert resp.status_code == 200
    new_id = resp.json()["id"]
    assert new_id != old.id
    assert client.get(f"/api/articles/{old.id}").status_code == 404
    assert [a["id"] for a in client.get("/api/articles").json()] == [new_id]


def test_regenerate_without_gemini_503_and_keeps_old_article(client, db, make_topic):
    old = make_article(db, topic_id=make_topic().id)
    assert client.post(f"/api/articles/{old.id}/regenerate").status_code == 503
    assert client.get(f"/api/articles/{old.id}").status_code == 200


def test_regenerate_404_for_unknown_or_topicless_article(client, db):
    assert client.post("/api/articles/77/regenerate").status_code == 404
    orphan = make_article(db, topic_id=None)
    assert client.post(f"/api/articles/{orphan.id}/regenerate").status_code == 404


def test_article_with_null_tags_serializes_as_empty_list(client, db):
    article = Article(title="no tags", tags=None)
    db.add(article)
    db.commit()
    assert client.get(f"/api/articles/{article.id}").json()["tags"] == []


def test_patch_tags_null_does_not_break_serialization(client, db):
    article = make_article(db)
    resp = client.patch(f"/api/articles/{article.id}", json={"tags": None})
    assert resp.status_code == 200 and resp.json()["tags"] == []

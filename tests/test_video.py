import pytest

from app.models import Script, VideoTopic
from app.services import video_pipeline


@pytest.fixture
def mock_titles(monkeypatch):
    payload = [
        {"title": "Short hook", "format": "short", "suggestion_score": 0.9},
        {"title": "Long deep dive", "format": "long", "suggestion_score": "0.4"},
        {"title": "Weird format", "format": "reel", "suggestion_score": "high"},
        {"title": "", "format": "long", "suggestion_score": 1},
    ]
    monkeypatch.setattr(video_pipeline, "generate_json", lambda prompt: payload)


def make_video_topic(db, topic_id=None, title="vt", score=None):
    vt = VideoTopic(topic_id=topic_id, title=title, suggestion_score=score, format="long")
    db.add(vt)
    db.commit()
    db.refresh(vt)
    return vt


def test_generate_titles_creates_and_normalizes_candidates(client, make_topic, mock_titles):
    topic = make_topic(suitable_for="video")
    resp = client.post("/api/video/generate-titles", json={"topic_id": topic.id})
    assert resp.status_code == 200
    by_title = {v["title"]: v for v in resp.json()}
    assert set(by_title) == {"Short hook", "Long deep dive", "Weird format"}
    assert by_title["Short hook"]["format"] == "short" and by_title["Short hook"]["suggestion_score"] == 0.9
    assert by_title["Long deep dive"]["suggestion_score"] == 0.4
    assert by_title["Weird format"]["format"] == "long" and by_title["Weird format"]["suggestion_score"] is None
    assert all(v["topic_id"] == topic.id for v in by_title.values())


def test_generate_titles_accepts_both_topics(client, make_topic, mock_titles):
    topic = make_topic(suitable_for="both")
    assert client.post("/api/video/generate-titles", json={"topic_id": topic.id}).status_code == 200


def test_generate_titles_non_list_gemini_response_creates_nothing(client, make_topic, monkeypatch):
    monkeypatch.setattr(video_pipeline, "generate_json", lambda p: {"oops": 1})
    topic = make_topic(suitable_for="video")
    assert client.post("/api/video/generate-titles", json={"topic_id": topic.id}).json() == []


def test_generate_titles_400_for_article_only_topic(client, make_topic, mock_titles):
    topic = make_topic(suitable_for="article")
    resp = client.post("/api/video/generate-titles", json={"topic_id": topic.id})
    assert resp.status_code == 400
    assert "not marked suitable for video" in resp.json()["detail"]


def test_generate_titles_400_for_unknown_topic(client, mock_titles):
    assert client.post("/api/video/generate-titles", json={"topic_id": 4040}).status_code == 400


def test_generate_titles_503_without_gemini_key(client, make_topic):
    topic = make_topic(suitable_for="video")
    assert client.post("/api/video/generate-titles", json={"topic_id": topic.id}).status_code == 503


def test_generate_scripts_creates_exactly_long_and_short(client, db, monkeypatch):
    vt = make_video_topic(db)
    monkeypatch.setattr(video_pipeline, "generate_json", lambda p: {"long": "LONG BODY", "short": "SHORT BODY"})
    resp = client.post("/api/video/scripts/generate", json={"video_topic_id": vt.id})
    assert resp.status_code == 200
    scripts = resp.json()
    assert [(s["variant"], s["body"], s["selected"]) for s in scripts] == [
        ("long", "LONG BODY", False),
        ("short", "SHORT BODY", False),
    ]
    assert db.query(Script).count() == 2


def test_generate_scripts_tolerates_malformed_gemini_output(client, db, monkeypatch):
    vt = make_video_topic(db)
    monkeypatch.setattr(video_pipeline, "generate_json", lambda p: ["not", "a", "dict"])
    scripts = client.post("/api/video/scripts/generate", json={"video_topic_id": vt.id}).json()
    assert [(s["variant"], s["body"]) for s in scripts] == [("long", ""), ("short", "")]


def test_generate_scripts_404_unknown_video_topic(client):
    assert client.post("/api/video/scripts/generate", json={"video_topic_id": 9}).status_code == 404


def test_generate_scripts_503_without_gemini_key(client, db):
    vt = make_video_topic(db)
    assert client.post("/api/video/scripts/generate", json={"video_topic_id": vt.id}).status_code == 503
    assert db.query(Script).count() == 0


def test_list_scripts_filters_by_video_topic_in_id_order(client, db):
    a, b = make_video_topic(db, title="a"), make_video_topic(db, title="b")
    db.add_all([Script(video_topic_id=a.id, variant="long"), Script(video_topic_id=b.id, variant="long"), Script(video_topic_id=a.id, variant="short")])
    db.commit()
    got = client.get("/api/video/scripts", params={"video_topic_id": a.id}).json()
    assert [s["variant"] for s in got] == ["long", "short"]
    assert client.get("/api/video/scripts").status_code == 422


def test_selecting_a_script_unselects_sibling_but_not_other_topics(client, db):
    a, b = make_video_topic(db, title="a"), make_video_topic(db, title="b")
    long_a = Script(video_topic_id=a.id, variant="long", selected=True)
    short_a = Script(video_topic_id=a.id, variant="short", selected=False)
    other = Script(video_topic_id=b.id, variant="long", selected=True)
    db.add_all([long_a, short_a, other])
    db.commit()

    resp = client.patch(f"/api/video/scripts/{short_a.id}", json={"selected": True})
    assert resp.status_code == 200 and resp.json()["selected"] is True

    db.expire_all()
    assert db.get(Script, long_a.id).selected is False
    assert db.get(Script, short_a.id).selected is True
    assert db.get(Script, other.id).selected is True


def test_deselecting_a_script_leaves_siblings_alone(client, db):
    a = make_video_topic(db)
    s1 = Script(video_topic_id=a.id, variant="long", selected=True)
    s2 = Script(video_topic_id=a.id, variant="short", selected=False)
    db.add_all([s1, s2])
    db.commit()
    assert client.patch(f"/api/video/scripts/{s1.id}", json={"selected": False}).json()["selected"] is False
    db.expire_all()
    assert db.get(Script, s2.id).selected is False


def test_patch_unknown_script_is_404(client):
    assert client.patch("/api/video/scripts/1", json={"selected": True}).status_code == 404


def test_video_topics_ordered_by_score_desc_and_filter_by_topic(client, db, make_topic):
    t1, t2 = make_topic("one"), make_topic("two")
    make_video_topic(db, t1.id, "low", 0.2)
    make_video_topic(db, t1.id, "high", 0.95)
    make_video_topic(db, t2.id, "mid", 0.5)
    make_video_topic(db, t1.id, "unscored", None)

    all_titles = [v["title"] for v in client.get("/api/video/topics").json()]
    assert all_titles == ["high", "mid", "low", "unscored"]

    filtered = [v["title"] for v in client.get("/api/video/topics", params={"topic_id": t1.id}).json()]
    assert filtered == ["high", "low", "unscored"]
    assert client.get("/api/video/topics", params={"topic_id": 999}).json() == []

"""Instagram insights (reach/engagement) and Reels publishing — all HTTP mocked."""

import pytest
import httpx
from sqlalchemy import create_engine, text

from app import migrations
from app.config import settings
from app.database import SessionLocal
from app.models import VideoGeneration, VideoProject
from app.routers import dashboard as dashboard_router
from app.services import instagram_client as ig, ig_token, instagram_publish, media_hosting


class FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError('unexpected HTTP error')


@pytest.fixture
def ig_config(monkeypatch):
    monkeypatch.setattr(settings, "ig_access_token", "tok")
    monkeypatch.setattr(settings, "ig_business_account_id", "999")
    monkeypatch.setattr(ig_token, "refresh_if_due", lambda: None)
    monkeypatch.setattr(ig_token, "current_token", lambda: "tok")
    monkeypatch.setattr(ig, "REEL_POLL_INTERVAL_S", 0)


class FakeGraph:
    """Records calls; `statuses` is the sequence of container status_code answers."""

    def __init__(self, monkeypatch, statuses=("IN_PROGRESS", "FINISHED"), permalink="https://www.instagram.com/reel/abc/"):
        self.calls, self.statuses, self.permalink = [], list(statuses), permalink
        monkeypatch.setattr(ig.httpx, "get", self.get)
        monkeypatch.setattr(ig.httpx, "post", self.post)

    def post(self, url, data=None, **kw):
        self.calls.append(("POST", url, data))
        if url.endswith("/media"):
            return FakeResp({"id": "cont1"})
        if url.endswith("/media_publish"):
            return FakeResp({"id": "media1"})
        raise AssertionError(url)

    def get(self, url, params=None, **kw):
        self.calls.append(("GET", url, params))
        if url.endswith("/cont1"):
            code = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return FakeResp({"status_code": code, "status": f"{code}: details"})
        if url.endswith("/media1"):
            return FakeResp({"permalink": self.permalink})
        if url.endswith("/insights"):
            return FakeResp({"data": [
                {"name": "reach", "total_value": {"value": 2000}},
                {"name": "total_interactions", "total_value": {"value": 300}},
                {"name": "accounts_engaged", "total_value": {"value": 150}},
            ]})
        if url.endswith("/999"):
            return FakeResp({"followers_count": 55})
        raise AssertionError(url)


# --- insights ---


def test_account_insights_params_and_values(ig_config, monkeypatch):
    graph = FakeGraph(monkeypatch)
    out = ig.get_account_insights()
    assert out == {"reach": 2000, "total_interactions": 300, "accounts_engaged": 150}
    _, url, params = graph.calls[0]
    assert url.endswith("/999/insights")
    assert params["metric"] == "reach,total_interactions,accounts_engaged"
    assert params["metric_type"] == "total_value" and params["period"] == "day"
    assert params["until"] - params["since"] == 30 * 86400


def test_refresh_fills_reach_and_engagement(client, ig_config, monkeypatch):
    FakeGraph(monkeypatch)
    body = client.post("/api/dashboard/instagram/refresh").json()
    assert body["followers"] == 55 and body["reach_30d"] == 2000
    assert body["engagement_rate"] == pytest.approx(0.15)  # fraction: frontend shows 15.0%
    assert body["warning"] is None
    stored = client.get("/api/dashboard/instagram/metrics").json()[0]
    assert stored["reach_30d"] == 2000 and stored["warning"] is None


def test_refresh_survives_insights_failure(client, ig_config, monkeypatch):
    monkeypatch.setattr(dashboard_router.instagram_client, "get_account_metrics", lambda: {"followers_count": 7})

    def boom():
        raise httpx.ConnectError("https://x/?access_token=SECRET")

    monkeypatch.setattr(dashboard_router.instagram_client, "get_account_insights", boom)
    body = client.post("/api/dashboard/instagram/refresh").json()
    assert body["followers"] == 7 and body["reach_30d"] is None and body["engagement_rate"] is None
    assert "unavailable" in body["warning"] and "SECRET" not in body["warning"]


def test_refresh_warns_on_graph_error(client, ig_config, monkeypatch):
    monkeypatch.setattr(dashboard_router.instagram_client, "get_account_metrics", lambda: {"followers_count": 7})

    def boom():
        raise ig.InstagramPublishError("Instagram API: Permission denied (code 10)")

    monkeypatch.setattr(dashboard_router.instagram_client, "get_account_insights", boom)
    assert "Permission denied" in client.post("/api/dashboard/instagram/refresh").json()["warning"]


# --- publish_reel ---


def test_publish_reel_happy_path(ig_config, monkeypatch):
    graph = FakeGraph(monkeypatch)
    out = ig.publish_reel("https://cdn/x.mp4", "hello", share_to_feed=False, cover_url="https://cdn/c.jpg")
    assert out == {"media_id": "media1", "permalink": "https://www.instagram.com/reel/abc/"}
    create = graph.calls[0]
    assert create[2]["media_type"] == "REELS" and create[2]["video_url"] == "https://cdn/x.mp4"
    assert create[2]["share_to_feed"] == "false" and create[2]["cover_url"] == "https://cdn/c.jpg"
    assert [c[0] + c[1].rsplit("/", 1)[1] for c in graph.calls] == ["POSTmedia", "GETcont1", "GETcont1", "POSTmedia_publish", "GETmedia1"]
    assert graph.calls[1][2]["fields"] == "status_code,status"


@pytest.mark.parametrize("code", ["ERROR", "EXPIRED"])
def test_publish_reel_container_failure_never_publishes(ig_config, monkeypatch, code):
    graph = FakeGraph(monkeypatch, statuses=["IN_PROGRESS", code])
    with pytest.raises(ig.InstagramPublishError, match=code):
        ig.publish_reel("https://cdn/x.mp4", "c")
    assert not any(c[1].endswith("media_publish") for c in graph.calls)


def test_publish_reel_times_out(ig_config, monkeypatch):
    FakeGraph(monkeypatch, statuses=["IN_PROGRESS"])
    monkeypatch.setattr(ig, "REEL_POLL_TIMEOUT_S", 0)
    with pytest.raises(ig.InstagramPublishError, match="Timed out"):
        ig.publish_reel("https://cdn/x.mp4", "c")


def test_publish_reel_api_error_message(ig_config, monkeypatch):
    monkeypatch.setattr(ig.httpx, "post", lambda *a, **k: FakeResp({"error": {"message": "Bad video", "code": 2207026}}, 400))
    with pytest.raises(ig.InstagramPublishError, match="Bad video"):
        ig.publish_reel("https://cdn/x.mp4", "c")


def test_publish_reel_permalink_failure_is_not_fatal(ig_config, monkeypatch):
    FakeGraph(monkeypatch, permalink=None)
    assert ig.publish_reel("u", "c")["permalink"] is None


# --- endpoints ---


def make_gen(db, **over):
    row = VideoGeneration(**{**dict(prompt="p", provider="veo", model="m", aspect_ratio="9:16", duration_seconds=8, resolution="720p",
                                    status="succeeded", video_url="https://cdn/gen.mp4"), **over})
    db.add(row)
    db.commit()
    return row.id


def make_project(db, **over):
    row = VideoProject(**{**dict(engine="motion", title="t", params='{"aspect": "9:16", "duration": 20}', plan="{}", assets="{}",
                                 status="succeeded", video_url="https://cdn/proj.mp4"), **over})
    db.add(row)
    db.commit()
    return row.id


def test_publish_generation_with_hosted_url(client, db, ig_config, monkeypatch):
    graph = FakeGraph(monkeypatch)
    gid = make_gen(db)
    resp = client.post(f"/api/video/generations/{gid}/publish-instagram", json={"caption": "hi", "share_to_feed": True})
    assert resp.status_code == 200 and resp.json()["instagram_status"] == "publishing"
    got = client.get(f"/api/video/generations/{gid}").json()
    assert got["instagram_status"] == "published" and got["instagram_media_id"] == "media1"
    assert got["instagram_permalink"].endswith("/reel/abc/") and got["instagram_error"] is None
    assert graph.calls[0][2]["video_url"] == "https://cdn/gen.mp4"


def test_publish_project_uploads_local_file_first(client, db, ig_config, monkeypatch, tmp_path):
    from app.services.studio import runner

    graph = FakeGraph(monkeypatch)
    pid = make_project(db, video_url=None, local_path="final.mp4", params="{}")
    folder = runner.project_dir(pid)
    folder.mkdir(parents=True)
    (folder / "final.mp4").write_bytes(b"not really a video")
    uploads = []
    monkeypatch.setattr(settings, "cloudinary_url", "cloudinary://k:s@c")
    monkeypatch.setattr(media_hosting, "upload_video", lambda path, public_id=None: uploads.append((path, public_id)) or "https://res.cloudinary/x.mp4")
    assert client.post(f"/api/studio/projects/{pid}/publish-instagram", json={"caption": "c"}).status_code == 200
    got = client.get(f"/api/studio/projects/{pid}").json()
    assert uploads == [(str(folder / "final.mp4"), f"studio_project-{pid}")]
    assert got["instagram_status"] == "published" and got["video_url"] == "https://res.cloudinary/x.mp4"
    assert graph.calls[0][2]["video_url"] == "https://res.cloudinary/x.mp4"


def test_publish_needs_cloudinary_when_no_hosted_url(client, db, ig_config):
    pid = make_project(db, video_url=None, local_path="final.mp4")
    from app.services.studio import runner

    (runner.project_dir(pid)).mkdir(parents=True)
    (runner.project_dir(pid) / "final.mp4").write_bytes(b"x")
    resp = client.post(f"/api/studio/projects/{pid}/publish-instagram", json={"caption": "c"})
    assert resp.status_code == 503 and "CLOUDINARY_URL" in resp.json()["detail"]


def test_publish_container_error_marks_failed_and_retry_allowed(client, db, ig_config, monkeypatch):
    FakeGraph(monkeypatch, statuses=["ERROR"])
    gid = make_gen(db)
    client.post(f"/api/video/generations/{gid}/publish-instagram", json={"caption": "c"})
    got = client.get(f"/api/video/generations/{gid}").json()
    assert got["instagram_status"] == "failed" and "ERROR" in got["instagram_error"] and got["instagram_media_id"] is None
    FakeGraph(monkeypatch)  # retry now succeeds
    assert client.post(f"/api/video/generations/{gid}/publish-instagram", json={"caption": "c"}).status_code == 200
    assert client.get(f"/api/video/generations/{gid}").json()["instagram_status"] == "published"


def test_second_publish_while_publishing_is_409(client, db, ig_config, monkeypatch):
    gid = make_gen(db, instagram_status="publishing")
    resp = client.post(f"/api/video/generations/{gid}/publish-instagram", json={"caption": "c"})
    assert resp.status_code == 409 and "in progress" in resp.json()["detail"]


def test_already_published_is_409(client, db, ig_config):
    pid = make_project(db, instagram_status="published", instagram_media_id="m")
    resp = client.post(f"/api/studio/projects/{pid}/publish-instagram", json={"caption": "c"})
    assert resp.status_code == 409 and "already published" in resp.json()["detail"]


def test_publish_validation(client, db, ig_config, monkeypatch):
    FakeGraph(monkeypatch)
    gid = make_gen(db, status="running")
    assert client.post(f"/api/video/generations/{gid}/publish-instagram", json={"caption": "c"}).status_code == 409
    gid = make_gen(db)
    assert client.post(f"/api/video/generations/{gid}/publish-instagram", json={"caption": "x" * 2201}).status_code == 422
    assert client.post(f"/api/video/generations/{gid}/publish-instagram", json={"caption": " ".join(f"#t{i}" for i in range(31))}).status_code == 422
    short = make_gen(db, duration_seconds=2)
    assert "at least 3" in client.post(f"/api/video/generations/{short}/publish-instagram", json={"caption": "c"}).json()["detail"]
    assert client.post("/api/video/generations/999/publish-instagram", json={"caption": "c"}).status_code == 404


def test_publish_warnings_for_aspect_and_length(client, db, ig_config, monkeypatch):
    FakeGraph(monkeypatch)
    gid = make_gen(db, aspect_ratio="16:9", duration_seconds=120)
    warnings = client.post(f"/api/video/generations/{gid}/publish-instagram", json={"caption": "c"}).json()["instagram_warnings"]
    assert len(warnings) == 2 and any("9:16" in w for w in warnings) and any("90" in w for w in warnings)


def test_publish_503_when_instagram_unconfigured(client, db):
    gid = make_gen(db)
    assert client.post(f"/api/video/generations/{gid}/publish-instagram", json={"caption": "c"}).status_code == 503


def test_check_reel_measures_real_file(tmp_path):
    from tests.studio_helpers import make_test_video, needs_ffmpeg  # noqa: F401

    path = make_test_video(tmp_path / "v.mp4", seconds=4, size="160x284")
    assert instagram_publish.check_reel(path, "16:9", 100) == ([], [])  # file facts override metadata
    path = make_test_video(tmp_path / "w.mp4", seconds=4, size="320x180")
    errors, warnings = instagram_publish.check_reel(path, None, None)
    assert errors == [] and len(warnings) == 1


# --- migrations ---


def test_migration_adds_columns_to_existing_sqlite_tables_idempotently(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with eng.begin() as conn:
        for t in ("video_generations", "video_projects"):
            conn.execute(text(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY, instagram_status VARCHAR)"))
        conn.execute(text("INSERT INTO video_projects (id, instagram_status) VALUES (1, 'publishing')"))
    migrations.run_migrations(eng)
    migrations.run_migrations(eng)  # idempotent
    with eng.connect() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(video_projects)"))}
        row = conn.execute(text("SELECT instagram_status, instagram_error FROM video_projects")).one()
    assert {"instagram_media_id", "instagram_permalink", "instagram_status", "instagram_error"} <= cols
    assert row[0] == "failed" and "restart" in row[1]  # stuck 'publishing' rows are released

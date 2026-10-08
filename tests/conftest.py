import atexit
import io
import os
import shutil
import socket
import tempfile
from pathlib import Path

_TMP_DIR = Path(tempfile.mkdtemp(prefix="content_studio_tests_"))
_TEST_DB = _TMP_DIR / "test.db"
atexit.register(shutil.rmtree, _TMP_DIR, ignore_errors=True)

os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"
for _name in (
    "LOCAL_ACCESS_PASSWORD",
    "GEMINI_API_KEY",
    "HF_API_KEY_ID",
    "HF_API_KEY_SECRET",
    "MUAPI_API_KEY",
    "PEXELS_API_KEY",
    "PIXABAY_API_KEY",
    "IG_BUSINESS_ACCOUNT_ID",
    "IG_ACCESS_TOKEN",
    "IG_PAGE_ID",
    "X_BEARER_TOKEN",
    "X_API_KEY",
    "X_API_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
    "CLOUDINARY_URL",
    "GETTY_USERNAME",
    "GETTY_PASSWORD",
):
    os.environ[_name] = ""

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.config import settings
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import TopicCandidate
from app.services import gemini_client

assert Path(engine.url.database) == _TEST_DB, "tests must never run against the real database"

_SECRET_FIELDS = (
    "local_access_password",
    "gemini_api_key",
    "hf_api_key_id",
    "hf_api_key_secret",
    "muapi_api_key",
    "pexels_api_key",
    "pixabay_api_key",
    "ig_business_account_id",
    "ig_access_token",
    "ig_page_id",
    "x_bearer_token",
    "x_api_key",
    "x_api_secret",
    "x_access_token",
    "x_access_token_secret",
    "cloudinary_url",
    "getty_username",
    "getty_password",
)


@pytest.fixture(autouse=True)
def blank_settings(monkeypatch):
    for field in _SECRET_FIELDS:
        monkeypatch.setattr(settings, field, "")
    monkeypatch.setattr(gemini_client, "_client", None)


@pytest.fixture(autouse=True)
def isolated_video_storage(monkeypatch, tmp_path):
    from app.services import video_generation

    monkeypatch.setattr(video_generation, "VIDEOS_DIR", tmp_path / "videos")
    monkeypatch.setattr(video_generation, "POLL_INTERVAL_S", 0)


@pytest.fixture(autouse=True)
def isolated_studio_storage(monkeypatch, tmp_path):
    from app import config
    from app.services.studio import runner
    from app.services.studio.kit import stock

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(runner, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(stock, "CACHE_DIR", tmp_path / "cache")


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture(autouse=True)
def fresh_db():
    assert Path(engine.url.database) == _TEST_DB
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(fresh_db):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def configure_gemini(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")


@pytest.fixture
def make_topic(db):
    def _make(title="Topic", suitable_for="both", rationale="why", status="new"):
        topic = TopicCandidate(title=title, rationale=rationale, suitable_for=suitable_for, status=status)
        db.add(topic)
        db.commit()
        db.refresh(topic)
        return topic

    return _make


@pytest.fixture
def image_bytes():
    def _make(size=(300, 200), color=(120, 40, 200), fmt="PNG"):
        buf = io.BytesIO()
        Image.new("RGB", size, color).save(buf, format=fmt)
        return buf.getvalue()

    return _make

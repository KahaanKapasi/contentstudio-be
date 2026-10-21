from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage"
UPLOADS_DIR = STORAGE_DIR / "uploads"
VIDEOS_DIR = STORAGE_DIR / "videos"
PROJECTS_DIR = STORAGE_DIR / "projects"  # Video Studio engine projects: inputs, stage outputs, final.mp4
CACHE_DIR = STORAGE_DIR / "cache"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(BASE_DIR.parent / ".env", BASE_DIR / ".env"), extra="ignore", env_ignore_empty=True)

    database_url: str = f"sqlite:///{STORAGE_DIR / 'content_studio.db'}"

    @field_validator("database_url")
    @classmethod
    def _use_psycopg_driver(cls, v: str) -> str:
        # Neon/Render/Supabase all hand out plain postgres(ql):// URLs, which
        # SQLAlchemy defaults to psycopg2 for. We install psycopg (v3)
        # instead, so upgrade the scheme automatically rather than asking
        # whoever sets DATABASE_URL to remember the "+psycopg" suffix.
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://") :]
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://") :]
        return v
    local_access_password: str = ""

    gemini_api_key: str = ""
    gemini_text_model: str = "gemini-3.7-flash"
    # Cheap/fast model for simple extraction tasks (e.g. Discovery topic generation). Empty = use gemini_text_model.
    gemini_fast_model: str = ""
    # Verified against google-genai 1.75.0: generate_content + response_modalities=["AUDIO"] + SpeechConfig.
    gemini_tts_model: str = "gemini-3.8-flash-tts"
    gemini_image_model: str = "gemini-3.1-flash-image-preview"

    # Discovery: re-scrape and generate topics every N minutes in the background (0 = off). Meant for an always-on host.
    auto_scrape_minutes: int = 0

    pexels_api_key: str = ""
    pixabay_api_key: str = ""  # optional second stock source

    hf_api_key_id: str = ""
    hf_api_key_secret: str = ""
    # Official-SDK style credential "key-id:key-secret". Either this or the separate id/secret pair above works.
    hf_key: str = ""

    muapi_api_key: str = ""

    ig_business_account_id: str = ""
    ig_access_token: str = ""
    ig_page_id: str = ""
    ig_graph_base: str = "https://graph.facebook.com/v21.0"

    x_bearer_token: str = ""
    x_api_key: str = ""
    x_api_secret: str = ""
    x_access_token: str = ""
    x_access_token_secret: str = ""

    cloudinary_url: str = ""

    # Footonomy admin API (the football app). Studio only ever calls out to it; Footonomy never calls Studio.
    # URL is the app's private admin port (e.g. http://127.0.0.1:8082, or its Tailscale address). Empty token = integration off.
    footonomy_admin_url: str = "http://127.0.0.1:8082"
    footonomy_admin_token: str = ""

    # Cost awareness: optional JSON map of price id -> usd that overrides the built-in price table,
    # e.g. {"gemini.image.out": 0.08}. See app/services/costs/prices.py.
    price_overrides_json: str = ""

    # Self-hosting: path to a built frontend (frontend/dist). When set, the API also serves the UI on the same origin.
    frontend_dist: str = ""

    # Subject-cutout model for the Posts "background removed" template (rembg). See services/image_processing.py.
    rembg_model: str = "u2net"
    # Preload the cutout model at startup (skipped automatically on <1.5 GB hosts). Set false to load it lazily.
    warm_cutout: bool = True

    getty_username: str = ""
    getty_password: str = ""


settings = Settings()

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

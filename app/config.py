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
    # Verified against google-genai 1.75.0: generate_content + response_modalities=["AUDIO"] + SpeechConfig.
    gemini_tts_model: str = "gemini-3.8-flash-tts"
    gemini_image_model: str = "gemini-3.1-flash-image-preview"

    pexels_api_key: str = ""
    pixabay_api_key: str = ""  # optional second stock source

    hf_api_key_id: str = ""
    hf_api_key_secret: str = ""

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

    getty_username: str = ""
    getty_password: str = ""


settings = Settings()

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage"
UPLOADS_DIR = STORAGE_DIR / "uploads"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR.parent / ".env", extra="ignore")

    database_url: str = f"sqlite:///{STORAGE_DIR / 'content_studio.db'}"
    local_access_password: str = ""

    gemini_api_key: str = ""

    ig_business_account_id: str = ""
    ig_access_token: str = ""
    ig_page_id: str = ""

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

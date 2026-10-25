"""Public media hosting — required because Instagram's publish API fetches
media via public URL, not local file upload (per 04_Posts_Carousel_Studio.md).

Cloudinary chosen as the default (simplest unsigned-upload API, generous free
tier) — this was an open item in the doc; swap this module's implementation
if a different host is preferred later, callers only depend on `upload_image`.
"""

from urllib.parse import urlparse

import cloudinary
import cloudinary.uploader

from app.config import settings


class MediaHostingNotConfigured(RuntimeError):
    pass


# Cloudinary's free plan rejects images larger than 10 MB. Checked before the round trip so the editor gets a clear 413.
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def status() -> dict:
    """Is image hosting usable? `missing` names exactly what to set (shown in the editor's publish / send-to-phone dialogs)."""
    url = settings.cloudinary_url
    if not url:
        return {"configured": False, "missing": ["CLOUDINARY_URL"], "problem": "CLOUDINARY_URL is not set on the backend.", "max_image_bytes": MAX_IMAGE_BYTES}
    parsed = urlparse(url)
    if parsed.scheme != "cloudinary" or not (parsed.hostname and parsed.username and parsed.password):
        return {
            "configured": False,
            "missing": ["CLOUDINARY_URL"],
            "problem": "CLOUDINARY_URL is set but malformed. It must look like cloudinary://API_KEY:API_SECRET@CLOUD_NAME.",
            "max_image_bytes": MAX_IMAGE_BYTES,
        }
    return {"configured": True, "missing": [], "problem": None, "max_image_bytes": MAX_IMAGE_BYTES}


_configured = False


def _ensure_configured():
    global _configured
    if not settings.cloudinary_url:
        raise MediaHostingNotConfigured("CLOUDINARY_URL is not set in .env")
    if not _configured:
        # cloudinary.config() has no cloudinary_url kwarg; the SDK only reads that
        # URL from os.environ, but pydantic loads .env without exporting it. Parse it.
        parsed = urlparse(settings.cloudinary_url)
        cloudinary.config(
            cloud_name=parsed.hostname,
            api_key=parsed.username,
            api_secret=parsed.password,
            secure=True,
        )
        _configured = True


def upload_image(image_bytes: bytes, public_id: str | None = None) -> str:
    """Uploads image bytes, returns the public HTTPS URL."""
    _ensure_configured()
    result = cloudinary.uploader.upload(image_bytes, public_id=public_id, resource_type="image")
    return result["secure_url"]


def upload_video(path: str, public_id: str | None = None) -> str:
    """Uploads a local video file (chunked, so big mp4s are fine), returns the public HTTPS URL."""
    _ensure_configured()
    result = cloudinary.uploader.upload_large(path, public_id=public_id, resource_type="video")
    return result["secure_url"]

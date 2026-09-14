"""Public media hosting — required because Instagram's publish API fetches
media via public URL, not local file upload (per 04_Posts_Carousel_Studio.md).

Cloudinary chosen as the default (simplest unsigned-upload API, generous free
tier) — this was an open item in the doc; swap this module's implementation
if a different host is preferred later, callers only depend on `upload_image`.
"""

import cloudinary
import cloudinary.uploader

from app.config import settings


class MediaHostingNotConfigured(RuntimeError):
    pass


_configured = False


def _ensure_configured():
    global _configured
    if not settings.cloudinary_url:
        raise MediaHostingNotConfigured("CLOUDINARY_URL is not set in .env")
    if not _configured:
        cloudinary.config(cloudinary_url=settings.cloudinary_url)
        _configured = True


def upload_image(image_bytes: bytes, public_id: str | None = None) -> str:
    """Uploads image bytes, returns the public HTTPS URL."""
    _ensure_configured()
    result = cloudinary.uploader.upload(image_bytes, public_id=public_id, resource_type="image")
    return result["secure_url"]

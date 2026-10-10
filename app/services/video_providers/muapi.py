"""Muapi.ai hosted API: POST /api/v1/{endpoint} -> poll /api/v1/predictions/{id}/result -> outputs[0]."""

import io
import logging
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageOps

from app.config import settings
from app.services.video_providers import (
    JobHandle,
    JobParams,
    PollResult,
    ProviderError,
    ProviderNotConfigured,
)
from app.services.video_providers import catalog, http_common

log = logging.getLogger(__name__)
UPLOAD_MAX_SIDE_PX = 2048
_SUCCESS = {"completed", "succeeded", "success"}
_FAILURE = {"failed", "error", "cancelled", "canceled"}


class MuapiProvider:
    id = "muapi"
    label = "Muapi"

    def missing_keys(self) -> list[str]:
        return [] if settings.muapi_api_key else ["MUAPI_API_KEY"]

    def _client(self) -> httpx.Client:
        if not settings.muapi_api_key:
            raise ProviderNotConfigured(self.id, self.missing_keys())
        return httpx.Client(timeout=http_common.TIMEOUT, headers={"x-api-key": settings.muapi_api_key})

    def upload_image(self, image_path: str) -> str:
        """Public URL for a local image: Muapi's own POST /api/v1/upload_file (multipart `file`), falling back to
        Cloudinary when that fails and Cloudinary is configured. The image is sent as a <=2048px JPEG."""
        data = _jpeg_bytes(image_path)
        try:
            with self._client() as client:
                reply = http_common.request(
                    client, "POST", f"{catalog.MUAPI_BASE_URL}/api/v1/upload_file", label="Muapi",
                    files={"file": (Path(image_path).stem + ".jpg", data, "image/jpeg")},
                )
            url = _uploaded_url(reply)
            if url:
                return url
            raise ProviderError("Muapi did not return a link for the uploaded image.")
        except ProviderNotConfigured:
            raise
        except ProviderError:
            if not settings.cloudinary_url:
                raise
            log.warning("Muapi upload_file failed; falling back to Cloudinary", exc_info=True)
        from app.services import media_hosting

        try:
            return media_hosting.upload_image(data)
        except Exception as exc:
            raise ProviderError(f"Could not upload the start image ({type(exc).__name__}).") from exc

    def submit(self, params: JobParams) -> JobHandle:
        spec = catalog.get_model(self.id, params.model)
        image_url = None
        if spec.image_to_video:
            if not params.image_path:
                raise ProviderError(f"{spec.label} needs a start image.")
            image_url = self.upload_image(params.image_path)
        body = catalog.build_body(spec, params.prompt, params.aspect_ratio, params.duration_seconds, params.resolution, image_url)
        with self._client() as client:
            data = http_common.request(
                client, "POST", f"{catalog.MUAPI_BASE_URL}/api/v1/{spec.path}", label="Muapi", json=body
            )
        request_id = data.get("request_id") or data.get("id")
        if not request_id:
            raise ProviderError("Muapi did not return a request id.")
        return JobHandle(str(request_id), f"{catalog.MUAPI_BASE_URL}/api/v1/predictions/{request_id}/result")

    def poll(self, job: JobHandle) -> PollResult:
        url = job.status_url or f"{catalog.MUAPI_BASE_URL}/api/v1/predictions/{job.job_id}/result"
        with self._client() as client:
            data = http_common.request(client, "GET", url, label="Muapi", polling=True)
        status = str(data.get("status") or "").lower()
        if status in _SUCCESS:
            video_url = http_common.find_video_url(data)
            if not video_url:
                return PollResult("failed", error="Muapi finished but returned no video.")
            return PollResult("succeeded", output=video_url)
        if status in _FAILURE:
            return PollResult("failed", error=f"Muapi could not generate this video{_detail(data)}")
        return PollResult("running")

    def download(self, output: Any, dest: Path) -> None:
        http_common.download_url(output, dest)


def _detail(data: dict) -> str:
    err = data.get("error")
    msg = err if isinstance(err, str) else (err or {}).get("message") if isinstance(err, dict) else data.get("message")
    return f": {str(msg)[:200]}" if isinstance(msg, str) and msg else "."


def _jpeg_bytes(path: str) -> bytes:
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((UPLOAD_MAX_SIDE_PX, UPLOAD_MAX_SIDE_PX))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=92)
    except OSError as exc:
        raise ProviderError("The start image could not be read.") from exc
    return buf.getvalue()


def _uploaded_url(reply: dict) -> str | None:
    """upload_file has no response schema in the OpenAPI, so accept the usual shapes."""
    inner = reply.get("data") if isinstance(reply.get("data"), dict) else {}
    for candidate in (reply.get("url"), reply.get("file_url"), reply.get("image_url"), inner.get("url"), inner.get("file_url")):
        if isinstance(candidate, str) and candidate.startswith("http"):
            return candidate
    return None

"""Muapi.ai hosted API: POST /api/v1/{endpoint} -> poll /api/v1/predictions/{id}/result -> outputs[0]."""

from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.services.video_providers import (
    JobHandle,
    JobParams,
    PollResult,
    ProviderError,
    ProviderNotConfigured,
)
from app.services.video_providers import catalog, http_common

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

    def submit(self, params: JobParams) -> JobHandle:
        spec = catalog.get_model(self.id, params.model)
        body = catalog.build_body(spec, params.prompt, params.aspect_ratio, params.duration_seconds, params.resolution)
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

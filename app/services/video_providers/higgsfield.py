"""Higgsfield platform API (docs.higgsfield.ai): POST /{model path} -> poll status_url -> video.url."""

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

_TERMINAL_FAILED = {"failed": "Higgsfield could not generate this video.", "nsfw": "Higgsfield's moderation blocked this prompt.", "canceled": "The request was canceled at Higgsfield.", "cancelled": "The request was canceled at Higgsfield."}


class HiggsfieldProvider:
    id = "higgsfield"
    label = "Higgsfield"

    def missing_keys(self) -> list[str]:
        missing = []
        if not settings.hf_api_key_id:
            missing.append("HF_API_KEY_ID")
        if not settings.hf_api_key_secret:
            missing.append("HF_API_KEY_SECRET")
        return missing

    def _client(self) -> httpx.Client:
        missing = self.missing_keys()
        if missing:
            raise ProviderNotConfigured(self.id, missing)
        return httpx.Client(
            timeout=http_common.TIMEOUT,
            headers={"Authorization": f"Key {settings.hf_api_key_id}:{settings.hf_api_key_secret}"},
        )

    def submit(self, params: JobParams) -> JobHandle:
        spec = catalog.get_model(self.id, params.model)
        body = catalog.build_body(spec, params.prompt, params.aspect_ratio, params.duration_seconds, params.resolution)
        headers = {"Idempotency-Key": params.idempotency_key} if params.idempotency_key else {}
        with self._client() as client:
            data = http_common.request(
                client, "POST", f"{catalog.HIGGSFIELD_BASE_URL}{spec.path}", label="Higgsfield", json=body, headers=headers
            )
        request_id = data.get("request_id")
        if not request_id:
            raise ProviderError("Higgsfield did not return a request id.")
        status_url = data.get("status_url") or f"{catalog.HIGGSFIELD_BASE_URL}/requests/{request_id}/status"
        return JobHandle(str(request_id), status_url)

    def poll(self, job: JobHandle) -> PollResult:
        url = job.status_url or f"{catalog.HIGGSFIELD_BASE_URL}/requests/{job.job_id}/status"
        with self._client() as client:
            data = http_common.request(client, "GET", url, label="Higgsfield", polling=True)
        status = str(data.get("status") or "").lower()
        if status == "completed":
            video_url = http_common.find_video_url(data)
            if not video_url:
                return PollResult("failed", error="Higgsfield finished but returned no video.")
            return PollResult("succeeded", output=video_url)
        if status in _TERMINAL_FAILED:
            detail = data.get("error")
            msg = _TERMINAL_FAILED[status] + (f" ({detail})" if isinstance(detail, str) and detail else "")
            return PollResult("failed", error=msg)
        return PollResult("running")  # queued / in_progress / anything unknown

    def download(self, output: Any, dest: Path) -> None:
        http_common.download_url(output, dest)

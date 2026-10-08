"""Google Veo through google-genai. The operation name is all we persist, so a restarted server
rebuilds the operation from it and keeps polling. Veo outputs are deleted by Google after ~2 days,
which is why the service downloads the moment a job succeeds."""

from pathlib import Path
from typing import Any

from google.genai import errors as genai_errors
from google.genai import types

from app.config import settings
from app.services import gemini_client
from app.services.video_providers import (
    JobHandle,
    JobParams,
    PollResult,
    ProviderError,
    ProviderNotConfigured,
    TransientProviderError,
)


class VeoProvider:
    id = "veo"
    label = "Google Veo"

    def missing_keys(self) -> list[str]:
        return [] if settings.gemini_api_key else ["GEMINI_API_KEY"]

    def _client(self):
        if not settings.gemini_api_key:
            raise ProviderNotConfigured(self.id, self.missing_keys())
        return gemini_client.get_client()

    def submit(self, params: JobParams) -> JobHandle:
        client = self._client()
        extra = {"image": types.Image.from_file(location=params.image_path)} if params.image_path else {}
        try:
            operation = client.models.generate_videos(
                model=params.model,
                prompt=params.prompt,
                **extra,
                config=types.GenerateVideosConfig(
                    aspect_ratio=params.aspect_ratio,
                    duration_seconds=params.duration_seconds,
                    resolution=params.resolution,
                ),
            )
        except genai_errors.APIError as exc:
            raise ProviderError(_api_error_message(exc)) from exc
        if not operation.name:
            raise ProviderError("Veo did not return an operation id.")
        return JobHandle(operation.name)

    def poll(self, job: JobHandle) -> PollResult:
        client = self._client()
        try:
            operation = client.operations.get(types.GenerateVideosOperation(name=job.job_id))
        except genai_errors.ServerError as exc:
            raise TransientProviderError(str(exc)) from exc
        except genai_errors.APIError as exc:
            raise ProviderError(_api_error_message(exc)) from exc
        if not operation.done:
            return PollResult("running")
        if operation.error:
            message = operation.error.get("message") if isinstance(operation.error, dict) else None
            return PollResult("failed", error=f"Veo could not generate this video: {str(message or 'unknown error')[:200]}")
        response = operation.response or operation.result
        videos = (response.generated_videos if response else None) or []
        if not videos or not videos[0].video:
            reasons = ", ".join(response.rai_media_filtered_reasons or []) if response else ""
            suffix = f" ({reasons[:200]})" if reasons else ""
            return PollResult("failed", error=f"Veo blocked this video with its safety filters{suffix}. Try rewording the prompt.")
        return PollResult("succeeded", output=videos[0].video)

    def download(self, output: Any, dest: Path) -> None:
        data = getattr(output, "video_bytes", None)
        if not data:
            try:
                data = self._client().files.download(file=output)
            except genai_errors.APIError as exc:
                raise ProviderError(_api_error_message(exc)) from exc
        if not data:
            raise ProviderError("Veo returned an empty video file.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_suffix(dest.suffix + ".part")
        part.write_bytes(data)
        part.replace(dest)


def _api_error_message(exc: genai_errors.APIError) -> str:
    code = getattr(exc, "code", None)
    detail = (getattr(exc, "message", None) or "").strip().splitlines()[0:1]
    detail = detail[0][:200] if detail else ""
    hints = {
        400: "the request was rejected (check model, format and prompt)",
        401: "the API key was rejected",
        403: "the API key does not have access to Veo (it needs a paid Gemini API plan)",
        404: "the model was not found",
        429: "quota or rate limit reached",
    }
    reason = hints.get(code, "the request failed")
    return f"Veo: {reason}{f' ({code})' if code else ''}. {detail}".strip()

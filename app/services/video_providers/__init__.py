"""Video generation providers behind one interface:

    submit(params) -> JobHandle          start the job at the provider
    poll(job)      -> PollResult         one non-blocking status check
    download(output, dest)               write the finished mp4 to `dest`

A JobHandle is just two strings so it can live in the DB and be rebuilt after a restart.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class ProviderNotConfigured(RuntimeError):
    def __init__(self, provider: str, missing_keys: list[str]):
        self.provider = provider
        self.missing_keys = missing_keys
        super().__init__(f"{provider} is not configured: set {', '.join(missing_keys)} in .env")


class ProviderError(RuntimeError):
    """Permanent failure; the message is safe to show the user."""


class TransientProviderError(RuntimeError):
    """Network blip / 5xx while polling — keep the job running and try again."""


@dataclass
class JobParams:
    prompt: str
    model: str
    aspect_ratio: str
    duration_seconds: int
    resolution: str
    idempotency_key: str | None = None


@dataclass
class JobHandle:
    job_id: str
    status_url: str | None = None


@dataclass
class PollResult:
    state: str  # running | succeeded | failed
    output: Any = None  # provider-specific; handed back to download()
    error: str | None = None


class VideoProvider(Protocol):
    id: str

    def missing_keys(self) -> list[str]: ...
    def submit(self, params: JobParams) -> JobHandle: ...
    def poll(self, job: JobHandle) -> PollResult: ...
    def download(self, output: Any, dest: Path) -> None: ...


def get_provider(provider_id: str) -> VideoProvider:
    # Imported lazily so importing the package never pulls in google-genai/httpx for nothing.
    if provider_id == "veo":
        from app.services.video_providers.veo import VeoProvider

        return VeoProvider()
    if provider_id == "higgsfield":
        from app.services.video_providers.higgsfield import HiggsfieldProvider

        return HiggsfieldProvider()
    if provider_id == "muapi":
        from app.services.video_providers.muapi import MuapiProvider

        return MuapiProvider()
    raise ProviderError(f"Unknown provider '{provider_id}'.")

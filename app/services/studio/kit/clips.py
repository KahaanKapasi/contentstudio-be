"""Synchronous wrapper over the single-clip video providers (doc 09), for scene pipelines that need one
generated clip per scene: submit, poll, download, return the local file."""

import time
import uuid
from pathlib import Path

from app.services.video_providers import (
    JobHandle,
    JobParams,
    PollResult,
    ProviderError,
    ProviderNotConfigured,
    TransientProviderError,
    catalog,
    get_provider,
)

POLL_INTERVAL_S = 5.0
CLIP_TIMEOUT_S = 10 * 60


def estimate_cost(provider: str, model: str, resolution: str, seconds: int) -> float | None:
    return catalog.estimate_cost(provider, model, resolution, seconds)


def generate_clip(
    prompt: str, dest: Path, *, provider: str, model: str, aspect_ratio: str, duration_seconds: int, resolution: str,
    image: Path | None = None, timeout: float = CLIP_TIMEOUT_S,
) -> Path:
    """Blocks until the provider has produced the clip and it is saved at `dest`.
    Raises catalog.CatalogError for unsupported combos, ProviderNotConfigured for missing keys, ProviderError otherwise.
    `image` (image-to-video) is honoured by Veo and by Muapi's image-to-video models; given a Muapi text-to-video
    model, its image-to-video sibling is used instead (duration/resolution snapped to what that one offers).
    Providers with no image-to-video endpoint (Higgsfield) ignore the image."""
    if image and not catalog.supports_image(provider, model):
        sibling = catalog.image_variant(provider, model)
        if sibling:
            model = sibling
            duration_seconds, resolution = catalog.fit(catalog.get_model(provider, model), duration_seconds, resolution)
    catalog.validate(provider, model, aspect_ratio, duration_seconds, resolution)
    impl = get_provider(provider)
    missing = impl.missing_keys()
    if missing:
        raise ProviderNotConfigured(provider, missing)
    params = JobParams(
        prompt, model, aspect_ratio, duration_seconds, resolution,
        idempotency_key=str(uuid.uuid4()), image_path=str(image) if image else None,
    )
    job: JobHandle = impl.submit(params)
    deadline = time.monotonic() + timeout
    while True:
        try:
            result: PollResult = impl.poll(job)
        except TransientProviderError:
            result = PollResult("running")
        if result.state == "succeeded":
            impl.download(result.output, dest)
            _record_usage(provider, model, resolution, duration_seconds)
            return dest
        if result.state == "failed":
            raise ProviderError(result.error or "The provider could not generate this clip.")
        if time.monotonic() > deadline:
            raise ProviderError(f"The clip did not finish within {int(timeout // 60)} minutes.")
        time.sleep(POLL_INTERVAL_S)


def _record_usage(provider: str, model: str, resolution: str, seconds: int) -> None:
    """Add the finished (billed) clip to the active project's usage; unknown prices are recorded as estimates."""
    try:
        from app.services.costs import usage

        project = usage.current()
        if project is not None:
            project.add_clip(provider, model, resolution, seconds, catalog.estimate_cost(provider, model, resolution, seconds))
    except Exception:
        pass

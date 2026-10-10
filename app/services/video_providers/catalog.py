"""Provider/model catalog for video generation — data first, logic second.

Prices come from the provider docs (Veo) or are None when unknown (Higgsfield bills in credits
that vary by model/params; the API has a per-request estimate endpoint but we don't call it).
Higgsfield paths and body fields were checked against docs.higgsfield.ai (per-model pages and
openapi.json); anything not confirmed there must say so in `notes`.
"""

from dataclasses import dataclass, field


class CatalogError(ValueError):
    """Unsupported provider/model/format combination; message is shown to the user as-is."""


@dataclass(frozen=True)
class ModelSpec:
    id: str
    provider: str
    label: str
    aspect_ratios: tuple[str, ...]
    durations: tuple[int, ...]
    resolutions: tuple[str, ...]
    price_per_second_usd: dict[str, float] | None = None
    notes: str | None = None
    # Higgsfield/Muapi only: endpoint path, which request fields the model accepts, and fixed extras.
    path: str | None = None
    body_fields: tuple[str, ...] = ()  # subset of aspect_ratio / duration / resolution
    extra_body: dict = field(default_factory=dict)
    field_map: dict = field(default_factory=dict)  # our field name -> provider's name, e.g. resolution -> quality
    # Resolution values that only work at a specific duration (Veo: 1080p/4k need 8s).
    resolution_durations: dict[str, tuple[int, ...]] = field(default_factory=dict)
    # Muapi image-to-video: the request field that carries the start frame ("image_url", or "images_list" = list of URLs).
    # `image_to_video` models need an image; they are kept out of `MODELS` (the plain text-to-video picker).
    image_to_video: bool = False
    image_field: str = "image_url"


PROVIDER_LABELS = {"veo": "Google Veo", "higgsfield": "Higgsfield", "muapi": "Muapi"}
DEFAULT_MODELS = {"veo": "veo-3.1-fast-generate-preview", "higgsfield": "seedance-2.0", "muapi": "wan2.5-text-to-video"}
PROVIDER_ENV_KEYS = {
    "veo": ("GEMINI_API_KEY",),
    "higgsfield": ("HF_API_KEY_ID", "HF_API_KEY_SECRET"),
    "muapi": ("MUAPI_API_KEY",),
}

HIGGSFIELD_BASE_URL = "https://api.higgsfield.ai"
MUAPI_BASE_URL = "https://api.muapi.ai"

_VEO_NOTE = "Paid only. Native audio. Google deletes outputs after ~2 days; the app downloads immediately."
_VEO_HQ = {"1080p": (8,), "4k": (8,)}
_HF_NOTE = "Billed in Higgsfield credits (price varies by params)."

VEO_MODELS = {
    m.id: m
    for m in (
        ModelSpec(
            "veo-3.1-lite-generate-preview", "veo", "Veo 3.1 Lite",
            ("16:9", "9:16"), (4, 6, 8), ("720p", "1080p"),
            {"720p": 0.05, "1080p": 0.08}, _VEO_NOTE, resolution_durations=_VEO_HQ,
        ),
        ModelSpec(
            "veo-3.1-fast-generate-preview", "veo", "Veo 3.1 Fast",
            ("16:9", "9:16"), (4, 6, 8), ("720p", "1080p", "4k"),
            {"720p": 0.10, "1080p": 0.12, "4k": 0.30}, _VEO_NOTE, resolution_durations=_VEO_HQ,
        ),
        ModelSpec(
            "veo-3.1-generate-preview", "veo", "Veo 3.1",
            ("16:9", "9:16"), (4, 6, 8), ("720p", "1080p", "4k"),
            {"720p": 0.40, "1080p": 0.40, "4k": 0.60}, _VEO_NOTE, resolution_durations=_VEO_HQ,
        ),
    )
}

# model id -> spec. `path` is the only place an endpoint is named; change it here, not in code.
HIGGSFIELD_MODELS = {
    m.id: m
    for m in (
        ModelSpec(
            "seedance-2.0", "higgsfield", "Seedance 2.0",
            ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9"), (4, 5, 6, 8, 10, 12, 15),
            ("480p", "720p", "1080p", "4k"), None,
            f"{_HF_NOTE} Native audio. Path verified against docs.higgsfield.ai.",
            path="/bytedance/seedance-2.0/text-to-video",
            body_fields=("aspect_ratio", "duration", "resolution"), extra_body={"generate_audio": True},
        ),
        ModelSpec(
            "seedance-2.5", "higgsfield", "Seedance 2.5",
            ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9"), (4, 5, 6, 8, 10, 15, 20, 30),
            ("480p", "720p", "1080p"), None,
            f"{_HF_NOTE} Native audio, up to 30s. Path verified against docs.higgsfield.ai.",
            path="/bytedance/seedance-2.5/text-to-video",
            body_fields=("aspect_ratio", "duration", "resolution"), extra_body={"generate_audio": True},
        ),
        ModelSpec(
            "kling-3.0-turbo", "higgsfield", "Kling 3.0 Turbo",
            ("16:9", "9:16", "1:1"), (3, 5, 8, 10, 15), ("720p", "1080p"), None,
            f"{_HF_NOTE} No audio field. Path verified against docs.higgsfield.ai.",
            path="/kling-video/v3.0-turbo/text-to-video", body_fields=("aspect_ratio", "duration", "resolution"),
        ),
        ModelSpec(
            "kling-3.0-std", "higgsfield", "Kling 3.0 Standard",
            ("16:9", "9:16", "1:1"), (3, 5, 8, 10, 15), ("default",), None,
            f"{_HF_NOTE} Native audio. Resolution is not selectable. Path verified against docs.higgsfield.ai.",
            path="/kling-video/v3.0/std/text-to-video", body_fields=("aspect_ratio", "duration"),
            extra_body={"sound": "on"},
        ),
        ModelSpec(
            "kling-3.0-pro", "higgsfield", "Kling 3.0 Pro",
            ("16:9", "9:16", "1:1"), (3, 5, 8, 10, 15), ("default",), None,
            f"{_HF_NOTE} Native audio. Resolution is not selectable. Path verified against docs.higgsfield.ai.",
            path="/kling-video/v3.0/pro/text-to-video", body_fields=("aspect_ratio", "duration"),
            extra_body={"sound": "on"},
        ),
    )
}

_MU_NOTE = "Billed in Muapi credits. Endpoint and fields checked against Open-Generative-AI models.js."

# model id -> spec; `path` is the Muapi endpoint id (POST /api/v1/{path}). Prices are None: Muapi's
# per-model prices aren't in models.js, so we don't guess.
MUAPI_MODELS = {
    m.id: m
    for m in (
        ModelSpec(
            "wan2.5-text-to-video", "muapi", "Wan 2.5",
            ("16:9", "9:16"), (5, 10), ("480p", "720p", "1080p"), None, _MU_NOTE,
            path="wan2.5-text-to-video", body_fields=("aspect_ratio", "duration", "resolution"),
        ),
        ModelSpec(
            "wan2.6-text-to-video", "muapi", "Wan 2.6",
            ("16:9", "9:16"), (5, 10, 15), ("720p", "1080p"), None, _MU_NOTE,
            path="wan2.6-text-to-video", body_fields=("aspect_ratio", "duration", "resolution"),
        ),
        ModelSpec(
            "kling-v3.0-standard-text-to-video", "muapi", "Kling 3.0 Standard",
            ("16:9", "9:16", "1:1"), (3, 5, 8, 10, 15), ("720p",), None,
            f"{_MU_NOTE} Native audio; resolution is fixed.",
            path="kling-v3.0-standard-text-to-video", body_fields=("aspect_ratio", "duration"),
            extra_body={"resolution": "720p", "generate_audio": True},
        ),
        ModelSpec(
            "kling-v3.0-pro-text-to-video", "muapi", "Kling 3.0 Pro",
            ("16:9", "9:16", "1:1"), (3, 5, 8, 10, 15), ("1080p",), None,
            f"{_MU_NOTE} Native audio; resolution is fixed.",
            path="kling-v3.0-pro-text-to-video", body_fields=("aspect_ratio", "duration"),
            extra_body={"resolution": "1080p", "generate_audio": True},
        ),
        ModelSpec(
            "seedance-v2.0-t2v", "muapi", "Seedance 2.0",
            ("16:9", "9:16", "4:3", "3:4"), (5, 10, 15), ("basic", "high"), None,
            f"{_MU_NOTE} Resolution here is the Seedance quality tier (basic/high).",
            path="seedance-v2.0-t2v", body_fields=("aspect_ratio", "duration", "resolution"),
            field_map={"resolution": "quality"},
        ),
        ModelSpec(
            "veo3.1-lite-text-to-video", "muapi", "Veo 3.1 Lite",
            ("16:9", "9:16"), (8,), ("720p", "1080p", "4k"), None, _MU_NOTE,
            path="veo3.1-lite-text-to-video", body_fields=("aspect_ratio", "duration", "resolution"),
        ),
        ModelSpec(
            "veo3.1-text-to-video", "muapi", "Veo 3.1",
            ("16:9", "9:16"), (8,), ("720p", "1080p", "4k"), None, _MU_NOTE,
            path="veo3.1-text-to-video", body_fields=("aspect_ratio", "duration", "resolution"),
        ),
    )
}

_MU_I2V = "Image-to-video: the shot still becomes the start frame, so the character keeps its look. Fields verified against api.muapi.ai/openapi.json (2026-10-09); billed in Muapi credits, price not published."
_I2V = dict(image_to_video=True)

# Muapi image-to-video endpoints (exact ids and request fields from https://api.muapi.ai/openapi.json). Aspect
# ratio is taken from the start image on most of them, so it is not sent unless the schema has the field.
MUAPI_I2V_MODELS = {
    m.id: m
    for m in (
        ModelSpec(
            "kling-v3.0-standard-image-to-video", "muapi", "Kling 3.0 Standard (image-to-video)",
            ("16:9", "9:16", "1:1"), (3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15), ("720p",), None,
            f"{_MU_I2V} Native audio. Aspect ratio follows the image.",
            path="kling-v3.0-standard-image-to-video", body_fields=("duration",), extra_body={"generate_audio": True}, **_I2V,
        ),
        ModelSpec(
            "kling-v3.0-pro-image-to-video", "muapi", "Kling 3.0 Pro (image-to-video)",
            ("16:9", "9:16", "1:1"), (3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15), ("1080p",), None,
            f"{_MU_I2V} Native audio. Aspect ratio follows the image.",
            path="kling-v3.0-pro-image-to-video", body_fields=("duration",), extra_body={"generate_audio": True}, **_I2V,
        ),
        ModelSpec(
            "kling-v2.6-pro-i2v", "muapi", "Kling 2.6 Pro (image-to-video)",
            ("16:9", "9:16", "1:1"), (5, 10), ("1080p",), None,
            f"{_MU_I2V} Aspect ratio follows the image.",
            path="kling-v2.6-pro-i2v", body_fields=("duration",), extra_body={"sound": True}, **_I2V,
        ),
        ModelSpec(
            "seedance-v2.0-i2v", "muapi", "Seedance 2.0 (image-to-video)",
            ("16:9", "9:16", "4:3", "3:4"), (5, 10, 15), ("480p", "720p"), None,
            f"{_MU_I2V} The image goes in `images_list` and is referenced as @image1 in the prompt.",
            path="seedance-v2.0-i2v", body_fields=("aspect_ratio", "duration", "resolution"), image_field="images_list", **_I2V,
        ),
        ModelSpec(
            "wan2.6-image-to-video", "muapi", "Wan 2.6 (image-to-video)",
            ("16:9", "9:16", "1:1"), (5, 10, 15), ("720p", "1080p"), None,
            f"{_MU_I2V} Aspect ratio follows the image.",
            path="wan2.6-image-to-video", body_fields=("duration", "resolution"), **_I2V,
        ),
        ModelSpec(
            "wan2.5-image-to-video", "muapi", "Wan 2.5 (image-to-video)",
            ("16:9", "9:16", "1:1"), (5, 10), ("480p", "720p", "1080p"), None,
            f"{_MU_I2V} Aspect ratio follows the image.",
            path="wan2.5-image-to-video", body_fields=("duration", "resolution"), **_I2V,
        ),
        ModelSpec(
            "veo3.1-lite-image-to-video", "muapi", "Veo 3.1 Lite (image-to-video)",
            ("16:9", "9:16"), (8,), ("720p", "1080p", "4k"), None, _MU_I2V,
            path="veo3.1-lite-image-to-video", body_fields=("aspect_ratio", "duration", "resolution"), **_I2V,
        ),
        ModelSpec(
            "veo3.1-image-to-video", "muapi", "Veo 3.1 (image-to-video)",
            ("16:9", "9:16"), (8,), ("720p", "1080p", "4k"), None, _MU_I2V,
            path="veo3.1-image-to-video", body_fields=("aspect_ratio", "duration", "resolution"), **_I2V,
        ),
    )
}
DEFAULT_I2V_MODEL = {"muapi": "kling-v3.0-standard-image-to-video"}

MODELS: dict[str, dict[str, ModelSpec]] = {"veo": VEO_MODELS, "higgsfield": HIGGSFIELD_MODELS, "muapi": MUAPI_MODELS}


def build_body(spec: ModelSpec, prompt: str, aspect_ratio: str, duration_seconds: int, resolution: str, image_url: str | None = None) -> dict:
    """Request JSON for the REST-style providers, driven entirely by the spec's data."""
    values = {"aspect_ratio": aspect_ratio, "duration": duration_seconds, "resolution": resolution}
    body = {"prompt": prompt, **spec.extra_body}
    if image_url and spec.image_to_video:
        if spec.image_field == "images_list":
            body["images_list"] = [image_url]
            if "@image1" not in prompt:  # Seedance addresses its reference images from the prompt
                body["prompt"] = f"@image1 {prompt}"
        else:
            body[spec.image_field] = image_url
    for name in spec.body_fields:
        body[spec.field_map.get(name, name)] = values[name]
    return body


def get_model(provider: str, model: str) -> ModelSpec:
    if provider not in MODELS:
        raise CatalogError(f"Unknown provider '{provider}'. Choose one of: {', '.join(MODELS)}.")
    spec = MODELS[provider].get(model) or (MUAPI_I2V_MODELS.get(model) if provider == "muapi" else None)
    if spec is None:
        raise CatalogError(f"Model '{model}' is not available for {PROVIDER_LABELS[provider]}. Choose one of: {', '.join(MODELS[provider])}.")
    return spec


def validate(provider: str, model: str, aspect_ratio: str, duration_seconds: int, resolution: str) -> ModelSpec:
    spec = get_model(provider, model)
    if aspect_ratio not in spec.aspect_ratios:
        raise CatalogError(f"{spec.label} does not support aspect ratio {aspect_ratio}. Supported: {', '.join(spec.aspect_ratios)}.")
    if duration_seconds not in spec.durations:
        raise CatalogError(f"{spec.label} does not support a {duration_seconds}s duration. Supported: {', '.join(map(str, spec.durations))} seconds.")
    if resolution not in spec.resolutions:
        raise CatalogError(f"{spec.label} does not support {resolution} resolution. Supported: {', '.join(spec.resolutions)}.")
    required = spec.resolution_durations.get(resolution)
    if required and duration_seconds not in required:
        raise CatalogError(f"{spec.label} at {resolution} requires a duration of {' or '.join(map(str, required))} seconds.")
    return spec


def estimate_cost(provider: str, model: str, resolution: str, duration_seconds: int) -> float | None:
    spec = MODELS.get(provider, {}).get(model) or (MUAPI_I2V_MODELS.get(model) if provider == "muapi" else None)
    if spec is None or not spec.price_per_second_usd:
        return None
    price = spec.price_per_second_usd.get(resolution)
    return None if price is None else round(price * duration_seconds, 4)


def image_variant(provider: str, model: str) -> str | None:
    """The image-to-video sibling of a text-to-video model (same provider), or None when the model already
    takes an image or the provider has no verified image-to-video endpoint (Veo takes it natively)."""
    if provider != "muapi":
        return None
    if model in MUAPI_I2V_MODELS:
        return model
    for old, new in (("text-to-video", "image-to-video"), ("-t2v", "-i2v")):
        if old in model and model.replace(old, new) in MUAPI_I2V_MODELS:
            return model.replace(old, new)
    return None


def supports_image(provider: str, model: str) -> bool:
    """True when `model` can start from an image (Veo always; Muapi only its image-to-video endpoints)."""
    return provider == "veo" or (provider == "muapi" and model in MUAPI_I2V_MODELS)


def fit(spec: ModelSpec, duration_seconds: int, resolution: str) -> tuple[int, str]:
    """Nearest (duration, resolution) `spec` offers: the smallest duration that covers the request, else the longest."""
    durs = sorted(spec.durations)
    dur = duration_seconds if duration_seconds in durs else next((d for d in durs if d >= duration_seconds), durs[-1])
    return dur, resolution if resolution in spec.resolutions else spec.resolutions[-1]

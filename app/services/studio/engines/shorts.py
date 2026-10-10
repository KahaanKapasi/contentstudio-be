"""V5: AI short films (scene-film core, _scenefilm.py) and trend dances (Muapi Kling motion control).

shorts/dance flow: check photo + trend video (plan) -> upload both to Cloudinary for public URLs -> POST the
motion-control model on Muapi -> poll -> download -> strip audio (default) and normalise to 1080x1920.
Request fields verified against https://api.muapi.ai/openapi.json (KlingV26/V30StdMotionControlRequest):
POST /api/v1/{model} with prompt, image_url, video_url, character_orientation ("image" | "video"), and for v3.0
keep_original_sound (bool); v2.6 has no sound option, so audio is handled locally either way."""

import io
import time

from PIL import Image, ImageOps

from app.services import media_hosting
from app.services.costs import prices
from app.services.studio import registry
from app.services.studio.engines import _scenefilm as sf
from app.services.studio.engines import _scenerender as sr
from app.services.studio.engines import _shared
from app.services.studio.kit import ffmpeg
from app.services.studio.registry import FieldSpec, RecipeSpec, Stage, StageError
from app.services.video_providers import JobHandle, PollResult, ProviderError, TransientProviderError, catalog, http_common
from app.services.video_providers.muapi import MuapiProvider

MOTION_MODELS = [
    {"value": "kling-v2.6-std-motion-control", "label": "Kling 2.6 motion control"},
    {"value": "kling-v3.0-std-motion-control", "label": "Kling 3.0 motion control"},
]
POLL_INTERVAL_S = 5.0
JOB_TIMEOUT_S = 15 * 60
MAX_TREND_S = 30.0
IMAGE_MAX_TREND_S = 10.0  # character_orientation "image" caps the reference video at 10 s on Muapi
MIN_SIDE_PX = 300
MIN_VIDEO_SIDE_PX = 340  # Kling accepts reference videos from 340 px
DANCE_PROMPT = "The person from the image performs exactly the same dance moves as in the video. Keep their face, outfit and the background from the image. Smooth, natural motion."

DANCE = RecipeSpec(
    "dance", "Dance trend", "Make a photo perform the moves of a trending video (motion control).",
    [
        FieldSpec("photo", "Photo of the character", "image", required=True),
        FieldSpec("trend_video", "Trend video (up to 30 s)", "video", required=True),
        FieldSpec("model", "Motion-control model", "select", default=MOTION_MODELS[0]["value"], options=MOTION_MODELS),
        FieldSpec("character_orientation", "Pose reference", "select", default="auto", options=[
            {"value": "auto", "label": "Auto (photo pose up to 10 s, video pose up to 30 s)"},
            {"value": "image", "label": "Follow the photo's orientation (video up to 10 s)"},
            {"value": "video", "label": "Follow the video's orientation (video up to 30 s)"}]),
        FieldSpec("keep_audio", "Keep the trend video's audio", "toggle", default=False,
                  help="Off by default: add the trending sound inside Instagram, which is licensed."),
    ],
    paid=True, keys=("MUAPI_API_KEY", "CLOUDINARY_URL"),
)

FILM_STYLES = {
    "3d-cartoon": "stylised 3D animated cartoon, glossy feature-film look, expressive faces, soft cinematic lighting, rich colour",
    "2d-flat": "2D flat cartoon, big-head caricatures, thick black outlines, bright flat colours, simple backgrounds",
    "claymation": "stop-motion claymation, handmade clay characters with visible fingerprints, miniature sets, warm lighting",
    "cinematic": "photoreal cinematic film still, 35mm, shallow depth of field, dramatic lighting, teal and orange grade",
}
FILM = RecipeSpec(
    "film", "Parody mini-film", "A short cinematic or 3D-cartoon parody film from a premise, with consistent characters.",
    [
        FieldSpec("premise", "Premise or controversy", "textarea", required=True),
        sf.RESEARCH,
        *sf.characters_fields(),
        sf.style_field([("3d-cartoon", "3D cartoon"), ("2d-flat", "2D flat"), ("claymation", "Claymation"), ("cinematic", "Cinematic")], "3d-cartoon"),
        FieldSpec("scenes", "Scenes", "number", default=5, min=3, max=8),
        sf.motion_quality_field("ai"),
        sf.CLIP_PROVIDER,
        FieldSpec("dialogue", "Dialogue", "select", default="native", options=[{"value": "native", "label": "Native model audio (needs AI motion)"}, {"value": "tts", "label": "Text-to-speech"}]),
        *_shared.tts_fields(),
        sf.LANGUAGE,
        sf.SUBTITLES,
        sf.PARODY_LABEL,
        sf.ASPECT,
    ],
    paid=True, keys=("GEMINI_API_KEY",),
)
FILM_FLAVOUR = sf.Flavour(
    "shorts/film", "dialogue",
    "a 30-45 second parody mini-film: a cinematic comedy scene with a clear premise, escalating absurdity and a final gag. "
    "Example genre: a satirical dictator-style locker-room speech by a football star, played as an obvious fictional parody.",
    sf.SATIRE_RULES + "\n- Obvious parody: never state anything as a fact about a real person; rename or caricature when in doubt.\n- 1 to 3 speaking characters; each line is 14 words or fewer and names its speaker.\n- Describe sound in 'sfx_note' (crowd, whistle, echo).",
    lambda p: FILM_STYLES.get(p.get("visual_style", "3d-cartoon"), FILM_STYLES["3d-cartoon"]), "scenes", (4.0, 8.0), "cut", "bold-pop", False, "premise",
    min_shot=3.0,
)


# --- dance ---


def _check_inputs(ctx) -> None:
    p = ctx.params
    photo, video = ctx.inputs["photo"][0], ctx.inputs["trend_video"][0]
    try:
        with Image.open(photo) as im:
            width, height = im.size
    except OSError:
        raise StageError("The photo could not be read. Upload a JPG, PNG or WebP image.") from None
    if min(width, height) < MIN_SIDE_PX:
        raise StageError(f"The photo is too small ({width}x{height}). Use one at least {MIN_SIDE_PX}px on each side.")
    probe = ffmpeg.probe(video)
    if probe.duration > MAX_TREND_S + 0.5:
        raise StageError(f"The trend video is {probe.duration:.0f} s long; motion control takes up to {MAX_TREND_S:.0f} s. Trim it first.")
    if min(probe.width or MIN_VIDEO_SIDE_PX, probe.height or MIN_VIDEO_SIDE_PX) < MIN_VIDEO_SIDE_PX:
        raise StageError(f"The trend video is too small ({probe.width}x{probe.height}). Use one at least {MIN_VIDEO_SIDE_PX}px on each side.")
    orient = p.get("character_orientation", "auto")
    if orient == "auto":
        orient = "image" if probe.duration <= IMAGE_MAX_TREND_S else "video"
    elif orient == "image" and probe.duration > IMAGE_MAX_TREND_S:
        raise StageError(f"Following the photo's orientation allows trend videos up to {IMAGE_MAX_TREND_S:.0f} s; yours is {probe.duration:.0f} s. Choose 'video' or trim it.")
    ctx.data["dance"] = {"orientation": orient, "duration": round(probe.duration, 1)}
    ctx.register("photo", photo, label="Photo", kind="image", preview=True)
    keep = bool(p.get("keep_audio"))
    ctx.plan.update(
        summary=f"{probe.duration:.0f} s vertical clip: your photo performs the moves of the trend video ({dict((m['value'], m['label']) for m in MOTION_MODELS)[p['model']]}, pose follows the {orient}).",
        script=None,
        scenes=[{"index": 1, "text": "Photo performs the trend moves", "visual": "Motion control from the uploaded trend video", "duration_s": round(probe.duration, 1)}],
        notes=("The trend video's audio is kept in the result (make sure you have the rights to use it)." if keep
               else "The result has no sound: add the trending sound inside Instagram, which is licensed. ")
        + "Billed in Muapi credits (price not shown here). The photo and video are uploaded to your Cloudinary account to get public links for Muapi.",
    )


def _upload(ctx) -> None:
    d = ctx.data["dance"]
    if d.get("photo_url") and d.get("video_url"):
        return
    photo, video = ctx.inputs["photo"][0], ctx.inputs["trend_video"][0]
    try:
        with Image.open(photo) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((2048, 2048))
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=92)
        d["photo_url"] = media_hosting.upload_image(buf.getvalue(), public_id=f"studio-dance-{ctx.project_id}-photo")
        ctx.progress(0.3)
        d["video_url"] = media_hosting.upload_video(str(video), public_id=f"studio-dance-{ctx.project_id}-trend")
    except media_hosting.MediaHostingNotConfigured as exc:
        raise StageError(f"{exc}.") from exc
    except Exception as exc:  # cloudinary raises its own exception types
        raise StageError(f"Uploading the inputs to Cloudinary failed ({type(exc).__name__}). Check CLOUDINARY_URL and retry.") from exc


def build_motion_body(model: str, image_url: str, video_url: str, orientation: str, keep_audio: bool) -> dict:
    body = {"prompt": DANCE_PROMPT, "image_url": image_url, "video_url": video_url, "character_orientation": orientation}
    if model.startswith("kling-v3"):  # only v3.0 has a sound option in Muapi's schema
        body["keep_original_sound"] = bool(keep_audio)
    return body


def _generate(ctx) -> None:
    p, d = ctx.params, ctx.data["dance"]
    out = ctx.path("motion_raw.mp4")
    if out.is_file():
        return
    provider = MuapiProvider()
    if d.get("request_id"):  # a previous attempt already paid for this job: keep polling it instead of resubmitting
        job = JobHandle(d["request_id"], d.get("status_url"))
    else:
        body = build_motion_body(p["model"], d["photo_url"], d["video_url"], d["orientation"], bool(p.get("keep_audio")))
        with provider._client() as client:
            data = http_common.request(client, "POST", f"{catalog.MUAPI_BASE_URL}/api/v1/{p['model']}", label="Muapi", json=body)
        request_id = data.get("request_id") or data.get("id")
        if not request_id:
            raise ProviderError("Muapi did not return a request id.")
        job = JobHandle(str(request_id), f"{catalog.MUAPI_BASE_URL}/api/v1/predictions/{request_id}/result")
        d["request_id"], d["status_url"] = job.job_id, job.status_url
    started = time.monotonic()
    while True:
        try:
            result: PollResult = provider.poll(job)
        except TransientProviderError:
            result = PollResult("running")
        if result.state == "succeeded":
            provider.download(result.output, out)
            _record_clip_usage(ctx, p["model"], d)
            return
        if result.state == "failed":
            d.pop("request_id", None)
            raise ProviderError(result.error or "Muapi could not generate this video.")
        waited = time.monotonic() - started
        if waited > JOB_TIMEOUT_S:
            raise ProviderError("Muapi did not finish within 15 minutes. Retry to keep waiting for the same job.")
        ctx.remaining()
        ctx.progress(min(waited / 240, 0.95))
        time.sleep(POLL_INTERVAL_S)


def _record_clip_usage(ctx, model: str, d: dict) -> None:
    try:  # v3.0 motion control has a published per-second price; v2.6 does not (recorded as an estimate)
        price = prices.usd("muapi.kling_v3_std_motion_control") if model.startswith("kling-v3") else None
        secs = float(d.get("duration") or 0)
        ctx.usage.add_clip("muapi", model, "default", secs, None if price is None else price * secs)
    except Exception:
        pass


def _finish_dance(ctx) -> None:
    raw = ctx.path("motion_raw.mp4")
    probe = ffmpeg.probe(raw)
    w, h = ffmpeg.target_size("9:16")
    norm = ctx.path("motion_norm.mp4")
    ffmpeg.normalize_clip(raw, norm, w, h, probe.duration, loop=False, mode="contain")
    audio = None
    if ctx.params.get("keep_audio"):  # v3.0 returns the sound itself; otherwise reuse the trend video's own track
        source = raw if probe.has_audio else ctx.inputs["trend_video"][0]
        audio = sr.extract_audio(source, ctx.path("audio", "kept.wav"))
    ffmpeg.finalize(norm, ctx.path(registry.FINAL_NAME), audio=audio, seconds=probe.duration)
    raw.unlink(missing_ok=True)
    norm.unlink(missing_ok=True)


class ShortsEngine(sf.SceneFilmEngine):
    id = "shorts"
    label = "AI short films"
    description = "Trend dances with motion control and cinematic parody mini-films."
    recipes = [DANCE, FILM]
    flavours = {"film": FILM_FLAVOUR}
    implemented = True

    def title(self, recipe, params):
        return "Dance trend" if recipe.id == "dance" else super().title(recipe, params)

    def stages(self, recipe, params):
        if recipe.id != "dance":
            return super().stages(recipe, params)
        return [
            Stage("Checking photo and video", _check_inputs, "plan"),
            Stage("Uploading inputs", _upload, "render"),
            Stage("Generating motion", _generate, "render", 10),
            Stage("Finishing video", _finish_dance, "render", 2),
        ]

    def estimate_cost(self, recipe, params, plan):
        return None if recipe.id == "dance" else super().estimate_cost(recipe, params, plan)


ENGINE = ShortsEngine()

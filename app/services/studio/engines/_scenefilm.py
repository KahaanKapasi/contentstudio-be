"""Field specs shared by the scene-film engines (toons, shorts/film, explainer3d). The scene-film core itself
(script -> shot list -> reference images -> per-shot visuals -> voices -> captions -> stitch) is built in a later
pass on top of kit.images (references), kit.clips (image-to-video), kit.tts (per-sentence voices) and kit.captions."""

from app.services.studio.engines import _shared
from app.services.studio.registry import FieldSpec

MOTION_QUALITY = FieldSpec(
    "motion_quality", "Motion quality", "select", default="cheap",
    options=[{"value": "cheap", "label": "Cheap: stills with camera moves (Gemini image cost only)"}, {"value": "ai", "label": "AI: image-to-video per shot (paid per second)"}],
)
RESEARCH = FieldSpec("research", "Research the topic on the web first", "toggle", default=False)
PARODY_LABEL = FieldSpec("parody_label", "Burn in an 'AI parody' label", "toggle", default=True)
CLIP_PROVIDER = FieldSpec("clip_provider", "Clip provider (AI motion)", "select", default="veo", options=[{"value": "veo", "label": "Google Veo"}, {"value": "muapi", "label": "Muapi"}])


def characters_fields() -> list[FieldSpec]:
    return [
        FieldSpec("characters", "Characters (one per line: Name: description)", "textarea"),
        FieldSpec("character_images", "Character photos (optional)", "images", help="Used as reference so characters look consistent in every shot."),
    ]


def style_field(options: list[tuple[str, str]], default: str) -> FieldSpec:
    return FieldSpec("visual_style", "Visual style", "select", default=default, options=[{"value": v, "label": l} for v, l in options])


ASPECT = _shared.aspect_field()

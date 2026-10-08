"""V5: AI short films and trend dances. Registered with full field specs; stages come in a later pass."""

from app.services.studio.engines import _scenefilm as sf
from app.services.studio.registry import Engine, FieldSpec, RecipeSpec

MOTION_MODELS = [
    {"value": "kling-v2.6-std-motion-control", "label": "Kling 2.6 motion control"},
    {"value": "kling-v3.0-std-motion-control", "label": "Kling 3.0 motion control"},
]

DANCE = RecipeSpec(
    "dance", "Dance trend", "Make a photo perform the moves of a trending video (motion control).",
    [
        FieldSpec("photo", "Photo of the character", "image", required=True),
        FieldSpec("trend_video", "Trend video (up to 30 s)", "video", required=True),
        FieldSpec("model", "Motion-control model", "select", default=MOTION_MODELS[0]["value"], options=MOTION_MODELS),
        FieldSpec("keep_audio", "Keep the trend video's audio", "toggle", default=False,
                  help="Off by default: add the trending sound inside Instagram, which is licensed."),
    ],
    paid=True, keys=("MUAPI_API_KEY", "CLOUDINARY_URL"),
)
FILM = RecipeSpec(
    "film", "Parody mini-film", "A short cinematic or 3D-cartoon parody film from a premise, with consistent characters.",
    [
        FieldSpec("premise", "Premise or controversy", "textarea", required=True),
        sf.RESEARCH,
        *sf.characters_fields(),
        sf.style_field([("3d-cartoon", "3D cartoon"), ("2d-flat", "2D flat"), ("claymation", "Claymation"), ("cinematic", "Cinematic")], "3d-cartoon"),
        FieldSpec("scenes", "Scenes", "number", default=5, min=3, max=8),
        sf.CLIP_PROVIDER,
        FieldSpec("dialogue", "Dialogue", "select", default="native", options=[{"value": "native", "label": "Native model audio"}, {"value": "tts", "label": "Text-to-speech"}]),
        sf.PARODY_LABEL,
        sf.ASPECT,
    ],
    paid=True, keys=("GEMINI_API_KEY",),
)


class ShortsEngine(Engine):
    id = "shorts"
    label = "AI short films"
    description = "Trend dances with motion control and cinematic parody mini-films."
    recipes = [DANCE, FILM]
    implemented = False


ENGINE = ShortsEngine()

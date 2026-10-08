"""V4: 442oons-style sketch comedy. Registered with full field specs; stages come in a later pass."""

from app.services.studio.engines import _scenefilm as sf
from app.services.studio.engines import _shared
from app.services.studio.registry import Engine, FieldSpec, RecipeSpec

SKETCH = RecipeSpec(
    "sketch", "Sketch comedy", "2D flat big-head caricature sketch with character voices and a punchline card.",
    [
        FieldSpec("premise", "Premise or controversy", "textarea", required=True),
        sf.RESEARCH,
        *sf.characters_fields(),
        FieldSpec("scenes", "Scenes", "number", default=5, min=3, max=8),
        sf.MOTION_QUALITY,
        sf.CLIP_PROVIDER,
        *_shared.tts_fields(),
        sf.PARODY_LABEL,
        sf.ASPECT,
    ],
    paid=True, keys=("GEMINI_API_KEY",),
)
PARODY_SONG = RecipeSpec(
    "parody-song", "Parody song", "A football parody song with lyric captions over illustrated scenes.",
    [
        FieldSpec("concept", "Song concept", "textarea", required=True),
        FieldSpec("target", "Target (player or club)", "text", required=True),
        FieldSpec("instrumental", "Instrumental (optional)", "audio", help="Without one, a rhythmic sung-style voice is used."),
        sf.MOTION_QUALITY,
        *_shared.tts_fields(),
        sf.PARODY_LABEL,
        sf.ASPECT,
    ],
    paid=True, keys=("GEMINI_API_KEY",),
)


class ToonsEngine(Engine):
    id = "toons"
    label = "Sketch comedy"
    description = "442oons-style 2D caricature sketches and parody songs."
    recipes = [SKETCH, PARODY_SONG]
    implemented = False


ENGINE = ToonsEngine()

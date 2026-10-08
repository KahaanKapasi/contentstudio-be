"""V3: recipe presets. Registered with their full field specs so the UI can show them; stages come in a later pass."""

from app.services.studio.engines import _shared
from app.services.studio.registry import Engine, FieldSpec, RecipeSpec

_VIDEO_SOURCE = [{"value": "generate", "label": "Generate with an AI video model"}, {"value": "upload", "label": "Use my own video / images"}]

SINGING_STORY = RecipeSpec(
    "singing-story", "Singing story", "A story told line by line over your song (or a rhythmic spoken narration), with consistent AI illustrations.",
    [
        FieldSpec("idea", "Story idea", "textarea", required=True),
        FieldSpec("style", "Illustration style", "select", default="storybook", options=[
            {"value": v, "label": l} for v, l in (("storybook", "Storybook"), ("anime", "Anime"), ("watercolor", "Watercolor"), ("pixar-3d", "3D animated"), ("comic", "Comic book"))]),
        FieldSpec("song", "Song (optional)", "audio", help="Without a song, a rhythmic spoken-word narration is used."),
        FieldSpec("lines", "Story lines", "number", default=8, min=4, max=14),
        _shared.aspect_field(),
    ],
    paid=True, keys=("GEMINI_API_KEY",),
)
PODCAST = RecipeSpec(
    "podcast", "AI podcast", "Two AI hosts discuss a topic over live waveform cards and captions.",
    [
        FieldSpec("topic", "Topic", "textarea", required=True),
        FieldSpec("research", "Research the topic on the web first", "toggle", default=False),
        FieldSpec("host_a", "Host 1 name", "text", default="Alex"),
        FieldSpec("host_b", "Host 2 name", "text", default="Sam"),
        FieldSpec("duration", "Length (seconds)", "number", default=60, min=30, max=180),
        FieldSpec("tts_provider", "Voice provider", "select", default="edge", options=[{"value": "edge", "label": "Edge TTS (free, 2 voices)"}, {"value": "gemini", "label": "Gemini TTS (multi-speaker)"}]),
        _shared.aspect_field(),
    ],
    paid=False, keys=("GEMINI_API_KEY",),
)


def _stylised(recipe_id: str, label: str, description: str) -> RecipeSpec:
    return RecipeSpec(
        recipe_id, label, description,
        [
            FieldSpec("idea", "Idea", "textarea", required=True),
            FieldSpec("source", "Footage source", "select", default="generate", options=_VIDEO_SOURCE),
            FieldSpec("video", "Your video (if using your own)", "video"),
            FieldSpec("images", "Your images (if using your own)", "images"),
            FieldSpec("duration", "Length (seconds)", "number", default=8, min=4, max=12),
            _shared.aspect_field(),
        ],
        paid=True, keys=("GEMINI_API_KEY",),
    )


PS1 = _stylised("ps1-lowpoly", "PS1 low-poly", "Crunchy PlayStation-1 look: low resolution, dithering, jittery 15 fps.")
SPIDERVERSE = _stylised("spiderverse", "Spider-Verse comic", "Comic-book look: posterised colours, halftone feel, on-twos 12 fps and caption boxes.")
PRODUCT_DEMO = RecipeSpec(
    "product-demo", "Product demo", "Screenshots with zoom/pan callouts, narration and captions.",
    [
        FieldSpec("product", "Product name", "text", required=True),
        FieldSpec("url", "Product URL (text only, not crawled)", "text"),
        FieldSpec("screenshots", "Screenshots", "images", required=True),
        FieldSpec("features", "Key features (one per line)", "textarea", required=True),
        *_shared.tts_fields(),
        _shared.aspect_field(),
    ],
    paid=False, keys=("GEMINI_API_KEY",),
)


class SkillEngine(Engine):
    id = "skill"
    label = "Recipe presets"
    description = "One-click video recipes: singing story, podcast, PS1 low-poly, Spider-Verse, product demo."
    recipes = [SINGING_STORY, PODCAST, PS1, SPIDERVERSE, PRODUCT_DEMO]
    implemented = False


ENGINE = SkillEngine()

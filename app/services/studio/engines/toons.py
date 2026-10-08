"""V4: 442oons-style sketch comedy and parody songs, on the shared scene-film core (_scenefilm.py)."""

from app.services.studio.engines import _scenefilm as sf
from app.services.studio.engines import _shared
from app.services.studio.registry import FieldSpec, RecipeSpec

TOON_STYLE = (
    "2D flat cartoon in the style of football sketch-comedy animation: big-head caricatures with oversized heads and small bodies, "
    "thick black outlines, bright flat colours, no gradients, simple clean backgrounds, exaggerated comedic expressions"
)

SKETCH = RecipeSpec(
    "sketch", "Sketch comedy", "2D flat big-head caricature sketch with character voices and a punchline card.",
    [
        FieldSpec("premise", "Premise or controversy", "textarea", required=True),
        sf.RESEARCH,
        *sf.characters_fields(),
        FieldSpec("scenes", "Scenes", "number", default=5, min=3, max=8),
        sf.motion_quality_field("cheap"),
        sf.CLIP_PROVIDER,
        *_shared.tts_fields(),
        sf.LANGUAGE,
        sf.SUBTITLES,
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
        FieldSpec("instrumental", "Instrumental (optional)", "audio", help="Without one, a rhythmic spoken-style voice is used."),
        FieldSpec("scenes", "Lyric scenes", "number", default=8, min=4, max=12),
        sf.motion_quality_field("cheap"),
        sf.CLIP_PROVIDER,
        *_shared.tts_fields(),
        sf.LANGUAGE,
        sf.SUBTITLES,
        sf.PARODY_LABEL,
        sf.ASPECT,
    ],
    paid=True, keys=("GEMINI_API_KEY",),
)

SKETCH_FLAVOUR = sf.Flavour(
    "toons/sketch", "dialogue",
    "a 30-60 second comedy sketch in the style of football sketch channels like 442oons: two or three characters trading punchy, quotable lines, "
    "an absurd escalation, and a punchline at the end.",
    sf.SATIRE_RULES + "\n- Start on the joke, no setup longer than one shot.\n- Each line is 12 words or fewer.\n- 2 to 4 characters; every line names its speaker.\n- The 'end_card' is the punchline or a button line, 8 words or fewer.",
    lambda p: TOON_STYLE, "scenes", (3.0, 7.0), "xfade", "bold-pop", True, "premise",
)
SONG_FLAVOUR = sf.Flavour(
    "toons/parody-song", "lyrics",
    "a short football parody song (original lyrics set to a generic upbeat chant or pop rhythm, NOT copying any existing song's lyrics). Each shot is one lyric couplet sung by the main character.",
    sf.SATIRE_RULES + "\n- Original lyrics only; never reproduce lines of a real song.\n- Each shot holds ONE lyric line (a rhyming couplet at most 14 words) in 'dialogue' with the singer as speaker.\n- Keep a clear rhyme and a chorus feel; the last shot is the hook.\n- One singer character only (a caricature of the target); the 'end_card' is the song title.",
    lambda p: TOON_STYLE + ", music-video staging with stadium and crowd backdrops", "scenes", (3.0, 6.0), "xfade", "karaoke", True, "concept",
)


class ToonsEngine(sf.SceneFilmEngine):
    id = "toons"
    label = "Sketch comedy"
    description = "442oons-style 2D caricature sketches and parody songs."
    recipes = [SKETCH, PARODY_SONG]
    flavours = {"sketch": SKETCH_FLAVOUR, "parody-song": SONG_FLAVOUR}
    implemented = True


ENGINE = ToonsEngine()

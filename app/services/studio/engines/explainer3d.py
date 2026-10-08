"""V6: Zack D Films-style 3D explainers, on the shared scene-film core (_scenefilm.py)."""

from app.services.studio.engines import _scenefilm as sf
from app.services.studio.engines import _shared
from app.services.studio.registry import FieldSpec, RecipeSpec

STYLE = (
    "clean stylised 3D render, Blender/Octane look, soft studio lighting, smooth plain backdrop, clear cutaway and x-ray inserts when "
    "showing the inside of a body, object or machine, glossy materials, no text or labels"
)

WHATIF = RecipeSpec(
    "whatif", "What if...", "3D-render explainer: hook, mechanism with cutaway shots, escalating consequence, twist.",
    [
        FieldSpec("premise", "Question or premise", "textarea", required=True, help="Football angles work well, e.g. 'What happens to your brain if you head a ball 1000 times'."),
        sf.RESEARCH,
        FieldSpec("shots", "Shots", "number", default=12, min=10, max=20),
        sf.motion_quality_field("cheap"),
        sf.CLIP_PROVIDER,
        *_shared.tts_fields(),
        sf.LANGUAGE,
        sf.SUBTITLES,
        sf.ASPECT,
    ],
    paid=True, keys=("GEMINI_API_KEY",),
)

FLAVOUR = sf.Flavour(
    "explainer3d/whatif", "narration",
    "a 40-60 second 'what if' explainer in the style of fast 3D explainer channels (Zack D Films): one calm narrator, short punchy sentences, "
    "concrete numbers, football angle when it fits the topic.",
    "- Shot 1 narration is a one-sentence hook that makes people stay (a surprising fact or the question itself).\n"
    "- Then the mechanism (what actually happens, shown with cutaway / x-ray shots), then escalating consequences, then a twist ending that reframes the topic.\n"
    "- Each shot's 'narration' is a single short sentence of 8 words or fewer (about 2.5 words per second).\n"
    "- Be accurate and hedge uncertain claims; no medical advice, no invented statistics, no fear-mongering.\n"
    "- 'characters' is an empty list; the visuals show objects, bodies and places, not recurring actors.\n- Vary the shot types: wide establishing, macro close-up, x-ray cutaway, diagram-like cross-section.",
    lambda p: STYLE, "shots", (2.0, 4.0), "cut", "bold-pop", False, "premise", min_shot=2.0,
)


class Explainer3DEngine(sf.SceneFilmEngine):
    id = "explainer3d"
    label = "3D explainers"
    description = "Clean 3D-render 'what if' explainers with cutaways, fast cuts and a twist ending."
    recipes = [WHATIF]
    flavours = {"whatif": FLAVOUR}
    implemented = True


ENGINE = Explainer3DEngine()

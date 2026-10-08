"""V6: Zack D Films-style 3D explainers. Registered with full field specs; stages come in a later pass."""

from app.services.studio.engines import _scenefilm as sf
from app.services.studio.engines import _shared
from app.services.studio.registry import Engine, FieldSpec, RecipeSpec

WHATIF = RecipeSpec(
    "whatif", "What if...", "3D-render explainer: hook, mechanism with cutaway shots, escalating consequence, twist.",
    [
        FieldSpec("premise", "Question or premise", "textarea", required=True, help="Football angles work well, e.g. 'What happens to your brain if you head a ball 1000 times'."),
        sf.RESEARCH,
        FieldSpec("shots", "Shots", "number", default=12, min=10, max=20),
        sf.MOTION_QUALITY,
        sf.CLIP_PROVIDER,
        *_shared.tts_fields(),
        sf.ASPECT,
    ],
    paid=True, keys=("GEMINI_API_KEY",),
)


class Explainer3DEngine(Engine):
    id = "explainer3d"
    label = "3D explainers"
    description = "Clean 3D-render 'what if' explainers with cutaways, fast cuts and a twist ending."
    recipes = [WHATIF]
    implemented = False


ENGINE = Explainer3DEngine()

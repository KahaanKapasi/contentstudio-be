"""V3: recipe presets. One engine, five recipes; each recipe lives in its own `_skill_*` module with its field specs
(`SPEC`), stages and cost estimate, and this file only dispatches to them."""

from app.services.studio.engines import _skill_demo, _skill_podcast, _skill_stylise, _skill_story
from app.services.studio.registry import Engine, RecipeSpec, Stage


class SkillEngine(Engine):
    id = "skill"
    label = "Recipe presets"
    description = "One-click video recipes: singing story, podcast, PS1 low-poly, Spider-Verse, product demo."
    recipes = [_skill_story.SPEC, _skill_podcast.SPEC, _skill_stylise.PS1, _skill_stylise.SPIDERVERSE, _skill_demo.SPEC]
    implemented = True

    def _module(self, recipe: RecipeSpec):
        return {"singing-story": _skill_story, "podcast": _skill_podcast, "product-demo": _skill_demo}.get(recipe.id)

    def validate(self, recipe: RecipeSpec, params: dict, files: dict[str, list]) -> dict:
        out = super().validate(recipe, params, files)
        if recipe.id in ("ps1-lowpoly", "spiderverse"):
            _skill_stylise.validate(recipe.id, out, files)
        elif recipe.id == "product-demo":
            _skill_demo.validate(out, files)
        return out

    def title(self, recipe: RecipeSpec, params: dict) -> str:
        module = self._module(recipe) or _skill_stylise
        return module.title(params)

    def stages(self, recipe: RecipeSpec, params: dict) -> list[Stage]:
        module = self._module(recipe)
        return module.stages(params) if module else _skill_stylise.stages(recipe.id, params)

    def estimate_cost(self, recipe: RecipeSpec, params: dict, plan: dict) -> float | None:
        module = self._module(recipe) or _skill_stylise
        return module.estimate_cost(params, plan) if hasattr(module, "estimate_cost") else None


ENGINE = SkillEngine()

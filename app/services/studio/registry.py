"""Engine registry: adding an engine = one module under `engines/` that defines recipes (FieldSpec lists) and
an ordered list of stages, then listing it in `ENGINES`. The runner and the router only know this interface.

An engine builds its stages from the validated params; each stage is `fn(ctx)` and is either part of the
`plan` phase (cheap, runs first; paid recipes stop for approval after it) or the `render` phase.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.config import settings

FINAL_NAME = "final.mp4"  # the finished video every engine writes into the project folder
FILE_TYPES = ("image", "images", "video", "audio")
FILE_EXTENSIONS = {
    "image": {".png", ".jpg", ".jpeg", ".webp"},
    "images": {".png", ".jpg", ".jpeg", ".webp"},
    "video": {".mp4", ".mov", ".webm", ".m4v"},
    "audio": {".mp3", ".wav", ".m4a", ".aac", ".ogg"},
}
FILE_MAX_BYTES = {"image": 15 << 20, "images": 15 << 20, "video": 200 << 20, "audio": 40 << 20}


class ValidationError(ValueError):
    """Bad user input; the message names the field and is shown as-is (HTTP 400)."""


class StageError(RuntimeError):
    """A stage failed for a reason the user can act on; the message is shown as-is."""


@dataclass
class FieldSpec:
    name: str
    label: str
    type: str  # text | textarea | select | number | toggle | image | images | video | audio
    required: bool = False
    default: Any = None
    options: list[dict] | None = None  # [{value, label}]
    min: float | None = None
    max: float | None = None
    help: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class RecipeSpec:
    id: str
    label: str
    description: str
    fields: list[FieldSpec]
    paid: bool = False
    keys: tuple[str, ...] = ()  # env vars always needed (settings attribute names are the lower-cased env vars)


@dataclass
class Stage:
    name: str  # shown as `stage` while running, e.g. "Writing script"
    fn: Callable[["Any"], None]  # fn(ctx: runner.Ctx)
    phase: str = "render"  # plan | render
    weight: int = 1


def missing_env(keys: tuple[str, ...] | list[str]) -> list[str]:
    return [k for k in keys if not getattr(settings, k.lower(), "")]


class Engine:
    id: str
    label: str
    description: str
    recipes: list[RecipeSpec]
    implemented: bool = True

    def recipe(self, recipe_id: str | None) -> RecipeSpec | None:
        if recipe_id in (None, ""):
            return self.recipes[0] if len(self.recipes) == 1 else None
        return next((r for r in self.recipes if r.id == recipe_id), None)

    def extra_keys(self, recipe: RecipeSpec, params: dict) -> list[str]:
        """Env vars needed because of the chosen params (e.g. a TTS provider)."""
        return []

    def validate(self, recipe: RecipeSpec, params: dict, files: dict[str, list]) -> dict:
        return validate_params(recipe.fields, params, files)

    def title(self, recipe: RecipeSpec, params: dict) -> str:
        return recipe.label

    def stages(self, recipe: RecipeSpec, params: dict) -> list[Stage]:
        raise NotImplementedError

    def estimate_cost(self, recipe: RecipeSpec, params: dict, plan: dict) -> float | None:
        return None


def validate_params(fields: list[FieldSpec], raw: dict, files: dict[str, list]) -> dict:
    """Coerce/validate `raw` against the specs. File fields are checked against `files` (name -> upload names)."""
    out: dict[str, Any] = {}
    for f in fields:
        if f.type in FILE_TYPES:
            names = files.get(f.name) or []
            if f.required and not names:
                raise ValidationError(f"'{f.name}' ({f.label}) is required: upload a file.")
            if f.type != "images" and len(names) > 1:
                raise ValidationError(f"'{f.name}' ({f.label}) takes a single file.")
            for n in names:
                if Path(n).suffix.lower() not in FILE_EXTENSIONS[f.type]:
                    raise ValidationError(f"'{f.name}' ({f.label}): unsupported file type '{Path(n).suffix or n}'.")
            continue
        value = raw.get(f.name)
        if value is None or (isinstance(value, str) and not value.strip()):
            if f.required and f.default is None:
                raise ValidationError(f"'{f.name}' ({f.label}) is required.")
            value = f.default
        if value is None:
            continue
        out[f.name] = _coerce(f, value)
    return out


def _coerce(f: FieldSpec, value: Any) -> Any:
    if f.type in ("text", "textarea"):
        if not isinstance(value, (str, int, float)):
            raise ValidationError(f"'{f.name}' ({f.label}) must be text.")
        text = str(value).strip()
        if f.required and not text:
            raise ValidationError(f"'{f.name}' ({f.label}) is required.")
        if len(text) > (20000 if f.type == "textarea" else 500):
            raise ValidationError(f"'{f.name}' ({f.label}) is too long.")
        return text
    if f.type == "select":
        allowed = [o["value"] for o in f.options or []]
        if value not in allowed:
            raise ValidationError(f"'{f.name}' ({f.label}) must be one of: {', '.join(str(a) for a in allowed if a != '')}.")
        return value
    if f.type == "number":
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ValidationError(f"'{f.name}' ({f.label}) must be a number.") from None
        if (f.min is not None and num < f.min) or (f.max is not None and num > f.max):
            raise ValidationError(f"'{f.name}' ({f.label}) must be between {f.min:g} and {f.max:g}.")
        return int(num) if num == int(num) and isinstance(f.default, int) else num
    if f.type == "toggle":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "on", "off"):
            return value.lower() in ("true", "1", "on")
        raise ValidationError(f"'{f.name}' ({f.label}) must be true or false.")
    return value


def _load_engines() -> dict[str, Engine]:
    from app.services.studio.engines import explainer3d, faceless, motion, shorts, skill, toons

    return {e.id: e for e in (motion.ENGINE, faceless.ENGINE, skill.ENGINE, toons.ENGINE, shorts.ENGINE, explainer3d.ENGINE)}


_engines: dict[str, Engine] | None = None


def engines() -> dict[str, Engine]:
    global _engines
    if _engines is None:
        _engines = _load_engines()
    return _engines


def listing() -> list[dict]:
    out = []
    for e in engines().values():
        recipes = []
        for r in e.recipes:
            missing = missing_env(r.keys)
            recipes.append(
                {
                    "id": r.id, "label": r.label, "description": r.description + ("" if e.implemented else " (coming soon)"),
                    "configured": not missing, "missing_keys": missing, "paid": r.paid,
                    "fields": [f.to_dict() for f in r.fields],
                }
            )
        out.append({"id": e.id, "label": e.label, "description": e.description + ("" if e.implemented else " (coming soon)"), "recipes": recipes})
    return out

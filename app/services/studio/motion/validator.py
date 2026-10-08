"""Static checks for Gemini-written scene code. Defence in depth: this AST whitelist runs first, then the code
executes with restricted builtins inside a resource-limited, env-scrubbed subprocess (sandbox.py)."""

import ast

ALLOWED_MODULES = {"studio_motion", "math", "random"}
BANNED_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "getattr", "setattr", "delattr", "globals", "locals", "vars",
    "dir", "input", "breakpoint", "exit", "quit", "help", "memoryview", "type", "object", "super", "classmethod",
    "staticmethod", "property", "bytearray",
}
BANNED_NODES = {
    ast.ClassDef: "class definitions", ast.AsyncFunctionDef: "async functions", ast.Await: "await", ast.Yield: "generators",
    ast.YieldFrom: "generators", ast.Global: "global", ast.Nonlocal: "nonlocal", ast.With: "with statements",
    ast.AsyncWith: "async with", ast.AsyncFor: "async for",
}
MAX_CHARS = 30_000
MAX_NODES = 12_000


class MotionCodeError(ValueError):
    """The scene code is not allowed or does not parse; the message is fed back to Gemini."""


def _fail(node: ast.AST | None, message: str):
    line = f"line {node.lineno}: " if node is not None and hasattr(node, "lineno") else ""
    raise MotionCodeError(f"{line}{message}")


def validate(code: str) -> None:
    if len(code) > MAX_CHARS:
        raise MotionCodeError("The scene code is too long; make it shorter.")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise MotionCodeError(f"line {exc.lineno}: syntax error: {exc.msg}") from exc

    count = 0
    for node in ast.walk(tree):
        count += 1
        if count > MAX_NODES:
            raise MotionCodeError("The scene code is too long; make it shorter.")
        banned = BANNED_NODES.get(type(node))
        if banned:
            _fail(node, f"{banned} are not allowed")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_MODULES:
                    _fail(node, f"import of '{alias.name}' is not allowed; only studio_motion, math and random")
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in ALLOWED_MODULES:
                _fail(node, f"import from '{'.' * node.level}{node.module}' is not allowed; only studio_motion, math and random")
        elif isinstance(node, ast.Name):
            if node.id in BANNED_NAMES or node.id.startswith("__"):
                _fail(node, f"'{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                _fail(node, f"attribute '{node.attr}' is not allowed (no private or dunder attributes)")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "__" in node.value:
            _fail(node, "strings containing '__' are not allowed")

    defines_scene = any(
        isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "scene" for t in n.targets) for n in tree.body
    )
    if not defines_scene:
        raise MotionCodeError("The code must define a top-level variable named `scene` (scene = Scene(...)).")

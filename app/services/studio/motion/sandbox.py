"""Child process of the motion render (run as `python -I sandbox.py scene.py config.json`).

Applies resource limits, execs the validated scene code with restricted builtins, renders a few preflight frames
to fail fast, then renders every frame; a writer thread feeds stdout (wired straight into ffmpeg's stdin by
render.py) so rendering and I/O overlap. Must not import anything from `app`: it runs with a clean sys.path.
"""

import json
import os
import queue
import resource
import sys
import threading
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
RENDER_TIMEOUT_S = 120
USER_FILE = "<scene>"
SAFE_BUILTINS = (
    "abs all any bool dict divmod enumerate filter float int isinstance len list map max min pow range repr "
    "reversed round set slice sorted str sum tuple zip ord chr format iter next callable print "
    "Exception ValueError KeyError IndexError TypeError ZeroDivisionError StopIteration ArithmeticError"
).split()


def _limit_resources(duration: float) -> None:
    def cap(which, value):
        try:
            resource.setrlimit(which, (value, value))
        except (ValueError, OSError):  # e.g. RLIMIT_AS is not enforceable on macOS; the parent watchdog covers memory
            pass

    cap(resource.RLIMIT_CPU, int(RENDER_TIMEOUT_S * 1.5))
    cap(resource.RLIMIT_FSIZE, 64 << 20)
    if hasattr(resource, "RLIMIT_AS"):
        cap(resource.RLIMIT_AS, 4 << 30)
    cap(resource.RLIMIT_CORE, 0)


def _guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    root = name.split(".")[0]
    if level or root not in ("studio_motion", "math", "random"):
        raise ImportError(f"import of '{name}' is not allowed")
    return __import__(name, globals, locals, fromlist, level)


def _user_error(exc: BaseException) -> str:
    frames = [f for f in traceback.extract_tb(exc.__traceback__) if f.filename == USER_FILE]
    where = f" (line {frames[-1].lineno}: {frames[-1].line})" if frames else ""
    return f"{type(exc).__name__}: {exc}{where}"


def main(code_path: str, config_path: str) -> int:
    cfg = json.loads(Path(config_path).read_text())
    _limit_resources(cfg["duration"])
    out = os.fdopen(os.dup(1), "wb", buffering=0)  # keep the real stdout for frames
    sys.stdout = sys.stderr  # stray print() must never corrupt the raw video stream
    sys.path.insert(0, str(HERE))
    import studio_motion as sm

    sm.configure(cfg["width"], cfg["height"], cfg["fps"], cfg["duration"], cfg["assets"])
    import builtins

    safe = {name: getattr(builtins, name) for name in SAFE_BUILTINS}
    safe["__import__"] = _guarded_import
    namespace = {"__builtins__": safe, "__name__": "scene"}
    try:
        exec(compile(Path(code_path).read_text(), USER_FILE, "exec"), namespace)
        scene = namespace.get("scene")
        if not isinstance(scene, sm.Scene):
            raise TypeError("`scene` must be a Scene (scene = Scene(...))")
        fps, total = cfg["fps"], round(cfg["duration"] * cfg["fps"])
        for t in (0.0, cfg["duration"] / 2, cfg["duration"] - 1 / fps):  # fail fast, before any frame is streamed
            scene.render_frame(t)
        if cfg["preflight_only"]:
            return 0
        pending: queue.Queue = queue.Queue(maxsize=8)
        failed: list[BaseException] = []

        def writer():
            try:
                while (item := pending.get()) is not None:
                    out.write(item)
            except BaseException as exc:  # broken pipe when ffmpeg died; the parent reports ffmpeg's side
                failed.append(exc)
                while pending.get() is not None:
                    pass

        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        for i in range(total):
            if failed:
                return 3
            pending.put(scene.render_frame(i / fps).tobytes())
            if i % 15 == 0:
                print(f"PROGRESS {i}", file=sys.stderr, flush=True)
        pending.put(None)
        thread.join()
        return 3 if failed else 0
    except BaseException as exc:
        print(_user_error(exc), file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))

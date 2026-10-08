import pytest

from app.services.studio.kit import ffmpeg
from app.services.studio.motion import codegen, examples, render, validator
from studio_helpers import needs_ffmpeg


@pytest.mark.parametrize("name,code", examples.EXAMPLES)
def test_examples_pass_validation(name, code):
    validator.validate(code)


@pytest.mark.parametrize(
    "snippet",
    [
        "import os\nscene = Scene()",
        "import subprocess\nscene = Scene()",
        "from os import path\nscene = Scene()",
        "from studio_motion import *\nscene = Scene()\nx = scene.__class__",
        "from studio_motion import *\nscene = Scene()\nx = ().__class__.__bases__",
        "from studio_motion import *\nscene = Scene()\nf = open('/etc/passwd')",
        "from studio_motion import *\nscene = Scene()\neval('1+1')",
        "from studio_motion import *\nscene = Scene()\nexec('x=1')",
        "from studio_motion import *\nscene = Scene()\ngetattr(scene, 'x')",
        "from studio_motion import *\nscene = Scene()\nglobals()",
        "from studio_motion import *\nscene = Scene()\nx = '{0.__class__}'.format(scene)",
        "from studio_motion import *\nscene = Scene()\nclass A: pass",
        "from studio_motion import *\nscene = Scene()\nx = scene._tracks",
        "from studio_motion import *\nscene = Scene()\nwith x: pass",
        "from studio_motion import *\nscene = Scene()\nx = __builtins__",
        "from studio_motion import *\nscene = Scene()\nx = type(scene)",
        "from studio_motion import *\nx = 1",  # no `scene`
        "def broken(:\n",
    ],
)
def test_validator_rejects_dangerous_code(snippet):
    with pytest.raises(validator.MotionCodeError):
        validator.validate(snippet)


def test_validator_accepts_math_random_and_loops():
    validator.validate(
        "import math\nimport random\nfrom studio_motion import *\nscene = Scene()\n"
        "for i in range(3):\n    scene.add(Circle(i * 10, 5, 4 + math.sin(i)))\nx = [v * 2 for v in range(4)]\n"
    )


def test_extract_code_handles_fences_and_plain():
    assert codegen.extract_code("text\n```python\na = 1\n```\nmore") == "a = 1"
    assert codegen.extract_code("a = 1") == "a = 1"


@needs_ffmpeg
def test_real_tiny_motion_render(tmp_path):
    out = render.render_scene(examples.STAT_REVEAL, tmp_path / "m.mp4", width=270, height=480, fps=30, duration=1.0)
    probe = ffmpeg.probe(out)
    assert (probe.width, probe.height) == (270, 480) and probe.duration == pytest.approx(1.0, abs=0.15)


@needs_ffmpeg
def test_top5_example_renders_landscape(tmp_path):
    out = render.render_scene(examples.TOP5_COUNTDOWN, tmp_path / "m.mp4", width=480, height=270, fps=30, duration=1.0)
    assert ffmpeg.probe(out).width == 480


@needs_ffmpeg
def test_runtime_error_reports_line(tmp_path):
    code = "from studio_motion import *\nscene = Scene()\nscene.add(Text('a'))\nundefined_name + 1\n"
    with pytest.raises(render.MotionRenderError) as exc:
        render.render_scene(code, tmp_path / "m.mp4", width=108, height=192, fps=30, duration=1.0)
    assert "NameError" in str(exc.value) and "line 4" in str(exc.value)
    assert not (tmp_path / "m.mp4").exists()


@needs_ffmpeg
def test_sandbox_blocks_imports_even_if_validator_is_bypassed(tmp_path):
    code = "import os\nscene = None\n"
    with pytest.raises(render.MotionRenderError) as exc:
        render.render_scene(code, tmp_path / "m.mp4", width=108, height=192, fps=30, duration=1.0, preflight_only=True)
    assert "not allowed" in str(exc.value)


@needs_ffmpeg
def test_infinite_loop_is_killed_by_timeout(tmp_path):
    code = "from studio_motion import *\nscene = Scene()\nwhile True:\n    pass\n"
    with pytest.raises(render.MotionRenderError) as exc:
        render.render_scene(code, tmp_path / "m.mp4", width=108, height=192, fps=30, duration=1.0, timeout=2, preflight_only=True)
    assert "longer than" in str(exc.value)


@needs_ffmpeg
def test_write_scene_auto_fixes_once(monkeypatch):
    replies = iter(["```python\nimport os\nscene = 1\n```", f"```python\n{examples.STAT_REVEAL}\n```"])
    prompts = []

    def fake(prompt_text):
        prompts.append(prompt_text)
        return next(replies)

    monkeypatch.setattr(codegen.gemini_client, "generate_text", fake)
    code = codegen.write_scene("base prompt", width=1080, height=1920, fps=30, duration=2.0, assets={})
    assert "Scene(palette" in code and len(prompts) == 2
    assert "import of 'os' is not allowed" in prompts[1] and "base prompt" in prompts[1]


def test_write_scene_gives_up_after_one_fix(monkeypatch):
    monkeypatch.setattr(codegen.gemini_client, "generate_text", lambda p: "```python\nimport os\n```")
    with pytest.raises(codegen.SceneCodeError):
        codegen.write_scene("p", width=1080, height=1920, fps=30, duration=2.0, assets={})

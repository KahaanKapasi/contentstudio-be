"""Real usage tracking for Studio projects: usage_metadata from Gemini text/image/TTS calls and paid clip seconds
accumulate per project, persist in assets (resume-safe) and are booked as the CostEvent's actual_usd."""

import json
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models import CostEvent, VideoProject
from app.services import gemini_client
from app.services.costs import prices, usage as project_usage
from app.services.studio import registry, runner
from app.services.studio.kit import clips, images, tts
from app.services.studio.registry import Engine, FieldSpec, RecipeSpec, Stage, StageError


def meta(prompt=0, out=0, thoughts=0):
    return SimpleNamespace(prompt_token_count=prompt, candidates_token_count=out, thoughts_token_count=thoughts, tool_use_prompt_token_count=0)


def png_bytes():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, "PNG")
    return buf.getvalue()


# --- accumulator + pricing ---


def test_cost_from_all_usage_kinds():
    u = project_usage.ProjectUsage()
    u.add_text(1_000_000, 100_000, 50_000, grounded=True)  # 0.75 + 0.15*3.75 + 0.014
    u.add_image(2_000, 1_120, 1)  # tokens: 1120*60/1M + 2000*0.5/1M
    u.add_image(0, 0, 1)  # no usage_metadata -> $0.067 per image
    u.add_tts(100, 250, 0)
    u.add_clip("veo", "m", "720p", 8, 0.8)
    u.add_clip("muapi", "kling", "720p", 5, None)  # unpriced: midpoint of muapi.default x seconds, flagged
    total, b = u.cost(on="2026-10-10")
    text = (1_000_000 * 0.75 + 150_000 * 3.75) / 1e6
    image = (1_120 * 60 + 2_000 * 0.5) / 1e6 + 0.067
    speech = (100 * 0.5 + 250 * 9.0) / 1e6
    mu = 5 * (0.05 + 0.40) / 2
    assert b["gemini_text"]["usd"] == pytest.approx(text, abs=1e-6)
    assert b["grounding"]["usd"] == pytest.approx(0.014)
    assert b["gemini_image"]["usd"] == pytest.approx(image, abs=1e-6)
    assert b["gemini_tts"]["usd"] == pytest.approx(speech, abs=1e-6)
    assert b["clips"]["usd"] == pytest.approx(0.8 + mu) and b["clips"]["estimated_usd"] == pytest.approx(mu)
    assert total == pytest.approx(text + 0.014 + image + speech + 0.8 + mu, abs=1e-5)
    assert b["has_estimate"] is True and b["estimated_usd"] == pytest.approx(mu)


def test_usage_roundtrips_through_dict():
    u = project_usage.ProjectUsage()
    u.add_text(10, 20, 30, False)
    u.add_clip("veo", "m", "720p", 4, 0.4)
    again = project_usage.ProjectUsage(json.loads(json.dumps(u.to_dict())))
    assert again.cost()[0] == u.cost()[0] and again.text["calls"] == 1 and len(again.clips) == 1


# --- instrumented calls report into the active accumulator ---


def test_gemini_text_image_and_tts_calls_are_recorded(tmp_path, monkeypatch):
    class Models:
        def generate_content(self, model, contents, config=None):
            if model == settings.gemini_image_model:
                part = SimpleNamespace(inline_data=SimpleNamespace(data=png_bytes(), mime_type="image/png"))
                return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))], usage_metadata=meta(300, 1120))
            if model == settings.gemini_tts_model:
                part = SimpleNamespace(inline_data=SimpleNamespace(data=b"\x00\x00" * 2400))  # raw PCM, 0.1 s
                return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))], usage_metadata=meta(20, 40))
            return SimpleNamespace(text='{"a": 1}', usage_metadata=meta(1000, 200, 100))

    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr(gemini_client, "_client", SimpleNamespace(models=Models()))
    u = project_usage.ProjectUsage()
    with project_usage.use(u):
        gemini_client.generate_json("p")
        images.generate_image("cat", tmp_path / "i.png")
        tts.synthesize(["Hello there, friend."], tmp_path / "s.wav", provider="gemini")
    assert (u.text["prompt_tokens"], u.text["output_tokens"], u.text["thinking_tokens"]) == (1000, 200, 100)
    assert (u.image["prompt_tokens"], u.image["output_tokens"], u.image["images"]) == (300, 1120, 1)
    assert (u.tts["prompt_tokens"], u.tts["output_tokens"], u.tts["estimated_output_tokens"]) == (20, 40, 0)
    total, b = u.cost()
    assert b["gemini_image"]["usd"] == pytest.approx((1120 * 60 + 300 * 0.5) / 1e6, abs=1e-6)
    assert total == pytest.approx(b["gemini_text"]["usd"] + b["gemini_image"]["usd"] + b["gemini_tts"]["usd"], abs=1e-5)


def test_missing_usage_metadata_falls_back_to_per_image_and_audio_length(tmp_path, monkeypatch):
    class Models:
        def generate_content(self, model, contents, config=None):
            if model == settings.gemini_image_model:
                part = SimpleNamespace(inline_data=SimpleNamespace(data=png_bytes(), mime_type="image/png"))
            else:
                part = SimpleNamespace(inline_data=SimpleNamespace(data=b"\x00\x00" * 24000))  # 1 s of PCM
            return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])

    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr(gemini_client, "_client", SimpleNamespace(models=Models()))
    u = project_usage.ProjectUsage()
    with project_usage.use(u):
        images.generate_image("cat", tmp_path / "i.png")
        tts.synthesize(["Hello there, friend."], tmp_path / "s.wav", provider="gemini")
    _, b = u.cost()
    assert b["gemini_image"]["usd"] == pytest.approx(prices.usd("gemini.image.out"))
    assert u.tts["estimated_output_tokens"] == 25 and b["gemini_tts"]["usd"] == pytest.approx(25 * 9.0 / 1e6, abs=1e-6)


def test_recording_is_inert_without_an_active_project():
    gemini_client.record_usage(SimpleNamespace(usage_metadata=meta(5, 5)), kind="image", fallback=1)  # must not raise
    assert project_usage.current() is None


def test_finished_clip_is_recorded_priced_or_estimated(tmp_path, monkeypatch):
    from app.services.video_providers import JobHandle, PollResult

    class Provider:
        def missing_keys(self):
            return []

        def submit(self, params):
            return JobHandle("j")

        def poll(self, job):
            return PollResult("succeeded", output="o")

        def download(self, output, dest):
            dest.write_bytes(b"mp4")

    monkeypatch.setattr(clips, "get_provider", lambda p: Provider())
    u = project_usage.ProjectUsage()
    with project_usage.use(u):
        clips.generate_clip("p", tmp_path / "a.mp4", provider="veo", model="veo-3.1-fast-generate-preview", aspect_ratio="9:16", duration_seconds=8, resolution="720p")
        clips.generate_clip("p", tmp_path / "b.mp4", provider="muapi", model="wan2.5-text-to-video", aspect_ratio="9:16", duration_seconds=5, resolution="720p")
    veo, mu = u.clips
    assert veo["usd"] == pytest.approx(0.8) and veo["estimate"] is False
    assert mu["estimate"] is True and mu["usd"] == pytest.approx(5 * 0.225)


# --- runner: persistence, resume, ledger ---


class SpendEngine(Engine):
    id = "spend"
    label = "Spend"
    description = "t"
    fail_in: set = set()

    def __init__(self):
        self.recipes = [RecipeSpec("spend", "Spend", "d", [FieldSpec("topic", "Topic", "text", required=True)], paid=False)]

    def stages(self, recipe, params):
        def plan(ctx):  # 1M prompt tokens = $0.75
            gemini_client.record_usage(SimpleNamespace(usage_metadata=meta(1_000_000, 0)))

        def render1(ctx):  # one 8 s Veo clip = $0.80, then maybe a failure
            ctx.usage.add_clip("veo", "m", "720p", 8, 0.8)
            if "render1" in SpendEngine.fail_in:
                raise StageError("boom")

        def render2(ctx):  # 1M output tokens = $3.75
            gemini_client.record_usage(SimpleNamespace(usage_metadata=meta(0, 1_000_000)))
            ctx.path(registry.FINAL_NAME).write_bytes(b"mp4")

        return [Stage("Plan", plan, "plan"), Stage("Render one", render1), Stage("Render two", render2)]

    def estimate_cost(self, recipe, params, plan):
        return 5.0


@pytest.fixture
def spend(monkeypatch):
    SpendEngine.fail_in = set()
    engine = SpendEngine()
    monkeypatch.setattr(registry, "_engines", {"spend": engine})
    return engine


def run(db, project):
    runner.run_project(project.id)
    db.expire_all()
    return db.get(VideoProject, project.id)


def events(db):
    db.expire_all()
    return db.query(CostEvent).filter(CostEvent.action == "studio.project").order_by(CostEvent.id).all()


def test_success_logs_actual_usd_with_breakdown(db, spend, tmp_path):
    project = runner.create_project(db, engine=spend, recipe=spend.recipes[0], params={"topic": "x"}, auto_approve=True)
    project = run(db, project)
    assert project.status == "succeeded"
    (ev,) = events(db)
    assert ev.actual_usd == pytest.approx(0.75 + 0.80 + 3.75)
    d = json.loads(ev.details)
    assert d["status"] == "succeeded" and d["has_estimate"] is False
    assert d["usage"]["clips"]["seconds"] == 8 and d["usage"]["gemini_text"]["prompt_tokens"] == 1_000_000
    assert d["usage"]["total_usd"] == pytest.approx(5.3)
    assert ev.estimated_low_usd is not None  # estimate range kept alongside the actual


def test_failed_project_logs_what_was_spent_and_resume_logs_only_the_rest(db, spend):
    SpendEngine.fail_in = {"render1"}
    project = runner.create_project(db, engine=spend, recipe=spend.recipes[0], params={"topic": "x"}, auto_approve=True)
    project = run(db, project)
    assert project.status == "failed"
    (failed,) = events(db)
    assert failed.actual_usd == pytest.approx(0.75 + 0.80) and json.loads(failed.details)["status"] == "failed"
    stored = json.loads(project.assets)["usage"]  # persisted with the project
    assert stored["clips"][0]["seconds"] == 8 and stored["text"]["prompt_tokens"] == 1_000_000

    SpendEngine.fail_in = set()
    runner.retry(db, project)
    project = run(db, project)
    assert project.status == "succeeded"
    ok = events(db)[-1]
    assert ok.actual_usd == pytest.approx(0.80 + 3.75)  # render1 ran again (+$0.80) and render2; plan was not repeated
    d = json.loads(ok.details)
    assert d["total_usd"] == pytest.approx(0.75 + 0.80 + 0.80 + 3.75) and d["usage"]["gemini_text"]["prompt_tokens"] == 1_000_000
    assert sum(e.actual_usd for e in events(db)) == pytest.approx(d["total_usd"])  # nothing double counted


def test_usage_survives_the_approval_pause(db, monkeypatch):
    engine = SpendEngine()
    engine.recipes[0].paid = True
    monkeypatch.setattr(registry, "_engines", {"spend": engine})
    # make Plan a plan-phase stage and the others render-phase (Stage default phase)
    project = runner.create_project(db, engine=engine, recipe=engine.recipes[0], params={"topic": "x"}, auto_approve=False)
    project = run(db, project)
    assert project.status == "awaiting_approval"
    (paused,) = events(db)
    assert paused.actual_usd == pytest.approx(0.75) and json.loads(paused.details)["status"] == "awaiting_approval"
    runner.approve(db, project)
    project = run(db, project)
    assert project.status == "succeeded"
    assert sum(e.actual_usd for e in events(db)) == pytest.approx(5.3)


def test_estimated_price_part_marks_the_event(db, monkeypatch):
    class E(SpendEngine):
        def stages(self, recipe, params):
            def go(ctx):
                ctx.usage.add_clip("muapi", "kling", "720p", 10, None)
                ctx.path(registry.FINAL_NAME).write_bytes(b"mp4")
            return [Stage("Go", go, "plan")]

    engine = E()
    monkeypatch.setattr(registry, "_engines", {"spend": engine})
    project = runner.create_project(db, engine=engine, recipe=engine.recipes[0], params={"topic": "x"}, auto_approve=True)
    run(db, project)
    (ev,) = events(db)
    assert json.loads(ev.details)["has_estimate"] is True and ev.actual_usd == pytest.approx(2.25)
    from app.services.costs import ledger

    assert ledger._usd(ev) == (pytest.approx(2.25), True)  # dashboard counts it as an estimate


def test_ledger_failure_does_not_break_the_project(db, spend, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(runner.ledger, "log_event", boom)
    project = runner.create_project(db, engine=spend, recipe=spend.recipes[0], params={"topic": "x"}, auto_approve=True)
    assert run(db, project).status == "succeeded"

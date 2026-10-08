import json
import time

import pytest

from app.models import VideoProject
from app.services.studio import registry, runner
from app.services.studio.registry import Engine, FieldSpec, RecipeSpec, Stage, StageError


class FakeEngine(Engine):
    id = "fake"
    label = "Fake"
    description = "test"
    calls: list = []
    fail_once = {"render1"}

    def __init__(self, paid=False):
        self.recipes = [RecipeSpec("fake", "Fake", "d", [FieldSpec("topic", "Topic", "text", required=True)], paid=paid)]

    def stages(self, recipe, params):
        def make(name, writes=None):
            def fn(ctx):
                FakeEngine.calls.append(name)
                if name in FakeEngine.fail_once:
                    FakeEngine.fail_once.discard(name)
                    raise StageError("boom in " + name)
                ctx.progress(0.5)
                if name == "plan1":
                    ctx.plan.update(summary="the plan", script="s")
                    ctx.path("preview.png").write_bytes(b"png")
                    ctx.register("sheet", ctx.path("preview.png"), label="Sheet", preview=True)
                if name == "render2":
                    ctx.path(registry.FINAL_NAME).write_bytes(b"mp4")
            return fn
        return [Stage("Plan one", make("plan1"), "plan"), Stage("Render one", make("render1")), Stage("Render two", make("render2"), weight=2)]

    def estimate_cost(self, recipe, params, plan):
        return 1.5


@pytest.fixture
def fake(monkeypatch):
    FakeEngine.calls = []
    FakeEngine.fail_once = {"render1"}
    engines = {"fake": FakeEngine(paid=False)}
    monkeypatch.setattr(registry, "_engines", engines)
    return engines["fake"]


def make(db, engine, auto=False):
    return runner.create_project(db, engine=engine, recipe=engine.recipes[0], params={"topic": "x"}, auto_approve=auto)


def test_stage_progression_failure_and_resume(db, fake):
    project = make(db, fake)
    runner.run_project(project.id)
    db.expire_all()
    project = db.get(VideoProject, project.id)
    assert project.status == "failed" and "boom in render1" in project.error
    assert project.stage == "Render one"
    assert json.loads(project.assets)["done"] == ["Plan one"]
    assert json.loads(project.plan)["summary"] == "the plan"

    runner.retry(db, project)
    assert project.status == "queued" and project.error is None
    runner.run_project(project.id)
    db.expire_all()
    project = db.get(VideoProject, project.id)
    assert project.status == "succeeded" and project.progress == 100 and project.local_path == "final.mp4"
    assert FakeEngine.calls == ["plan1", "render1", "render1", "render2"]  # plan1 did not run again
    assert runner.local_file(project).read_bytes() == b"mp4"


def test_paid_recipe_stops_for_approval_then_continues(db, fake, monkeypatch):
    paid = FakeEngine(paid=True)
    FakeEngine.fail_once = set()
    monkeypatch.setattr(registry, "_engines", {"fake": paid})
    project = make(db, paid)
    runner.run_project(project.id)
    db.expire_all()
    project = db.get(VideoProject, project.id)
    assert project.status == "awaiting_approval" and project.estimated_cost_usd == 1.5
    assert FakeEngine.calls == ["plan1"]
    with pytest.raises(ValueError):
        runner.retry(db, project)
    runner.approve(db, project)
    runner.run_project(project.id)
    db.expire_all()
    assert db.get(VideoProject, project.id).status == "succeeded"
    assert FakeEngine.calls == ["plan1", "render1", "render2"]
    with pytest.raises(ValueError):
        runner.approve(db, db.get(VideoProject, project.id))


def test_auto_approve_skips_gate(db, fake, monkeypatch):
    paid = FakeEngine(paid=True)
    FakeEngine.fail_once = set()
    monkeypatch.setattr(registry, "_engines", {"fake": paid})
    project = make(db, paid, auto=True)
    runner.run_project(project.id)
    db.expire_all()
    assert db.get(VideoProject, project.id).status == "succeeded"


def test_replan_reruns_planning(db, fake, monkeypatch):
    paid = FakeEngine(paid=True)
    monkeypatch.setattr(registry, "_engines", {"fake": paid})
    project = make(db, paid)
    runner.run_project(project.id)
    db.expire_all()
    project = db.get(VideoProject, project.id)
    assert project.status == "awaiting_approval"
    runner.replan(db, project)
    assert json.loads(project.plan) == {} and json.loads(project.assets)["done"] == []
    runner.run_project(project.id)
    db.expire_all()
    project = db.get(VideoProject, project.id)
    assert project.status == "awaiting_approval" and FakeEngine.calls.count("plan1") == 2


def test_unexpected_exception_becomes_generic_failure(db, fake, monkeypatch):
    FakeEngine.fail_once = set()

    def explode(self, recipe, params):
        return [Stage("Boom", lambda ctx: 1 / 0, "plan")]

    monkeypatch.setattr(FakeEngine, "stages", explode)
    project = make(db, fake)
    runner.run_project(project.id)
    db.expire_all()
    project = db.get(VideoProject, project.id)
    assert project.status == "failed" and "Boom" in project.error and "division" not in project.error


def test_secrets_are_scrubbed_from_errors(db, fake, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "pexels_api_key", "SECRET123")
    FakeEngine.fail_once = set()

    def leak(self, recipe, params):
        def fn(ctx):
            raise StageError("request failed with key SECRET123")
        return [Stage("Leak", fn, "plan")]

    monkeypatch.setattr(FakeEngine, "stages", leak)
    project = make(db, fake)
    runner.run_project(project.id)
    db.expire_all()
    assert "SECRET123" not in db.get(VideoProject, project.id).error


def test_missing_final_video_fails(db, fake, monkeypatch):
    FakeEngine.fail_once = set()
    monkeypatch.setattr(FakeEngine, "stages", lambda self, r, p: [Stage("Nothing", lambda ctx: None, "plan")])
    project = make(db, fake)
    runner.run_project(project.id)
    db.expire_all()
    assert db.get(VideoProject, project.id).status == "failed"


def test_reconcile_fails_orphaned_active_project(db, fake):
    project = make(db, fake)
    assets = json.loads(project.assets)
    assets["touched"] = time.time() - 600
    project.assets = json.dumps(assets)
    project.status = "rendering"
    db.commit()
    runner.reconcile(db, project)
    assert project.status == "failed" and "restarted" in project.error


def test_reconcile_leaves_fresh_and_inflight_projects_alone(db, fake):
    project = make(db, fake)
    project.status = "rendering"
    db.commit()
    assert runner.reconcile(db, project).status == "rendering"
    assets = json.loads(project.assets)
    assets["touched"] = time.time() - 600
    project.assets = json.dumps(assets)
    db.commit()
    runner._inflight.add(project.id)
    try:
        assert runner.reconcile(db, project).status == "rendering"
    finally:
        runner._inflight.discard(project.id)


def test_overall_timeout_stops_the_job(db, fake, monkeypatch):
    monkeypatch.setattr(runner, "OVERALL_TIMEOUT_S", -1)
    project = make(db, fake)
    runner.run_project(project.id)
    db.expire_all()
    project = db.get(VideoProject, project.id)
    assert project.status == "failed" and "Timed out" in project.error


def test_preview_asset_served_via_api(client, db, fake):
    FakeEngine.fail_once = set()
    project = make(db, fake)
    runner.run_project(project.id)
    body = client.get(f"/api/studio/projects/{project.id}").json()
    assert body["previews"] == [{"name": "sheet", "label": "Sheet", "kind": "image"}]
    assert body["status"] == "succeeded" and body["has_file"] is True and body["plan"]["summary"] == "the plan"
    assert client.get(f"/api/studio/projects/{project.id}/assets/sheet").content == b"png"
    assert client.get(f"/api/studio/projects/{project.id}/assets/final").status_code == 404
    assert client.get(f"/api/studio/projects/{project.id}/file").content == b"mp4"


def test_approve_and_retry_endpoints(client, db, fake, monkeypatch):
    monkeypatch.setattr(runner, "run_project", lambda pid: None)
    project = make(db, fake)
    project.status = "failed"
    db.commit()
    r = client.post(f"/api/studio/projects/{project.id}/retry")
    assert r.status_code == 200 and r.json()["status"] == "queued"

"""Scene-film engines (toons, shorts/film, explainer3d) and shorts/dance, with Gemini / images / TTS / clips / Muapi /
Cloudinary mocked and tiny real ffmpeg renders (270x480)."""

import json
import shutil
import subprocess

import pytest
from PIL import Image

from app.config import settings
from app.services import gemini_client, media_hosting
from app.services.studio import runner
from app.services.studio.engines import _scenefilm as sf
from app.services.studio.engines import _scenerender as sr
from app.services.studio.engines import explainer3d, shorts, toons
from app.services.studio.kit import clips as kitclips
from app.services.studio.kit import ffmpeg, images, tts
from app.services.video_providers import PollResult, ProviderError, catalog, http_common
from app.services.video_providers.muapi import MuapiProvider
from studio_helpers import fake_edge_fetch, make_test_video, needs_ffmpeg

SMALL = (270, 480)


# --- plan parsing / repair (pure) ---


def test_parse_characters_lines():
    chars = sf.parse_characters("Kylian: fast striker, red kit\n- Coach - grumpy, wears a tracksuit\nRef\n\nkylian: duplicate")
    assert [c["name"] for c in chars] == ["Kylian", "Coach", "Ref"]
    assert chars[0]["description"] == "fast striker, red kit" and chars[1]["description"].startswith("grumpy")


def test_parse_plan_repairs_dialogue_forms_speakers_and_durations():
    raw = {
        "title": "  Locker **Room**  ", "characters": [{"name": "Captain Big", "description": "huge head", "voice": "deep male"}, "Keeper"],
        "shots": [
            {"visual": "Captain at a lectern", "duration_s": 99, "camera": "push in", "dialogue": "Captain Big: (shouting) We will WIN!"},
            {"visual": "Keeper hides", "duration_s": "x", "dialogue": [{"character": "keeper", "line": "Is he serious?"}, "Boss: new guy line", {"speaker": "", "text": "  "}]},
            {"visual": "x"},  # too short a visual: dropped
            {"visual": "Everyone cheers wildly", "dialogue": [{"speaker": "Captain", "text": "Pizza for all."}]},
            {"visual": "Extra shot that gets truncated", "dialogue": "Keeper: hi there"},
        ],
        "end_card": " The end! ",
    }
    p = {"scenes": 3, "premise": "p", "characters": "Referee: whistles a lot"}
    plan = sf.parse_plan(raw, toons.SKETCH_FLAVOUR, p)
    assert len(plan["shots"]) == 3 and [s["index"] for s in plan["shots"]] == [1, 2, 3]
    assert plan["title"] == "Locker Room" and plan["end_card"] == "The end!"
    names = [c["name"] for c in plan["characters"]]
    assert names[:2] == ["Captain Big", "Keeper"] and "Referee" in names and "Boss" in names  # user character + unknown speaker added
    s1, s2, s3 = plan["shots"]
    assert s1["duration_s"] == 7.0 and s1["dialogue"] == [{"speaker": "Captain Big", "text": "We will WIN!"}]
    assert s2["duration_s"] == 5.0  # unparseable duration falls back to the middle of the range
    assert s2["dialogue"][0] == {"speaker": "Keeper", "text": "Is he serious?"} and s2["dialogue"][1]["speaker"] == "Boss"
    assert s3["dialogue"][0]["speaker"] == "Captain Big"


def test_parse_plan_rejects_unusable_answers():
    fl, p = toons.SKETCH_FLAVOUR, {"scenes": 5, "premise": "p"}
    for bad in ("nope", {}, {"shots": []}, {"shots": [{"visual": "one good shot here", "dialogue": "A: hi"}]}):
        with pytest.raises(sf.PlanError):
            sf.parse_plan(bad, fl, p)
    with pytest.raises(sf.PlanError, match="no characters"):
        sf.parse_plan({"shots": [{"visual": f"shot number {i}", "dialogue": ""} for i in range(5)]}, fl, p)
    wrapped = {"plan": {"characters": [{"name": "A"}], "shots": [{"visual": f"shot number {i}", "dialogue": [{"speaker": "A", "text": "hi"}]} for i in range(5)]}}
    assert len(sf.parse_plan(wrapped, fl, p)["shots"]) == 5  # {"plan": {...}} is unwrapped


def test_parse_plan_narration_and_lyrics_kinds():
    shots = [{"visual": f"cutaway {i}", "narration": f"Line {i} here."} for i in range(10)]
    plan = sf.parse_plan({"characters": [{"name": "Ignored"}], "shots": shots}, explainer3d.FLAVOUR, {"shots": 10, "premise": "p"})
    assert plan["characters"] == [] and plan["end_card"] is None
    assert plan["shots"][0]["dialogue"] == [{"speaker": "Narrator", "text": "Line 0 here."}] and plan["shots"][0]["duration_s"] == 3.0
    song = [{"visual": f"verse scene {i}", "dialogue": [{"speaker": "x", "text": f"la la {i}"}]} for i in range(4)]
    plan = sf.parse_plan({"shots": song, "end_card": "Song Title"}, toons.SONG_FLAVOUR, {"scenes": 4, "concept": "c", "target": "t"})
    assert [c["name"] for c in plan["characters"]] == ["Singer"] and {l["speaker"] for s in plan["shots"] for l in s["dialogue"]} == {"Singer"}


def test_assign_voices_distinct_and_gendered():
    chars = [{"name": "A", "voice": "deep male"}, {"name": "B", "voice": "bright young female"}, {"name": "C", "voice": "gruff man"}, {"name": "D", "description": "she is the referee"}]
    v = sf.assign_voices(chars, "edge", "en", None)
    assert len(set(v.values())) == 4 and v["B"] in sf.EDGE_FEMALE and v["D"] in sf.EDGE_FEMALE and v["A"] in sf.EDGE_MALE
    assert sf.assign_voices(chars, "edge", "en", "en-GB-RyanNeural")["A"] == "en-GB-RyanNeural"
    assert sf.assign_voices(chars[:2], "gemini", "en", None)["B"] in sf.GEMINI_FEMALE
    assert set(sf.assign_voices(chars, "edge", "fr", None).values()) == {""}  # non-English: language default


def test_assign_photos_by_name_then_order(tmp_path):
    chars = [{"name": "Kylian Mbappe"}, {"name": "Coach"}, {"name": "Ref"}]
    paths = [tmp_path / "ref_pic.png", tmp_path / "kylian-mbappe.jpg", tmp_path / "other.png"]
    got = sf.assign_photos(chars, paths)
    assert got["Kylian Mbappe"] == [paths[1]] and got["Ref"] == [paths[0]] and got["Coach"] == [paths[2]]


def test_clip_len_and_estimates():
    assert sf.clip_len({"clip_provider": "veo"}, 3.0) == 4 and sf.clip_len({"clip_provider": "veo"}, 4.4) == 6 and sf.clip_len({"clip_provider": "veo"}, 20) == 8
    cheap = sf.estimate({"motion_quality": "cheap"}, [3, 3, 3], 5)
    assert cheap == {"images": 5, "image_usd": 0.2, "clip_seconds": 0, "clip_usd": 0.0, "usd": 0.2}
    ai = sf.estimate({"motion_quality": "ai", "clip_provider": "veo"}, [3, 5, 7], 6)
    assert ai["clip_seconds"] == 4 + 6 + 8 and ai["clip_usd"] == pytest.approx(0.9) and ai["usd"] == pytest.approx(1.14)  # Veo 3.1 Lite 720p $0.05/s
    mu = sf.estimate({"motion_quality": "ai", "clip_provider": "muapi"}, [3, 5], 4)
    assert mu["clip_usd"] is None and mu["usd"] == 0.16  # Muapi clips are unpriced: only the images count


# --- engine runs ---

pytestmark_ffmpeg = needs_ffmpeg


@pytest.fixture
def keys(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-test-key")
    monkeypatch.setattr(settings, "muapi_api_key", "muapi-test-key")
    monkeypatch.setattr(settings, "cloudinary_url", "cloudinary://k:s@cloud")


@pytest.fixture
def small(monkeypatch):
    monkeypatch.setitem(ffmpeg.TARGETS, "9:16", SMALL)
    monkeypatch.setattr(tts, "_edge_fetch", fake_edge_fetch)


def sketch_plan(n=3):
    return {
        "title": "Locker Room", "logline": "A coach loses it.",
        "characters": [{"name": "Coach", "description": "round red-faced coach", "voice": "deep gruff male"}, {"name": "Star", "description": "tall striker, gold boots", "voice": "bright young female"}],
        "shots": [{"index": i + 1, "duration_s": 3, "visual": f"Coach and Star in the dressing room, beat {i + 1}", "camera": ["push in", "handheld shake", "pan left"][i % 3],
                   "dialogue": [{"speaker": "Coach", "text": "You missed again."}, {"speaker": "Star", "text": "It was windy."}], "sfx_note": "crowd murmur"} for i in range(n)],
        "end_card": "Wind is not an excuse",
    }


class Calls:
    def __init__(self):
        self.prompts, self.images, self.clips, self.json_prompts = [], [], [], []


@pytest.fixture
def calls(monkeypatch):
    c = Calls()

    def fake_image(prompt, dest, *, aspect="9:16", references=None):
        c.images.append({"prompt": prompt, "dest": dest, "aspect": aspect, "refs": [str(r) for r in references or []]})
        dest.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", SMALL if aspect == "9:16" else (256, 256), (40 + 20 * len(c.images) % 200, 90, 160)).save(dest)
        return dest

    monkeypatch.setattr(images, "generate_image", fake_image)
    return c


def av_video(path, seconds=5.0, size="160x284"):
    subprocess.run(
        [ffmpeg.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True,
    )
    return path


def create(client, engine, params, recipe=None, files=None, auto=False):
    data = {"engine": engine, "params": json.dumps(params)}
    if recipe:
        data["recipe"] = recipe
    if auto:
        data["auto_approve"] = "true"
    return client.post("/api/studio/projects", data=data, files=files)


def get(client, pid):
    return client.get(f"/api/studio/projects/{pid}").json()


@needs_ffmpeg
def test_toons_sketch_cheap_gate_then_render(client, db, keys, small, calls, monkeypatch):
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: calls.json_prompts.append(p) or sketch_plan())
    seen = {}
    real = sr.finalize_film
    monkeypatch.setattr(sr, "finalize_film", lambda ctx, video, **kw: seen.update(kw) or real(ctx, video, **kw))
    photo = Image.new("RGB", (64, 64), (200, 10, 10))
    import io
    buf = io.BytesIO()
    photo.save(buf, format="PNG")
    r = create(client, "toons", {"premise": "Coach vs striker", "scenes": 3, "characters": "Coach: round red-faced coach\nStar: tall striker"}, recipe="sketch",
               files=[("character_images", ("coach.png", buf.getvalue(), "image/png"))])
    assert r.status_code == 200, r.text
    body = get(client, r.json()["id"])
    assert body["status"] == "awaiting_approval", body["error"]
    # plan + previews: one sheet per character, plus the voices
    assert [p["name"] for p in body["previews"]] == ["sheet_coach-0", "sheet_star-1", "voices"]
    assert body["plan"]["scenes"][0]["text"].startswith("Coach: You missed again.") and len(body["plan"]["scenes"]) == 3
    assert "Wind is not an excuse" in body["plan"]["script"] and "AI PARODY" in body["plan"]["notes"]
    assert "COMEDY" not in body["plan"]["summary"] and "3 shots" in body["plan"]["summary"]
    # cheap path: the estimate is images only (2 sheets + 3 stills)
    assert body["estimated_cost_usd"] == pytest.approx(5 * sf.IMAGE_COST_USD)
    assert "2D flat cartoon" in calls.json_prompts[0] and "No sexual content" in calls.json_prompts[0]
    # the photo conditioned the matching character's sheet only
    sheet_calls = [c for c in calls.images if c["aspect"] == "1:1"]
    assert len(sheet_calls) == 2 and sheet_calls[0]["refs"] and sheet_calls[1]["refs"] == []
    assert not [c for c in calls.images if c["aspect"] == "9:16"]  # no stills before approval
    assert client.get(f"/api/studio/projects/{body['id']}/assets/sheet_coach-0").status_code == 200

    assert client.post(f"/api/studio/projects/{body['id']}/approve").status_code == 200
    done = get(client, body["id"])
    assert done["status"] == "succeeded", done["error"]
    stills = [c for c in calls.images if c["aspect"] == "9:16"]
    assert len(stills) == 3 and all(len(c["refs"]) == 2 for c in stills)  # both characters speak in every shot: both sheets are references
    assert all("keep each identical to its reference" in c["prompt"] for c in stills)
    probe = ffmpeg.probe(runner.project_dir(done["id"]) / "final.mp4")
    assert (probe.width, probe.height) == SMALL and probe.has_audio and probe.duration >= 3 * 3 + sf.CARD_S - 0.5
    assert seen["tag"] == "AI PARODY" and seen["style"] == "bold-pop" and seen["subtitles"] is True and len(seen["sentences"]) == 6
    assert (runner.project_dir(done["id"]) / "tag.png").is_file()
    assert not list((runner.project_dir(done["id"]) / "clips").glob("*.mp4"))  # intermediates cleaned


@needs_ffmpeg
def test_toons_no_parody_tag_and_no_subtitles(client, db, keys, small, calls, monkeypatch):
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: sketch_plan())
    seen = {}
    real = sr.finalize_film
    monkeypatch.setattr(sr, "finalize_film", lambda ctx, video, **kw: seen.update(kw) or real(ctx, video, **kw))
    r = create(client, "toons", {"premise": "x", "scenes": 3, "parody_label": False, "subtitles": False}, recipe="sketch", auto=True)
    done = get(client, r.json()["id"])
    assert done["status"] == "succeeded", done["error"]
    assert seen["tag"] is None and seen["subtitles"] is False and "AI PARODY" not in (done["plan"]["notes"] or "")
    assert not (runner.project_dir(done["id"]) / "tag.png").exists()


@needs_ffmpeg
def test_parody_song_with_instrumental_uses_karaoke_and_mix(client, db, keys, small, calls, monkeypatch):
    song = {"title": "Chant", "characters": [{"name": "Striker"}], "end_card": "Chant of Doom",
            "shots": [{"visual": f"stadium verse {i}", "duration_s": 3, "dialogue": [{"speaker": "Striker", "text": f"We sing the line number {i}"}]} for i in range(4)]}
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: song)
    seen = {}
    real = sr.finalize_film
    monkeypatch.setattr(sr, "finalize_film", lambda ctx, video, **kw: seen.update(kw) or real(ctx, video, **kw))
    wav = subprocess.run([ffmpeg.ffmpeg_exe(), "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=220:duration=3", "-f", "wav", "pipe:1"], capture_output=True, check=True).stdout
    r = create(client, "toons", {"concept": "Why we lose", "target": "Some FC", "scenes": 4}, recipe="parody-song", files=[("instrumental", ("beat.wav", wav, "audio/wav"))], auto=True)
    done = get(client, r.json()["id"])
    assert done["status"] == "succeeded", done["error"]
    assert seen["style"] == "karaoke" and "instrumental" in done["plan"]["notes"]
    assert (runner.project_dir(done["id"]) / "audio" / "mix.wav").is_file()


@needs_ffmpeg
def test_explainer_research_style_sheet_cut_stitching(client, db, keys, small, calls, monkeypatch):
    shots = [{"index": i + 1, "duration_s": 2, "visual": f"x-ray cutaway of the skull, moment {i}", "camera": "pull back", "narration": f"Fact number {i} lands hard."} for i in range(10)]
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: calls.json_prompts.append(p) or {"title": "Heading", "logline": "l", "characters": [{"name": "Nope"}], "shots": shots})
    monkeypatch.setattr(gemini_client, "generate_grounded", lambda p: ("- Heading a ball 1000 times is a lot\n- Studies exist on the topic of heading", [{"title": "Study", "url": "https://x.test/s"}]))
    r = create(client, "explainer3d", {"premise": "What if you head a ball 1000 times", "research": True, "shots": 10}, auto=False)
    body = get(client, r.json()["id"])
    assert body["status"] == "awaiting_approval", body["error"]
    assert "Heading a ball 1000 times" in calls.json_prompts[0] and "Blender/Octane" in calls.json_prompts[0]
    assert [p["name"] for p in body["previews"]] == ["sheet_style", "voices"]  # no characters: one style reference
    assert "Researched: Study" in body["plan"]["notes"] and len(body["plan"]["scenes"]) == 10
    assert body["plan"]["scenes"][0]["text"] == "Fact number 0 lands hard." and body["plan"]["scenes"][0]["duration_s"] >= 2.0
    assert body["estimated_cost_usd"] == pytest.approx(11 * sf.IMAGE_COST_USD)
    client.post(f"/api/studio/projects/{body['id']}/approve")
    done = get(client, body["id"])
    assert done["status"] == "succeeded", done["error"]
    assert "AI PARODY" not in (done["plan"]["notes"] or "")  # explainer has no parody label
    assert len([c for c in calls.images if c["aspect"] == "9:16"]) == 10
    probe = ffmpeg.probe(runner.project_dir(done["id"]) / "final.mp4")
    assert probe.has_audio and probe.duration == pytest.approx(sum(s["duration_s"] for s in done["plan"]["scenes"]), abs=0.6)  # hard cuts: no card, no overlap


@needs_ffmpeg
def test_film_ai_native_audio_veo_costs_and_clip_calls(client, db, keys, small, calls, monkeypatch, tmp_path):
    src = av_video(tmp_path / "veo.mp4", 8.0)
    clip_calls = []

    def fake_clip(prompt, dest, *, provider, model, aspect_ratio, duration_seconds, resolution, image=None, timeout=0):
        clip_calls.append({"prompt": prompt, "provider": provider, "model": model, "aspect": aspect_ratio, "secs": duration_seconds, "res": resolution, "image": image})
        shutil.copy(src, dest)
        return dest

    monkeypatch.setattr(kitclips, "generate_clip", fake_clip)
    plan = sketch_plan(3)
    plan["end_card"] = "ignored for film"
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: plan)
    r = create(client, "shorts", {"premise": "Dictator in the locker room", "scenes": 3, "motion_quality": "ai", "dialogue": "native"}, recipe="film")
    body = get(client, r.json()["id"])
    assert body["status"] == "awaiting_approval", body["error"]
    assert [p["name"] for p in body["previews"]] == ["sheet_coach-0", "sheet_star-1"]  # native audio: no TTS voices preview
    # 3 shots planned at 3 s -> 4 s Veo Lite clips at $0.05/s; 2 sheets + 3 stills
    assert body["estimated_cost_usd"] == pytest.approx(5 * sf.IMAGE_COST_USD + 3 * 4 * 0.05)
    assert "native audio" in body["plan"]["notes"] and "AI PARODY" in body["plan"]["notes"]
    assert not clip_calls
    client.post(f"/api/studio/projects/{body['id']}/approve")
    done = get(client, body["id"])
    assert done["status"] == "succeeded", done["error"]
    assert len(clip_calls) == 3 and all(c["provider"] == "veo" and c["model"] == "veo-3.1-lite-generate-preview" and c["secs"] == 4 and c["res"] == "720p" for c in clip_calls)
    assert all(str(c["image"]).endswith(".png") for c in clip_calls)  # image-to-video from the still
    assert 'Coach (deep gruff male voice) says: "You missed again."' in clip_calls[0]["prompt"]
    probe = ffmpeg.probe(runner.project_dir(done["id"]) / "final.mp4")
    assert probe.has_audio and probe.duration == pytest.approx(12.0, abs=0.7)


@needs_ffmpeg
def test_film_ai_with_tts_drops_clip_audio_and_cheap_is_cheaper(client, db, keys, small, calls, monkeypatch, tmp_path):
    src = av_video(tmp_path / "veo.mp4", 8.0)
    seen = {"n": 0}

    def fake_clip(prompt, dest, **kw):
        seen["n"] += 1
        seen["prompt"] = prompt
        shutil.copy(src, dest)

    monkeypatch.setattr(kitclips, "generate_clip", fake_clip)
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: sketch_plan(3))
    ai = get(client, create(client, "shorts", {"premise": "p", "scenes": 3, "motion_quality": "ai", "dialogue": "tts"}, recipe="film").json()["id"])
    cheap = get(client, create(client, "shorts", {"premise": "p", "scenes": 3, "motion_quality": "cheap", "dialogue": "native"}, recipe="film").json()["id"])
    assert ai["status"] == cheap["status"] == "awaiting_approval"
    assert ai["estimated_cost_usd"] > cheap["estimated_cost_usd"] == pytest.approx(5 * sf.IMAGE_COST_USD)
    assert "voices" in [p["name"] for p in ai["previews"]] and "voices" in [p["name"] for p in cheap["previews"]]  # native without AI motion falls back to TTS
    client.post(f"/api/studio/projects/{ai['id']}/approve")
    done = get(client, ai["id"])
    assert done["status"] == "succeeded", done["error"]
    assert seen["n"] == 3 and "No speech" in seen["prompt"]
    assert ffmpeg.probe(runner.project_dir(done["id"]) / "final.mp4").has_audio  # the TTS track, not the clip's tone


@needs_ffmpeg
def test_paid_clip_is_kept_when_a_later_step_fails_and_retry_does_not_pay_twice(client, db, keys, small, calls, monkeypatch, tmp_path):
    src = av_video(tmp_path / "veo.mp4", 8.0)
    paid = []

    def fake_clip(prompt, dest, **kw):
        paid.append(dest.name)
        shutil.copy(src, dest)

    real_norm = ffmpeg.normalize_clip
    boom = {"on": True}

    def flaky_norm(*a, **kw):
        if boom["on"] and len(paid) == 2:
            raise ffmpeg.FFmpegError("disk full")
        return real_norm(*a, **kw)

    monkeypatch.setattr(kitclips, "generate_clip", fake_clip)
    monkeypatch.setattr(ffmpeg, "normalize_clip", flaky_norm)
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: sketch_plan(3))
    r = create(client, "shorts", {"premise": "p", "scenes": 3, "motion_quality": "ai", "dialogue": "tts"}, recipe="film", auto=True)
    failed = get(client, r.json()["id"])
    assert failed["status"] == "failed" and "disk full" in failed["error"]
    assert paid == ["raw_00.mp4", "raw_01.mp4"]
    boom["on"] = False
    n_images = len(calls.images)
    client.post(f"/api/studio/projects/{failed['id']}/retry")
    done = get(client, failed["id"])
    assert done["status"] == "succeeded", done["error"]
    assert paid == ["raw_00.mp4", "raw_01.mp4", "raw_02.mp4"]  # shot 1's paid clip reused, shot 0 not regenerated
    assert len(calls.images) == n_images  # stills were kept too


def test_script_retries_once_then_fails_clearly(client, db, keys, small, calls, monkeypatch):
    answers = [{"shots": []}, sketch_plan(3)]
    prompts = []
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: prompts.append(p) or answers.pop(0))
    body = get(client, create(client, "toons", {"premise": "p", "scenes": 3}, recipe="sketch").json()["id"])
    assert body["status"] == "awaiting_approval" and "rejected because it has no 'shots' list" in prompts[1]

    monkeypatch.setattr(gemini_client, "generate_json", lambda p: {"shots": []})
    failed = get(client, create(client, "toons", {"premise": "p", "scenes": 3}, recipe="sketch").json()["id"])
    assert failed["status"] == "failed" and "unusable" in failed["error"]

    def broken(p):
        raise json.JSONDecodeError("bad", "x", 0)

    monkeypatch.setattr(gemini_client, "generate_json", broken)
    assert "unusable" in get(client, create(client, "toons", {"premise": "p", "scenes": 3}, recipe="sketch").json()["id"])["error"]


# --- validation / keys ---


def test_missing_keys_503_for_all_recipes(client):
    for engine, recipe, params in (("toons", "sketch", {"premise": "x"}), ("toons", "parody-song", {"concept": "x", "target": "y"}), ("shorts", "film", {"premise": "x"}), ("explainer3d", None, {"premise": "x"})):
        r = create(client, engine, params, recipe=recipe)
        assert r.status_code == 503 and "GEMINI_API_KEY" in r.json()["detail"], (engine, r.text)
    r = create(client, "shorts", {}, recipe="dance", files=[("photo", ("a.png", b"x", "image/png")), ("trend_video", ("a.mp4", b"x", "video/mp4"))])
    assert r.status_code == 503 and "MUAPI_API_KEY" in r.json()["detail"] and "CLOUDINARY_URL" in r.json()["detail"]


def test_ai_motion_key_and_aspect_rules(client, monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "k")
    r = create(client, "explainer3d", {"premise": "x", "motion_quality": "ai", "clip_provider": "muapi"})
    assert r.status_code == 503 and "MUAPI_API_KEY" in r.json()["detail"]
    r = create(client, "explainer3d", {"premise": "x", "motion_quality": "ai", "aspect": "1:1"})
    assert r.status_code == 400 and "Veo does not make 1:1" in r.json()["detail"]
    assert create(client, "explainer3d", {"premise": "x", "shots": 5}).status_code == 400  # 10..20
    assert create(client, "toons", {"premise": "x", "scenes": 9}, recipe="sketch").status_code == 400  # 3..8


def test_engines_registered_implemented_and_paid(client):
    engines = {e["id"]: e for e in client.get("/api/studio/engines").json()}
    for eid in ("toons", "shorts", "explainer3d"):
        assert not engines[eid]["description"].endswith("(coming soon)")
        assert all(r["paid"] for r in engines[eid]["recipes"])
    names = {f["name"] for f in engines["shorts"]["recipes"][1]["fields"]}
    assert {"premise", "characters", "character_images", "visual_style", "scenes", "clip_provider", "dialogue", "parody_label", "motion_quality"} <= names
    film = {f["name"]: f for f in engines["shorts"]["recipes"][1]["fields"]}
    assert film["parody_label"]["default"] is True and film["motion_quality"]["default"] == "ai"


# --- shorts/dance ---


@pytest.fixture
def dance_env(monkeypatch, tmp_path, keys, small):
    """Cloudinary + Muapi mocked; the 'generated' dance is a short video with audio."""
    result = av_video(tmp_path / "result.mp4", 3.0)
    env = {"uploads": [], "bodies": [], "polls": 0, "result": result}
    monkeypatch.setattr(media_hosting, "upload_image", lambda data, public_id=None: env["uploads"].append(("image", public_id, len(data))) or "https://res.test/photo.jpg")
    monkeypatch.setattr(media_hosting, "upload_video", lambda path, public_id=None: env["uploads"].append(("video", public_id, path)) or "https://res.test/trend.mp4")

    def fake_request(client, method, url, *, label, polling=False, **kw):
        env["bodies"].append((method, url, kw.get("json"), client.headers.get("x-api-key")))
        return {"request_id": "req-1", "status": "processing"}

    def fake_poll(self, job):
        env["polls"] += 1
        env["job"] = job
        return PollResult("running") if env["polls"] < 3 else PollResult("succeeded", output="https://cdn.test/out.mp4")

    monkeypatch.setattr(http_common, "request", fake_request)
    monkeypatch.setattr(MuapiProvider, "poll", fake_poll)
    monkeypatch.setattr(MuapiProvider, "download", lambda self, output, dest: shutil.copy(env["result"], dest))
    monkeypatch.setattr(shorts, "POLL_INTERVAL_S", 0)
    return env


def dance_files(tmp_path, photo_size=(400, 600), seconds=3.0):
    photo = tmp_path / "p.png"
    Image.new("RGB", photo_size, (10, 200, 10)).save(photo)
    return [("photo", ("me.png", photo.read_bytes(), "image/png")), ("trend_video", ("trend.mp4", av_video(tmp_path / "t.mp4", seconds, "360x640").read_bytes(), "video/mp4"))]


@needs_ffmpeg
def test_dance_flow_upload_submit_poll_download_strips_audio(client, db, dance_env, tmp_path):
    r = create(client, "shorts", {}, recipe="dance", files=dance_files(tmp_path))
    assert r.status_code == 200, r.text
    body = get(client, r.json()["id"])
    assert body["status"] == "awaiting_approval", body["error"]  # paid: plan first, nothing uploaded or submitted yet
    assert not dance_env["uploads"] and not dance_env["bodies"]
    assert "trending sound inside Instagram" in body["plan"]["notes"] and body["estimated_cost_usd"] is None
    assert [p["name"] for p in body["previews"]] == ["photo"]
    client.post(f"/api/studio/projects/{body['id']}/approve")
    done = get(client, body["id"])
    assert done["status"] == "succeeded", done["error"]
    assert [u[:2] for u in dance_env["uploads"][:2]] == [("image", f"studio-dance-{body['id']}-photo"), ("video", f"studio-dance-{body['id']}-trend")]
    method, url, payload, api_key = dance_env["bodies"][0]
    assert (method, url, api_key) == ("POST", f"{catalog.MUAPI_BASE_URL}/api/v1/kling-v2.6-std-motion-control", "muapi-test-key")
    assert payload["image_url"] == "https://res.test/photo.jpg" and payload["video_url"] == "https://res.test/trend.mp4"
    assert payload["character_orientation"] == "image" and payload["prompt"] and "keep_original_sound" not in payload  # v2.6 has no sound option
    assert dance_env["polls"] == 3 and dance_env["job"].job_id == "req-1" and dance_env["job"].status_url.endswith("/predictions/req-1/result")
    probe = ffmpeg.probe(runner.project_dir(done["id"]) / "final.mp4")
    assert (probe.width, probe.height) == SMALL and not probe.has_audio  # audio stripped by default


@needs_ffmpeg
def test_dance_v3_keep_audio_and_orientation_auto(client, db, dance_env, tmp_path):
    files = dance_files(tmp_path, seconds=12.0)  # > 10 s: follow the video's orientation
    r = create(client, "shorts", {"model": "kling-v3.0-std-motion-control", "keep_audio": True}, recipe="dance", files=files, auto=True)
    done = get(client, r.json()["id"])
    assert done["status"] == "succeeded", done["error"]
    payload = dance_env["bodies"][0][2]
    assert dance_env["bodies"][0][1].endswith("/kling-v3.0-std-motion-control")
    assert payload["character_orientation"] == "video" and payload["keep_original_sound"] is True
    assert ffmpeg.probe(runner.project_dir(done["id"]) / "final.mp4").has_audio


@needs_ffmpeg
def test_dance_rejects_small_photo_and_overlong_video(client, db, dance_env, tmp_path):
    small_photo = get(client, create(client, "shorts", {}, recipe="dance", files=dance_files(tmp_path, photo_size=(200, 200))).json()["id"])
    assert small_photo["status"] == "failed" and "too small" in small_photo["error"]
    explicit = get(client, create(client, "shorts", {"character_orientation": "image"}, recipe="dance", files=dance_files(tmp_path, seconds=12.0)).json()["id"])
    assert explicit["status"] == "failed" and "up to 10 s" in explicit["error"]
    assert not dance_env["uploads"] and not dance_env["bodies"]


@needs_ffmpeg
def test_dance_retry_resumes_the_same_muapi_job(client, db, dance_env, tmp_path, monkeypatch):
    monkeypatch.setattr(shorts, "JOB_TIMEOUT_S", -1)  # first attempt gives up at once, after submitting
    r = create(client, "shorts", {}, recipe="dance", files=dance_files(tmp_path), auto=True)
    failed = get(client, r.json()["id"])
    assert failed["status"] == "failed" and "Retry to keep waiting" in failed["error"]
    assert len(dance_env["bodies"]) == 1
    monkeypatch.setattr(shorts, "JOB_TIMEOUT_S", 900)
    client.post(f"/api/studio/projects/{failed['id']}/retry")
    done = get(client, failed["id"])
    assert done["status"] == "succeeded", done["error"]
    assert len(dance_env["bodies"]) == 1 and len([u for u in dance_env["uploads"] if u[1].startswith("studio-dance")]) == 2  # no second submit, no second input upload (the third upload is the runner publishing final.mp4)


@needs_ffmpeg
def test_dance_failed_muapi_job_reports_error(client, db, dance_env, tmp_path, monkeypatch):
    monkeypatch.setattr(MuapiProvider, "poll", lambda self, job: PollResult("failed", error="Muapi could not generate this video: bad pose"))
    failed = get(client, create(client, "shorts", {}, recipe="dance", files=dance_files(tmp_path), auto=True).json()["id"])
    assert failed["status"] == "failed" and "bad pose" in failed["error"]


def test_motion_body_fields():
    assert shorts.build_motion_body("kling-v2.6-std-motion-control", "i", "v", "image", True) == {
        "prompt": shorts.DANCE_PROMPT, "image_url": "i", "video_url": "v", "character_orientation": "image"}
    assert shorts.build_motion_body("kling-v3.0-std-motion-control", "i", "v", "video", False)["keep_original_sound"] is False

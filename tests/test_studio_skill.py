"""The V3 `skill` recipes end to end, with Gemini / image generation / Veo / edge-tts mocked and real tiny ffmpeg renders."""

import io
import json
import shutil

import pytest
from PIL import Image

from app.config import settings
from app.services import gemini_client
from app.services.studio import runner
from app.services.studio.engines import _skill_common as common
from app.services.studio.engines import _skill_demo, _skill_podcast, _skill_stylise
from app.services.studio.engines import _skill_story as story
from app.services.studio.kit import clips, ffmpeg, images, tts
from studio_helpers import fake_edge_fetch, make_test_video, make_wav_bytes, needs_ffmpeg

pytestmark = needs_ffmpeg


@pytest.fixture(autouse=True)
def tiny(monkeypatch):
    monkeypatch.setattr(settings, "gemini_api_key", "gemini-test-key")
    monkeypatch.setattr(tts, "_edge_fetch", fake_edge_fetch)
    for aspect, size in (("9:16", (270, 480)), ("16:9", (480, 270)), ("1:1", (320, 320))):
        monkeypatch.setitem(ffmpeg.TARGETS, aspect, size)


def create(client, recipe, params, files=None, auto=True):
    data = {"engine": "skill", "recipe": recipe, "params": json.dumps(params)}
    if auto:
        data["auto_approve"] = "true"
    return client.post("/api/studio/projects", data=data, files=files)


def run(client, recipe, params, files=None, auto=True):
    created = create(client, recipe, params, files, auto)
    assert created.status_code == 200, created.text
    return client.get(f"/api/studio/projects/{created.json()['id']}").json()


def png_bytes(size=(400, 300), color=(40, 90, 200)):
    img = Image.new("RGB", size, color)
    img.paste((240, 240, 240), (size[0] // 8, size[1] // 8, size[0] // 2, size[1] // 3))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def final_probe(body):
    return ffmpeg.probe(runner.project_dir(body["id"]) / "final.mp4")


# --- listing / validation ---


def test_skill_engine_is_live_with_all_five_recipes(client):
    skill = next(e for e in client.get("/api/studio/engines").json() if e["id"] == "skill")
    assert "coming soon" not in skill["description"]
    assert [r["id"] for r in skill["recipes"]] == ["singing-story", "podcast", "ps1-lowpoly", "spiderverse", "product-demo"]
    assert all(r["fields"] and "coming soon" not in r["description"] for r in skill["recipes"])
    paid = {r["id"]: r["paid"] for r in skill["recipes"]}
    assert paid == {"singing-story": True, "podcast": False, "ps1-lowpoly": True, "spiderverse": True, "product-demo": False}


def test_upload_source_requires_footage(client):
    r = create(client, "ps1-lowpoly", {"idea": "a corridor", "source": "upload"})
    assert r.status_code == 400 and "upload a video or some images" in r.json()["detail"]


def test_product_demo_rejects_more_than_eight_screenshots(client):
    files = [("screenshots", (f"s{i}.png", png_bytes(), "image/png")) for i in range(9)]
    r = create(client, "product-demo", {"product": "App", "features": "Fast"}, files)
    assert r.status_code == 400 and "at most 8" in r.json()["detail"]


# --- podcast ---

DIALOGUE = {
    "title": "Why Madrid win",
    "lines": [
        {"speaker": "Alex", "text": "Welcome back to the show everyone."},
        {"speaker": "Sam", "text": "Today we talk about Madrid."},
        {"speaker": "Alex", "text": "They keep on winning trophies."},
        {"speaker": "Sam", "text": "Fifteen European Cups and counting."},
        {"speaker": "Alex", "text": "Thanks for listening, see you soon."},
    ],
}


def test_podcast_end_to_end_with_real_envelope(client, monkeypatch):
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: DIALOGUE)
    monkeypatch.setattr(gemini_client, "generate_grounded", lambda p: pytest.fail("research is off"))
    body = run(client, "podcast", {"topic": "Madrid", "duration": 30})
    assert body["status"] == "succeeded", body["error"]
    probe = final_probe(body)
    assert (probe.width, probe.height) == (270, 480) and probe.has_audio
    folder = runner.project_dir(body["id"])
    env = json.loads((folder / "envelope.json").read_text())
    assert max(env) > 0.5 and min(env) < 0.2  # follows the speech, not a constant
    assert probe.duration == pytest.approx(len(env) / ffmpeg.FPS, abs=0.6)
    assert (folder / "captions.ass").is_file() or list(folder.glob("caps/*"))
    assert [s["text"].split(":")[0] for s in body["plan"]["scenes"]] == ["Alex", "Sam", "Alex", "Sam", "Alex"]
    assert body["plan"]["script"].startswith("Alex: Welcome back")
    assert not (folder / "podcast.mp4").exists()


def test_podcast_with_research_grounds_the_dialogue(client, monkeypatch):
    prompts = []
    monkeypatch.setattr(gemini_client, "generate_grounded", lambda p: ("- Madrid have 15 European Cups", [{"title": "UEFA", "url": "https://uefa.com"}]))
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: prompts.append(p) or DIALOGUE)
    body = run(client, "podcast", {"topic": "Madrid", "duration": 30, "research": True, "host_a": "Alex", "host_b": "Sam", "subtitles": False, "aspect": "16:9"})
    assert body["status"] == "succeeded", body["error"]
    assert "Madrid have 15 European Cups" in prompts[0] and "UEFA" in body["plan"]["notes"]
    probe = final_probe(body)
    assert (probe.width, probe.height) == (480, 270)
    assert [p["name"] for p in body["previews"]] == ["narration"]


def test_podcast_fails_cleanly_on_empty_dialogue(client, monkeypatch):
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: {"lines": [{"speaker": "Alex", "text": "Hi there friend."}]})
    body = run(client, "podcast", {"topic": "x", "duration": 30})
    assert body["status"] == "failed" and "too little dialogue" in body["error"] and body["stage"] == "Writing the dialogue"


def test_podcast_speaker_mapping_and_voices(client, monkeypatch):
    names = ["Alex", "Sam"]
    assert _skill_podcast.host_names({"host_a": "Zed", "host_b": "zed"}) == ["Zed", "zed 2"]
    assert _skill_podcast.host_voices({"tts_provider": "edge"}) == list(_skill_podcast.EDGE_HOSTS)
    assert _skill_podcast.host_voices({"tts_provider": "gemini", "voice_a": "Charon", "voice_b": "en-US-AriaNeural"}) == ["Charon", "Kore"]
    same = _skill_podcast.host_voices({"tts_provider": "edge", "voice_a": "en-US-AriaNeural", "voice_b": "en-US-AriaNeural"})
    assert same[0] != same[1]
    assert _skill_podcast.initials("Ana María") == "AM" and _skill_podcast.initials("12") == "?" and names


def test_audio_envelope_tracks_loudness(tmp_path):
    wav = tmp_path / "t.wav"
    wav.write_bytes(make_wav_bytes(1.0))
    env = common.audio_envelope(wav, 30)
    assert 28 <= len(env) <= 32 and all(0.0 <= v <= 1.0 for v in env) and sum(env) / len(env) > 0.5


@pytest.mark.parametrize("size", [(270, 480), (480, 270), (320, 320)])
def test_podcast_scene_renders_in_every_aspect(tmp_path, size):
    env = [0.0] * 10 + [0.9] * 20 + [0.0] * 30
    out = _skill_podcast.render_podcast(
        tmp_path / "p.mp4", title_text="A very long podcast title that has to wrap neatly", names=["Alexandra", "Sam"],
        segments=[[0, 0.3, 1.0], [1, 1.1, 1.9]], env=env, width=size[0], height=size[1], fps=30, duration=2.0, timeout=60,
    )
    probe = ffmpeg.probe(out)
    assert (probe.width, probe.height) == size and probe.duration == pytest.approx(2.0, abs=0.2)


# --- singing story ---

LINES = [{"text": f"Line number {i} of the little brave story", "visual": f"scene {i} with Pip the fox"} for i in range(8)]
STORY = {"title": "Pip the fox", "style_prompt": "Pip is a small orange fox with a blue scarf.", "lines": LINES}


@pytest.fixture
def fake_images(monkeypatch):
    calls = []

    def fake(prompt, dest, *, aspect="9:16", references=None):
        calls.append({"prompt": prompt, "aspect": aspect, "refs": list(references or [])})
        dest.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (240, 424), (200, 120, 40 + 20 * len(calls))).save(dest)
        return dest

    monkeypatch.setattr(images, "generate_image", fake)
    return calls


def test_singing_story_with_spoken_narration_pauses_for_approval(client, fake_images, monkeypatch):
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: STORY)
    created = create(client, "singing-story", {"idea": "a brave fox", "lines": 8}, auto=False).json()
    body = client.get(f"/api/studio/projects/{created['id']}").json()
    assert body["status"] == "awaiting_approval" and fake_images == []
    assert body["estimated_cost_usd"] == pytest.approx(8 * common.IMAGE_COST_USD)
    assert len(body["plan"]["scenes"]) == 8 and all(s["duration_s"] >= 2 for s in body["plan"]["scenes"])
    assert client.post(f"/api/studio/projects/{created['id']}/approve").status_code == 200
    done = client.get(f"/api/studio/projects/{created['id']}").json()
    assert done["status"] == "succeeded", done["error"]
    assert len(fake_images) == 8 and fake_images[0]["refs"] == [] and len(fake_images[1]["refs"]) == 1  # style anchored on picture 1
    assert "small orange fox" in fake_images[3]["prompt"] and "scene 3" in fake_images[3]["prompt"]
    probe = final_probe(done)
    assert probe.has_audio and (probe.width, probe.height) == (270, 480)
    slots = done["plan"]["scenes"]
    assert probe.duration == pytest.approx(sum(s["duration_s"] for s in slots), abs=1.0)
    pulse = 2 * story.BEAT_S
    assert all(round(s["duration_s"] / pulse, 1) == pytest.approx(round(s["duration_s"] / pulse), abs=0.2) for s in slots)  # on the beat grid


def test_singing_story_spreads_lines_over_an_uploaded_song(client, fake_images, monkeypatch):
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: STORY)
    song = make_wav_bytes(8.0)
    body = run(client, "singing-story", {"idea": "a brave fox", "lines": 8, "style": "anime", "aspect": "16:9", "caption_style": "karaoke"}, files={"song": ("song.wav", song, "audio/wav")})
    assert body["status"] == "succeeded", body["error"]
    assert not any(p["name"] == "narration" for p in body["previews"])
    probe = final_probe(body)
    assert probe.has_audio and probe.duration == pytest.approx(8.0, abs=0.4) and (probe.width, probe.height) == (480, 270)
    assert fake_images[0]["aspect"] == "16:9"
    assert sum(s["duration_s"] for s in body["plan"]["scenes"]) == pytest.approx(8.0, abs=0.6)


def test_singing_story_caps_picture_count_and_survives_a_failed_picture():
    assert story.image_count(16) == story.MAX_IMAGES and story.image_count(8) == 8
    used = [story.image_index(i, 16) for i in range(16)]
    assert used == sorted(used) and set(used) == set(range(12))


def test_singing_story_reuses_previous_picture_when_one_fails(client, monkeypatch, fake_images):
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: STORY)
    real = images.generate_image
    state = {"n": 0}

    def flaky(prompt, dest, **kw):
        state["n"] += 1
        if "scene 4" in prompt:
            raise images.ImageGenError("blocked")
        return real(prompt, dest, **kw)

    monkeypatch.setattr(images, "generate_image", flaky)
    body = run(client, "singing-story", {"idea": "fox", "lines": 8})
    assert body["status"] == "succeeded", body["error"]


def test_singing_story_first_picture_failure_fails_the_stage(client, monkeypatch):
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: STORY)
    monkeypatch.setattr(images, "generate_image", lambda *a, **k: (_ for _ in ()).throw(images.ImageGenError("Gemini returned no image")))
    body = run(client, "singing-story", {"idea": "fox", "lines": 8})
    assert body["status"] == "failed" and "no image" in body["error"] and body["stage"] == "Illustrating the story"


# --- PS1 / Spider-Verse ---


@pytest.fixture
def source_video(tmp_path):
    return make_test_video(tmp_path / "src.mp4", seconds=2.0, size="160x284")


def test_ps1_from_uploaded_video_with_narration_and_captions(client, source_video, monkeypatch):
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: {"prompt": "x", "caption": "HELLO", "narration": "A lonely corridor hums in the dark."})
    body = run(
        client, "ps1-lowpoly", {"idea": "a haunted corridor", "source": "upload", "duration": 4, "narration": True},
        files={"video": ("clip.mp4", source_video.read_bytes(), "video/mp4")},
    )
    assert body["status"] == "succeeded", body["error"]
    probe = final_probe(body)
    assert probe.has_audio and (probe.width, probe.height) == (270, 480) and probe.duration >= 3.9
    assert body["plan"]["script"].startswith("A lonely corridor")
    assert not (runner.project_dir(body["id"]) / "source.mp4").exists()


def test_ps1_filter_has_jitter_and_posterise():
    vf = _skill_stylise.style_filter("ps1-lowpoly", 1080)
    assert "crop=" in vf and "sin(n" in vf and "fps=15" in vf and "scale=iw/4" in vf
    assert "crop=" not in _skill_stylise.style_filter("spiderverse", 1080) and "fps=12" in _skill_stylise.style_filter("spiderverse", 1080)


def test_spiderverse_from_images_adds_halftone_and_title_box(client):
    files = [("images", (f"i{i}.png", png_bytes((300, 400), (30 * i + 20, 80, 160)), "image/png")) for i in range(3)]
    body = run(client, "spiderverse", {"idea": "a swinging hero", "source": "upload", "duration": 4}, files=files)
    assert body["status"] == "succeeded", body["error"]
    folder = runner.project_dir(body["id"])
    assert (folder / "overlays" / "halftone.png").is_file() and (folder / "overlays" / "title.png").is_file()
    probe = final_probe(body)
    assert not probe.has_audio and probe.duration == pytest.approx(4.0, abs=0.3)


def test_spiderverse_narration_becomes_comic_boxes(client, source_video, monkeypatch):
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: {"prompt": "x", "caption": "MEANWHILE", "narration": "The hero leaps. The city gasps."})
    body = run(
        client, "spiderverse", {"idea": "a hero", "source": "upload", "duration": 4, "narration": True},
        files={"video": ("clip.mp4", source_video.read_bytes(), "video/mp4")},
    )
    assert body["status"] == "succeeded", body["error"]
    folder = runner.project_dir(body["id"])
    assert (folder / "overlays" / "box_0.png").is_file() and (folder / "overlays" / "box_1.png").is_file()
    assert final_probe(body).has_audio


def test_generate_source_is_gated_by_approval_and_priced_from_the_catalog(client, source_video, monkeypatch):
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: {"prompt": "A neon alley chase", "caption": "NIGHT FALLS"})
    calls = []

    def fake_clip(prompt, dest, **kw):
        calls.append({"prompt": prompt, **kw})
        shutil.copy(source_video, dest)
        return dest

    monkeypatch.setattr(clips, "generate_clip", fake_clip)
    created = create(client, "spiderverse", {"idea": "alley chase", "duration": 5, "aspect": "9:16"}, auto=False).json()
    body = client.get(f"/api/studio/projects/{created['id']}").json()
    assert body["status"] == "awaiting_approval" and calls == []
    assert body["estimated_cost_usd"] == pytest.approx(0.30)  # veo lite, 720p, 6 s clip for a 5 s video
    client.post(f"/api/studio/projects/{created['id']}/approve")
    done = client.get(f"/api/studio/projects/{created['id']}").json()
    assert done["status"] == "succeeded", done["error"]
    assert len(calls) == 1 and calls[0]["duration_seconds"] == 6 and calls[0]["aspect_ratio"] == "9:16" and calls[0]["resolution"] == "720p"
    assert "neon alley" in calls[0]["prompt"].lower() and "halftone" in calls[0]["prompt"].lower()


def test_generate_is_not_paid_twice_when_a_later_step_is_retried(client, source_video, monkeypatch):
    monkeypatch.setattr(gemini_client, "generate_json", lambda p: {"prompt": "A neon alley chase", "caption": "NIGHT"})
    calls = []
    monkeypatch.setattr(clips, "generate_clip", lambda prompt, dest, **kw: (calls.append(1), shutil.copy(source_video, dest))[1])
    fail = {"on": True}
    real = _skill_stylise.ffmpeg.stylise_filter

    def boom(name):
        if fail["on"]:
            raise RuntimeError("boom")
        return real(name)

    monkeypatch.setattr(_skill_stylise.ffmpeg, "stylise_filter", boom)
    body = run(client, "ps1-lowpoly", {"idea": "a corridor", "duration": 4})
    assert body["status"] == "failed" and len(calls) == 1
    fail["on"] = False
    client.post(f"/api/studio/projects/{body['id']}/retry")
    done = client.get(f"/api/studio/projects/{body['id']}").json()
    assert done["status"] == "succeeded", done["error"]
    assert len(calls) == 1


def test_comic_overlays_are_valid_pngs(tmp_path):
    halftone = _skill_stylise.halftone_overlay(tmp_path / "h.png", 270, 480)
    assert Image.open(halftone).size == (270, 480) and Image.open(halftone).getchannel("A").getextrema()[1] > 0
    path, x, y = _skill_stylise.comic_box(tmp_path / "b.png", "Meanwhile, somewhere in the pixel city, something stirs", (270, 480))
    box = Image.open(path)
    assert 0 < box.width < 270 and x + box.width <= 270 and y < 100


# --- product demo ---


def test_product_demo_end_to_end(client, monkeypatch):
    seen = {}

    def vision(prompt, imgs):
        seen.update(prompt=prompt, n=len(imgs))
        return {
            "tagline": "Ship faster",
            "beats": [
                {"narration": "Meet Acme, the fastest way to plan your week.", "label": "Weekly planner", "box": [0.1, 0.1, 0.4, 0.3]},
                {"narration": "Drag tasks around and they sync everywhere.", "label": "Drag and drop", "box": [40, 50, 30, 20]},  # percent
                {"narration": "Then share it with one click.", "label": "Share", "box": "nonsense"},
            ],
        }

    monkeypatch.setattr(common, "gemini_vision_json", vision)
    files = [("screenshots", (f"s{i}.png", png_bytes((400 + 40 * i, 300), (20 * i, 90, 200)), "image/png")) for i in range(3)]
    body = run(client, "product-demo", {"product": "Acme Planner", "url": "https://acme.test", "features": "- Planner\n- Sync\n- Share"}, files=files)
    assert body["status"] == "succeeded", body["error"]
    assert seen["n"] == 3 and "Acme Planner" in seen["prompt"] and "exactly 3 beats" in seen["prompt"]
    assert len(body["plan"]["scenes"]) == 3 and body["plan"]["script"].startswith("Meet Acme")
    probe = final_probe(body)
    assert probe.has_audio and (probe.width, probe.height) == (270, 480)
    folder = runner.project_dir(body["id"])
    assert (folder / "frames" / "end.png").is_file() and not list(folder.glob("clips/*.mp4"))
    assert probe.duration == pytest.approx(sum(s["duration_s"] for s in body["plan"]["scenes"]) + _skill_demo.END_CARD_S, abs=0.8)


def test_demo_boxes_are_clamped_into_the_screenshot():
    assert _skill_demo._clamp_box([0.9, 0.9, 0.5, 0.5], 0) == pytest.approx((0.5, 0.5, 0.5, 0.5))
    assert _skill_demo._clamp_box([10, 20, 30, 40], 0) == pytest.approx((0.1, 0.2, 0.3, 0.4))
    assert _skill_demo._clamp_box(None, 1) == _skill_demo.DEFAULT_BOXES[1]


def test_callout_frame_and_end_card_fill_the_canvas(tmp_path):
    shot = tmp_path / "s.png"
    shot.write_bytes(png_bytes())
    for w, h in ((270, 480), (480, 270)):
        frame = _skill_demo.callout_frame(shot, tmp_path / "f.png", w, h, box=(0.1, 0.1, 0.4, 0.3), label="Very long callout label text", product="Acme", step="1/3")
        card = _skill_demo.end_card(tmp_path / "e.png", w, h, product="A Really Long Product Name For Wrapping", tagline="Ship faster every week", url="https://acme.test/")
        assert Image.open(frame).size == (w, h) == Image.open(card).size


# --- motion engine: 9:16 vertical distribution ---


def test_motion_prompt_plans_the_whole_tall_canvas():
    from app.services.studio.motion import prompt

    tall = prompt.layout_guidance(1080, 1920, captions=True)
    assert "EVERY row" in tall and "1344" in tall and "Burned-in captions" in tall  # content ends above the caption strip
    assert "1574" in prompt.layout_guidance(1080, 1920, captions=False)  # without captions it may use the full safe height
    assert "wide" in prompt.layout_guidance(1920, 1080, True) and "square" in prompt.layout_guidance(1080, 1080, False)
    full = prompt.scene_prompt(brief="b", data="", aspect="9:16", width=1080, height=1920, duration=10, palette="auto", cues=[], assets=[])
    assert "Layout (tall 1080x1920" in full and "Use the whole canvas" in full


def test_motion_engine_tells_the_model_where_captions_will_be(client, monkeypatch):
    from app.services.studio.motion import examples

    monkeypatch.setattr(gemini_client, "generate_json", lambda p: {"title": "T", "narration": "Madrid have won fifteen titles. Nobody else is close."})
    prompts = []
    monkeypatch.setattr(gemini_client, "generate_text", lambda p: prompts.append(p) or f"```python\n{examples.STAT_REVEAL}\n```")
    data = {"engine": "motion", "params": json.dumps({"brief": "15 titles", "duration": 5, "narration": True, "subtitles": True})}
    assert client.post("/api/studio/projects", data=data).status_code == 200
    data["params"] = json.dumps({"brief": "15 titles", "duration": 5})
    assert client.post("/api/studio/projects", data=data).status_code == 200
    assert "Burned-in captions" in prompts[0] and "Burned-in captions" not in prompts[1]

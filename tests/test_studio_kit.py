import wave
from types import SimpleNamespace

import httpx
import pytest

from app.config import settings
from app.services.studio.kit import captions, clips, ffmpeg, images, stock, tts
from studio_helpers import fake_edge_fetch, make_test_video, make_wav_bytes, needs_ffmpeg


# --- tts ---


def test_split_sentences_merges_fragments():
    assert tts.split_sentences("Hello there friend. Ok. This is the end!") == ["Hello there friend.", "Ok. This is the end!"]
    assert tts.split_sentences("  One sentence only  ") == ["One sentence only"]


def test_resolve_voice():
    assert tts.resolve_voice("edge", "", "en") == "en-US-AndrewNeural"
    assert tts.resolve_voice("edge", "en-GB-SoniaNeural") == "en-GB-SoniaNeural"
    assert tts.resolve_voice("edge", "Kore", "es") == "es-ES-AlvaroNeural"  # a Gemini voice name falls back
    assert tts.resolve_voice("gemini", "Puck") == "Puck" and tts.resolve_voice("gemini", "en-US-AndrewNeural") == "Kore"


@needs_ffmpeg
def test_edge_synthesis_timings_are_exact(tmp_path, monkeypatch):
    monkeypatch.setattr(tts, "_edge_fetch", fake_edge_fetch)
    result = tts.synthesize(["Hello big world.", "Second sentence here now."], tmp_path / "v.wav", provider="edge")
    s1, s2 = result.sentences
    assert s1["start"] == 0 and s1["end"] == pytest.approx(0.9, abs=0.01)
    assert s2["start"] == pytest.approx(s1["end"] + tts.GAP_S, abs=0.001)
    assert [w["text"] for w in s2["words"]] == ["Second", "sentence", "here", "now."]
    assert s2["words"][1]["start"] == pytest.approx(s2["start"] + 0.3, abs=0.001)
    with wave.open(str(result.path)) as w:
        assert w.getnframes() / w.getframerate() == pytest.approx(result.duration, abs=0.001)


@needs_ffmpeg
def test_per_sentence_voices_are_used(tmp_path, monkeypatch):
    used = []
    monkeypatch.setattr(tts, "_edge_fetch", lambda text, voice: (used.append(voice), fake_edge_fetch(text, voice))[1])
    tts.synthesize(["One two three.", "Four five six."], tmp_path / "v.wav", voice="en-US-GuyNeural", voices=[None, "en-GB-RyanNeural"])
    assert used == ["en-US-GuyNeural", "en-GB-RyanNeural"]


@needs_ffmpeg
@pytest.mark.parametrize("mime_wav", [True, False])
def test_gemini_audio_forms_are_normalised(tmp_path, monkeypatch, mime_wav):
    wav = make_wav_bytes(0.5)
    raw = tts._to_pcm(wav)
    payload = wav if mime_wav else raw
    seen = {}

    class Models:
        def generate_content(self, model, contents, config):
            seen.update(model=model, contents=contents, voice=config.speech_config.voice_config.prebuilt_voice_config.voice_name, mods=config.response_modalities)
            part = SimpleNamespace(inline_data=SimpleNamespace(data=payload, mime_type="audio/wav"))
            return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])

    from app.services import gemini_client

    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr(gemini_client, "_client", SimpleNamespace(models=Models()))
    result = tts.synthesize(["Short test sentence."], tmp_path / "g.wav", provider="gemini", voice="Puck")
    assert seen["voice"] == "Puck" and seen["mods"] == ["AUDIO"] and seen["model"] == settings.gemini_tts_model
    assert result.sentences[0]["end"] == pytest.approx(0.5, abs=0.02)
    assert [w["text"] for w in result.sentences[0]["words"]] == ["Short", "test", "sentence."]


# --- captions ---

SENTENCES = [
    {"text": "Real Madrid keeps winning.", "start": 0.0, "end": 2.0, "words": [
        {"text": "Real", "start": 0.0, "end": 0.4}, {"text": "Madrid", "start": 0.5, "end": 1.0},
        {"text": "keeps", "start": 1.1, "end": 1.5}, {"text": "winning.", "start": 1.6, "end": 2.0}]},
]


@pytest.mark.parametrize("style", captions.STYLES)
def test_ass_writer_styles(tmp_path, style):
    path = captions.write_ass(tmp_path / "c.ass", SENTENCES, style=style, size=(1080, 1920))
    text = path.read_text()
    assert "PlayResX: 1080" in text and "Style: Cap,Anton," in text
    events = [l for l in text.splitlines() if l.startswith("Dialogue:")]
    if style == "bold-pop":
        assert len(events) == 4  # one event per word state
        assert "REAL" in text and "\\fscx118" in text
    elif style == "karaoke":
        assert "\\kf" in text
    else:
        assert "Real Madrid" in text and "REAL" not in text


def test_caption_positions_set_alignment(tmp_path):
    for pos, align in (("bottom", "2"), ("center", "5"), ("top", "8")):
        text = captions.write_ass(tmp_path / "c.ass", SENTENCES, position=pos).read_text()
        style = next(l for l in text.splitlines() if l.startswith("Style:")).split(",")
        assert style[18] == align


def test_bundled_font_exists_with_license():
    assert captions.FONT_FILE.is_file() and (captions.FONTS_DIR / "OFL.txt").is_file()


# --- ffmpeg (real, tiny) ---


@needs_ffmpeg
def test_probe_normalize_and_concat(tmp_path):
    src = make_test_video(tmp_path / "src.mp4", seconds=2.0)
    assert ffmpeg.probe(src).duration == pytest.approx(2.0, abs=0.1)
    a = ffmpeg.normalize_clip(src, tmp_path / "a.mp4", 270, 480, 1.0, loop=False)
    b = ffmpeg.normalize_clip(src, tmp_path / "b.mp4", 270, 480, 1.5, start=0.2, loop=False)
    joined = ffmpeg.concat([a, b], tmp_path / "j.mp4")
    probe = ffmpeg.probe(joined)
    assert (probe.width, probe.height) == (270, 480) and probe.duration == pytest.approx(2.5, abs=0.2)


@needs_ffmpeg
def test_normalize_loops_short_source_and_contain_mode(tmp_path):
    src = make_test_video(tmp_path / "s.mp4", seconds=1.0, size="320x180")
    looped = ffmpeg.normalize_clip(src, tmp_path / "l.mp4", 180, 320, 2.5, loop=True)
    assert ffmpeg.probe(looped).duration == pytest.approx(2.5, abs=0.2)
    contained = ffmpeg.normalize_clip(src, tmp_path / "c.mp4", 180, 320, 1.0, mode="contain")
    assert (ffmpeg.probe(contained).width, ffmpeg.probe(contained).height) == (180, 320)


@needs_ffmpeg
def test_loop_to_duration_and_xfade(tmp_path):
    src = make_test_video(tmp_path / "s.mp4", seconds=1.0, size="160x284")
    assert ffmpeg.probe(ffmpeg.loop_to_duration(src, tmp_path / "l.mp4", 2.5)).duration == pytest.approx(2.5, abs=0.2)
    out = ffmpeg.xfade_chain([src, src], [1.0, 1.0], tmp_path / "x.mp4", td=0.3)
    assert ffmpeg.probe(out).duration == pytest.approx(1.7, abs=0.2)


@needs_ffmpeg
def test_mix_audio_and_finalize_with_ass(tmp_path):
    video = make_test_video(tmp_path / "v.mp4", seconds=3.0, size="270x480")
    voice, bgm = tmp_path / "voice.wav", tmp_path / "bgm.wav"
    voice.write_bytes(make_wav_bytes(1.5))
    bgm.write_bytes(make_wav_bytes(1.0))
    mix = ffmpeg.mix_audio(voice, bgm, tmp_path / "mix.wav", 3.0)
    assert ffmpeg.probe(mix).duration == pytest.approx(3.0, abs=0.1)
    ass = captions.write_ass(tmp_path / "c.ass", SENTENCES, size=(270, 480))
    out = ffmpeg.finalize(video, tmp_path / "final.mp4", audio=mix, ass=ass, fonts_dir=captions.FONTS_DIR, seconds=3.0)
    probe = ffmpeg.probe(out)
    assert probe.has_audio and probe.duration == pytest.approx(3.0, abs=0.2) and probe.width == 270


@needs_ffmpeg
def test_finalize_png_overlay_fallback_burns_captions(tmp_path):
    video = make_test_video(tmp_path / "v.mp4", seconds=2.0, size="270x480")
    overlays = captions.render_overlays(tmp_path / "caps", SENTENCES, size=(270, 480))
    assert overlays and all(p.is_file() for p, *_ in overlays)
    plain = ffmpeg.finalize(video, tmp_path / "plain.mp4", seconds=2.0)
    burned = ffmpeg.finalize(video, tmp_path / "burned.mp4", overlays=overlays, seconds=2.0)
    frames = {}
    for name, f in (("plain", plain), ("burned", burned)):
        ffmpeg.run(["-ss", "0.5", "-i", f, "-frames:v", "1", tmp_path / f"{name}.png"])
        frames[name] = (tmp_path / f"{name}.png").read_bytes()
    assert frames["plain"] != frames["burned"]


@needs_ffmpeg
def test_compose_final_uses_overlay_fallback_without_libass(tmp_path, monkeypatch):
    from app.services.studio import runner
    from app.services.studio.engines import _shared
    from app.models import VideoProject

    monkeypatch.setattr(ffmpeg, "subtitle_filter_name", lambda: None)
    video = make_test_video(tmp_path / "v.mp4", seconds=2.0, size="270x480")
    project = VideoProject(id=1, engine="motion", title="t", params="{}", plan="{}", assets="{}")
    ctx = runner.Ctx(project, 1e12, lambda f: None)
    ctx.dir = tmp_path
    out = _shared.compose_final(ctx, video, seconds=2.0, sentences=SENTENCES, captions_on=True)
    assert out.is_file() and (tmp_path / "caps").is_dir()


@needs_ffmpeg
def test_ken_burns_effects(tmp_path):
    from PIL import Image

    img = tmp_path / "i.png"
    Image.new("RGB", (400, 300), (200, 30, 30)).save(img)
    for effect in images.KEN_BURNS_EFFECTS:
        out = images.ken_burns(img, tmp_path / f"{effect}.mp4", 1.0, 180, 320, effect=effect)
        probe = ffmpeg.probe(out)
        assert (probe.width, probe.height) == (180, 320) and probe.duration == pytest.approx(1.0, abs=0.15)


def test_stylise_filters_known():
    assert "neighbor" in ffmpeg.stylise_filter("ps1")
    with pytest.raises(ValueError):
        ffmpeg.stylise_filter("nope")


@needs_ffmpeg
@pytest.mark.parametrize("name", ["ps1", "spiderverse"])
def test_stylise_filters_run(tmp_path, name):
    src = make_test_video(tmp_path / "s.mp4", seconds=1.0, size="160x284")
    out = tmp_path / f"{name}.mp4"
    ffmpeg.run(["-i", src, "-vf", ffmpeg.stylise_filter(name), *ffmpeg.VIDEO_ARGS, out])
    assert ffmpeg.probe(out).duration > 0.5


# --- images (Gemini mocked) ---


def test_generate_image_saves_png_with_references(tmp_path, monkeypatch):
    import io

    from PIL import Image

    from app.services import gemini_client

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "PNG")
    seen = {}

    class Models:
        def generate_content(self, model, contents, config):
            seen.update(model=model, n=len(contents), ar=config.image_config.aspect_ratio, mods=config.response_modalities)
            part = SimpleNamespace(inline_data=SimpleNamespace(data=buf.getvalue(), mime_type="image/png"))
            return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])

    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr(gemini_client, "_client", SimpleNamespace(models=Models()))
    ref = tmp_path / "ref.png"
    ref.write_bytes(buf.getvalue())
    out = images.generate_image("a cat", tmp_path / "o.png", aspect="9:16", references=[ref])
    assert Image.open(out).size == (8, 8)
    assert seen == {"model": "gemini-3.1-flash-image-preview", "n": 2, "ar": "9:16", "mods": ["IMAGE"]}


def test_generate_image_without_image_part_raises(tmp_path, monkeypatch):
    from app.services import gemini_client

    class Models:
        def generate_content(self, **kw):
            return SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(inline_data=None)]))])

    monkeypatch.setattr(settings, "gemini_api_key", "k")
    monkeypatch.setattr(gemini_client, "_client", SimpleNamespace(models=Models()))
    with pytest.raises(images.ImageGenError):
        images.generate_image("x", tmp_path / "o.png")


# --- stock ---


def _pexels_payload():
    def vid(i, dur, files):
        return {"id": i, "duration": dur, "url": f"https://pexels.com/v/{i}", "image": "t.jpg", "video_files": files}

    f = lambda w, h, link: {"width": w, "height": h, "link": link, "file_type": "video/mp4"}  # noqa: E731
    return {"videos": [
        vid(1, 12, [f(720, 1280, "sd1"), f(1080, 1920, "hd1"), f(2160, 3840, "uhd1")]),
        vid(2, 3, [f(1080, 1920, "short")]),  # too short for min_duration 5
        vid(3, 20, [f(540, 960, "tiny3"), f(720, 1280, "sd3")]),  # nothing covers the target: take the largest
        vid(1, 12, [f(1080, 1920, "dup")]),
    ]}


@pytest.fixture
def pexels(monkeypatch):
    monkeypatch.setattr(settings, "pexels_api_key", "pk")
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append((url, headers, params))
        return httpx.Response(200, json=_pexels_payload(), request=httpx.Request("GET", url))

    monkeypatch.setattr(stock.httpx, "get", fake_get)
    return calls


def test_pexels_search_rules_and_cache(pexels):
    found = stock.search("real madrid", "9:16", 5)
    assert [(c.id, c.url) for c in found] == [("1", "hd1"), ("3", "sd3"), ("1", "dup")]  # smallest >= 1080x1920; long enough
    assert pexels[0][1]["Authorization"] == "pk" and pexels[0][2]["orientation"] == "portrait"
    stock.search("Real Madrid", "9:16", 5)
    assert len(pexels) == 1  # second call served from the 24 h cache


def test_cache_expires_after_24h(pexels, monkeypatch):
    stock.search("goal", "16:9", 5)
    assert pexels[0][2]["orientation"] == "landscape"
    real = stock.time.time
    monkeypatch.setattr(stock.time, "time", lambda: real() + stock.CACHE_TTL_S + 5)
    stock.search("goal", "16:9", 5)
    assert len(pexels) == 2


def test_find_clips_interleaves_and_dedupes(pexels):
    clips_found = stock.find_clips(["a", "b"], "9:16", 5)
    assert [c.id for c in clips_found] == ["1", "3"]


def test_pexels_errors(monkeypatch):
    monkeypatch.setattr(settings, "pexels_api_key", "pk")
    monkeypatch.setattr(stock.httpx, "get", lambda *a, **k: httpx.Response(401, request=httpx.Request("GET", "x")))
    with pytest.raises(stock.StockError, match="rejected"):
        stock.search("x", "9:16", 5)
    monkeypatch.setattr(settings, "pexels_api_key", "")
    with pytest.raises(stock.StockError, match="PEXELS_API_KEY"):
        stock.search("x", "9:16", 5)


def test_pixabay_fallback(monkeypatch):
    monkeypatch.setattr(settings, "pixabay_api_key", "px")
    payload = {"hits": [
        {"id": 7, "duration": 9, "pageURL": "p", "videos": {"large": {"url": "L", "width": 1080, "height": 1920}, "tiny": {"url": "T", "width": 360, "height": 640}}},
        {"id": 8, "duration": 9, "pageURL": "p", "videos": {"large": {"url": "wide", "width": 1920, "height": 1080}}},  # wrong orientation
    ]}
    monkeypatch.setattr(stock.httpx, "get", lambda url, **k: httpx.Response(200, json=payload, request=httpx.Request("GET", url)))
    found = stock.search("goal", "9:16", 5)
    assert [(c.source, c.url) for c in found] == [("pixabay", "L")]


def test_download_streams_and_caps_size(tmp_path, monkeypatch):
    class Stream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self, n):
            for _ in range(4):
                yield b"x" * 1000

    monkeypatch.setattr(stock.httpx, "stream", lambda *a, **k: Stream())
    clip = stock.StockClip("1", "pexels", 5, 1, 1, "u")
    assert stock.download(clip, tmp_path / "a.mp4").stat().st_size == 4000
    with pytest.raises(stock.StockError, match="larger"):
        stock.download(clip, tmp_path / "b.mp4", max_bytes=2500)
    assert not (tmp_path / "b.mp4").exists() and not (tmp_path / "b.mp4.part").exists()


# --- clips ---


def test_generate_clip_polls_until_done(tmp_path, monkeypatch):
    from app.services.video_providers import JobHandle, PollResult

    states = iter([PollResult("running"), PollResult("succeeded", output="out")])
    seen = {}

    class Provider:
        def missing_keys(self):
            return []

        def submit(self, params):
            seen["image"] = params.image_path
            return JobHandle("j")

        def poll(self, job):
            return next(states)

        def download(self, output, dest):
            dest.write_bytes(b"mp4")

    monkeypatch.setattr(clips, "get_provider", lambda p: Provider())
    monkeypatch.setattr(clips.catalog, "validate", lambda *a: None)
    monkeypatch.setattr(clips, "POLL_INTERVAL_S", 0)
    out = clips.generate_clip("p", tmp_path / "c.mp4", provider="veo", model="m", aspect_ratio="9:16", duration_seconds=4, resolution="720p", image=tmp_path / "i.png")
    assert out.read_bytes() == b"mp4" and seen["image"].endswith("i.png")


def test_generate_clip_failure_and_missing_keys(tmp_path, monkeypatch):
    from app.services.video_providers import JobHandle, PollResult, ProviderError, ProviderNotConfigured

    class Provider:
        keys = []

        def missing_keys(self):
            return self.keys

        def submit(self, params):
            return JobHandle("j")

        def poll(self, job):
            return PollResult("failed", error="nope")

    provider = Provider()
    monkeypatch.setattr(clips, "get_provider", lambda p: provider)
    monkeypatch.setattr(clips.catalog, "validate", lambda *a: None)
    kw = dict(provider="veo", model="m", aspect_ratio="9:16", duration_seconds=4, resolution="720p")
    with pytest.raises(ProviderError, match="nope"):
        clips.generate_clip("p", tmp_path / "c.mp4", **kw)
    provider.keys = ["MUAPI_API_KEY"]
    with pytest.raises(ProviderNotConfigured):
        clips.generate_clip("p", tmp_path / "c.mp4", **kw)

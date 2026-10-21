def _ttf_bytes() -> bytes:
    """Bytes that start like a TrueType file (sfnt version 0x00010000); the server only checks the magic number."""
    return b"\x00\x01\x00\x00" + bytes(range(256)) * 4


def test_font_round_trip(client):
    data = _ttf_bytes()
    up = client.post("/api/posts/fonts", files={"font": ("My Font.ttf", data, "font/ttf")}, data={"family": "My Font"})
    assert up.status_code == 200
    body = up.json()
    assert body["family"] == "My Font" and body["format"] == "ttf" and body["size"] == len(data)

    assert [f["id"] for f in client.get("/api/posts/fonts").json()] == [body["id"]]
    got = client.get(f"/api/posts/fonts/{body['id']}/file")
    assert got.status_code == 200 and got.content == data and got.headers["content-type"] == "font/ttf"

    assert client.delete(f"/api/posts/fonts/{body['id']}").status_code == 200
    assert client.get("/api/posts/fonts").json() == []
    assert client.get(f"/api/posts/fonts/{body['id']}/file").status_code == 404


def test_duplicate_upload_is_deduplicated(client):
    data = _ttf_bytes()
    a = client.post("/api/posts/fonts", files={"font": ("a.ttf", data, "font/ttf")}).json()
    b = client.post("/api/posts/fonts", files={"font": ("b.ttf", data, "font/ttf")}).json()
    assert a["id"] == b["id"]
    assert len(client.get("/api/posts/fonts").json()) == 1


def test_rejects_non_font_files(client):
    bad_ext = client.post("/api/posts/fonts", files={"font": ("x.png", _ttf_bytes(), "image/png")})
    assert bad_ext.status_code == 400
    not_a_font = client.post("/api/posts/fonts", files={"font": ("x.ttf", b"definitely not a font", "font/ttf")})
    assert not_a_font.status_code == 400


def test_woff_magic_is_accepted(client):
    fake_woff = b"wOFF" + b"\x00" * 60
    r = client.post("/api/posts/fonts", files={"font": ("w.woff", fake_woff, "font/woff")})
    assert r.status_code == 200 and r.json()["format"] == "woff"

"""Match notes CRUD (Notes panel + mobile Notes page)."""

import pytest

from app.config import settings


def bullets(*texts):
    return [{"id": f"b{i}", "text": t} for i, t in enumerate(texts)]


def make(client, client_id="c1", **kw):
    body = {"client_id": client_id, "workspace": "madridonomy", "title": "Real Madrid 3-1 Barcelona, 26 Oct", "bullets": bullets("Bellingham ran it", "Vini was lethal"), **kw}
    return client.post("/api/notes", json=body)


def test_create_and_read_back(client):
    resp = make(client)
    assert resp.status_code == 200
    note = resp.json()
    assert note["title"].startswith("Real Madrid 3-1") and note["workspace"] == "madridonomy"
    assert [b["text"] for b in note["bullets"]] == ["Bellingham ran it", "Vini was lethal"]
    # timestamps carry an explicit UTC offset so a phone in another timezone reads them correctly
    assert note["created_at"].endswith("+00:00") and note["updated_at"].endswith("+00:00")
    assert client.get(f"/api/notes/{note['id']}").json() == note


def test_create_is_idempotent_per_client_id(client):
    first = make(client).json()
    again = make(client, title="a retry must not rename or duplicate").json()
    assert again["id"] == first["id"] and again["title"] == first["title"]
    assert len(client.get("/api/notes").json()) == 1


def test_update_replaces_bullets_and_keeps_order(client):
    note = make(client).json()
    resp = client.patch(f"/api/notes/{note['id']}", json={"bullets": bullets("third", "first", "second")})
    assert resp.status_code == 200
    assert [b["text"] for b in resp.json()["bullets"]] == ["third", "first", "second"]
    assert resp.json()["title"] == note["title"]  # untouched fields stay
    renamed = client.patch(f"/api/notes/{note['id']}", json={"title": "New title"}).json()
    assert renamed["title"] == "New title" and len(renamed["bullets"]) == 3


def test_list_filters_by_workspace_and_sorts_newest_edit_first(client):
    a = make(client, "a").json()
    b = make(client, "b", workspace="footonomy").json()
    c = make(client, "c").json()
    client.patch(f"/api/notes/{a['id']}", json={"title": "touched last"})
    ids = [n["id"] for n in client.get("/api/notes", params={"workspace": "madridonomy"}).json()]
    assert ids == [a["id"], c["id"]]
    assert [n["id"] for n in client.get("/api/notes", params={"workspace": "footonomy"}).json()] == [b["id"]]
    assert len(client.get("/api/notes").json()) == 3


def test_delete_and_missing(client):
    note = make(client).json()
    assert client.delete(f"/api/notes/{note['id']}").json() == {"ok": True}
    assert client.get(f"/api/notes/{note['id']}").status_code == 404
    assert client.patch(f"/api/notes/{note['id']}", json={"title": "x"}).status_code == 404
    assert client.delete(f"/api/notes/{note['id']}").status_code == 404


def test_validation(client):
    assert make(client, workspace="nope").status_code == 400
    assert client.get("/api/notes", params={"workspace": "nope"}).status_code == 400
    assert make(client, "x", title="t" * 201).status_code == 422
    assert make(client, "y", bullets=[{"id": "1", "text": "z" * 2001}]).status_code == 422
    assert make(client, "z", bullets=bullets(*["b"] * 301)).status_code == 422
    assert client.post("/api/notes", json={"title": "no client id"}).status_code == 422


def test_empty_bullets_and_unicode_round_trip(client):
    note = make(client, bullets=[{"id": "u", "text": "Vinícius Jr. – 2 goals, 1 assist ⚽"}, {"id": "e", "text": ""}]).json()
    got = client.get(f"/api/notes/{note['id']}").json()
    assert got["bullets"][0]["text"] == "Vinícius Jr. – 2 goals, 1 assist ⚽" and got["bullets"][1]["text"] == ""


def test_notes_are_behind_the_access_password(client, monkeypatch):
    monkeypatch.setattr(settings, "local_access_password", "secret")
    assert client.get("/api/notes").status_code == 401
    assert client.get("/api/notes", headers={"X-Access-Password": "secret"}).status_code == 200

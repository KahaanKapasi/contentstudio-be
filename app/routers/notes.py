"""Match notes (the Notes panel in the Posts editor and the mobile Notes page).

A sheet is one match or session: a title and an ordered list of bullet opinions. The browser keeps a local copy and
pushes changes here so the phone and the desktop share one set of notes. `client_id` makes creation idempotent: a
sheet written offline can be sent again after a lost response without being duplicated. Writes are last-write-wins
(single user), and bullets are replaced as a whole list on every update.
"""

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import MatchNote
from app.schemas import NoteCreate, NoteOut, NoteUpdate
from app.services.scraping import workspaces

router = APIRouter(prefix="/api/notes", tags=["notes"])


def _workspace(value: str) -> str:
    try:
        return workspaces.get(value).id
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _dump(bullets) -> str:
    return json.dumps([b.model_dump() for b in bullets], ensure_ascii=False)


def _get(db: Session, note_id: int) -> MatchNote:
    note = db.get(MatchNote, note_id)
    if not note:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


@router.get("", response_model=list[NoteOut])
def list_notes(workspace: str | None = None, db: Session = Depends(get_db)):
    """Newest-edited first. No `workspace` returns every sheet."""
    query = db.query(MatchNote)
    if workspace:
        query = query.filter(MatchNote.workspace == _workspace(workspace))
    return query.order_by(MatchNote.updated_at.desc(), MatchNote.id.desc()).all()


@router.post("", response_model=NoteOut)
def create_note(payload: NoteCreate, db: Session = Depends(get_db)):
    existing = db.query(MatchNote).filter(MatchNote.client_id == payload.client_id).first()
    if existing:  # a retried create: hand back what is already stored
        return existing
    note = MatchNote(
        client_id=payload.client_id,
        workspace=_workspace(payload.workspace),
        title=payload.title,
        bullets=_dump(payload.bullets),
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return note


@router.get("/{note_id}", response_model=NoteOut)
def get_note(note_id: int, db: Session = Depends(get_db)):
    return _get(db, note_id)


@router.patch("/{note_id}", response_model=NoteOut)
def update_note(note_id: int, payload: NoteUpdate, db: Session = Depends(get_db)):
    note = _get(db, note_id)
    if payload.title is not None:
        note.title = payload.title
    if payload.bullets is not None:
        note.bullets = _dump(payload.bullets)
    db.commit()
    db.refresh(note)
    return note


@router.delete("/{note_id}")
def delete_note(note_id: int, db: Session = Depends(get_db)):
    db.delete(_get(db, note_id))
    db.commit()
    return {"ok": True}

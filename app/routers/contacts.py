from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..db import SessionLocal
from ..models import Contact, SyncState
import json
from ..schemas import ContactOut


router = APIRouter(prefix="/api/contacts", tags=["contacts"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("", response_model=list[ContactOut])
def list_contacts(db: Session = Depends(get_db)):
    items = db.execute(select(Contact).order_by(Contact.rating.desc())).scalars().all()
    return [ContactOut.model_validate(i) for i in items]


@router.post("/{contact_id}/rating")
def set_rating(contact_id: str, delta: int, db: Session = Depends(get_db)):
    c = db.get(Contact, contact_id)
    if not c:
        raise HTTPException(404, "contact not found")
    c.rating = max(0, min(100, (c.rating or 50) + delta))
    db.add(c)
    db.commit()
    return {"id": c.id, "rating": c.rating}


@router.delete("/{contact_id}")
def delete_contact(contact_id: str, db: Session = Depends(get_db)):
    c = db.get(Contact, contact_id)
    if not c:
        raise HTTPException(404, "contact not found")
    db.delete(c)
    db.commit()
    return {"status": "ok"}


@router.post("/{contact_id}/blacklist")
def add_to_blacklist(contact_id: str, db: Session = Depends(get_db)):
    row = db.get(SyncState, "blacklist_senders")
    arr: list[str] = []
    if row and row.value:
        try:
            data = json.loads(row.value)
            if isinstance(data, list):
                arr = [str(x) for x in data]
        except Exception:
            arr = []
    if contact_id not in arr:
        arr.append(contact_id)
    payload = json.dumps(arr)
    if not row:
        row = SyncState(key="blacklist_senders", value=payload)
    else:
        row.value = payload
    db.add(row)
    db.commit()
    return {"status": "ok", "blacklist_senders": arr}

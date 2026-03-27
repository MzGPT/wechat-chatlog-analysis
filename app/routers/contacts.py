from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..db import SessionLocal
from ..models import Contact, SyncState
from ..config import settings
import json
from ..schemas import ContactOut, ContactsLookupRequest
from ..services.chatlog_contact_book import resolve_contact_db, iter_chatlog_contacts


router = APIRouter(prefix="/api/contacts", tags=["contacts"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("", response_model=list[ContactOut])
def list_contacts(
    include_labels: bool = Query(default=False, description="Include contact labels in list payload."),
    db: Session = Depends(get_db),
):
    items = db.execute(select(Contact).order_by(Contact.rating.desc())).scalars().all()
    out: list[ContactOut] = []
    for i in items:
        payload = {
            "id": i.id,
            "name": i.name,
            "alias": i.alias,
            "rating": i.rating,
            "labels": i.labels if include_labels else None,
        }
        out.append(ContactOut.model_validate(payload))
    return out


@router.get("/labels")
def list_contact_labels():
    """List all WeChat contact labels (tags) from chatlog contact.db."""
    contact_db = resolve_contact_db(settings.CHATLOG_DIR)
    if not contact_db:
        raise HTTPException(400, "chatlog contact.db not found (check CHATLOG_DIR)")
    import sqlite3

    con = sqlite3.connect(str(contact_db))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT label_id_, label_name_ FROM contact_label ORDER BY sort_order_, label_id_").fetchall()
        items = []
        for r in rows:
            name = str(r["label_name_"] or "").strip()
            if not name:
                continue
            items.append({"id": int(r["label_id_"]), "name": name})
        return {"status": "ok", "items": items, "contact_db": str(contact_db)}
    finally:
        con.close()


@router.post("/lookup", response_model=list[ContactOut])
def lookup_contacts(body: ContactsLookupRequest, db: Session = Depends(get_db)):
    ids = [str(x).strip() for x in (body.ids or []) if str(x).strip()]
    if not ids:
        return []
    items = db.execute(select(Contact).where(Contact.id.in_(ids))).scalars().all()
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


@router.post("/sync-book")
def sync_contact_book(limit: int | None = None, insert_missing: bool = True, db: Session = Depends(get_db)):
    """Sync contact nick/remark/labels from local chatlog contact.db into our contacts table.

    This enables displaying remark names and WeChat labels (tags) in UI.
    """
    contact_db = resolve_contact_db(settings.CHATLOG_DIR)
    if not contact_db:
        raise HTTPException(400, "chatlog contact.db not found (check CHATLOG_DIR)")

    existing = {c.id: c for c in db.execute(select(Contact)).scalars().all()}
    inserted = 0
    updated = 0
    updated_alias = 0
    updated_labels = 0

    for rec in iter_chatlog_contacts(contact_db, limit=limit):
        cid = rec.wxid
        if not cid:
            continue
        c = existing.get(cid)
        if not c:
            if not insert_missing:
                continue
            c = Contact(
                id=cid,
                name=rec.nick_name or None,
                alias=rec.remark or None,
                rating=50,
                labels=({"tags": rec.label_names, "source": "chatlog_contact_db"} if rec.label_names else None),
                stats=None,
            )
            db.add(c)
            existing[cid] = c
            inserted += 1
            continue

        changed = False
        if rec.nick_name and (not c.name or c.name != rec.nick_name):
            c.name = rec.nick_name
            changed = True
        if rec.remark and (not c.alias or c.alias != rec.remark):
            c.alias = rec.remark
            changed = True
            updated_alias += 1
        if rec.label_names:
            next_labels = {"tags": rec.label_names, "source": "chatlog_contact_db"}
            if not c.labels or c.labels != next_labels:
                c.labels = next_labels
                changed = True
                updated_labels += 1
        if changed:
            db.add(c)
            updated += 1

    db.commit()
    return {
        "status": "ok",
        "contact_db": str(contact_db),
        "inserted": inserted,
        "updated": updated,
        "updated_alias": updated_alias,
        "updated_labels": updated_labels,
    }

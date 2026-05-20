from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import select, desc, func
from typing import Optional
from datetime import datetime

from ..db import SessionLocal
from ..models import ExtAdapter, AdapterMessage, Message, Contact, Chat
from ..schemas import ExtAdapterIn, ExtAdapterOut, PaginatedAdapterMessages, AdapterMessageOut
from ..services.ext_adapter_service import ingest_adapter_logs
from ..services.sync_service import _build_chatlog_media_url, _extract_contents_dict
from ..config import settings


router = APIRouter(prefix="/api/extensions", tags=["extensions"])


def _display_name_for_contact_or_chat(db: Session, wxid: str | None, fallback: str | None = None) -> str | None:
    candidate = str(wxid or "").strip()
    fb = str(fallback or "").strip()
    if candidate:
        contact = db.get(Contact, candidate)
        if contact:
            alias = str(contact.alias or "").strip()
            name = str(contact.name or "").strip()
            if alias and alias != candidate:
                return alias
            if name:
                return name
            if alias:
                return alias
        chat = db.get(Chat, candidate)
        if chat:
            title = str(chat.title or "").strip()
            if title and title != candidate:
                return title
    if fb and fb != candidate:
        return fb
    return candidate or fb or None


def _extract_gateway_media_payload(row: Message) -> tuple[dict, str | None]:
    meta = row.meta if isinstance(row.meta, dict) else {}
    raw = meta.get("raw") if isinstance(meta.get("raw"), dict) else {}
    data = raw.get("Data") if isinstance(raw.get("Data"), dict) else {}
    raw_content = str(data.get("Content") or row.content_text or "")
    contents: dict[str, str] = {}
    media_url = row.media_url
    if raw_content.lstrip().startswith("<"):
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(raw_content)
            img = root.find("img")
            if img is not None:
                for key in ("cdnthumburl", "md5", "aeskey", "cdnmidimgurl"):
                    val = str(img.attrib.get(key) or "").strip()
                    if val:
                        contents[key] = val
                media_url = media_url or _build_chatlog_media_url(row.type, contents)
        except Exception:
            pass
    return contents, media_url


def _to_adapter_message_out_from_main(db: Session, row: Message) -> AdapterMessageOut:
    contents, media_url = _extract_gateway_media_payload(row)
    sender_name = _display_name_for_contact_or_chat(db, row.sender_id, row.sender_name)
    talker_name = _display_name_for_contact_or_chat(db, row.chat_id, row.talker_name)
    return AdapterMessageOut(
        id=int(row.id),
        adapter_key="wechat",
        external_id=(str((row.meta or {}).get("external_new_msg_id")) if isinstance(row.meta, dict) and (row.meta or {}).get("external_new_msg_id") is not None else None),
        chat_id=row.chat_id,
        sender=sender_name or row.sender_id,
        timestamp=row.timestamp,
        direction=str(row.direction or "in"),
        content_text=row.content_text,
        meta={
            "sender_id": row.sender_id,
            "sender_name": sender_name,
            "talker_name": talker_name,
            "source": (row.meta or {}).get("source") if isinstance(row.meta, dict) else None,
            "media_url": media_url,
            "contents": contents,
        },
    )


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/adapters", response_model=list[ExtAdapterOut])
def list_adapters(db: Session = Depends(get_db)):
    rows = db.execute(select(ExtAdapter).order_by(ExtAdapter.id.desc())).scalars().all()
    return [ExtAdapterOut.model_validate(r) for r in rows]


@router.post("/adapters", response_model=ExtAdapterOut)
def upsert_adapter(body: ExtAdapterIn, db: Session = Depends(get_db)):
    row = db.execute(select(ExtAdapter).where(ExtAdapter.key == body.key)).scalar_one_or_none()
    if row:
        for k, v in body.model_dump().items():
            setattr(row, k, v)
        row.updated_at = datetime.utcnow()
    else:
        row = ExtAdapter(**body.model_dump())
        row.updated_at = datetime.utcnow()
        db.add(row)
    db.commit()
    db.refresh(row)
    return ExtAdapterOut.model_validate(row)


@router.delete("/adapters/{adapter_key}")
def delete_adapter(adapter_key: str, db: Session = Depends(get_db)):
    row = db.execute(select(ExtAdapter).where(ExtAdapter.key == adapter_key)).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "adapter not found")
    db.delete(row)
    db.commit()
    return {"status": "ok"}


@router.post("/adapters/{adapter_key}/ingest")
def ingest_adapter(adapter_key: str, db: Session = Depends(get_db)):
    row = db.execute(select(ExtAdapter).where(ExtAdapter.key == adapter_key)).scalar_one_or_none()
    if not row:
        raise HTTPException(404, "adapter not found")
    base_dir = row.config.get("log_dir") or settings.__dict__.get("LANGBOT_ADAPTER_LOG_DIR") or "./data/adapters"
    try:
        n = ingest_adapter_logs(db, row, base_dir, since=None)
        db.commit()
        return {"status": "ok", "new": n}
    except Exception as e:
        db.rollback()
        raise HTTPException(502, f"ingest error: {e}")


@router.get("/messages", response_model=PaginatedAdapterMessages)
def list_adapter_messages(
    adapter_key: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    if adapter_key == "wechat":
        query = select(Message).where(Message.meta["source"].as_string() == "wechat_gateway")
        total = db.execute(select(func.count()).select_from(Message).where(Message.meta["source"].as_string() == "wechat_gateway")).scalar_one()
        rows = db.execute(
            query.order_by(Message.timestamp.desc(), Message.id.desc()).limit(limit).offset(offset)
        ).scalars().all()
        items = [_to_adapter_message_out_from_main(db, row) for row in rows]
        return {"total": int(total), "items": items}

    query = select(AdapterMessage).where(AdapterMessage.adapter_key == adapter_key)
    total = db.execute(select(func.count()).select_from(AdapterMessage).where(AdapterMessage.adapter_key == adapter_key)).scalar_one()
    rows = db.execute(query.order_by(AdapterMessage.timestamp.desc(), AdapterMessage.id.desc()).limit(limit).offset(offset)).scalars().all()
    items = [AdapterMessageOut.model_validate(r) for r in rows]
    return {"total": int(total), "items": items}

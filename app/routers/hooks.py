from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from datetime import datetime
from ..db import SessionLocal
from ..models import Message, Chat, Contact
from ..schemas import ChatlogWebhookBody
from ..services.sync_service import _build_chatlog_media_url, _extract_contents_dict


router = APIRouter(prefix="/hooks", tags=["hooks"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/chatlog")
def chatlog_webhook(body: ChatlogWebhookBody, db: Session = Depends(get_db)):
    host = None
    if body.messages and body.messages[0].contents:
        host = body.messages[0].contents.get("host")

    new_count = 0
    for m in body.messages:
        # Upsert chat
        chat = db.get(Chat, m.talker)
        if not chat:
            chat = Chat(id=m.talker, title=m.talkerName or m.talker, is_chatroom=m.isChatRoom)
            db.add(chat)

        # Upsert contact
        if m.sender:
            contact = db.get(Contact, m.sender)
            if not contact:
                contact = Contact(id=m.sender, name=m.senderName)
                db.add(contact)

        ts = None
        try:
            ts = datetime.fromisoformat(m.time)
        except Exception:
            pass

        msg = Message(
            chat_id=m.talker,
            sender_id=m.sender,
            sender_name=m.senderName,
            talker_name=m.talkerName,
            timestamp=ts,
            direction="in" if not m.isSelf else "out",
            type=str(m.type),
            content_text=m.content,
            media_url=_build_chatlog_media_url(m.type, _extract_contents_dict(m.contents)),
            meta={"subType": m.subType, **({"contents": m.contents} if isinstance(m.contents, dict) else {})},
        )
        db.add(msg)
        new_count += 1

        if chat:
            chat.last_message_at = ts or chat.last_message_at

    db.commit()

    return {"status": "ok", "inserted": new_count, "host": host}

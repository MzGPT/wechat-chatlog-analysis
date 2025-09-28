from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..db import SessionLocal
from ..models import Chat
from ..schemas import ChatOut


router = APIRouter(prefix="/api/chats", tags=["chats"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("", response_model=list[ChatOut])
def list_chats(db: Session = Depends(get_db)):
    items = db.execute(select(Chat).order_by(Chat.last_message_at.desc().nullslast())).scalars().all()
    return [ChatOut.model_validate(i) for i in items]


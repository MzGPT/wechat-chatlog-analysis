from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..db import SessionLocal
from ..models import SyncState
import json


router = APIRouter(prefix="/api/news", tags=["news"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/config")
def get_news_config(db: Session = Depends(get_db)):
    row = db.get(SyncState, "newsnow_config")
    conf = json.loads(row.value) if row and row.value else {}
    return {"base_url": conf.get("base_url"), "auth_token": conf.get("auth_token")}


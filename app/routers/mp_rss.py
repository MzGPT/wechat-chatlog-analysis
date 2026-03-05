from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import SyncState

from ..services.mp_rss_store import get_mp_article, list_mp_articles


router = APIRouter(prefix="/api/mp", tags=["mp-rss"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_mp_config(db: Session) -> dict:
    row = db.get(SyncState, "mp_config")
    if not row or not row.value:
        return {}
    try:
        data = json.loads(row.value)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


@router.get("/articles")
def api_list_articles(
    limit: int = Query(100, ge=1, le=300),
    offset: int = Query(0, ge=0),
    q: str | None = None,
    db: Session = Depends(get_db),
):
    cfg = _get_mp_config(db)
    db_path = str(cfg.get("db_path") or "").strip() or None
    return list_mp_articles(limit=limit, offset=offset, q=q, db_path=db_path)


@router.get("/articles/{article_id}")
def api_get_article(article_id: str, include_content: bool = False, db: Session = Depends(get_db)):
    cfg = _get_mp_config(db)
    db_path = str(cfg.get("db_path") or "").strip() or None
    try:
        item = get_mp_article(article_id, include_content=include_content, db_path=db_path)
    except FileNotFoundError as exc:
        raise HTTPException(400, str(exc))
    if not item:
        raise HTTPException(404, "article not found")
    return item

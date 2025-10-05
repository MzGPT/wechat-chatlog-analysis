from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import select, desc
from typing import Optional

from ..db import SessionLocal
from ..models import ExtAdapter, AdapterMessage
from ..schemas import ExtAdapterIn, ExtAdapterOut, PaginatedAdapterMessages, AdapterMessageOut
from ..services.ext_adapter_service import ingest_adapter_logs
from ..config import settings


router = APIRouter(prefix="/api/extensions", tags=["extensions"])


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
    else:
        row = ExtAdapter(**body.model_dump())
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
        n = ingest_adapter_logs(db, row, base_dir)
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
    query = select(AdapterMessage).where(AdapterMessage.adapter_key == adapter_key)
    total = db.execute(query.with_only_columns(AdapterMessage.id)).all()
    rows = db.execute(query.order_by(desc(AdapterMessage.timestamp.nullslast()), desc(AdapterMessage.id)).limit(limit).offset(offset)).scalars().all()
    items = [AdapterMessageOut.model_validate(r) for r in rows]
    return {"total": len(total), "items": items}


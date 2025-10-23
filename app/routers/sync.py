from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from datetime import datetime
from ..db import SessionLocal
from ..services.sync_service import sync_from_chatlog, sync_full, compare_with_chatlog
from ..services.snapshot_service import refresh_default_snapshots
from ..models import SyncState


router = APIRouter(prefix="/api/sync", tags=["sync"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/chatlog")
def sync_chatlog(since: str | None = None, db: Session = Depends(get_db)):
    # Accept ISO strings with/without timezone and trailing Z; normalize to naive local time
    parsed_since = None
    if since:
        try:
            s = since.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            parsed_since = dt
        except Exception:
            parsed_since = None
    try:
        res = sync_from_chatlog(db, parsed_since)
        # After sync, immediately write fallback summaries so UI shows grey entries
        from sqlalchemy import select
        from ..models import Message
        from ..services.ai_tools import populate_fallback_derived, ensure_message_features
        # build a conservative window since parsed_since (or 3 days if None)
        from datetime import datetime, timedelta
        cutoff = parsed_since or (datetime.utcnow() - timedelta(days=3))
        recent = db.execute(select(Message).where(Message.timestamp >= cutoff).order_by(Message.id.desc()).limit(5000)).scalars().all()
        try:
            populate_fallback_derived(db, recent, force=False)
        except Exception:
            pass
        # Fire-and-forget AI overlay on same window (does not block response)
        try:
            import threading
            from ..db import SessionLocal as _SessionLocal
            ids = [m.id for m in recent]
            def _overlay(ids: list[int]):
                sess = _SessionLocal()
                try:
                    rows = sess.execute(select(Message).where(Message.id.in_(ids))).scalars().all()
                    ensure_message_features(sess, rows, force=False, concurrency=8)
                except Exception:
                    pass
                finally:
                    sess.close()
            threading.Thread(target=_overlay, args=(ids,), daemon=True).start()
        except Exception:
            pass
        refresh_default_snapshots(db)
        db.commit()
        return res
    except Exception:
        db.rollback()
        raise


@router.get("/state")
def sync_state(db: Session = Depends(get_db)):
    row = db.get(SyncState, "chatlog_last_sync")
    return {"last_sync": row.value if row else None}


@router.post("/chatlog/full")
def sync_chatlog_full(days: int = 30, db: Session = Depends(get_db)):
    try:
        res = sync_full(db, days=days)
        refresh_default_snapshots(db)
        db.commit()
        return res
    except Exception:
        db.rollback()
        raise


@router.get("/compare")
def sync_compare(days: int | None = 1, date: str | None = None, fix: bool | None = False, db: Session = Depends(get_db)):
    """Compare DB with chatlog for a date range or a specific day.

    - days: compare [now-days+1 .. now]; ignored if `date` is provided
    - date: YYYY-MM-DD for single day
    - fix: when true, insert missing chatlog messages into DB
    """
    # Run compare; when fix=True internal engine-level transaction is used, so no session commit here
    res = compare_with_chatlog(db, days=days, date=date, fix=bool(fix))
    return res

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import json
from ..db import SessionLocal
from ..services.sync_service import sync_from_chatlog, sync_full, compare_with_chatlog, sync_from_langbot_adapters
from ..services.snapshot_service import refresh_default_snapshots
from ..models import SyncState


router = APIRouter(prefix="/api/sync", tags=["sync"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _langbot_backup_enabled(db: Session) -> bool:
    row = db.get(SyncState, "extensions_config")
    if not row or not row.value:
        return False
    try:
        cfg = json.loads(row.value) or {}
        return bool((cfg or {}).get("langbot_backup_enabled", False))
    except Exception:
        return False


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
        # Also ingest LangBot adapter logs as a backup source and merge into main messages table (deduped).
        if _langbot_backup_enabled(db):
            try:
                langbot_since = parsed_since or (datetime.now() - timedelta(days=3))
                res["langbot"] = sync_from_langbot_adapters(db, since=langbot_since, ingest=True)
            except Exception:
                res["langbot"] = {"status": "error"}
        else:
            res["langbot"] = {"status": "disabled"}
        # After sync, immediately write fallback summaries so UI shows grey entries
        from sqlalchemy import select
        from ..models import Message
        from ..services.ai_tools import populate_fallback_derived, ensure_message_features
        # build a conservative window since parsed_since (or 3 days if None)
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
                    # read ai-runtime switch; allow turning off overlay from settings
                    from ..models import SyncState
                    import json as _json
                    sw = sess.get(SyncState, 'ai_runtime')
                    cfg = {}
                    try:
                        if sw and sw.value:
                            cfg = _json.loads(sw.value) or {}
                    except Exception:
                        cfg = {}
                    if bool((cfg or {}).get('enable_msg_tool_overlay', True)):
                        rows = sess.execute(select(Message).where(Message.id.in_(ids))).scalars().all()
                        cc = int((cfg or {}).get('default_concurrency', 3) or 3)
                        # Keep a conservative concurrency to avoid 429 Too Many Requests
                        ensure_message_features(sess, rows, force=False, concurrency=max(1, min(16, cc)))
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
        if _langbot_backup_enabled(db):
            try:
                langbot_since = datetime.now() - timedelta(days=max(1, int(days or 30)))
                res["langbot"] = sync_from_langbot_adapters(db, since=langbot_since, ingest=True)
            except Exception:
                res["langbot"] = {"status": "error"}
        else:
            res["langbot"] = {"status": "disabled"}
        refresh_default_snapshots(db)
        db.commit()
        return res
    except Exception:
        db.rollback()
        raise


@router.post("/langbot")
def sync_langbot(days: int = 7, force: bool = False, db: Session = Depends(get_db)):
    """Manual: merge LangBot adapter logs into main messages table (deduped)."""
    try:
        since_dt = datetime.now() - timedelta(days=max(1, int(days or 7)))
        res = sync_from_langbot_adapters(db, since=since_dt, ingest=True, force=bool(force))
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

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import uuid
from time import perf_counter
from ..db import SessionLocal
from ..services.sync_service import sync_from_chatlog, sync_full, compare_with_chatlog
from ..services.snapshot_service import refresh_default_snapshots
from ..services import sync_runtime
from ..models import SyncState


router = APIRouter(prefix="/api/sync", tags=["sync"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _load_chatlog_sync_policy(db: Session) -> dict[str, float | int]:
    return sync_runtime.get_chatlog_sync_policy(db, model_cls=SyncState)


@router.post("/chatlog")
def sync_chatlog(since: str | None = None, db: Session = Depends(get_db)):
    # Accept ISO strings with/without timezone and trailing Z; normalize to naive local time
    parsed_since = None
    started_at = datetime.utcnow()
    run_id = f"chatlog-{uuid.uuid4().hex[:12]}"
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
        t0 = perf_counter()
        policy = _load_chatlog_sync_policy(db)
        res, attempts, sync_err = sync_runtime.run_with_retry(
            lambda: sync_from_chatlog(db, parsed_since),
            max_attempts=int(policy["max_attempts"]),
            sleep_seconds=float(policy["sleep_seconds"]),
            on_error=lambda _exc: db.rollback(),
        )
        if sync_err is not None:
            error_code, _ = sync_runtime.classify_sync_error(sync_err)
            payload = {
                "run_id": run_id,
                "status": "error",
                "started_at": started_at.isoformat(),
                "ended_at": datetime.utcnow().isoformat(),
                "duration_ms": int((perf_counter() - t0) * 1000),
                "attempts": int(attempts),
                "error_code": error_code,
                "error": str(sync_err),
                "fetched": 0,
                "inserted": 0,
                "since": parsed_since.isoformat() if parsed_since else None,
            }
            sync_runtime.persist_sync_run(db, SyncState, payload)
            db.commit()
            return {
                "status": "error",
                "run_id": run_id,
                "attempts": int(attempts),
                "error_code": error_code,
                "error": str(sync_err),
                "fetched": 0,
                "inserted": 0,
                "since": parsed_since.isoformat() if parsed_since else None,
                "until": datetime.now().isoformat(),
            }
        res = dict(res or {})
        res["run_id"] = run_id
        res["attempts"] = int(attempts)
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
        try:
            refresh_default_snapshots(db)
        except Exception as e:
            res["snapshot_error"] = str(e)
        payload = {
            "run_id": run_id,
            "status": "ok",
            "started_at": started_at.isoformat(),
            "ended_at": datetime.utcnow().isoformat(),
            "duration_ms": int((perf_counter() - t0) * 1000),
            "attempts": int(attempts),
            "error_code": None,
            "error": None,
            "fetched": int(res.get("fetched") or 0),
            "inserted": int(res.get("inserted") or 0),
            "since": res.get("since"),
            "until": res.get("until"),
        }
        sync_runtime.persist_sync_run(db, SyncState, payload)
        db.commit()
        return res
    except Exception as e:
        db.rollback()
        error_code, _ = sync_runtime.classify_sync_error(e)
        return {
            "status": "error",
            "run_id": run_id,
            "attempts": 1,
            "error_code": error_code,
            "error": str(e),
            "fetched": 0,
            "inserted": 0,
            "since": parsed_since.isoformat() if parsed_since else None,
            "until": datetime.now().isoformat(),
        }


@router.get("/state")
def sync_state(db: Session = Depends(get_db)):
    return sync_runtime.build_sync_state_payload(db, _model_cls=SyncState)


@router.get("/policy")
def get_sync_policy(db: Session = Depends(get_db)):
    return sync_runtime.get_chatlog_sync_policy(db, model_cls=SyncState)


@router.post("/policy")
def set_sync_policy(body: dict, db: Session = Depends(get_db)):
    policy = sync_runtime.save_chatlog_sync_policy(db, model_cls=SyncState, payload=body or {})
    db.commit()
    return {"status": "ok", "policy": policy}


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

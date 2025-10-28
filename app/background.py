from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from .config import settings
from .db import SessionLocal
from .services.sync_service import sync_from_chatlog
from .services.email_engine import imap_fetch, FetchOptions
from .services.ms_graph import fetch_messages_graph, refresh_token
from .models import EmailAccount, ExtAdapter
from .services.ext_adapter_service import ingest_adapter_logs


async def _sync_loop():
    interval = int(settings.__dict__.get("SYNC_INTERVAL_SECONDS", 0) or 0)
    if interval <= 0:
        return
    while True:
        try:
            db = SessionLocal()
            try:
                res = sync_from_chatlog(db)
                # After sync, apply fallback summaries for recent messages (last 3 days)
                from sqlalchemy import select
                from datetime import datetime, timedelta
                from .models import Message
                from .services.ai_tools import populate_fallback_derived, ensure_message_features
                cutoff = datetime.utcnow() - timedelta(days=3)
                recent = db.execute(select(Message).where(Message.timestamp >= cutoff).order_by(Message.id.desc()).limit(2000)).scalars().all()
                try:
                    populate_fallback_derived(db, recent, force=False)
                except Exception:
                    pass
                # Start async overlay in a background task (best-effort, small batch)
                try:
                    # Respect runtime switch from SyncState.ai_runtime
                    from .models import SyncState
                    import json as _json
                    sw = db.get(SyncState, 'ai_runtime')
                    cfg = {}
                    try:
                        if sw and sw.value:
                            cfg = _json.loads(sw.value) or {}
                    except Exception:
                        cfg = {}
                    if bool((cfg or {}).get('enable_msg_tool_overlay', True)):
                        cc = int((cfg or {}).get('default_concurrency', 3) or 3)
                        # Lower concurrency to reduce provider rate-limit (429) under burst loads
                        ensure_message_features(db, recent, force=False, concurrency=max(1, min(16, cc)))
                except Exception:
                    pass
                db.commit()
            finally:
                db.close()
        except Exception:
            pass
        await asyncio.sleep(interval)


async def _email_loop():
    interval = int(settings.__dict__.get("EMAIL_SYNC_INTERVAL_SECONDS", 0) or 0)
    if interval <= 0:
        return
    while True:
        try:
            db = SessionLocal()
            try:
                accounts = db.query(EmailAccount).filter(EmailAccount.enabled == True).all()  # noqa
                for acc in accounts:
                    try:
                        prov = (acc.provider or "").lower()
                        oauth = (acc.auth or {}).get("oauth") if acc.auth else None
                        if prov in ("outlook", "office365", "hotmail") and oauth and oauth.get("access_token"):
                            try:
                                fetch_messages_graph(db, acc, oauth.get("access_token"), top=50)
                            except Exception:
                                # best-effort refresh then retry once
                                try:
                                    if oauth.get("refresh_token"):
                                        new_tok = refresh_token(oauth.get("refresh_token"))
                                        auth = acc.auth or {}
                                        auth["oauth"] = new_tok
                                        acc.auth = auth
                                        db.add(acc)
                                        db.flush()
                                        fetch_messages_graph(db, acc, new_tok.get("access_token"), top=50)
                                except Exception:
                                    pass
                        else:
                            imap_fetch(db, acc, FetchOptions(limit=50, unseen_only=True))
                    except Exception:
                        pass
                db.commit()
            finally:
                db.close()
        except Exception:
            pass
        await asyncio.sleep(interval)


async def _ext_adapter_loop():
    # poll every 30 seconds by default to ingest adapter logs if configured
    interval = 30
    base_dir = settings.__dict__.get("LANGBOT_ADAPTER_LOG_DIR") or "./data/adapters"
    if not base_dir:
        return
    while True:
        try:
            db = SessionLocal()
            try:
                adapters = db.query(ExtAdapter).filter(ExtAdapter.enabled == True).all()  # noqa
                for a in adapters:
                    try:
                        ingest_adapter_logs(db, a, a.config.get("log_dir") or base_dir)
                    except Exception:
                        pass
                db.commit()
            finally:
                db.close()
        except Exception:
            pass
        await asyncio.sleep(interval)


def install_background(app: FastAPI):
    @app.on_event("startup")
    async def start_sync():
        interval = int(settings.__dict__.get("SYNC_INTERVAL_SECONDS", 0) or 0)
        if interval and interval > 0:
            asyncio.create_task(_sync_loop())
        # 邮件同步改为“仅手动触发”，不再定时自动拉取
        # 如需恢复定时，请显式改回并确保 EMAIL_SYNC_INTERVAL_SECONDS > 0
        # email_interval = int(settings.__dict__.get("EMAIL_SYNC_INTERVAL_SECONDS", 0) or 0)
        # if email_interval and email_interval > 0:
        #     asyncio.create_task(_email_loop())
        if settings.__dict__.get("LANGBOT_ADAPTER_LOG_DIR"):
            asyncio.create_task(_ext_adapter_loop())

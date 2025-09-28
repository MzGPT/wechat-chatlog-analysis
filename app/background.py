from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from .config import settings
from .db import SessionLocal
from .services.sync_service import sync_from_chatlog


async def _sync_loop():
    interval = int(settings.__dict__.get("SYNC_INTERVAL_SECONDS", 0) or 0)
    if interval <= 0:
        return
    while True:
        try:
            db = SessionLocal()
            try:
                sync_from_chatlog(db)
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


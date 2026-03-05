from __future__ import annotations

import json
import requests
from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.responses import FileResponse

from ..config import settings
from ..db import SessionLocal
from sqlalchemy.orm import Session
from ..models import SyncState
from ..services.media_store import (
    list_media_items,
    list_media_meeting_records,
    resolve_media_meeting_audio_path,
)


router = APIRouter(prefix="/api/media", tags=["media"])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_media_config(db: Session) -> dict:
    row = db.get(SyncState, "media_config")
    if not row or not row.value:
        return {}
    try:
        data = json.loads(row.value)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _media_server_base() -> str:
    base = (settings.MEDIA_SERVER_BASE or "").strip()
    return base.rstrip("/")


def _proxy_media_server(method: str, path: str, *, json: dict | None = None, params: dict | None = None) -> dict:
    base = _media_server_base()
    if not base:
        raise HTTPException(
            status_code=503,
            detail="MEDIA_SERVER_BASE not configured (start MediaCrawlerPro server and set MEDIA_SERVER_BASE, e.g. http://127.0.0.1:8001)",
        )
    url = base + path
    try:
        r = requests.request(method.upper(), url, json=json, params=params, timeout=5)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"media server unreachable: {e}")
    if r.status_code >= 400:
        detail = (r.text or "").strip()
        raise HTTPException(status_code=502, detail=f"media server error {r.status_code}: {detail[:300]}")
    try:
        return r.json()
    except Exception:
        return {"ok": True, "raw": (r.text or "")[:2000]}


@router.get("/items")
def api_list_media_items(
    limit: int = Query(200, ge=1, le=500),
    q: str | None = None,
    db: Session = Depends(get_db),
):
    conf = _get_media_config(db)
    project_dir = str(conf.get("project_dir") or "").strip() or None
    return list_media_items(limit=limit, q=q, project_dir=project_dir)


@router.get("/meeting-records")
def api_list_meeting_records(limit: int = Query(200, ge=1, le=500)):
    return list_media_meeting_records(limit=limit)


@router.get("/meeting/audio/{record_id}")
def api_get_meeting_audio(record_id: str):
    p = resolve_media_meeting_audio_path(record_id)
    if not p:
        raise HTTPException(404, "audio not found")
    return FileResponse(str(p), media_type="audio/wav", filename=p.name)


# ---- Meeting recorder controls (proxy to MediaCrawlerPro server, if running) ----
@router.get("/meeting/status")
def api_meeting_status():
    return _proxy_media_server("GET", "/api/meeting/status")


@router.post("/meeting/start_listen")
def api_meeting_start_listen(device_index: int | None = None):
    # MediaCrawlerPro expects optional query param device_index
    params = {"device_index": device_index} if device_index is not None else None
    return _proxy_media_server("POST", "/api/meeting/start_listen", params=params)


@router.post("/meeting/stop_listen")
def api_meeting_stop_listen():
    return _proxy_media_server("POST", "/api/meeting/stop_listen")

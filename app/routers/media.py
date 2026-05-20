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
from ..services.media_collector_store import list_all_items as list_collector_items, get_collector_status


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
    """自媒体列表数据。

    优先使用轻量 media-collector 的 data/hot + data/search。
    若 collector 暂无数据，再回退到旧 MediaCrawlerPro data/results。
    """
    try:
        collector = list_collector_items(limit=limit, keyword=q)
        items = collector.get("items") or []
        if items:
            status = get_collector_status()
            # 适配前端旧表格字段：summary/task_source/transcript_status/stats.like 等
            adapted = []
            for it in items[:limit]:
                stats = it.get("stats") if isinstance(it.get("stats"), dict) else {}
                extra = stats.get("extra") if isinstance(stats.get("extra"), dict) else {}
                heat = it.get("heat") or stats.get("heat") or 0
                adapted.append({
                    **it,
                    "summary": it.get("description") or it.get("title") or "",
                    "task_source": it.get("keyword") or it.get("source_file") or it.get("source_type") or "",
                    "source_keyword": it.get("keyword") or "",
                    "transcript_status": it.get("source_type") or "collector",
                    "stats": {
                        **stats,
                        "like": extra.get("like") or extra.get("view") or extra.get("views") or heat or stats.get("rank") or 0,
                        "comment": extra.get("comment") or extra.get("comments") or 0,
                        "share": extra.get("share") or extra.get("shares") or 0,
                        "collect": extra.get("collect") or extra.get("favorite") or extra.get("favorites") or 0,
                    },
                })
            return {
                "items": adapted,
                "total": len(adapted),
                "source": {
                    "kind": "media-collector",
                    "latest_day": status.get("hot", {}).get("latest_day") or status.get("search", {}).get("latest_day"),
                    "latest_files": status.get("hot", {}).get("latest_files", []),
                    "project_dir": str(settings.BASE_DIR) if hasattr(settings, "BASE_DIR") else "",
                    "results_dir": str(status.get("data_dir") or ""),
                },
            }
    except Exception:
        # 不中断旧数据源
        pass

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

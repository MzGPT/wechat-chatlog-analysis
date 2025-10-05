from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from ..db import SessionLocal
from ..models import SyncState
import json
from ..config import settings
import requests


router = APIRouter(prefix="/api", tags=["config"])


@router.get("/config")
def get_config():
    return {
        "chatlog_http_base": settings.CHATLOG_HTTP_BASE,
        "chatlog_dir": settings.CHATLOG_DIR,
        "n8n": {
            "reply": settings.N8N_REPLY_WEBHOOK,
            "summary": settings.N8N_SUMMARY_WEBHOOK,
            "contact": settings.N8N_CONTACT_WEBHOOK,
            "send": settings.N8N_SEND_WEBHOOK,
            "auth": bool(settings.N8N_AUTH_TOKEN),
        },
    }


@router.get("/config/test")
def test_connectivity():
    checks = {}
    # chatlog
    try:
        r = requests.get(f"{settings.CHATLOG_HTTP_BASE}/api/v1/session", timeout=3)
        checks["chatlog"] = r.status_code
    except Exception as e:
        checks["chatlog"] = f"error: {e}"
    # n8n endpoints presence
    checks["n8n_reply_configured"] = bool(settings.N8N_REPLY_WEBHOOK)
    checks["n8n_summary_configured"] = bool(settings.N8N_SUMMARY_WEBHOOK)
    checks["n8n_contact_configured"] = bool(settings.N8N_CONTACT_WEBHOOK)
    checks["n8n_send_configured"] = bool(settings.N8N_SEND_WEBHOOK)
    return checks


# --------- Black/White List Management (persisted in SyncState) ---------

def _get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_json_list(db: Session, key: str) -> list[str]:
    row = db.get(SyncState, key)
    if not row or not row.value:
        return []
    try:
        data = json.loads(row.value)
        if isinstance(data, list):
            return [str(x) for x in data]
        return []
    except Exception:
        return []


def _set_json_list(db: Session, key: str, values: list[str]) -> None:
    row = db.get(SyncState, key)
    payload = json.dumps(list(dict.fromkeys([str(v) for v in values])))
    if not row:
        row = SyncState(key=key, value=payload)
    else:
        row.value = payload
    db.add(row)


@router.get("/filters")
def get_filters(db: Session = Depends(_get_db)):
    return {
        "blacklist_senders": _get_json_list(db, "blacklist_senders"),
        "blacklist_talkers": _get_json_list(db, "blacklist_talkers"),
        "whitelist_senders": _get_json_list(db, "whitelist_senders"),
        "whitelist_talkers": _get_json_list(db, "whitelist_talkers"),
    }


@router.post("/filters/blacklist")
def set_blacklist(payload: dict, db: Session = Depends(_get_db)):
    senders = payload.get("senders") or []
    talkers = payload.get("talkers") or []
    if not isinstance(senders, list) or not isinstance(talkers, list):
        raise HTTPException(400, "invalid payload")
    _set_json_list(db, "blacklist_senders", [str(x) for x in senders])
    _set_json_list(db, "blacklist_talkers", [str(x) for x in talkers])
    db.commit()
    return {"status": "ok"}


# --------- Module Configurations (persisted in SyncState) ---------

def _get_json_obj(db: Session, key: str) -> dict:
    row = db.get(SyncState, key)
    if not row or not row.value:
        return {}
    try:
        data = json.loads(row.value)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _set_json_obj(db: Session, key: str, obj: dict) -> None:
    payload = json.dumps(obj or {})
    row = db.get(SyncState, key)
    if not row:
        row = SyncState(key=key, value=payload)
    else:
        row.value = payload
    db.add(row)


@router.get("/config/newsnow")
def get_newsnow_config(db: Session = Depends(_get_db)):
    return _get_json_obj(db, "newsnow_config")


@router.post("/config/newsnow")
def set_newsnow_config(payload: dict, db: Session = Depends(_get_db)):
    # expected: { base_url: str, auth_token?: str }
    if not isinstance(payload, dict):
        raise HTTPException(400, "invalid payload")
    _set_json_obj(db, "newsnow_config", payload)
    db.commit()
    return {"status": "ok"}


@router.get("/config/folo")
def get_folo_config(db: Session = Depends(_get_db)):
    return _get_json_obj(db, "folo_config")


@router.post("/config/folo")
def set_folo_config(payload: dict, db: Session = Depends(_get_db)):
    # expected: { base_url: str, api_key?: str }
    if not isinstance(payload, dict):
        raise HTTPException(400, "invalid payload")
    _set_json_obj(db, "folo_config", payload)
    db.commit()
    return {"status": "ok"}


@router.get("/config/extensions")
def get_extensions_config(db: Session = Depends(_get_db)):
    return _get_json_obj(db, "extensions_config")


@router.post("/config/extensions")
def set_extensions_config(payload: dict, db: Session = Depends(_get_db)):
    # expected: { langbot_log_dir?: str, enabled_adapters?: [str] }
    if not isinstance(payload, dict):
        raise HTTPException(400, "invalid payload")
    _set_json_obj(db, "extensions_config", payload)
    db.commit()
    return {"status": "ok"}


@router.post("/filters/whitelist")
def set_whitelist(payload: dict, db: Session = Depends(_get_db)):
    senders = payload.get("senders") or []
    talkers = payload.get("talkers") or []
    if not isinstance(senders, list) or not isinstance(talkers, list):
        raise HTTPException(400, "invalid payload")
    _set_json_list(db, "whitelist_senders", [str(x) for x in senders])
    _set_json_list(db, "whitelist_talkers", [str(x) for x in talkers])
    db.commit()
    return {"status": "ok"}

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..db import SessionLocal
from ..models import Task
from ..schemas import SendRequest, TaskOut
from ..services.n8n_client import N8NClient
from ..services.wechatpad_client import WeChatPadClient


router = APIRouter(prefix="/api", tags=["send"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/send", response_model=TaskOut)
def send(body: SendRequest, db: Session = Depends(get_db)):
    ctx = {
        "request_id": "send-task",
        "items": [i.model_dump() for i in body.items],
    }
    task = Task(type="send", payload=ctx, status="pending")
    db.add(task)
    db.commit()
    db.refresh(task)

    client = N8NClient()
    try:
        result = client.send(ctx)
        task.status = "done"
        task.result = result
        db.add(task)
        db.commit()
        db.refresh(task)
    except Exception as e:
        task.status = "failed"
        task.result = {"error": str(e)}
        db.add(task)
        db.commit()
        db.refresh(task)

    return TaskOut(id=task.id, type=task.type, status=task.status, result=task.result)


@router.post("/send/wechatpad")
def send_wechatpad(body: SendRequest):
    client = WeChatPadClient()
    if not client.configured():
        return {"status": "error", "error": "WeChatPadPro base not configured"}
    # Echo target with result for UI summary
    raw_items = [i.model_dump() for i in body.items]
    res = client.send_batch(raw_items)
    for idx, it in enumerate(res.get("results", [])):
        try:
            it["target"] = raw_items[idx].get("target") or raw_items[idx].get("chat_id")
        except Exception:
            pass
    return res

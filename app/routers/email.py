from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from threading import Lock
from sqlalchemy.orm import Session
from sqlalchemy import select, desc, or_, case
from datetime import datetime
from typing import Optional

from ..db import SessionLocal
from ..models import EmailAccount, EmailMessage
from ..schemas import EmailAccountIn, EmailAccountOut, EmailMessageOut, PaginatedEmailMessages, EmailSendRequest
from ..services.email_engine import imap_fetch, FetchOptions, smtp_send, pop3_fetch
from ..services.email_features import build_email_features, persist_email_features
from ..services.ms_graph import send_mail_graph


router = APIRouter(prefix="/api/email", tags=["email"])

# serialize per-account sync to avoid SQLite "database is locked" under concurrent writes
_ACCOUNT_LOCKS: dict[int, Lock] = {}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/accounts", response_model=list[EmailAccountOut])
def list_accounts(db: Session = Depends(get_db)):
    rows = db.execute(select(EmailAccount).order_by(EmailAccount.id.desc())).scalars().all()
    return [EmailAccountOut.model_validate(r) for r in rows]


@router.post("/accounts", response_model=EmailAccountOut)
def create_account(body: EmailAccountIn, db: Session = Depends(get_db)):
    # basic validation for required hosts
    if not (body.imap_host or "").strip() or not (body.smtp_host or "").strip():
        raise HTTPException(400, "imap_host 和 smtp_host 不能为空")
    row = EmailAccount(**body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return EmailAccountOut.model_validate(row)


@router.put("/accounts/{account_id}", response_model=EmailAccountOut)
def update_account(account_id: int, body: EmailAccountIn, db: Session = Depends(get_db)):
    row = db.get(EmailAccount, account_id)
    if not row:
        raise HTTPException(404, "account not found")
    if not (body.imap_host or "").strip() or not (body.smtp_host or "").strip():
        raise HTTPException(400, "imap_host 和 smtp_host 不能为空")
    for k, v in body.model_dump().items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return EmailAccountOut.model_validate(row)


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    row = db.get(EmailAccount, account_id)
    if not row:
        raise HTTPException(404, "account not found")
    db.delete(row)
    db.commit()
    return {"status": "ok"}


@router.post("/accounts/{account_id}/sync")
def sync_account(account_id: int, unseen_only: bool = True, limit: int = 100, db: Session = Depends(get_db)):
    row = db.get(EmailAccount, account_id)
    if not row:
        raise HTTPException(404, "account not found")
    lock = _ACCOUNT_LOCKS.setdefault(account_id, Lock())
    with lock:
        try:
            n = imap_fetch(db, row, FetchOptions(limit=limit, unseen_only=unseen_only))
            row.last_sync_at = datetime.utcnow()
            db.commit()
            return {"status": "ok", "new": n, "mode": "imap"}
        except Exception as e:
            # Fallback to POP3 on auth/IMAP errors
            db.rollback()
            try:
                n = pop3_fetch(db, row, limit=limit)
                row.last_sync_at = datetime.utcnow()
                db.commit()
                return {"status": "ok", "new": n, "mode": "pop3", "imap_error": str(e)}
            except Exception as e2:
                db.rollback()
                msg = f"imap_error={str(e)}; pop3_error={str(e2)}"
                if 'Authentication unsuccessful' in msg or 'LOGIN failed' in msg or 'Logon failure' in msg:
                    msg += "; 请在邮箱设置中开启 IMAP/POP，或使用应用专用密码，或改用 OAuth(微软/谷歌建议)。"
                raise HTTPException(502, f"sync error: {msg}")


@router.get("/messages", response_model=PaginatedEmailMessages)
def list_email_messages(
    account_id: Optional[int] = None,
    q: Optional[str] = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    query = select(EmailMessage)
    if account_id:
        query = query.where(EmailMessage.account_id == account_id)
    if q:
        like = f"%{q}%"
        query = query.where(
            or_(EmailMessage.subject.like(like), EmailMessage.snippet.like(like), EmailMessage.from_addr.like(like))
        )
    total = db.execute(query.with_only_columns(EmailMessage.id)).all()
    # SQLite 不支持 NULLS LAST 语法，这里用 CASE 将 NULL 置后
    order_nulls_last = case((EmailMessage.sent_at == None, 1), else_=0)  # noqa: E711
    rows = (
        db.execute(
            query.order_by(order_nulls_last.asc(), desc(EmailMessage.sent_at)).limit(limit).offset(offset)
        )
        .scalars()
        .all()
    )
    # 兼容旧前端：如果存在 summary/summary_full，则在响应中填充 key_info 和 key_info_origin
    def _compose_display_summary(d: dict | None) -> str:
        try:
            if not isinstance(d, dict):
                return ""
            num = (d.get("meeting_number") or "").strip()
            plat = (d.get("platform") or "").strip()
            key = (d.get("key_info") or "").strip()
            left = " ".join([x for x in (num, plat) if x])
            if key:
                return f"{left} | {key}" if left else key
            return left
        except Exception:
            return ""

    items: list[EmailMessageOut] = []
    for r in rows:
        out = EmailMessageOut.model_validate(r)
        d = dict(out.derived or {})
        # 前端旧逻辑优先读取 key_info；我们用新的字段回填
        if (d.get("summary_full") or d.get("summary")) and not d.get("key_info"):
            d["key_info"] = d.get("summary_full") or d.get("summary") or ""
        if d.get("summary_origin") and not d.get("key_info_origin"):
            d["key_info_origin"] = d.get("summary_origin")
        d["display_summary"] = _compose_display_summary(d)
        # 确保类型为 dict（pydantic 模型允许赋值）
        out.derived = d
        items.append(out)
    return {"total": len(total), "items": items}


@router.post("/features")
def derive_email_features(payload: dict, db: Session = Depends(get_db)):
    items = payload.get("items") or []
    if not isinstance(items, list):
        raise HTTPException(400, "invalid payload: items must be list")
    features = build_email_features(items)
    # 兼容前端：填充 key_info/key_info_origin，保持与 summary/summary_origin 一致
    compat = {}
    for k, f in (features or {}).items():
        if not isinstance(f, dict):
            compat[k] = f
            continue
        g = dict(f)
        if (g.get("summary_full") or g.get("summary")) and not g.get("key_info"):
            g["key_info"] = g.get("summary_full") or g.get("summary") or ""
        if g.get("summary_origin") and not g.get("key_info_origin"):
            g["key_info_origin"] = g.get("summary_origin")
        compat[k] = g
    features = compat
    if payload.get("persist", True):
        ids = [int(it.get("id")) for it in items if it.get("id") is not None]
        if ids:
            rows = db.execute(select(EmailMessage).where(EmailMessage.id.in_(ids))).scalars().all()
            persist_email_features(db, rows, precomputed=features, force=True, commit=True)
    return {"features": features}


@router.post("/derive")
def derive_email_messages(payload: dict, db: Session = Depends(get_db)):
    ids = payload.get("ids") or []
    if not ids:
        raise HTTPException(400, "ids is required")
    try:
        id_list = [int(i) for i in ids]
    except Exception:
        raise HTTPException(400, "invalid ids")
    rows = db.execute(select(EmailMessage).where(EmailMessage.id.in_(id_list))).scalars().all()
    features = persist_email_features(db, rows, force=bool(payload.get("force", False)), commit=True)
    # readback for debug visibility
    readback: list[dict] = []
    try:
        rws = db.execute(select(EmailMessage.id, EmailMessage.derived).where(EmailMessage.id.in_(id_list))).all()
        for rid, derv in rws:
            readback.append({
                "id": int(rid),
                "summary_origin": (derv or {}).get("summary_origin") if isinstance(derv, dict) else None,
                "has_ai": bool(isinstance(derv, dict) and isinstance(derv.get("summary"), str) and derv.get("summary"," ").lower().strip().startswith("ai:")),
            })
    except Exception:
        pass
    # 兼容前端：填充 key_info/key_info_origin 字段
    compat = {}
    for k, f in (features or {}).items():
        if not isinstance(f, dict):
            compat[k] = f
            continue
        g = dict(f)
        if (g.get("summary_full") or g.get("summary")) and not g.get("key_info"):
            g["key_info"] = g.get("summary_full") or g.get("summary") or ""
        if g.get("summary_origin") and not g.get("key_info_origin"):
            g["key_info_origin"] = g.get("summary_origin")
        compat[k] = g
    return {"status": "ok", "processed": len(rows), "features": compat, "debug_readback": readback[:50]}

@router.post("/send")
def send_email(body: EmailSendRequest, db: Session = Depends(get_db)):
    acc = db.get(EmailAccount, body.account_id)
    if not acc:
        raise HTTPException(404, "account not found")
    try:
        # Prefer Microsoft Graph when provider is outlook and OAuth is present
        if (acc.provider or "").lower() in ("outlook", "office365", "hotmail") and ((acc.auth or {}).get("oauth")):
            resp = send_mail_graph(db, acc, body.to, body.subject, body.body_text, cc=body.cc, bcc=body.bcc)
        else:
            resp = smtp_send(db, acc, body.to, body.subject, body.body_text, cc=body.cc, bcc=body.bcc)
        db.commit()
        return resp
    except Exception as e:
        db.rollback()
        raise HTTPException(502, f"send error: {e}")

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import select, desc, or_
from datetime import datetime
from typing import Optional

from ..db import SessionLocal
from ..models import EmailAccount, EmailMessage
from ..schemas import EmailAccountIn, EmailAccountOut, EmailMessageOut, PaginatedEmailMessages, EmailSendRequest
from ..services.email_engine import imap_fetch, FetchOptions, smtp_send, pop3_fetch


router = APIRouter(prefix="/api/email", tags=["email"])


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
            msg = str(e2)
            # Friendly hints for common providers that have disabled basic auth
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
    rows = db.execute(query.order_by(desc(EmailMessage.sent_at.nullslast())).limit(limit).offset(offset)).scalars().all()
    items = [EmailMessageOut.model_validate(r) for r in rows]
    return {"total": len(total), "items": items}


@router.post("/send")
def send_email(body: EmailSendRequest, db: Session = Depends(get_db)):
    acc = db.get(EmailAccount, body.account_id)
    if not acc:
        raise HTTPException(404, "account not found")
    try:
        resp = smtp_send(db, acc, body.to, body.subject, body.body_text, cc=body.cc, bcc=body.bcc)
        db.commit()
        return resp
    except Exception as e:
        db.rollback()
        raise HTTPException(502, f"send error: {e}")

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select
from ..db import SessionLocal
from ..models import EmailAccount
from ..services.ms_graph import start_device_code, poll_device_token, fetch_profile, fetch_messages_graph


router = APIRouter(prefix="/api/email/ms", tags=["email-ms"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/start")
def ms_start():
    try:
        j = start_device_code()
        # return important fields to UI
        return {
            "user_code": j.get("user_code"),
            "device_code": j.get("device_code"),
            "verification_uri": j.get("verification_uri"),
            "message": j.get("message"),
            "interval": j.get("interval", 5),
        }
    except Exception as e:
        raise HTTPException(502, f"start device code error: {e}")


@router.post("/poll")
def ms_poll(device_code: str, account_id: int | None = None, db: Session = Depends(get_db)):
    try:
        tok = poll_device_token(device_code)
        access_token = tok.get("access_token")
        if not access_token:
            raise HTTPException(502, "no access_token")
        prof = fetch_profile(access_token)
        email = prof.get("userPrincipalName") or prof.get("mail")
        if not email:
            raise HTTPException(502, "cannot resolve profile email")
        # ensure an EmailAccount exists
        acc = db.execute(select(EmailAccount).where(EmailAccount.email_address == email)).scalar_one_or_none()
        if not acc:
            acc = EmailAccount(
                name=prof.get("displayName") or email,
                email_address=email,
                provider="outlook",
                imap_host="",
                imap_port=993,
                imap_ssl=True,
                smtp_host="",
                smtp_port=587,
                smtp_ssl=False,
                auth={"oauth": tok},
                enabled=True,
            )
            db.add(acc)
        else:
            auth = acc.auth or {}
            auth["oauth"] = tok
            acc.auth = auth
            # Make sure provider is set to outlook so background + send use Graph
            if (acc.provider or "").lower() not in ("outlook", "office365", "hotmail"):
                acc.provider = "outlook"
        db.commit()
        db.refresh(acc)
        return {"status": "ok", "account_id": acc.id, "email": email}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"poll error: {e}")


@router.post("/fetch")
def ms_fetch(account_id: int, top: int = 50, db: Session = Depends(get_db)):
    acc = db.get(EmailAccount, account_id)
    if not acc:
        raise HTTPException(404, "account not found")
    tok = (acc.auth or {}).get("oauth") or {}
    access_token = tok.get("access_token")
    if not access_token:
        raise HTTPException(400, "no access_token; please authorize first")
    try:
        n = fetch_messages_graph(db, acc, access_token, top=top)
        db.commit()
        return {"status": "ok", "new": n}
    except Exception as e:
        raise HTTPException(502, f"fetch error: {e}")

from __future__ import annotations

import time
from typing import Dict, Any, List, Optional
import requests
from sqlalchemy.orm import Session
from sqlalchemy import select

from ..config import settings
from ..models import EmailAccount, EmailMessage


MS_AUTH_BASE = "https://login.microsoftonline.com"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def start_device_code(client_id: str | None = None, tenant: str | None = None) -> Dict[str, Any]:
    client_id = client_id or settings.MS_CLIENT_ID
    tenant = tenant or settings.MS_TENANT or "consumers"
    if not client_id:
        raise RuntimeError("MS_CLIENT_ID not configured")
    url = f"{MS_AUTH_BASE}/{tenant}/oauth2/v2.0/devicecode"
    data = {
        "client_id": client_id,
        # Delegated scopes for reading mail
        "scope": "Mail.Read offline_access openid profile",
    }
    r = requests.post(url, data=data, timeout=15)
    r.raise_for_status()
    return r.json()


def poll_device_token(device_code: str, client_id: str | None = None, tenant: str | None = None, interval: int | None = None, timeout_sec: int = 600) -> Dict[str, Any]:
    client_id = client_id or settings.MS_CLIENT_ID
    tenant = tenant or settings.MS_TENANT or "consumers"
    if not client_id:
        raise RuntimeError("MS_CLIENT_ID not configured")
    url = f"{MS_AUTH_BASE}/{tenant}/oauth2/v2.0/token"
    form = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": client_id,
        "device_code": device_code,
    }
    start = time.time()
    while True:
        r = requests.post(url, data=form, timeout=20)
        if r.status_code == 200:
            return r.json()
        try:
            j = r.json()
        except Exception:
            r.raise_for_status()
        err = j.get("error")
        if err in ("authorization_pending", "slow_down"):
            time.sleep((interval or 5))
            if time.time() - start > timeout_sec:
                raise RuntimeError("device code authorization timeout")
            continue
        raise RuntimeError(f"oauth error: {j}")


def refresh_token(refresh_token: str, client_id: str | None = None, tenant: str | None = None) -> Dict[str, Any]:
    client_id = client_id or settings.MS_CLIENT_ID
    tenant = tenant or settings.MS_TENANT or "consumers"
    if not client_id:
        raise RuntimeError("MS_CLIENT_ID not configured")
    url = f"{MS_AUTH_BASE}/{tenant}/oauth2/v2.0/token"
    form = {
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": refresh_token,
        "scope": "Mail.Read offline_access openid profile",
    }
    r = requests.post(url, data=form, timeout=20)
    r.raise_for_status()
    return r.json()


def fetch_profile(access_token: str) -> Dict[str, Any]:
    r = requests.get(f"{GRAPH_BASE}/me", headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_messages_graph(db: Session, account: EmailAccount, access_token: str, top: int = 50) -> int:
    url = f"{GRAPH_BASE}/me/messages?$top={min(50, max(1, top))}"
    headers = {"Authorization": f"Bearer {access_token}"}
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    items = data.get("value") or []
    new_count = 0
    existing = set(
        x[0]
        for x in db.execute(select(EmailMessage.external_id).where(EmailMessage.account_id == account.id)).all()
        if x[0]
    )
    for it in items:
        ext_id = it.get("id")
        if not ext_id or ext_id in existing:
            continue
        row = EmailMessage(
            account_id=account.id,
            external_id=ext_id,
            subject=it.get("subject"),
            from_addr=(it.get("from") or {}).get("emailAddress", {}).get("address"),
            to_addrs=[(t.get("emailAddress") or {}).get("address") for t in (it.get("toRecipients") or [])],
            cc_addrs=[(t.get("emailAddress") or {}).get("address") for t in (it.get("ccRecipients") or [])],
            sent_at=it.get("sentDateTime"),
            direction="in",
            snippet=(it.get("bodyPreview") or "")[:400],
            body_text=None,
            body_html=None,
            flags=None,
            meta={"graph": True},
        )
        db.add(row)
        new_count += 1
    return new_count


def _ensure_valid_token(db: Session, account: EmailAccount) -> str:
    """Return a valid access_token, refreshing once if needed.

    This is a best-effort helper to keep logic simple without introducing a
    background scheduler here. We only refresh when we detect an auth failure
    from the Graph API call site.
    """
    tok = (account.auth or {}).get("oauth") or {}
    access_token = tok.get("access_token")
    if access_token:
        return access_token
    # Try refresh if we have a refresh_token
    refresh = tok.get("refresh_token")
    if not refresh:
        raise RuntimeError("no access_token; please authorize first")
    new_tok = refresh_token(refresh)
    auth = account.auth or {}
    auth["oauth"] = new_tok
    account.auth = auth
    db.add(account)
    db.flush()
    return new_tok.get("access_token")


def send_mail_graph(
    db: Session,
    account: EmailAccount,
    to: List[str],
    subject: str,
    body_text: str,
    cc: Optional[List[str]] = None,
    bcc: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Send a simple text email via Microsoft Graph.

    Falls back to a single refresh if the token is expired.
    Also persists an outgoing EmailMessage row for UI listing.
    """
    tok = (account.auth or {}).get("oauth") or {}
    access_token = tok.get("access_token")
    if not access_token:
        # try to refresh once
        rf = tok.get("refresh_token")
        if not rf:
            raise RuntimeError("no access_token; please authorize first")
        new_tok = refresh_token(rf)
        access_token = new_tok.get("access_token")
        auth = account.auth or {}
        auth["oauth"] = new_tok
        account.auth = auth
        db.add(account)
        db.flush()

    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    msg = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": [{"emailAddress": {"address": x}} for x in (to or [])],
        },
        "saveToSentItems": True,
    }
    if cc:
        msg["message"]["ccRecipients"] = [{"emailAddress": {"address": x}} for x in cc]
    if bcc:
        msg["message"]["bccRecipients"] = [{"emailAddress": {"address": x}} for x in bcc]

    url = f"{GRAPH_BASE}/me/sendMail"
    r = requests.post(url, headers=headers, json=msg, timeout=20)
    if r.status_code == 401:
        # try refresh once
        rf = ((account.auth or {}).get("oauth") or {}).get("refresh_token")
        if rf:
            new_tok = refresh_token(rf)
            auth = account.auth or {}
            auth["oauth"] = new_tok
            account.auth = auth
            db.add(account)
            db.flush()
            headers["Authorization"] = f"Bearer {new_tok.get('access_token')}"
            r = requests.post(url, headers=headers, json=msg, timeout=20)
    r.raise_for_status()

    # Persist an outgoing row (direction=out) for unified listing
    row = EmailMessage(
        account_id=account.id,
        external_id=None,
        thread_id=None,
        subject=subject,
        from_addr=f"{account.name} <{account.email_address}>",
        to_addrs=to,
        cc_addrs=cc,
        bcc_addrs=bcc,
        sent_at=None,
        direction="out",
        snippet=body_text[:400],
        body_text=body_text,
        body_html=None,
        flags=["sent"],
        meta={"graph": True},
    )
    db.add(row)
    db.flush()
    return {"status": "ok", "message_id": row.id}

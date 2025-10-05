from __future__ import annotations

"""Minimal email engine for multi-account receive/send.

Design goals:
- IMAP fetch (default) for inbound; POP3 could be added later.
- SMTP send with SSL/TLS; support login auth.
- Store lightweight message rows to DB; avoid large bodies/attachments for now.
- Idempotent fetch via Message UID/Message-ID tracking.
"""

from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, getaddresses
import imaplib
import smtplib
import ssl
from typing import Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import EmailAccount, EmailMessage


def _decode(s: str | bytes | None) -> str | None:
    if s is None:
        return None
    try:
        if isinstance(s, bytes):
            return str(make_header(decode_header(s)))
        return str(make_header(decode_header(s)))
    except Exception:
        return s.decode("utf-8", errors="ignore") if isinstance(s, bytes) else s


def _addr_list(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    try:
        pairs = getaddresses([raw])
        addrs = []
        for name, addr in pairs:
            if name:
                name = str(make_header(decode_header(name)))
                addrs.append(f"{name} <{addr}>")
            else:
                addrs.append(addr)
        return addrs
    except Exception:
        return [raw]


@dataclass
class FetchOptions:
    folder: str = "INBOX"
    limit: int = 100
    unseen_only: bool = False


def imap_fetch(db: Session, account: EmailAccount, opts: FetchOptions | None = None) -> int:
    """Fetch latest emails into the DB. Returns number of new rows inserted.

    Strategy:
    - IMAP SEARCH to get recent UIDs (unseen or all, up to limit*2 as buffer)
    - FETCH BODY.PEEK[HEADER] and optionally small TEXT part to build snippet.
    - Deduplicate by (account_id, external_id [UID]) if available; fallback to Message-ID.
    """

    opts = opts or FetchOptions()
    context = ssl.create_default_context()
    new_count = 0

    if account.imap_ssl:
        M = imaplib.IMAP4_SSL(account.imap_host, account.imap_port, ssl_context=context)
    else:
        M = imaplib.IMAP4(account.imap_host, account.imap_port)

    try:
        username = (account.auth or {}).get("username") or account.email_address
        password = (account.auth or {}).get("password") or ""
        M.login(username, password)
        M.select(opts.folder)

        criteria = "UNSEEN" if opts.unseen_only else "ALL"
        typ, data = M.search(None, criteria)
        if typ != "OK":
            return 0
        uids = data[0].split() if data and data[0] else []
        uids = uids[-(opts.limit * 2) :]

        # Build existing uid set to skip duplicates
        existing: set[str] = set(
            x[0]
            for x in db.execute(
                select(EmailMessage.external_id).where(EmailMessage.account_id == account.id)
            ).all()
            if x[0]
        )

        for uid in reversed(uids):  # newest first
            uid_s = uid.decode() if isinstance(uid, (bytes, bytearray)) else str(uid)
            if uid_s in existing:
                continue
            typ, msgdata = M.fetch(uid, "(RFC822)")
            if typ != "OK" or not msgdata:
                continue
            raw = msgdata[0][1]
            if not raw:
                continue
            try:
                em = message_from_bytes(raw)
            except Exception:
                continue

            subject = _decode(em.get("Subject"))
            from_raw = em.get("From")
            to_raw = em.get("To")
            cc_raw = em.get("Cc")
            date_hdr = em.get("Date")
            msg_id_hdr = em.get("Message-ID")

            sent_at = None
            try:
                if date_hdr:
                    sent_at = parsedate_to_datetime(date_hdr)
            except Exception:
                sent_at = None

            # Extract best-effort plain text for snippet
            body_text = None
            body_html = None
            try:
                if em.is_multipart():
                    for part in em.walk():
                        ctype = part.get_content_type()
                        disp = part.get("Content-Disposition", "") or ""
                        if ctype == "text/plain" and "attachment" not in disp:
                            body_text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                            break
                        if ctype == "text/html" and "attachment" not in disp and not body_html:
                            body_html = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                else:
                    payload = em.get_payload(decode=True)
                    if payload:
                        body_text = payload.decode(em.get_content_charset() or "utf-8", errors="ignore")
            except Exception:
                pass

            snippet = (body_text or "" ).strip().replace("\r", " ").replace("\n", " ")[:400]

            row = EmailMessage(
                account_id=account.id,
                external_id=uid_s or (msg_id_hdr or None),
                thread_id=None,
                subject=subject,
                from_addr=_addr_list(from_raw)[0] if _addr_list(from_raw) else from_raw,
                to_addrs=_addr_list(to_raw),
                cc_addrs=_addr_list(cc_raw),
                bcc_addrs=None,
                sent_at=sent_at,
                direction="in",
                snippet=snippet,
                body_text=(body_text or None),
                body_html=(body_html or None),
                flags=["seen"] if ("Seen" in (msgdata[0][0].decode(errors="ignore") if isinstance(msgdata[0][0], (bytes, bytearray)) else "")) else None,
                meta={"message_id": msg_id_hdr} if msg_id_hdr else None,
            )

            db.add(row)
            db.flush()
            new_count += 1

    finally:
        try:
            M.logout()
        except Exception:
            pass

    return new_count


def smtp_send(db: Session, account: EmailAccount, to: list[str], subject: str, body_text: str, cc: Optional[list[str]] = None, bcc: Optional[list[str]] = None) -> dict:
    """Send a simple plain-text email via SMTP.

    Returns a summary dict; also persists an outgoing EmailMessage row.
    """

    from email.message import EmailMessage as PyEmailMessage

    msg = PyEmailMessage()
    msg["From"] = f"{account.name} <{account.email_address}>"
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    msg.set_content(body_text)

    username = (account.auth or {}).get("username") or account.email_address
    password = (account.auth or {}).get("password") or ""

    if account.smtp_ssl:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, context=context) as s:
            s.login(username, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(account.smtp_host, account.smtp_port) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(username, password)
            s.send_message(msg)

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
        meta=None,
    )
    db.add(row)
    db.flush()

    return {"status": "ok", "message_id": row.id}


# --------- POP3 (fallback) ---------
import poplib

def pop3_fetch(db: Session, account: EmailAccount, limit: int = 50) -> int:
    """Fallback fetch using POP3 when IMAP is unavailable.

    For Outlook/Hotmail, host is usually 'pop-mail.outlook.com', port 995 (SSL).
    This fetches top-N messages (most recent IDs), retrieves headers and a small
    portion of the body for snippet.
    """
    host = account.imap_host or ''
    port = account.imap_port or 995
    # Best-effort: common POP hostname for outlook if IMAP host points to office365
    if 'office365.com' in host or 'outlook.' in host:
        host = 'pop-mail.outlook.com'
        port = 995

    username = (account.auth or {}).get("username") or account.email_address
    password = (account.auth or {}).get("password") or ""

    new_count = 0
    server = None
    try:
        server = poplib.POP3_SSL(host, port, timeout=30)
        server.user(username)
        server.pass_(password)
        num_messages = len(server.list()[1])
        start = max(1, num_messages - limit + 1)

        # Build existing ext ids as 'pop-<msgno>' to dedupe
        existing = set(
            x[0]
            for x in db.execute(
                select(EmailMessage.external_id).where(EmailMessage.account_id == account.id)
            ).all()
            if x[0]
        )

        for i in range(num_messages, start - 1, -1):
            ext_id = f"pop-{i}"
            if ext_id in existing:
                continue
            # TOP command: headers + first N lines of body
            try:
                resp, lines, octets = server.top(i, 50)
            except Exception:
                continue
            data = b"\r\n".join(lines)
            try:
                em = message_from_bytes(data)
            except Exception:
                continue
            subject = _decode(em.get("Subject"))
            from_raw = em.get("From")
            date_hdr = em.get("Date")
            sent_at = None
            try:
                if date_hdr:
                    sent_at = parsedate_to_datetime(date_hdr)
            except Exception:
                pass
            snippet = None
            try:
                if em.is_multipart():
                    for part in em.walk():
                        ctype = part.get_content_type()
                        disp = part.get("Content-Disposition", "") or ""
                        if ctype == "text/plain" and "attachment" not in disp:
                            snippet = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", errors="ignore")
                            break
                else:
                    payload = em.get_payload(decode=True)
                    if payload:
                        snippet = payload.decode(em.get_content_charset() or "utf-8", errors="ignore")
            except Exception:
                pass
            snippet = (snippet or "").strip().replace("\r"," ").replace("\n"," ")[:400]
            row = EmailMessage(
                account_id=account.id,
                external_id=ext_id,
                subject=subject,
                from_addr=_addr_list(from_raw)[0] if _addr_list(from_raw) else from_raw,
                sent_at=sent_at,
                direction="in",
                snippet=snippet,
                body_text=None,
            )
            db.add(row)
            new_count += 1
        return new_count
    finally:
        try:
            if server:
                server.quit()
        except Exception:
            pass

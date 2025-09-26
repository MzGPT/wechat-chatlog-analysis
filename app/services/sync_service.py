from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
import time

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy import select

from ..models import Message, Chat, Contact, SyncState
import json
from .chatlog_client import ChatlogClient


def _get_last_sync(db: Session) -> Optional[datetime]:
    row = db.get(SyncState, "chatlog_last_sync")
    if row and row.value:
        try:
            return datetime.fromisoformat(row.value)
        except Exception:
            return None
    # fallback to newest message timestamp
    latest: Optional[datetime] = db.execute(select(Message.timestamp).order_by(Message.timestamp.desc())).scalar()
    return latest


def _set_last_sync(db: Session, ts: datetime):
    row = db.get(SyncState, "chatlog_last_sync")
    if not row:
        row = SyncState(key="chatlog_last_sync", value=ts.isoformat())
    else:
        row.value = ts.isoformat()
        row.updated_at = datetime.utcnow()
    db.add(row)


def _safe_commit(db: Session, retries: int = 5, base_delay: float = 0.2) -> None:
    """Commit with retry to tolerate sqlite locked errors."""
    last_exc: OperationalError | None = None
    for attempt in range(retries):
        try:
            db.commit()
            return
        except OperationalError as exc:
            db.rollback()
            last_exc = exc
            time.sleep(base_delay * (attempt + 1))
    if last_exc is not None:
        raise last_exc


def _parse_messages(payload: Any) -> List[Dict[str, Any]]:
    # chatlog json may be a list or an object with messages
    if isinstance(payload, dict) and "messages" in payload:
        return payload["messages"]
    if isinstance(payload, list):
        return payload
    # unknown format
    return []


def sync_from_chatlog(db: Session, since: Optional[datetime] = None) -> Dict[str, Any]:
    """Incremental sync since a cutoff.

    Earlier implementation called the chatlog endpoint once per day without talker
    and without pagination, which can miss messages on some chatlog builds.
    This version mirrors the robust logic used in sync_full:
    - iterate day by day from since..now
    - fetch talkers from session list and walk each talker with pagination
    - fall back to non-talker queries if session list is unavailable
    """
    client = ChatlogClient()

    # Load filters once
    def _load_list(key: str) -> set[str]:
        row = db.get(SyncState, key)
        if not row or not row.value:
            return set()
        try:
            data = json.loads(row.value)
            if isinstance(data, list):
                return set(str(x) for x in data)
        except Exception:
            pass
        return set()

    bl_send = _load_list("blacklist_senders")
    bl_talk = _load_list("blacklist_talkers")
    wl_send = _load_list("whitelist_senders")
    wl_talk = _load_list("whitelist_talkers")
    has_wl = bool(wl_send or wl_talk)

    now = datetime.now()
    if since is None:
        since = _get_last_sync(db)
    if since is None:
        since = now - timedelta(days=1)

    # Prepare time window
    start_date = since.date()
    end_date = now.date()

    # Try to enumerate talkers for robust coverage
    try:
        session_payload = client.get_sessions()
        talkers = ChatlogClient.extract_talker_ids(session_payload)
    except Exception:
        talkers = []

    total_fetched = 0
    inserted = 0
    max_ts: Optional[datetime] = since

    cur = start_date
    while cur <= end_date:
        day = cur.isoformat()
        if talkers:
            # Robust path: walk each talker with pagination
            for talker in talkers:
                offset = 0
                while True:
                    try:
                        raw = client.get_chatlog(time_range=day, talker=talker, limit=500, offset=offset)
                        part = _parse_messages(raw)
                        if not part:
                            break
                        total_fetched += len(part)
                        for m in part:
                            talkerName = m.get("talkerName")
                            sender = m.get("sender")
                            senderName = m.get("senderName")
                            isChatRoom = bool(m.get("isChatRoom"))
                            isSelf = bool(m.get("isSelf"))
                            type_ = m.get("type")
                            content = m.get("content") or m.get("text")
                            meta_payload: dict[str, Any] = {}
                            try:
                                contents = m.get("contents")
                                if isinstance(contents, dict):
                                    meta_payload["contents"] = contents
                            except Exception:
                                pass
                            time_str = m.get("time") or m.get("timestamp")
                            ts = None
                            if time_str:
                                try:
                                    ts = datetime.fromisoformat(time_str)
                                except Exception:
                                    ts = None
                            if ts and (max_ts is None or ts > max_ts):
                                max_ts = ts
                            # white/black list
                            if has_wl and not ((talker and talker in wl_talk) or (sender and sender in wl_send)):
                                continue
                            if (talker and talker in bl_talk) or (sender and sender in bl_send):
                                continue
                            # cutoff
                            if ts and ts < since:
                                continue
                            # de-dup
                            exists = None
                            if ts:
                                exists = db.execute(
                                    select(Message.id).where(
                                        Message.chat_id == talker,
                                        Message.sender_id == sender,
                                        Message.timestamp == ts,
                                        Message.content_text == content,
                                    )
                                ).scalar()
                                if exists:
                                    continue
                            # upsert chat/contact
                            chat = db.get(Chat, talker)
                            if not chat:
                                chat = Chat(id=talker, title=talkerName or talker, is_chatroom=isChatRoom)
                                db.add(chat)
                            if sender:
                                c = db.get(Contact, sender)
                                if not c:
                                    c = Contact(id=sender, name=senderName)
                                    db.add(c)
                            msg = Message(
                                chat_id=talker,
                                sender_id=sender,
                                sender_name=senderName,
                                talker_name=talkerName,
                                timestamp=ts,
                                direction="out" if isSelf else "in",
                                type=str(type_) if type_ is not None else None,
                                content_text=content,
                                media_url=None,
                                meta=meta_payload,
                            )
                            db.add(msg)
                            inserted += 1
                            if chat and ts and (chat.last_message_at is None or ts > chat.last_message_at):
                                chat.last_message_at = ts
                                db.add(chat)
                        _safe_commit(db)
                        if len(part) < 500:
                            break
                        offset += 500
                    except Exception:
                        db.rollback()
                        break
        else:
            # Fallback path: no talkers available, try non-talker paginated fetch (if supported)
            offset = 0
            while True:
                try:
                    raw = client.get_chatlog(time_range=day, limit=500, offset=offset)
                    part = _parse_messages(raw)
                    if not part:
                        break
                    total_fetched += len(part)
                    for m in part:
                        talker = m.get("talker") or m.get("chat_id")
                        talkerName = m.get("talkerName")
                        sender = m.get("sender")
                        senderName = m.get("senderName")
                        isChatRoom = bool(m.get("isChatRoom"))
                        isSelf = bool(m.get("isSelf"))
                        type_ = m.get("type")
                        content = m.get("content") or m.get("text")
                        meta_payload: dict[str, Any] = {}
                        try:
                            contents = m.get("contents")
                            if isinstance(contents, dict):
                                meta_payload["contents"] = contents
                        except Exception:
                            pass
                        time_str = m.get("time") or m.get("timestamp")
                        ts = None
                        if time_str:
                            try:
                                ts = datetime.fromisoformat(time_str)
                            except Exception:
                                ts = None
                        if ts and (max_ts is None or ts > max_ts):
                            max_ts = ts
                        if has_wl and not ((talker and talker in wl_talk) or (sender and sender in wl_send)):
                            continue
                        if (talker and talker in bl_talk) or (sender and sender in bl_send):
                            continue
                        if ts and ts < since:
                            continue
                        exists = None
                        if ts and talker and sender and content:
                            exists = db.execute(
                                select(Message.id).where(
                                    Message.chat_id == talker,
                                    Message.sender_id == sender,
                                    Message.timestamp == ts,
                                    Message.content_text == content,
                                )
                            ).scalar()
                        if exists:
                            continue
                        if talker:
                            chat = db.get(Chat, talker)
                            if not chat:
                                chat = Chat(id=talker, title=talkerName or talker, is_chatroom=isChatRoom)
                                db.add(chat)
                        if sender:
                            c = db.get(Contact, sender)
                            if not c:
                                c = Contact(id=sender, name=senderName)
                                db.add(c)
                        msg = Message(
                            chat_id=talker,
                            sender_id=sender,
                            sender_name=senderName,
                            talker_name=talkerName,
                            timestamp=ts,
                            direction="out" if isSelf else "in",
                            type=str(type_) if type_ is not None else None,
                            content_text=content,
                            media_url=None,
                            meta=meta_payload,
                        )
                        db.add(msg)
                        inserted += 1
                        if talker and ts:
                            chat = db.get(Chat, talker)
                            if chat and (chat.last_message_at is None or ts > chat.last_message_at):
                                chat.last_message_at = ts
                                db.add(chat)
                    _safe_commit(db)
                    if len(part) < 500:
                        break
                    offset += 500
                except Exception:
                    db.rollback()
                    break
        cur = cur + timedelta(days=1)

    if max_ts:
        _set_last_sync(db, max_ts)
        _safe_commit(db)

    return {
        "status": "ok",
        "fetched": total_fetched,
        "inserted": inserted,
        "since": since.isoformat(),
        "until": now.isoformat(),
        "talkers": len(talkers),
    }


def sync_full(db: Session, days: int = 30) -> Dict[str, Any]:
    client = ChatlogClient()
    now = datetime.now()
    start_date = (now - timedelta(days=max(1, days) - 1)).date()
    end_date = now.date()

    # get talkers from session list
    # Robust session fetch via client helper which already handles plain text/JSON variants
    try:
        session_payload = client.get_sessions()
        talkers = ChatlogClient.extract_talker_ids(session_payload)
    except Exception:
        talkers = []
    # Load filters once
    def _load_list(key: str) -> set[str]:
        row = db.get(SyncState, key)
        if not row or not row.value:
            return set()
        try:
            data = json.loads(row.value)
            if isinstance(data, list):
                return set(str(x) for x in data)
        except Exception:
            pass
        return set()
    bl_send = _load_list("blacklist_senders")
    bl_talk = _load_list("blacklist_talkers")
    wl_send = _load_list("whitelist_senders")
    wl_talk = _load_list("whitelist_talkers")
    has_wl = bool(wl_send or wl_talk)

    total_fetched = 0
    total_inserted = 0
    cur = start_date
    max_ts: Optional[datetime] = None
    while cur <= end_date:
        day = cur.isoformat()
        for talker in talkers:
            offset = 0
            while True:
                try:
                    raw = client.get_chatlog(time_range=day, talker=talker, limit=500, offset=offset)
                    part = _parse_messages(raw)
                    if not part:
                        break
                    total_fetched += len(part)
                    for m in part:
                        talkerName = m.get("talkerName")
                        sender = m.get("sender")
                        senderName = m.get("senderName")
                        isChatRoom = bool(m.get("isChatRoom"))
                        isSelf = bool(m.get("isSelf"))
                        type_ = m.get("type")
                        content = m.get("content") or m.get("text")
                        meta_payload: dict[str, Any] = {}
                        try:
                            contents = m.get("contents")
                            if isinstance(contents, dict):
                                # keep original link payload for downstream summarization
                                meta_payload["contents"] = contents
                        except Exception:
                            pass
                        time_str = m.get("time") or m.get("timestamp")
                        ts = None
                        if time_str:
                            try:
                                ts = datetime.fromisoformat(time_str)
                            except Exception:
                                ts = None
                        if ts and max_ts is None or (ts and ts > max_ts):
                            max_ts = ts
                        # filter by lists
                        if has_wl and not ((talker and talker in wl_talk) or (sender and sender in wl_send)):
                            continue
                        if (talker and talker in bl_talk) or (sender and sender in bl_send):
                            continue
                        if ts:
                            exists = db.execute(
                                select(Message.id).where(
                                    Message.chat_id == talker,
                                    Message.sender_id == sender,
                                    Message.timestamp == ts,
                                    Message.content_text == content,
                                )
                            ).scalar()
                            if exists:
                                continue
                        chat = db.get(Chat, talker)
                        if not chat:
                            chat = Chat(id=talker, title=talkerName or talker, is_chatroom=isChatRoom)
                            db.add(chat)
                        if sender:
                            c = db.get(Contact, sender)
                            if not c:
                                c = Contact(id=sender, name=senderName)
                                db.add(c)
                        msg = Message(
                            chat_id=talker,
                            sender_id=sender,
                            sender_name=senderName,
                            talker_name=talkerName,
                            timestamp=ts,
                            direction="out" if isSelf else "in",
                            type=str(type_) if type_ is not None else None,
                            content_text=content,
                            media_url=None,
                            meta=meta_payload,
                        )
                        db.add(msg)
                        total_inserted += 1
                        if chat and ts and (chat.last_message_at is None or ts > chat.last_message_at):
                            chat.last_message_at = ts
                            db.add(chat)
                    _safe_commit(db)
                    if len(part) < 500:
                        break
                    offset += 500
                except Exception:
                    db.rollback()
                    break
        cur = cur + timedelta(days=1)

    if max_ts:
        _set_last_sync(db, max_ts)
        _safe_commit(db)
    return {"status": "ok", "fetched": total_fetched, "inserted": total_inserted, "from": start_date.isoformat(), "to": end_date.isoformat(), "talkers": len(talkers)}

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
import time

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy import select, text

from ..models import Message, Chat, Contact, SyncState
import json
from .chatlog_client import ChatlogClient


def _to_local_naive(dt: Optional[datetime]) -> Optional[datetime]:
    """Normalize a datetime to local naive time.

    - If `dt` is timezone-aware, convert to local timezone and drop tzinfo
    - If `dt` is already naive, return as-is
    - If `dt` is None, return None
    """
    if dt is None:
        return None
    try:
        if dt.tzinfo is not None:
            return dt.astimezone().replace(tzinfo=None)
    except Exception:
        pass
    return dt


def _get_last_sync(db: Session) -> Optional[datetime]:
    row = db.get(SyncState, "chatlog_last_sync")
    if row and row.value:
        try:
            return _to_local_naive(datetime.fromisoformat(row.value))
        except Exception:
            return None
    # fallback to newest message timestamp (already stored as naive local)
    latest: Optional[datetime] = db.execute(select(Message.timestamp).order_by(Message.timestamp.desc())).scalar()
    return _to_local_naive(latest)


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

    # Always use local naive clock for comparison with DB/chatlog timestamps
    now = _to_local_naive(datetime.now()) or datetime.now()
    # Normalize since against last_sync to avoid re-pulling very old data when the caller passes an early since
    last_seen = _get_last_sync(db)
    # Normalize caller-provided cutoff as well
    since = _to_local_naive(since)
    if since is None:
        since = last_seen
    # if caller provided a very early since, clamp to last_seen - 10min (safety window to avoid boundary misses)
    if last_seen is not None and since is not None:
        safety = timedelta(minutes=10)
        min_since = last_seen - safety
        # Compare in the same naive-local domain
        try:
            if since < min_since:
                since = min_since
        except TypeError:
            # In case any tz-aware sneaks in, normalize and compare again
            s1 = _to_local_naive(since)
            s2 = _to_local_naive(min_since)
            if s1 is not None and s2 is not None and s1 < s2:
                since = s2
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
            # Engine-level transaction already committed; avoid session commit to prevent unrelated flush
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
                                ts = _to_local_naive(datetime.fromisoformat(time_str))
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
                                ts = _to_local_naive(datetime.fromisoformat(time_str))
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
        # Persist as ISO (may include offset if source had it); downstream will normalize
        _set_last_sync(db, max_ts)
        _safe_commit(db)
    return {"status": "ok", "fetched": total_fetched, "inserted": total_inserted, "from": start_date.isoformat(), "to": end_date.isoformat(), "talkers": len(talkers)}


def _normalize_chatlog_record(m: Dict[str, Any]) -> tuple[str | None, str | None, datetime | None, str]:
    """Return a comparison key (talker, sender, ts_local_naive, content_text)."""
    talker = m.get("talker") or m.get("chat_id")
    sender = m.get("sender") or m.get("sender_id")
    # time can be 'time' or 'timestamp' ISO string (with/without Z/offset)
    ts = None
    ts_str = m.get("time") or m.get("timestamp")
    if ts_str:
        try:
            ts = _to_local_naive(datetime.fromisoformat(ts_str))
        except Exception:
            ts = None
    content = m.get("content") or m.get("text") or ""
    return (talker or None, sender or None, ts, content or "")


def compare_with_chatlog(db: Session, *, days: int | None = None, date: str | None = None, fix: bool = False) -> Dict[str, Any]:
    """Compare messages in DB with chatlog for a date range and optionally repair.

    - If `date` (YYYY-MM-DD) is provided, compare that day only.
    - Else if `days` is provided, compare [now-days+1 .. now] inclusive.
    - If `fix=True`, insert missing_in_db items into DB.
    """
    if date:
        try:
            start_date = datetime.fromisoformat(date).date()
        except Exception as e:
            raise ValueError(f"invalid date: {date}")
        end_date = start_date
    else:
        d = max(1, int(days or 1))
        now = _to_local_naive(datetime.now()) or datetime.now()
        start_date = (now - timedelta(days=d - 1)).date()
        end_date = now.date()

    client = ChatlogClient()
    # Collect talkers once, but tolerate failures
    try:
        session_payload = client.get_sessions()
        talkers = ChatlogClient.extract_talker_ids(session_payload)
    except Exception:
        talkers = []

    summary: Dict[str, Any] = {"days": [], "totals": {"chatlog": 0, "db": 0, "missing_in_db": 0, "extra_in_db": 0}}
    details_sample: list[dict] = []

    cur = start_date
    while cur <= end_date:
        day = cur.isoformat()
        chatlog_keys: set[tuple[str | None, str | None, datetime | None, str]] = set()
        chatlog_records: list[Dict[str, Any]] = []

        if talkers:
            for t in talkers:
                offset = 0
                while True:
                    raw = None
                    try:
                        raw = client.get_chatlog(time_range=day, talker=t, limit=500, offset=offset)
                    except Exception:
                        break
                    part = _parse_messages(raw)
                    if not part:
                        break
                    for m in part:
                        key = _normalize_chatlog_record(m)
                        chatlog_keys.add(key)
                        chatlog_records.append(m)
                    if len(part) < 500:
                        break
                    offset += 500
        else:
            offset = 0
            while True:
                raw = None
                try:
                    raw = client.get_chatlog(time_range=day, limit=500, offset=offset)
                except Exception:
                    break
                part = _parse_messages(raw)
                if not part:
                    break
                for m in part:
                    key = _normalize_chatlog_record(m)
                    chatlog_keys.add(key)
                    chatlog_records.append(m)
                if len(part) < 500:
                    break
                offset += 500

        # DB keys in the same day window [day 00:00, day 23:59:59.999]
        start_dt = datetime.fromisoformat(day + "T00:00:00")
        end_dt = datetime.fromisoformat(day + "T23:59:59.999999")
        rows = db.execute(
            select(Message.chat_id, Message.sender_id, Message.timestamp, Message.content_text)
            .where(Message.timestamp >= start_dt, Message.timestamp <= end_dt)
        ).all()
        db_keys: set[tuple[str | None, str | None, datetime | None, str]] = set()
        for (chat_id, sender_id, ts, content_text) in rows:
            db_keys.add((chat_id, sender_id, _to_local_naive(ts), content_text or ""))

        missing = chatlog_keys - db_keys
        extra = db_keys - chatlog_keys

        # Sample up to 50 missing for UI display
        sample_count = 0
        if missing:
            # Fast lookup from key to raw chatlog record
            index = {}
            for m in chatlog_records:
                index[_normalize_chatlog_record(m)] = m
            for key in list(missing):
                m = index.get(key)
                if not m:
                    continue
                details_sample.append({
                    "day": day,
                    "chat_id": m.get("talker") or m.get("chat_id"),
                    "sender_id": m.get("sender") or m.get("sender_id"),
                    "timestamp": m.get("time") or m.get("timestamp"),
                    "content": m.get("content") or m.get("text") or "",
                })
                sample_count += 1
                if sample_count >= 50:
                    break

        # Optional repair: insert missing messages
        repaired = 0
        if fix and missing:
            index = { _normalize_chatlog_record(m): m for m in chatlog_records }
            created_chats: set[str] = set()
            created_contacts: set[str] = set()
            # Use engine-level transaction to bypass ORM pending flush
            from ..db import engine as _engine
            with _engine.begin() as conn:
                for key in missing:
                    m = index.get(key)
                    if not m:
                        continue
                    talker = m.get("talker") or m.get("chat_id")
                    if not talker:
                        continue
                    talkerName = m.get("talkerName")
                    sender = m.get("sender")
                    senderName = m.get("senderName")
                    isChatRoom = 1 if bool(m.get("isChatRoom")) else 0
                    isSelf = bool(m.get("isSelf"))
                    type_ = m.get("type")
                    content = m.get("content") or m.get("text") or ""
                    meta_payload: dict[str, Any] = {}
                    contents = m.get("contents")
                    if isinstance(contents, dict):
                        meta_payload["contents"] = contents
                    ts = None
                    ts_str = m.get("time") or m.get("timestamp")
                    if ts_str:
                        try:
                            ts = _to_local_naive(datetime.fromisoformat(ts_str))
                        except Exception:
                            ts = None
                    if talker not in created_chats:
                        if not bool(conn.execute(text("SELECT 1 FROM chats WHERE id=:id"), {"id": talker}).first()):
                            conn.execute(
                                text("INSERT OR IGNORE INTO chats (id, title, type, is_chatroom, last_message_at) VALUES (:id, :title, NULL, :is_chatroom, NULL)"),
                                {"id": talker, "title": (talkerName or talker), "is_chatroom": isChatRoom},
                            )
                        created_chats.add(talker)
                    if sender and (sender not in created_contacts):
                        if not bool(conn.execute(text("SELECT 1 FROM contacts WHERE id=:id"), {"id": sender}).first()):
                            conn.execute(
                                text("INSERT OR IGNORE INTO contacts (id, name, alias, rating, labels, stats) VALUES (:id, :name, NULL, 50, NULL, NULL)"),
                                {"id": sender, "name": senderName},
                            )
                        created_contacts.add(sender)
                    if ts is not None:
                        conn.execute(
                            text("UPDATE chats SET last_message_at = :ts WHERE id = :id AND (last_message_at IS NULL OR last_message_at < :ts)"),
                            {"id": talker, "ts": ts},
                        )
                    conn.execute(
                        text(
                            """
                            INSERT INTO messages (
                                chat_id, sender_id, sender_name, talker_name,
                                timestamp, direction, type, content_text,
                                media_url, meta, tags, derived,
                                importance_score, upvotes, downvotes, ai_suggestions, send_status
                            ) VALUES (
                                :chat_id, :sender_id, :sender_name, :talker_name,
                                :timestamp, :direction, :type, :content_text,
                                NULL, :meta, NULL, NULL,
                                50, 0, 0, NULL, NULL
                            )
                            """
                        ),
                        {
                            "chat_id": talker,
                            "sender_id": sender,
                            "sender_name": senderName,
                            "talker_name": talkerName,
                            "timestamp": ts,
                            "direction": ("out" if isSelf else "in"),
                            "type": (str(type_) if type_ is not None else None),
                            "content_text": content,
                            "meta": json.dumps(meta_payload, ensure_ascii=False),
                        },
                    )
                    repaired += 1

        day_summary = {
            "day": day,
            "chatlog": len(chatlog_keys),
            "db": len(db_keys),
            "missing_in_db": len(missing),
            "extra_in_db": len(extra),
            "repaired": repaired,
        }
        summary["days"].append(day_summary)
        summary["totals"]["chatlog"] += len(chatlog_keys)
        summary["totals"]["db"] += len(db_keys)
        summary["totals"]["missing_in_db"] += len(missing)
        summary["totals"]["extra_in_db"] += len(extra)

        cur = cur + timedelta(days=1)

    summary["sample_missing"] = details_sample
    return summary

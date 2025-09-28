from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AnalysisSnapshot, Message, Contact
from .message_filters import filter_effective_messages


DEFAULT_PERIODS: Tuple[str, ...] = ("1day", "3days", "1week", "1month")


def _normalize_ids(message_ids: Optional[Iterable[int]]) -> List[int]:
    if not message_ids:
        return []
    normalized: List[int] = []
    for mid in message_ids:
        try:
            value = int(mid)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        normalized.append(value)
    return sorted(set(normalized))


def _scope_key(message_ids: List[int], filters: Optional[Dict[str, Any]]) -> str:
    payload = {
        "ids": message_ids,
        "filters": filters or {},
    }
    data = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha1(data.encode("utf-8")).hexdigest()


def _period_to_cutoff(period: Optional[str]) -> Optional[datetime]:
    if not period:
        return None
    period = period.lower()
    now = datetime.utcnow()
    mapping = {
        "1day": timedelta(days=1),
        "3days": timedelta(days=3),
        "1week": timedelta(weeks=1),
        "1month": timedelta(days=30),
    }
    delta = mapping.get(period)
    if not delta:
        return None
    return now - delta


def _message_to_dict(msg: Message) -> Dict[str, Any]:
    ts = msg.timestamp.isoformat() if msg.timestamp else None
    return {
        "id": msg.id,
        "chat_id": msg.chat_id,
        "sender_id": msg.sender_id,
        "sender_name": msg.sender_name,
        "talker_name": msg.talker_name,
        "timestamp": ts,
        "time": ts,
        "direction": msg.direction,
        "message_type": msg.type,
        "type": msg.type,
        "content": msg.content_text,
        "content_text": msg.content_text,
        "media_url": msg.media_url,
        "meta": msg.meta or {},
        "tags": msg.tags or {},
        "derived": msg.derived or {},
        "importance_score": msg.importance_score,
        "upvotes": msg.upvotes,
        "downvotes": msg.downvotes,
        "send_status": msg.send_status,
    }


def _collect_contacts(db: Session, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    sender_ids = {m.get("sender_id") for m in messages if m.get("sender_id")}
    if not sender_ids:
        return {}
    contacts = db.execute(select(Contact).where(Contact.id.in_(sender_ids))).scalars().all()
    data: Dict[str, Any] = {}
    for contact in contacts:
        data[contact.id] = {
            "rating": contact.rating,
            "name": contact.name,
            "alias": contact.alias,
            "labels": contact.labels or {},
        }
    return data


def _time_range(messages: List[Dict[str, Any]]) -> Tuple[Optional[datetime], Optional[datetime]]:
    times: List[datetime] = []
    for msg in messages:
        ts = msg.get("timestamp") or msg.get("time")
        if not ts:
            continue
        if isinstance(ts, datetime):
            times.append(ts)
            continue
        text = str(ts)
        if text.endswith("Z"):
            text = text[:-1]
        try:
            times.append(datetime.fromisoformat(text))
        except Exception:
            continue
    if not times:
        return None, None
    return min(times), max(times)


def collect_messages(
    db: Session,
    message_ids: Optional[Iterable[int]] = None,
    filters: Optional[Dict[str, Any]] = None,
) -> List[Message]:
    ids = _normalize_ids(message_ids)
    query = select(Message)
    if ids:
        query = query.where(Message.id.in_(ids))
    period = (filters or {}).get("period") if filters else None
    cutoff = _period_to_cutoff(period)
    if cutoff:
        query = query.where(Message.timestamp >= cutoff)
    # If filters specify direction or others in future, extend here.
    if not ids:
        query = query.order_by(Message.timestamp.desc()).limit(2000)
    else:
        query = query.order_by(Message.timestamp.asc())
    rows = db.execute(query).scalars().all()
    # ensure chronological order
    rows.sort(key=lambda m: (m.timestamp or datetime.min))
    # Apply the same filters as message list to guarantee consistency
    raw_rows = [
        {
            "id": m.id,
            "chat_id": m.chat_id,
            "sender_id": m.sender_id,
            "sender_name": m.sender_name,
            "talker_name": m.talker_name,
            "timestamp": m.timestamp,
            "direction": m.direction,
            "type": m.type,
            "content_text": m.content_text,
            "media_url": m.media_url,
            "tags": m.tags,
            "derived": m.derived,
        }
        for m in rows
    ]
    f = filters or {}
    eff = list(
        filter_effective_messages(
            raw_rows,
            external_only=bool(f.get("external_only", True)),
            exclude_short=bool(f.get("exclude_short", True)),
            exclude_system=bool(f.get("exclude_system", True)),
        )
    )
    id_map = {r["id"] for r in eff}
    rows = [m for m in rows if m.id in id_map]
    return rows


def upsert_snapshot(
    db: Session,
    *,
    message_ids: Optional[Iterable[int]] = None,
    filters: Optional[Dict[str, Any]] = None,
    options: Optional[Dict[str, Any]] = None,
    title: Optional[str] = None,
) -> AnalysisSnapshot:
    ids = _normalize_ids(message_ids)
    scope = _scope_key(ids, filters)
    snapshot = db.execute(
        select(AnalysisSnapshot).where(AnalysisSnapshot.scope_key == scope)
    ).scalar_one_or_none()

    messages = collect_messages(db, ids, filters)
    payload_messages = [_message_to_dict(m) for m in messages]
    contact_ratings = _collect_contacts(db, payload_messages)
    time_from, time_to = _time_range(payload_messages)

    meta = {
        "total_messages": len(payload_messages),
        "generated_at": datetime.utcnow().isoformat(),
        "period": (filters or {}).get("period") if filters else None,
    }

    if snapshot is None:
        snapshot = AnalysisSnapshot(
            scope_key=scope,
            title=title,
            filters=filters,
            options=options,
            message_ids=ids,
            messages=payload_messages,
            contact_ratings=contact_ratings,
            meta=meta,
            status="ready",
            message_count=len(payload_messages),
            time_from=time_from,
            time_to=time_to,
        )
        db.add(snapshot)
    else:
        snapshot.title = title or snapshot.title
        snapshot.filters = filters
        snapshot.options = options
        snapshot.message_ids = ids
        snapshot.messages = payload_messages
        snapshot.contact_ratings = contact_ratings
        snapshot.meta = meta
        snapshot.status = "ready"
        snapshot.message_count = len(payload_messages)
        snapshot.time_from = time_from
        snapshot.time_to = time_to
        snapshot.updated_at = datetime.utcnow()

    return snapshot


def refresh_default_snapshots(db: Session) -> List[AnalysisSnapshot]:
    snapshots: List[AnalysisSnapshot] = []
    for period in DEFAULT_PERIODS:
        snap = upsert_snapshot(db, filters={"period": period})
        snapshots.append(snap)
    return snapshots


__all__ = ["collect_messages", "upsert_snapshot", "refresh_default_snapshots"]

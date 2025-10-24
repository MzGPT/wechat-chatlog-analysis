from __future__ import annotations

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy import select, func, text, or_
from sqlalchemy.orm import Session
from typing import Optional
from ..db import session_scope, SessionLocal
from ..models import Message, Interaction, InteractionExt
from ..schemas import PaginatedMessages, MessageOut, UpDownVoteResult, TagUpdateIn, MessageDeriveRequest
from ..services.ai_tools import ensure_message_features
from ..services.llm_client import load_ai_config
from ..services.message_filters import filter_effective_messages
from starlette.responses import Response
from typing import Dict, Any, List
import csv, io, html, json
from datetime import datetime, timedelta, timezone


router = APIRouter(prefix="/api/messages", tags=["messages"])

# simple in-memory progress for derive tasks
PROGRESS: Dict[str, Dict[str, Any]] = {}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        # ensure proper close even if generator exits early
        db.close()


@router.get("", response_model=PaginatedMessages)
def list_messages(
    q: Optional[str] = Query(default=None, description="full text query"),
    chat_id: Optional[str] = None,
    sender_id: Optional[str] = None,
    type: Optional[str] = Query(default=None, alias="msg_type"),
    direction: Optional[str] = None,
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    page: int = 1,
    size: int = 50,
    db: Session = Depends(get_db),
):
    page = max(1, page)
    size = max(1, min(1000, size))

    direction = (direction or "").strip().lower() or None
    if direction == "external":
        direction = "in"
    if direction and direction not in {"in", "out"}:
        direction = None

    if q:
        # Use FTS5 when q exists. IMPORTANT: apply the same filters as non-FTS path
        # so search respects chat_id/sender_id/type/time/direction consistently.
        def _parse_dt(v: Optional[str]) -> Optional[datetime]:
            if not v:
                return None
            try:
                if len(v) == 10:
                    return datetime.fromisoformat(v + "T00:00:00")
                text_v = v.replace("Z", "+00:00")
                dt = datetime.fromisoformat(text_v)
                if dt.tzinfo is not None:
                    dt = dt.astimezone().replace(tzinfo=None)
                return dt
            except Exception:
                return None

        clauses: list[str] = ["messages_fts MATCH :q"]
        params = {"q": q, "limit": size, "offset": (page - 1) * size}

        if chat_id:
            clauses.append("m.chat_id = :chat_id")
            params["chat_id"] = chat_id
        if sender_id:
            clauses.append("m.sender_id = :sender_id")
            params["sender_id"] = sender_id
        if type:
            clauses.append("m.type = :type")
            params["type"] = type
        # direction: keep historic compatibility where NULL/'' treated as inbound
        if direction == "in":
            clauses.append("(m.direction = 'in' OR m.direction IS NULL OR m.direction = '')")
        elif direction == "out":
            clauses.append("m.direction = 'out'")

        dt_from = _parse_dt(time_from)
        dt_to = _parse_dt(time_to)
        if dt_from:
            clauses.append("m.timestamp >= :dt_from")
            params["dt_from"] = dt_from
        if dt_to:
            clauses.append("m.timestamp <= :dt_to")
            params["dt_to"] = dt_to

        where_sql = " AND ".join(clauses) if clauses else "1=1"
        base_sql = (
            "SELECT m.* FROM messages m "
            "JOIN messages_fts fts ON fts.rowid = m.id "
            f"WHERE {where_sql} "
        )
        fts_sql = text(base_sql + "ORDER BY m.timestamp DESC LIMIT :limit OFFSET :offset")
        count_sql = text(base_sql.replace("SELECT m.*", "SELECT COUNT(1) as cnt").replace(" ORDER BY m.timestamp DESC LIMIT :limit OFFSET :offset", ""))

        items = db.execute(fts_sql, params).mappings().all()
        ids = [row["id"] for row in items if row.get("id") is not None]
        derived_map: dict[int, dict | None] = {}
        # 为了保证列表接口快速稳定，这里不触发小模型派生；
        # 派生由前端在进入页面或点击“拉取”时调用 /api/messages/derive 完成
        total = db.execute(count_sql, {k: v for k, v in params.items() if k != "limit" and k != "offset"}).scalar() or 0
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

        data = []
        for row in items:
            rd = dict(row)
            msg_id = rd.get("id")
            if msg_id in derived_map:
                rd["derived"] = derived_map[msg_id]
            
            # Fix JSON field parsing for FTS queries
            # FTS queries return JSON fields as strings, need to parse them
            try:
                if isinstance(rd.get("meta"), str):
                    rd["meta"] = json.loads(rd["meta"]) if rd["meta"] else None
            except (json.JSONDecodeError, TypeError):
                rd["meta"] = None
                
            try:
                if isinstance(rd.get("derived"), str):
                    rd["derived"] = json.loads(rd["derived"]) if rd["derived"] else None
            except (json.JSONDecodeError, TypeError):
                rd["derived"] = None
                
            try:
                if isinstance(rd.get("tags"), str):
                    rd["tags"] = json.loads(rd["tags"]) if rd["tags"] else None
            except (json.JSONDecodeError, TypeError):
                rd["tags"] = None
            
            # include raw meta to allow frontend to render link/image badges
            if rd.get("meta") is None and hasattr(row, 'meta'):
                try:
                    rd["meta"] = row["meta"]
                except Exception:
                    pass
            # 兼容旧前端：回填 key_info/key_info_origin
            try:
                d = rd.get("derived") or {}
                if isinstance(d, dict):
                    if (d.get("summary_full") or d.get("summary")) and not d.get("key_info"):
                        d["key_info"] = d.get("summary_full") or d.get("summary") or ""
                    if d.get("summary_origin") and not d.get("key_info_origin"):
                        d["key_info_origin"] = d.get("summary_origin")
                    # Compose display-only summary from meeting_number/platform/key_info
                    d["display_summary"] = _compose_display_summary(d)
                    rd["derived"] = d
            except Exception:
                pass
            data.append(MessageOut(**rd))
        return {"total": int(total), "items": data}

    query = select(Message)
    if chat_id:
        query = query.where(Message.chat_id == chat_id)
    if sender_id:
        query = query.where(Message.sender_id == sender_id)
    if type:
        query = query.where(Message.type == type)
    if direction:
        if direction == "in":
            # 兼容历史数据：direction 为空/NULL 视作 "in"
            query = query.where(or_(Message.direction == "in", Message.direction == None, Message.direction == ""))
        else:
            query = query.where(Message.direction == direction)
    def _parse_dt(v: Optional[str]) -> Optional[datetime]:
        """Parse ISO-like datetime strings from the frontend.

        - Accepts date-only (YYYY-MM-DD) and full ISO timestamps (with/without Z/offset).
        - If timezone-aware, convert to local naive time to match DB stored timestamps
          (DB stores naive local times coming from chatlog). This avoids missing latest
          data due to UTC/local mismatches.
        """
        if not v:
            return None
        try:
            # support date-only like YYYY-MM-DD
            if len(v) == 10:
                return datetime.fromisoformat(v + "T00:00:00")
            text = v.replace("Z", "+00:00")  # allow trailing Z from toISOString()
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is not None:
                # convert to local naive time
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
        except Exception:
            return None

    dt_from = _parse_dt(time_from)
    dt_to = _parse_dt(time_to)
    if dt_from:
        query = query.where(Message.timestamp >= dt_from)
    if dt_to:
        query = query.where(Message.timestamp <= dt_to)

    total = db.execute(select(func.count()).select_from(query.subquery())).scalar() or 0
    items = db.execute(query.order_by(Message.timestamp.desc()).limit(size).offset((page - 1) * size)).scalars().all()
    # 同理：列表接口不做派生，避免阻塞首屏。/derive 负责派生。
    # try:
    #     conf = load_ai_config()
    #     adv = conf.get("analysis_defaults") or {}
    #     concurrency = int(adv.get("concurrency") or 8)
    # except Exception:
    #     concurrency = 8
    # ensure_message_features(db, list(items), concurrency=concurrency)
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

    compat_items: list[MessageOut] = []
    for i in items:
        out = MessageOut.model_validate(i)
        try:
            d = dict(out.derived or {})
            if (d.get("summary_full") or d.get("summary")) and not d.get("key_info"):
                d["key_info"] = d.get("summary_full") or d.get("summary") or ""
            if d.get("summary_origin") and not d.get("key_info_origin"):
                d["key_info_origin"] = d.get("summary_origin")
            d["display_summary"] = _compose_display_summary(d)
            out.derived = d
        except Exception:
            pass
        compat_items.append(out)
    return {"total": int(total), "items": compat_items}


@router.get("/effective", response_model=PaginatedMessages)
def list_effective_messages(
    period: str | None = Query(default=None, description="1day/3days/1week/1month"),
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    chat_id: Optional[str] = None,
    sender_id: Optional[str] = None,
    type: Optional[str] = Query(default=None, alias="msg_type"),
    page: int = 1,
    size: int = 1000,
    external_only: Optional[bool] = None,
    exclude_short: Optional[bool] = None,
    exclude_system: Optional[bool] = None,
    db: Session = Depends(get_db),
):
    page = max(1, page)
    size = max(1, min(2000, size))

    query = select(Message)
    if chat_id:
        query = query.where(Message.chat_id == chat_id)
    if sender_id:
        query = query.where(Message.sender_id == sender_id)
    if type:
        query = query.where(Message.type == type)

    def _parse_dt(v: Optional[str]) -> Optional[datetime]:
        if not v:
            return None
        try:
            if len(v) == 10:
                return datetime.fromisoformat(v + "T00:00:00")
            text = v.replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is not None:
                # convert to local naive to align with DB naive timestamps
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
        except Exception:
            return None

    def _cutoff_for_period(p: Optional[str]) -> Optional[datetime]:
        if not p:
            return None
        p = p.lower()
        mapping = {
            "1day": timedelta(days=1),
            "3days": timedelta(days=3),
            "1week": timedelta(weeks=1),
            "1month": timedelta(days=30),
        }
        delta = mapping.get(p)
        if not delta:
            return None
        return datetime.utcnow() - delta

    dt_from = _parse_dt(time_from)
    dt_to = _parse_dt(time_to)
    if period and not dt_from:
        dt_from = _cutoff_for_period(period)
    if dt_from:
        query = query.where(Message.timestamp >= dt_from)
    if dt_to:
        query = query.where(Message.timestamp <= dt_to)

    # Fetch a window and apply uniform backend filters to keep consistency with AI snapshot
    base_items = db.execute(query.order_by(Message.timestamp.desc()).limit(10000)).scalars().all()
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
            "importance_score": m.importance_score,
            "upvotes": m.upvotes,
            "downvotes": m.downvotes,
            "send_status": m.send_status,
        }
        for m in base_items
    ]
    # default filter switches from config if not specified
    try:
        conf = load_ai_config()
        mf = conf.get("message_filters") or {}
    except Exception:
        mf = {}
    eo = external_only if external_only is not None else bool(mf.get("external_only", True))
    es = exclude_short if exclude_short is not None else bool(mf.get("exclude_short", True))
    sy = exclude_system if exclude_system is not None else bool(mf.get("exclude_system", True))

    filtered = list(
        filter_effective_messages(
            raw_rows,
            external_only=eo,
            exclude_short=es,
            exclude_system=sy,
        )
    )
    total = len(filtered)
    page_slice = filtered[(page - 1) * size : (page - 1) * size + size]
    id_map = {row["id"] for row in page_slice}
    orm_page = []
    if id_map:
        orm_page = db.execute(select(Message).where(Message.id.in_(id_map))).scalars().all()
        orm_page.sort(key=lambda m: m.timestamp or datetime.min, reverse=True)
    compat_items: list[MessageOut] = []
    for i in orm_page:
        out = MessageOut.model_validate(i)
        try:
            d = dict(out.derived or {})
            if (d.get("summary_full") or d.get("summary")) and not d.get("key_info"):
                d["key_info"] = d.get("summary_full") or d.get("summary") or ""
            if d.get("summary_origin") and not d.get("key_info_origin"):
                d["key_info_origin"] = d.get("summary_origin")
            out.derived = d
        except Exception:
            pass
        compat_items.append(out)
    return {"total": int(total), "items": compat_items}


@router.post("/{message_id}/upvote", response_model=UpDownVoteResult)
def upvote(message_id: int, db: Session = Depends(get_db)):
    msg = db.get(Message, message_id)
    if not msg:
        raise HTTPException(404, "message not found")
    msg.upvotes += 1
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return UpDownVoteResult(id=msg.id, upvotes=msg.upvotes, downvotes=msg.downvotes)


@router.post("/{message_id}/downvote", response_model=UpDownVoteResult)
def downvote(message_id: int, db: Session = Depends(get_db)):
    msg = db.get(Message, message_id)
    if not msg:
        raise HTTPException(404, "message not found")
    msg.downvotes += 1
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return UpDownVoteResult(id=msg.id, upvotes=msg.upvotes, downvotes=msg.downvotes)


@router.post("/{message_id}/tags")
def update_tags(message_id: int, body: TagUpdateIn, db: Session = Depends(get_db)):
    msg = db.get(Message, message_id)
    if not msg:
        raise HTTPException(404, "message not found")
    msg.tags = body.tags
    db.add(msg)
    db.commit()
    return {"id": msg.id, "tags": msg.tags}


@router.post("/{message_id}/interact")
def interact(message_id: int, kind: str, db: Session = Depends(get_db)):
    if kind not in ("约","问","答","顶","踩"):
        raise HTTPException(400, "invalid kind")
    if not db.get(Message, message_id):
        raise HTTPException(404, "message not found")
    it = Interaction(message_id=message_id, kind=kind, payload=None)
    db.add(it)
    db.commit()
    return {"status": "ok", "id": it.id}


@router.post("/interact-ext")
def interact_ext(kind: str, payload: dict | None = None, db: Session = Depends(get_db)):
    if kind not in ("约","问","答","顶","踩"):
        raise HTTPException(400, "invalid kind")
    it = InteractionExt(kind=kind, payload=payload or {})
    db.add(it)
    db.commit()
    return {"status": "ok", "id": it.id}


@router.get("/export")
def export_messages(
    format: str = "csv",
    q: Optional[str] = None,
    chat_id: Optional[str] = None,
    sender_id: Optional[str] = None,
    type: Optional[str] = Query(default=None, alias="msg_type"),
    time_from: Optional[str] = None,
    time_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    # reuse list logic (without pagination)
    def _parse_dt(v: Optional[str]):
        # accept YYYY-MM-DD, ISO timestamps and trailing Z; normalize to naive UTC
        if not v:
            return None
        try:
            if len(v) == 10:
                return datetime.fromisoformat(v+"T00:00:00")
            text = v.replace("Z", "+00:00")
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except Exception:
            return None

    items: list[MessageOut]
    if q:
        # Apply same filters in FTS branch as non-FTS branch so exports match UI filters
        clauses: list[str] = ["messages_fts MATCH :q"]
        params: dict[str, Any] = {"q": q}
        if chat_id:
            clauses.append("m.chat_id = :chat_id")
            params["chat_id"] = chat_id
        if sender_id:
            clauses.append("m.sender_id = :sender_id")
            params["sender_id"] = sender_id
        if type:
            clauses.append("m.type = :type")
            params["type"] = type
        dt_from = _parse_dt(time_from)
        dt_to = _parse_dt(time_to)
        if dt_from:
            clauses.append("m.timestamp >= :dt_from")
            params["dt_from"] = dt_from
        if dt_to:
            clauses.append("m.timestamp <= :dt_to")
            params["dt_to"] = dt_to
        where_sql = " AND ".join(clauses) if clauses else "1=1"
        fts_sql = text(
            "SELECT m.* FROM messages m "
            "JOIN messages_fts fts ON fts.rowid = m.id "
            f"WHERE {where_sql} "
            "ORDER BY m.timestamp DESC"
        )
        rows = db.execute(fts_sql, params).mappings().all()
        items = []
        for r in rows:
            rd = dict(r)
            # Fix JSON field parsing for FTS queries
            try:
                if isinstance(rd.get("meta"), str):
                    rd["meta"] = json.loads(rd["meta"]) if rd["meta"] else None
            except (json.JSONDecodeError, TypeError):
                rd["meta"] = None
                
            try:
                if isinstance(rd.get("derived"), str):
                    rd["derived"] = json.loads(rd["derived"]) if rd["derived"] else None
            except (json.JSONDecodeError, TypeError):
                rd["derived"] = None
                
            try:
                if isinstance(rd.get("tags"), str):
                    rd["tags"] = json.loads(rd["tags"]) if rd["tags"] else None
            except (json.JSONDecodeError, TypeError):
                rd["tags"] = None
            
            items.append(MessageOut(**rd))
    else:
        query = select(Message)
        if chat_id:
            query = query.where(Message.chat_id == chat_id)
        if sender_id:
            query = query.where(Message.sender_id == sender_id)
        if type:
            query = query.where(Message.type == type)
        dt_from = _parse_dt(time_from)
        dt_to = _parse_dt(time_to)
        if dt_from:
            query = query.where(Message.timestamp >= dt_from)
        if dt_to:
            query = query.where(Message.timestamp <= dt_to)
        rows = db.execute(query.order_by(Message.timestamp.desc())).scalars().all()
        items = [MessageOut.model_validate(r) for r in rows]

    # build output
    fn = f"messages.{format}"
    if format == "csv":
        sio = io.StringIO()
        w = csv.writer(sio)
        w.writerow(["id","time","chat_id","talker_name","sender_id","sender_name","type","content"])
        for m in items:
            w.writerow([
                m.id,
                m.timestamp.isoformat() if m.timestamp else "",
                m.chat_id or "",
                m.talker_name or "",
                m.sender_id or "",
                m.sender_name or "",
                m.type or "",
                (m.content_text or "").replace("\n"," ")
            ])
        data = sio.getvalue()
        return Response(data, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename={fn}"})
    else:
        # html/xls use table
        rows = []
        for m in items:
            rows.append(
                f"<tr><td>{m.id}</td><td>{html.escape(m.timestamp.isoformat() if m.timestamp else '')}</td>"
                f"<td>{html.escape(m.chat_id or '')}</td><td>{html.escape(m.talker_name or '')}</td>"
                f"<td>{html.escape(m.sender_name or m.sender_id or '')}</td><td>{html.escape(m.type or '')}</td>"
                f"<td>{html.escape((m.content_text or '')[:200])}</td></tr>"
            )
        table = """
        <table border="1" cellspacing="0" cellpadding="4">
        <thead><tr><th>ID</th><th>时间</th><th>会话</th><th>对象</th><th>发送人</th><th>类型</th><th>内容</th></tr></thead>
        <tbody>{rows}</tbody></table>
        """.replace("{rows}", "\n".join(rows))
        mt = "text/html; charset=utf-8" if format == "html" else "application/vnd.ms-excel"
        ext = "html" if format == "html" else "xls"
        return Response(table, media_type=mt, headers={"Content-Disposition": f"attachment; filename=messages.{ext}"})


@router.post("/derive")
def derive_message_features(body: MessageDeriveRequest, progress_key: str | None = None, db: Session = Depends(get_db)):
    query = select(Message)
    if body.message_ids:
        query = query.where(Message.id.in_(body.message_ids))
    else:
        period = (body.period or "").lower()
        period_mapping = {
            "1day": 1,
            "3days": 3,
            "1week": 7,
            "1month": 30,
        }
        days = period_mapping.get(period)
        if days:
            since = datetime.utcnow() - timedelta(days=days)
            query = query.where(Message.timestamp >= since)
        if body.limit:
            query = query.order_by(Message.timestamp.desc()).limit(max(1, body.limit))
        elif not days:
            query = query.order_by(Message.timestamp.desc()).limit(500)
        else:
            query = query.order_by(Message.timestamp.desc())

    messages: List[Message] = db.execute(query).scalars().all()
    if not messages:
        if progress_key:
            PROGRESS[progress_key] = {"status": "done", "total": 0, "done": 0}
            return {"status": "ok", "updated": 0, "progress_key": progress_key}
        return {"status": "ok", "updated": 0}

    # fallback to configured defaults if fields not provided
    try:
        conf = load_ai_config()
        dd = conf.get("derive_defaults") or {}
    except Exception:
        dd = {}
    if body.batch_size is None:
        body.batch_size = int(dd.get("batch_size", 20))
    if body.concurrency is None:
        body.concurrency = int(dd.get("concurrency", 8))
    if body.temperature is None:
        body.temperature = float(dd.get("temperature", 0.1))
    if body.force is None:
        body.force = bool(dd.get("force", False))

    if progress_key:
        PROGRESS[progress_key] = {
            "status": "running",
            "total": len(messages),
            "done": 0,
        }
        # When explicitly deriving selected messages, default to force=True to bypass age cutoffs
        try:
            if (body.message_ids and len(body.message_ids) > 0):
                body.force = True
        except Exception:
            body.force = True
        # process in chunks to report progress
        bs = max(1, int(body.batch_size or 20))
        idx = 0
        while idx < len(messages):
            chunk = messages[idx : idx + bs]
            ensure_message_features(
                db,
                chunk,
                force=body.force,
                batch_size=bs,
                concurrency=body.concurrency,
                temperature=body.temperature,
            )
            PROGRESS[progress_key]["done"] = min(len(messages), idx + len(chunk))
            idx += bs
        PROGRESS[progress_key]["status"] = "done"
        return {"status": "ok", "updated": len(messages), "progress_key": progress_key}

    # When explicitly deriving selected messages (non-progress path), also force tool overlay by default
    ensure_message_features(
        db,
        messages,
        force=(True if (body.message_ids and len(body.message_ids) > 0) else body.force),
        batch_size=body.batch_size,
        concurrency=body.concurrency,
        temperature=body.temperature,
    )
    return {"status": "ok", "updated": len(messages)}


@router.get("/derive/progress")
def derive_progress(key: str):
    info = PROGRESS.get(key)
    if not info:
        return {"status": "unknown", "done": 0, "total": 0}
    return {"status": info.get("status"), "done": info.get("done"), "total": info.get("total")}


@router.get("/by-ids", response_model=PaginatedMessages)
def get_messages_by_ids(ids: str, db: Session = Depends(get_db)):
    try:
        id_list = [int(x) for x in ids.split(',') if x.strip().isdigit()]
    except Exception:
        id_list = []
    if not id_list:
        return {"total": 0, "items": []}
    rows = db.execute(select(Message).where(Message.id.in_(id_list))).scalars().all()
    # keep same order
    rows.sort(key=lambda m: (m.timestamp or datetime.min), reverse=True)

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

    items: list[MessageOut] = []
    for i in rows:
        out = MessageOut.model_validate(i)
        try:
            d = dict(out.derived or {})
            if (d.get("summary_full") or d.get("summary")) and not d.get("key_info"):
                d["key_info"] = d.get("summary_full") or d.get("summary") or ""
            if d.get("summary_origin") and not d.get("key_info_origin"):
                d["key_info_origin"] = d.get("summary_origin")
            d["display_summary"] = _compose_display_summary(d)
            out.derived = d
        except Exception:
            pass
        items.append(out)
    return {"total": len(rows), "items": items}

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select
from typing import List, Any
from ..db import SessionLocal
from ..models import Message, Task, Report, ReportArtifact
from ..schemas import AIReplyRequest, TaskOut
from ..services.n8n_client import N8NClient
from ..services.llm_client import (
    load_ai_config,
    save_ai_config,
    siliconflow_chat,
    siliconflow_tool_chat,
    DEFAULT_MODULE_PROMPTS,
    DEFAULT_TOOL_PROMPTS,
)
from ..services.ai_tools import extract_message_features, build_ai_input_messages
from ..services.report_artifacts import build_artifact_payloads
from ..services.snapshot_service import upsert_snapshot
import os
import html
import json
import re


router = APIRouter(prefix="/api/ai", tags=["ai"])


def _strip_llm_thoughts(text: str) -> str:
    if not isinstance(text, str):
        return text
    cleaned = text
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", cleaned, flags=re.IGNORECASE)
    for marker in ("<section", "<article", "<div", "<table", "<ol", "<ul", "<p>"):
        idx = cleaned.lower().find(marker)
        if idx > 0:
            cleaned = cleaned[idx:]
            break
    lines = cleaned.splitlines()
    filtered: list[str] = []
    skipping = True
    trigger_keywords = ("思考", "推理", "分析", "chain of thought", "reasoning")
    for line in lines:
        stripped = line.strip()
        if skipping:
            if not stripped:
                continue
            lower = stripped.lower()
            if any(keyword in lower for keyword in trigger_keywords) and not stripped.startswith("<"):
                continue
            skipping = False
        filtered.append(line)
    cleaned = "\n".join(filtered).lstrip()
    return cleaned


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.post("/suggest-replies", response_model=TaskOut)
def suggest_replies(body: AIReplyRequest, db: Session = Depends(get_db)):
    msgs: List[Message] = db.scalars(select(Message).where(Message.id.in_(body.message_ids))).all()
    if not msgs:
        raise HTTPException(400, "no messages found")

    ctx = {
        "request_id": f"reply-{','.join(map(str, body.message_ids))}",
        "context": {
            "messages": [
                {
                    "id": m.id,
                    "text": m.content_text,
                    "sender": m.sender_name or m.sender_id,
                    "ts": m.timestamp.isoformat() if m.timestamp else None,
                }
                for m in msgs
            ]
        },
        "prompt_hint": body.prompt_hint,
    }

    task = Task(type="ai_reply", payload=ctx, status="pending")
    db.add(task)
    db.commit()
    db.refresh(task)

    client = N8NClient()
    try:
        result = client.suggest_replies(ctx)
        task.status = "done"
        task.result = result
        db.add(task)
        db.commit()
        db.refresh(task)
    except Exception as e:
        db.rollback()
        task.status = "failed"
        task.result = {"error": str(e)}
        db.add(task)
        db.commit()
        db.refresh(task)

    return TaskOut(id=task.id, type=task.type, status=task.status, result=task.result)


@router.post("/summary", response_model=TaskOut)
def summary(payload: dict, db: Session = Depends(get_db)):
    message_ids_raw = payload.get("message_ids") or []
    if not isinstance(message_ids_raw, list):
        message_ids_raw = [message_ids_raw]
    message_ids: list[int] = []
    for mid in message_ids_raw:
        try:
            if mid is None:
                continue
            message_ids.append(int(mid))
        except (ValueError, TypeError):
            continue

    filters = payload.get("filters") or {}
    options = payload.get("options") or {"format": "markdown"}
    prompts = payload.get("prompts") or {}
    module_candidates = payload.get("modules")
    ALLOWED_MODULES = {"market", "meetings", "counter", "contacts", "newswatch", "socialwatch"}
    if isinstance(module_candidates, list) and module_candidates:
        modules = [m for m in module_candidates if m in ALLOWED_MODULES]
    else:
        modules = options.get("modules") or []
        if isinstance(modules, list):
            modules = [m for m in modules if m in ALLOWED_MODULES]
        else:
            modules = []
    if not modules:
        modules = ["market", "meetings", "counter", "contacts", "newswatch", "socialwatch"]
    temperature = options.get("temperature") if isinstance(options, dict) else None
    try:
        temperature = float(temperature) if temperature is not None else None
    except Exception:
        temperature = None
    concurrency = options.get("concurrency") if isinstance(options, dict) else None
    try:
        concurrency = int(concurrency) if concurrency is not None else None
    except Exception:
        concurrency = None
    force_snapshot = bool(options.get("force_snapshot", True)) if isinstance(options, dict) else True

    options = {**options, "modules": modules, "temperature": temperature, "concurrency": concurrency, "force_snapshot": force_snapshot}

    ctx = {
        "request_id": "summary-task",
        "scope": {"message_ids": message_ids, "filters": filters},
        "options": options,
        "prompts": prompts,
        "modules": modules,
    }

    task = Task(type="summary", payload=ctx, status="pending")
    db.add(task)
    db.commit()
    db.refresh(task)

    try:
        snapshot = upsert_snapshot(
            db,
            message_ids=message_ids,
            filters=filters,
            options=options,
        )
        db.flush()

        contacts_full = snapshot.contact_ratings or {}
        contact_ratings_simple: dict[str, Any] = {}
        for key, data in contacts_full.items():
            if isinstance(data, dict):
                rating_val = data.get("rating")
            else:
                rating_val = data
            if rating_val is None:
                continue
            try:
                contact_ratings_simple[str(key)] = float(rating_val)
            except Exception:
                continue

        # 保存原始数据集文件，供大模型完整读取与审计（按渠道分组）
        try:
            ds_dir = os.path.abspath(os.path.join(os.getcwd(), 'data', 'datasets'))
            os.makedirs(ds_dir, exist_ok=True)
            ds_name = f"messages_{(filters or {}).get('period') or 'custom'}_{snapshot.id}.json"
            ds_path = os.path.join(ds_dir, ds_name)
            by_channel: dict[str, list] = {}
            for m in (snapshot.messages or []):
                ch = str((m or {}).get('channel') or 'wechat')
                by_channel.setdefault(ch, []).append(m)
            dataset = {
                "period": (filters or {}).get("period") if filters else None,
                "counts": {k: len(v) for k, v in by_channel.items()},
                "channels": by_channel,
            }
            with open(ds_path, 'w', encoding='utf-8') as f:
                json.dump(dataset, f, ensure_ascii=False, indent=2)
        except Exception:
            ds_path = None

        summary_payload = {
            "messages": snapshot.messages or [],
            "prompts": prompts,
            "contact_ratings": contact_ratings_simple,
            "contact_details": contacts_full,
            "meta": snapshot.meta or {},
            "modules": modules,
            "temperature": temperature,
            "dataset_path": ds_path,
        }

        local_summary = _run_summary_local(summary_payload)
        status = local_summary.get("status", "error")
        summary_result = local_summary.get("result") or {}
        returned_modules = local_summary.get("modules") or modules

        artifact_payloads = build_artifact_payloads(summary_result) if status == "ok" else []

        time_range = None
        if snapshot.time_from and snapshot.time_to:
            time_range = f"{snapshot.time_from.isoformat()} ~ {snapshot.time_to.isoformat()}"

        if status == "ok":
            rep = Report(
                title="AI 报告",
                time_range=time_range,
                filters=filters,
                status="done",
                result_type="json",
                result_body=json.dumps(summary_result, ensure_ascii=False),
            )
            for art_payload in artifact_payloads:
                rep.artifacts.append(ReportArtifact(**art_payload))
            db.add(rep)

        task.status = "done" if status == "ok" else "failed"
        task.result = {
            "status": status,
            "snapshot_id": snapshot.id,
            "report": summary_result,
            "meta": {**(snapshot.meta or {}), **({"time_range": time_range} if time_range else {})},
            "modules": returned_modules,
            "options": {
                "modules": returned_modules,
                "temperature": temperature,
                "concurrency": concurrency,
                "force_snapshot": force_snapshot,
            },
        }
        if artifact_payloads:
            task.result["artifacts"] = artifact_payloads

        db.add(task)
        db.commit()
        db.refresh(task)
    except Exception as e:
        db.rollback()
        task.status = "failed"
        task.result = {"error": str(e)}
        db.add(task)
        db.commit()
        db.refresh(task)

    return TaskOut(id=task.id, type=task.type, status=task.status, result=task.result)


@router.get("/config")
def get_ai_config():
    conf = load_ai_config()
    # Also return UI/analysis defaults so the frontend can persist settings across restarts
    analysis_defaults = conf.get("analysis_defaults") or {}
    ui_prefs = conf.get("ui_prefs") or {}
    return {
        "api_url": conf.get("api_url"),
        "model": conf.get("model"),
        "has_key": bool(conf.get("api_key")),
        "tool_model": conf.get("tool_model"),
        "tool_model_messages": conf.get("tool_model_messages") or conf.get("tool_model"),
        "tool_model_emails": conf.get("tool_model_emails") or conf.get("tool_model"),
        "max_tokens": conf.get("max_tokens"),
        "model_temperature": conf.get("model_temperature"),
        # Send (WeChatPadPro) config surface for UI
        "wechatpad_http_base": conf.get("wechatpad_http_base"),
        "wechatpad_text_path": conf.get("wechatpad_text_path"),
        "wechatpad_ws_url": conf.get("wechatpad_ws_url"),
        "message_filters": conf.get("message_filters", {}),
        "module_prompts": conf.get("module_prompts", {}),
        "default_module_prompts": DEFAULT_MODULE_PROMPTS,
        "tool_prompts": conf.get("tool_prompts", {}),
        "default_tool_prompts": DEFAULT_TOOL_PROMPTS,
        "analysis_defaults": {
            "modules": analysis_defaults.get("modules") or ["market", "meetings", "counter", "contacts"],
            "concurrency": int(analysis_defaults.get("concurrency") or 32),
            "temperature": float(analysis_defaults.get("temperature") or 0.3),
            "force_snapshot": bool(analysis_defaults.get("force_snapshot") if analysis_defaults.get("force_snapshot") is not None else True),
            "last_period": analysis_defaults.get("last_period") or "1day",
        },
        "ui_prefs": ui_prefs,
        "message_filters": conf.get("message_filters", {}),
        "derive_defaults": conf.get("derive_defaults", {}),
    }


@router.post("/config")
def set_ai_config(conf: dict):
    merged = load_ai_config()
    for key in ("api_key", "api_url", "model", "tool_model", "tool_model_messages", "tool_model_emails"):
        if key in conf and conf[key] is not None:
            merged[key] = conf[key]
    # Allow frontend to configure WeChatPadPro endpoint without editing .env
    if "wechatpad_http_base" in conf and conf["wechatpad_http_base"] is not None:
        merged["wechatpad_http_base"] = conf["wechatpad_http_base"].strip()
    if "wechatpad_text_path" in conf and conf["wechatpad_text_path"] is not None:
        p = conf["wechatpad_text_path"].strip() or "/api/v1/message/sendText"
        merged["wechatpad_text_path"] = p if p.startswith("/") else "/" + p
    if "wechatpad_ws_url" in conf and conf["wechatpad_ws_url"] is not None:
        merged["wechatpad_ws_url"] = conf["wechatpad_ws_url"].strip()

    # optional runtime LLM params
    if "max_tokens" in conf and conf["max_tokens"] is not None:
        merged["max_tokens"] = conf["max_tokens"]
    if "model_temperature" in conf and conf["model_temperature"] is not None:
        merged["model_temperature"] = conf["model_temperature"]
    if "message_filters" in conf and isinstance(conf["message_filters"], dict):
        mf = merged.get("message_filters") or {}
        mf.update({
            "external_only": bool(conf["message_filters"].get("external_only", mf.get("external_only", True))),
            "exclude_short": bool(conf["message_filters"].get("exclude_short", mf.get("exclude_short", True))),
            "exclude_system": bool(conf["message_filters"].get("exclude_system", mf.get("exclude_system", True))),
        })
        merged["message_filters"] = mf
    if "derive_defaults" in conf and isinstance(conf["derive_defaults"], dict):
        dd = merged.get("derive_defaults") or {}
        try:
            bs = int(conf["derive_defaults"].get("batch_size", dd.get("batch_size", 20)))
            dd["batch_size"] = max(1, min(128, bs))
        except Exception:
            pass
        try:
            cc = int(conf["derive_defaults"].get("concurrency", dd.get("concurrency", 8)))
            dd["concurrency"] = max(1, min(64, cc))
        except Exception:
            pass
        try:
            tp = float(conf["derive_defaults"].get("temperature", dd.get("temperature", 0.1)))
            dd["temperature"] = 0.0 if tp < 0 else (1.0 if tp > 1 else tp)
        except Exception:
            pass
        if "force" in conf["derive_defaults"]:
            dd["force"] = bool(conf["derive_defaults"].get("force", dd.get("force", False)))
        merged["derive_defaults"] = dd

    # Persist analysis defaults & UI preferences if provided
    if isinstance(conf.get("analysis_defaults"), dict):
        ad = merged.get("analysis_defaults") or {}
        incoming = conf["analysis_defaults"]
        if "modules" in incoming and isinstance(incoming["modules"], list):
            # keep valid modules only
            valid = {"market", "meetings", "counter", "contacts"}
            ad["modules"] = [m for m in incoming["modules"] if m in valid] or ["market", "meetings", "counter", "contacts"]
        if "concurrency" in incoming:
            try:
                ad["concurrency"] = max(1, min(128, int(incoming["concurrency"])) )
            except Exception:
                pass
        if "temperature" in incoming:
            try:
                t = float(incoming["temperature"])  # 0..1
                ad["temperature"] = 0.0 if t < 0 else (1.0 if t > 1 else t)
            except Exception:
                pass
        if "force_snapshot" in incoming:
            ad["force_snapshot"] = bool(incoming["force_snapshot"])  # type: ignore[truthy-bool]
        if "last_period" in incoming:
            last = str(incoming["last_period"]).lower()
            if last in {"1day", "3days", "1week", "1month"}:
                ad["last_period"] = last
        merged["analysis_defaults"] = ad

    if isinstance(conf.get("ui_prefs"), dict):
        up = merged.get("ui_prefs") or {}
        up.update({k: v for k, v in conf["ui_prefs"].items()})
        merged["ui_prefs"] = up

    if "module_prompts" in conf and isinstance(conf["module_prompts"], dict):
        stored = merged.get("module_prompts", {})
        incoming = conf["module_prompts"]
        for module, prompts in incoming.items():
            if module not in DEFAULT_MODULE_PROMPTS:
                continue
            current = stored.get(module, {})
            if not isinstance(current, dict):
                current = {}
            if isinstance(prompts, dict):
                updated = current.copy()
                if "system" in prompts and isinstance(prompts["system"], str):
                    updated["system"] = prompts["system"]
                if "user" in prompts and isinstance(prompts["user"], str):
                    updated["user"] = prompts["user"]
                stored[module] = updated
        merged["module_prompts"] = stored
    if "tool_prompts" in conf and isinstance(conf["tool_prompts"], dict):
        stored_tool = merged.get("tool_prompts", {})
        incoming_tool = conf["tool_prompts"]
        for key, prompts in incoming_tool.items():
            if key not in DEFAULT_TOOL_PROMPTS:
                continue
            current = stored_tool.get(key, {})
            if not isinstance(current, dict):
                current = {}
            if isinstance(prompts, dict):
                updated = current.copy()
                if "system" in prompts and isinstance(prompts["system"], str):
                    updated["system"] = prompts["system"]
                if "user" in prompts and isinstance(prompts["user"], str):
                    updated["user"] = prompts["user"]
                stored_tool[key] = updated
        merged["tool_prompts"] = stored_tool
    save_ai_config(merged)
    return {"status": "ok"}


@router.get("/entities")
def get_entities():
    path = os.path.abspath(os.path.join(os.getcwd(), 'data', 'entities.json'))
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        else:
            data = {"industries": []}
    except Exception:
        data = {"industries": []}
    return data


@router.post("/entities")
def set_entities(body: dict):
    inds = body.get('industries')
    if inds is None or not isinstance(inds, list):
        raise HTTPException(400, 'industries must be a list')
    payload = {"industries": [str(x) for x in inds]}
    path = os.path.abspath(os.path.join(os.getcwd(), 'data', 'entities.json'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return {"status": "ok"}


def _run_summary_local(payload: dict) -> dict:
    # payload expects: { messages: [...], prompts: {...}, options:{}, contact_ratings:{}, contact_details:{} }
    msgs = payload.get("messages", []) or []
    prompts = payload.get("prompts", {}) or {}
    snapshot_meta = payload.get("meta", {}) or {}

    contacts_raw: dict[str, dict[str, Any]] = {}
    for key, value in (payload.get("contact_ratings") or {}).items():
        if isinstance(value, dict):
            contacts_raw[str(key)] = value.copy()
        else:
            try:
                contacts_raw[str(key)] = {"rating": float(value)}
            except Exception:
                contacts_raw[str(key)] = {}
    for key, value in (payload.get("contact_details") or {}).items():
        entry = contacts_raw.get(str(key), {}).copy()
        if isinstance(value, dict):
            entry.update(value)
        else:
            entry.setdefault("extra", value)
        contacts_raw[str(key)] = entry

    def _extract_text(message: dict) -> str:
        if not isinstance(message, dict):
            return ""
        for field in ("content", "content_text", "text"):
            value = message.get(field)
            if isinstance(value, str) and value.strip():
                return value
        raw = message.get("raw")
        if isinstance(raw, dict):
            for field in ("content", "content_text", "text"):
                value = raw.get(field)
                if isinstance(value, str) and value.strip():
                    return value
        return ""

    def _is_short_message(message: dict) -> bool:
        text = _extract_text(message).strip()
        if not text:
            return True
        compact = "".join(text.split())
        if not compact:
            return True
        chinese_chars = sum(1 for ch in compact if "\u4e00" <= ch <= "\u9fff")
        if chinese_chars > 0:
            return chinese_chars <= 15
        return len(compact) <= 30

    filtered_msgs: list[dict] = []
    for m in msgs[:2000]:
        direction = (m.get("direction") or "").lower()
        if direction == "out":
            continue
        if str(m.get("is_spam", "")).lower() == "true":
            continue
        if _is_short_message(m):
            continue
        filtered_msgs.append(m)

    msgs = filtered_msgs

    try:
        conf = load_ai_config()
        module_prompts = conf.get("module_prompts", {})

        # 使用原始消息作为大模型输入，不依赖小模型摘要
        enriched_messages = []
        for m in msgs[:2000]:
            enriched_messages.append({
                "id": m.get("id"),
                "time": m.get("time") or m.get("timestamp"),
                "sender": m.get("sender_name") or m.get("sender_id"),
                "talker": m.get("talker_name") or m.get("chat_id"),
                "message_type": m.get("message_type") or m.get("type"),
                "content": m.get("content") or m.get("content_text") or m.get("text"),
            })

        # 紧凑化传入大模型的数据，避免上下文过大导致超限/失败
        def _safe_str(v: Any) -> str:
            try:
                return str(v) if v is not None else ""
            except Exception:
                return ""

        def _iso(v: Any) -> str:
            s = _safe_str(v)
            return s

        # 排序：按重要度与时间倒序；会议模块再优先保留带 meeting_number 的消息
        def _sort_key(m: dict) -> tuple:
            imp = m.get("importance")
            try:
                impv = float(imp) if imp is not None else 0.0
            except Exception:
                impv = 0.0
            return (impv, _iso(m.get("time")))

        sorted_messages = sorted(enriched_messages, key=_sort_key, reverse=True)

        # —— 按模块预过滤，降低无关噪声并保证“遍历所有消息后再总结”的语义 ——
        meeting_terms = ("会议", "路演", "电话会", "报名", "通知", "腾讯会议", "进门财经", "Zoom", "Teams")
        market_terms = ("认为", "观点", "策略", "看多", "看空", "判断", "建议", "风险", "目标价", "估值", "行业", "公司", "基本面", "宏观", "政策")

        def _is_meeting(m: dict) -> bool:
            text = (m.get("content") or "").strip()
            return any(t in text for t in meeting_terms)

        def _is_market(m: dict) -> bool:
            text = (m.get("content") or "").strip()
            return any(t in text for t in market_terms)

        def _is_counter(m: dict) -> bool:
            text = (m.get("content") or "").strip()
            return any(t in text for t in market_terms)

        high_contact_senders = {c.get("sender") for c in locals().get("high_contacts", []) if c.get("sender")}

        # 统一裁剪长度，尽量依赖摘要/关键词，正文仅取前200字符
        def _compact(ms: list[dict], prefer_meetings: bool = False, limit: int = 400) -> list[dict]:
            selected: list[dict] = []
            pool = ms
            for m in pool:
                if len(selected) >= limit:
                    break
                selected.append({
                    "id": m.get("id"),
                    "time": m.get("time"),
                    "sender": m.get("sender") or m.get("sender_name"),
                    "talker": m.get("talker"),
                    "message_type": m.get("message_type"),
                    # 保留少量正文以便模型理解上下文，仅传原文
                    "content": (_safe_str(m.get("content"))[:200]),
                })
            return selected

        def _normalize_kw(kw: str) -> str:
            return (kw or "").strip().lower()

        stopwords = {
            "流通股本", "所属行业", "市值", "成交量", "换手率", "pe", "pb", "roe",
            "板块", "行业", "公司", "观点", "认为", "建议", "相关", "影响",
        }

        N = max(1, len(enriched_messages))
        df: dict[str, int] = {}
        for m in enriched_messages:
            kws = set(_normalize_kw(k) for k in (m.get("keywords") or []) if isinstance(k, str))
            for k in kws:
                if k and k not in stopwords:
                    df[k] = df.get(k, 0) + 1

        def _idf(term: str) -> float:
            import math

            return math.log((N + 1) / (1 + df.get(term, 0))) + 1.0

        for m in enriched_messages:
            kws = [_normalize_kw(k) for k in (m.get("keywords") or []) if isinstance(k, str)]
            scored = [
                (k, _idf(k))
                for k in kws
                if k and k not in stopwords
            ]
            scored.sort(key=lambda x: x[1], reverse=True)
            m["keywords"] = [k for k, _ in scored[:5]]

        from datetime import datetime, timedelta, timezone

        def _parse_time(ts: str | None):
            if not ts:
                return None
            text_ts = ts.replace("Z", "+00:00") if isinstance(ts, str) else ts
            try:
                return datetime.fromisoformat(text_ts)  # type: ignore[arg-type]
            except Exception:
                return None

        cutoff_dt = datetime.utcnow() - timedelta(days=3)
        norm_ratings: dict[str, float] = {}
        for cid, data in contacts_raw.items():
            rating_val = data.get("rating")
            if rating_val is None:
                continue
            try:
                rating = float(rating_val)
            except Exception:
                continue
            if rating <= 10:
                rating *= 10.0
            norm_ratings[cid] = rating

        activity: dict[str, int] = {}
        for m in enriched_messages:
            sender = (m.get("sender") or "").strip()
            t = _parse_time(m.get("time"))
            if not sender or not t:
                continue
            # 统一为 naive UTC 再比较，避免 aware/naive 混用
            try:
                tt = t
                if tt.tzinfo is not None:
                    tt = tt.astimezone(timezone.utc).replace(tzinfo=None)
                if tt >= cutoff_dt:
                    activity[sender] = activity.get(sender, 0) + 1
                continue
            except Exception:
                pass
            if t >= cutoff_dt:
                activity[sender] = activity.get(sender, 0) + 1

        high_contacts = []
        for sender in set(list(activity.keys()) + list(norm_ratings.keys())):
            rating = norm_ratings.get(sender)
            if rating is None:
                rating = 50.0
            active = activity.get(sender, 0)
            if active <= 0:
                continue
            record = {
                "sender": sender,
                "rating": rating,
                "activity": active,
            }
            detail = contacts_raw.get(sender)
            if detail:
                if detail.get("name"):
                    record["name"] = detail.get("name")
                if detail.get("alias"):
                    record["alias"] = detail.get("alias")
            high_contacts.append(record)
        high_contacts.sort(key=lambda x: (x["rating"], x["activity"]), reverse=True)
        # 若严格阈值导致为空，则以活跃度Top补足，避免“高评分联系人”卡片空白
        if not high_contacts:
            tmp = sorted(({"sender": s, "rating": norm_ratings.get(s, 50.0), "activity": a, **(contacts_raw.get(s) or {})} for s, a in activity.items()), key=lambda x:(x["activity"], x.get("rating",50.0)), reverse=True)
            high_contacts = [{"sender": t.get("sender"), "rating": float(t.get("rating",50.0)), "activity": t.get("activity",0), "name": t.get("name"), "alias": t.get("alias")} for t in tmp[:10]]

        time_min = min((m.get("time") for m in enriched_messages if m.get("time")), default=None)
        time_max = max((m.get("time") for m in enriched_messages if m.get("time")), default=None)

        base_payload = {
            "messages": enriched_messages,
            "raw_messages": msgs,
            "contact_ratings": {k: v.get("rating") for k, v in contacts_raw.items() if v.get("rating") is not None},
            "contacts": contacts_raw,
            "prompts": prompts,
            "meta": {
                "total_messages": len(enriched_messages),
                "time_range": [time_min, time_max],
                "high_score_contacts": high_contacts,
                "window_days": 3,
                "snapshot_meta": snapshot_meta,
            },
        }

        requested_modules = payload.get("modules")
        if isinstance(requested_modules, list) and requested_modules:
            module_filter = {m for m in requested_modules if m in {"market", "meetings", "counter", "contacts", "newswatch", "socialwatch"}}
        else:
            module_filter = {"market", "meetings", "counter", "contacts", "newswatch", "socialwatch"}

        temperature = payload.get("temperature")
        try:
            temperature = float(temperature) if temperature is not None else 0.3
        except Exception:
            temperature = 0.3

        module_map = {
            "market": "market_markdown",
            "meetings": "meetings_markdown",
            "counter": "counter_markdown",
            "contacts": "top_contacts_markdown",
            "newswatch": "newswatch_markdown",
            "socialwatch": "socialwatch_markdown",
        }

        module_titles = {
            "market": "市场观点总结",
            "meetings": "会议路演信息",
            "counter": "反驳观点分析",
            "contacts": "高评分联系人摘要",
            "newswatch": "新闻舆情监测",
            "socialwatch": "自媒体舆情监测",
        }

        result: dict[str, str] = {
            "market_markdown": "",
            "meetings_markdown": "",
            "counter_markdown": "",
            "top_contacts_markdown": "",
            "market_html": "",
            "meetings_html": "",
            "counter_html": "",
            "top_contacts_html": "",
            "newswatch_markdown": "",
            "socialwatch_markdown": "",
        }

        for module_key, result_key in module_map.items():
            if module_key not in module_filter:
                continue
            prompt_conf = module_prompts.get(module_key, {})
            defaults = DEFAULT_MODULE_PROMPTS.get(module_key, {})
            system_prompt = prompt_conf.get("system") or defaults.get("system") or ""
            user_template = prompt_conf.get("user") or defaults.get("user") or ""
            module_payload = base_payload.copy()
            # 为不同模块选择更相关的子集，既覆盖“全部消息”，又避免无关噪声
            if module_key == "meetings":
                source = [m for m in sorted_messages if _is_meeting(m)] or sorted_messages
                module_payload["messages"] = _compact(source, prefer_meetings=True, limit=250)
            elif module_key == "market":
                source = [m for m in sorted_messages if _is_market(m)] or sorted_messages
                module_payload["messages"] = _compact(source, prefer_meetings=False, limit=320)
            elif module_key == "counter":
                source = [m for m in sorted_messages if _is_counter(m)] or sorted_messages
                module_payload["messages"] = _compact(source, prefer_meetings=False, limit=280)
            elif module_key == "contacts":
                if high_contact_senders:
                    source = [m for m in sorted_messages if (m.get("sender") or m.get("sender_name")) in high_contact_senders]
                else:
                    source = sorted_messages
                module_payload["messages"] = _compact(source, prefer_meetings=False, limit=220)
            else:
                module_payload["messages"] = _compact(sorted_messages, prefer_meetings=False, limit=300)

            # 粗略 token 预算，防止超过大上下文（~128k tokens）；按字符估算并分块摘要再合并
            try:
                est_tokens = sum(len((m.get("content") or "")) for m in module_payload["messages"]) * 1.1
                if est_tokens > 120_000 and len(module_payload["messages"]) > 200:
                    chunks: list[list[dict]] = []
                    chunk: list[dict] = []
                    budget = 0
                    for m in module_payload["messages"]:
                        cost = len((m.get("content") or "")) + 64
                        if budget + cost > 40_000 and chunk:
                            chunks.append(chunk)
                            chunk = []
                            budget = 0
                        chunk.append(m)
                        budget += cost
                    if chunk:
                        chunks.append(chunk)

                    partial_markdowns: list[str] = []
                    for idx, ch in enumerate(chunks[:6]):  # 最多6片，避免长时间调用
                        cp = module_payload.copy()
                        cp["messages"] = ch
                        cp_str = json.dumps(cp, ensure_ascii=False)
                        if "{{messages_data}}" in user_template:
                            user_content = user_template.replace("{{messages_data}}", cp_str)
                        else:
                            user_content = user_template + "\n\n数据：\n" + cp_str
                        messages_payload = [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_content},
                        ]
                        try:
                            part = siliconflow_chat(messages_payload, temperature=temperature)
                        except Exception:
                            part = ""
                        if part:
                            partial_markdowns.append(_strip_llm_thoughts(part))

                    if partial_markdowns:
                        # 合并阶段：将多段 markdown 汇总为最终 markdown
                        merge_user = "\n".join([
                            "请将多段模块性摘要合并为一段更高质量的最终摘要，避免重复，保留结构化标题：",
                            "---",
                            "\n\n".join(f"[片段{idx+1}]\n{md}" for idx, md in enumerate(partial_markdowns[:6]))
                        ])
                        merge_payload = [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": merge_user},
                        ]
                        try:
                            output_text = siliconflow_chat(merge_payload, temperature=temperature)
                            parsed = None
                            try:
                                parsed = json.loads(output_text)
                            except Exception:
                                parsed = None
                            if isinstance(parsed, dict) and ("markdown" in parsed or "html" in parsed):
                                if "markdown" in parsed:
                                    result[result_key] = _strip_llm_thoughts(parsed.get("markdown", ""))
                                elif "html" in parsed:
                                    result[result_key] = _strip_llm_thoughts(parsed.get("html", ""))
                            else:
                                result[result_key] = _strip_llm_thoughts(output_text)
                            continue  # 已产出结果，跳过常规路径
                        except Exception:
                            pass
            except Exception:
                pass
            module_payload["target_module"] = module_key
            module_payload["module_title"] = module_titles.get(module_key, module_key)
            payload_str = json.dumps(module_payload, ensure_ascii=False)
            if "{{messages_data}}" in user_template:
                user_content = user_template.replace("{{messages_data}}", payload_str)
            else:
                user_content = user_template + "\n\n数据：\n" + payload_str

            messages_payload = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]

            try:
                output_text = siliconflow_chat(messages_payload, temperature=temperature)
            except Exception as exc:  # pragma: no cover
                result[result_key] = ""
                # 不中断，让后续使用本地兜底生成
                continue

            try:
                parsed = json.loads(output_text)
            except Exception:
                parsed = None

            if isinstance(parsed, dict) and ("markdown" in parsed or "html" in parsed):
                if "markdown" in parsed:
                    result[result_key] = _strip_llm_thoughts(parsed.get("markdown", ""))
                elif "html" in parsed:
                    result[result_key] = _strip_llm_thoughts(parsed.get("html", ""))
            else:
                result[result_key] = _strip_llm_thoughts(output_text)

        # ---------- Local fallbacks to guarantee useful content ----------
        POS = {"看多","看好","上涨","增持","买入","积极","乐观","超配","超预期","改善","提价","扩产","胜诉","达成"}
        NEG = {"看空","不看好","下跌","减持","卖出","悲观","谨慎","风险","压力","回调","不及预期","降价","停产","失利","受阻"}

        def _short(txt: str, n: int = 80) -> str:
            t = (txt or "").strip().replace("\n", " ")
            return t[: n] + ("…" if len(t) > n else "")

        def _short_cn(txt: str, limit: int = 10) -> str:
            t = (txt or "").strip().replace("\n", " ")
            if t.lower().startswith("ai:"):
                t = t[3:].strip()
            count = 0
            result_chars: list[str] = []
            for ch in t:
                count += 2 if ord(ch) > 127 else 1
                if count > limit * 2:
                    result_chars.append("…")
                    break
                result_chars.append(ch)
            return "".join(result_chars)

        def _has_any(text: str, words: set[str]) -> bool:
            return any(w in text for w in words)

        # Category rules
        CAT_RULES = [
            ("宏观政策", ["宏观","政策","降息","加息","降准","货币政策","财政政策","专项债","美联储","央行","社融","通胀","CPI","PPI"]),
            ("行业板块", ["行业","板块","AI","人工智能","芯片","半导体","新能源","煤炭","钢铁","地产","医药","消费","军工","汽车","高景气"]),
            ("公司基本面", ["公司","个股","业绩","盈利","估值","财报","公告","订单","收入","净利","股价"]),
            ("投资策略", ["策略","配置","仓位","增持","减仓","组合","资产配置","风格","价值","成长","红利","低波","大类资产"]),
            ("市场情绪", ["情绪","北向","资金","成交","量能","波动","风险偏好","恐慌","贪婪"]),
            ("其他观点", []),
        ]

        # Build indices for quick search
        def _match_cats(m: dict) -> list[str]:
            text = (m.get("content") or "").strip()
            kws = set(str(k) for k in (m.get("keywords") or []))
            cats: list[str] = []
            for name, keys in CAT_RULES:
                if not keys:
                    continue
                if any(k in text for k in keys) or (kws and any(k in " ".join(kws) for k in keys)):
                    cats.append(name)
            if not cats:
                cats.append("其他观点")
            return cats

        def _build_market_md() -> str:
            total = len(enriched_messages)
            pos = sum(1 for m in enriched_messages if _has_any(str(m.get("content") or ""), POS) or (m.get("tone") == "positive"))
            neg = sum(1 for m in enriched_messages if _has_any(str(m.get("content") or ""), NEG) or (m.get("tone") == "negative"))
            md = ["# 市场观点总结", f"- 样本数：{total}；正向：{pos}；负向：{neg}"]
            for name, _ in CAT_RULES:
                bucket = [m for m in enriched_messages if name in _match_cats(m)]
                if not bucket:
                    md.append(f"\n## {name}\n- 信息有限")
                    continue
                md.append(f"\n## {name}")
                # Top 5 highlights
                for m in bucket[:5]:
                    sent = m.get("sender") or m.get("sender_name") or "未知"
                    ts = m.get("time") or ""
                    text = _short(m.get("summary") or m.get("content") or "", 120)
                    tone = m.get("tone") or "neutral"
                    md.append(f"- ({tone}) {text}（来源：{sent} {ts}）")
            md.append("\n## 行动建议\n- 根据政策与基本面强弱，分配仓位并动态跟踪关键数据。")
            md.append("\n## 关注事项\n- 货币与财政节奏、订单与盈利质量、外部流动性与风险事件。")
            return "\n".join(md)

        def _build_market_html() -> str:
            total = len(enriched_messages)
            pos = sum(1 for m in enriched_messages if _has_any(str(m.get("content") or ""), POS) or (m.get("tone") == "positive"))
            neg = sum(1 for m in enriched_messages if _has_any(str(m.get("content") or ""), NEG) or (m.get("tone") == "negative"))
            sections = []
            sections.append(f"<p>样本数：{total}；正向：{pos}；负向：{neg}</p>")
            for name, _ in CAT_RULES:
                bucket = [m for m in enriched_messages if name in _match_cats(m)]
                if not bucket:
                    sections.append(f"<h2>{name}</h2><ul><li>信息有限</li></ul>")
                    continue
                items = []
                for m in bucket[:5]:
                    sent = m.get("sender") or m.get("sender_name") or "未知"
                    ts = m.get("time") or ""
                    text = _short(m.get("summary") or m.get("content") or "", 120)
                    tone = m.get("tone") or "neutral"
                    items.append(f"<li>({tone}) {html.escape(text)}（来源：{html.escape(sent)} {html.escape(ts or '')}）</li>")
                sections.append(f"<h2>{name}</h2><ul>{''.join(items)}</ul>")
            sections.append("<h2>行动建议</h2><ul><li>根据政策与基本面强弱，分配仓位并动态跟踪关键数据。</li></ul>")
            sections.append("<h2>关注事项</h2><ul><li>货币与财政节奏、订单与盈利质量、外部流动性与风险事件。</li></ul>")
            return "<h1>市场观点总结</h1>" + "".join(sections)

        PLATFORM_ABBREV = {
            "腾讯会议": "腾",
            "进门财经": "进",
            "飞书": "飞",
            "Zoom": "ZM",
            "Teams": "TM",
            "钉钉": "钉",
            "电话会议": "电",
        }

        def _detect_platform(text: str) -> str | None:
            t = text.lower()
            if "腾讯会议" in text: return "腾讯会议"
            if "进门财经" in text: return "进门财经"
            if "飞书" in text or "feishu" in t or "lark" in t: return "飞书"
            if "zoom" in t: return "Zoom"
            if "teams" in t: return "Teams"
            if "钉钉" in text or "dingtalk" in t: return "钉钉"
            if "电话会" in text or "电话会议" in text: return "电话会议"
            return None

        def _abbr_platform(name: str | None) -> str:
            if not name:
                return "待定"
            return PLATFORM_ABBREV.get(name, name[:2])

        def _fmt_meeting_time(ts: str | None) -> str:
            if not ts:
                return "待定"
            text = ts.replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(text)
            except Exception:
                return ts.replace("T", " ")[:16]
            return dt.strftime("%m-%d %H:%M")

        def _extract_time_from_text(text: str) -> str | None:
            # Try to parse patterns like 9-24 19:30 / 09-24 19:30 / 9月24日 19:30 / 9/24 19:30
            import re as _re
            t = (text or "").strip()
            if not t:
                return None
            pats = [
                _re.compile(r"(?P<m>\d{1,2})[-/\.月](?P<d>\d{1,2})(?:日)?\s*(?P<h>\d{1,2}):(?P<mi>\d{2})"),
                _re.compile(r"(?P<h>\d{1,2}):(?P<mi>\d{2})"),
            ]
            for p in pats:
                m = p.search(t)
                if m:
                    gd = m.groupdict()
                    try:
                        mm = int(gd.get('m') or 0)
                        dd = int(gd.get('d') or 0)
                        hh = int(gd.get('h') or 0)
                        mi = int(gd.get('mi') or 0)
                        if mm and dd:
                            return f"{mm:02d}-{dd:02d} {hh:02d}:{mi:02d}"
                        # If only time found, use today as date
                        from datetime import datetime as _dt
                        now = _dt.utcnow()
                        return f"{now.month:02d}-{now.day:02d} {hh:02d}:{mi:02d}"
                    except Exception:
                        continue
            return None

        def _extract_meetings() -> list[dict]:
            import re as _re
            items: list[dict] = []
            seen: set[str] = set()
            for m in enriched_messages:
                text = (m.get("content") or "")
                platform = _detect_platform(text)
                meeting_no = (m.get("meeting_number") or "").strip()
                if not meeting_no:
                    mm = _re.search(r"(?<!\d)(\d{8,12})(?!\d)", text)
                    if mm:
                        meeting_no = mm.group(1)
                if platform or meeting_no or ("会议" in text) or ("路演" in text) or ("报名" in text):
                    key = f"{m.get('time')}|{meeting_no}|{platform}"
                    if key in seen:
                        continue
                    seen.add(key)
                    # 使用 summary 作为主题要点来源（已移除 key_info 字段）
                    base_summary = (m.get("summary") or m.get("content") or "").strip()
                    shown_time = _extract_time_from_text(text) or _fmt_meeting_time(m.get("time"))
                    items.append({
                        "id": m.get("id") or m.get("message_id"),
                        "time": shown_time,
                        "platform": _abbr_platform(platform),
                        "number": meeting_no or "待确认",
                        "speaker": m.get("sender") or m.get("sender_name") or "-",
                        "topic": _short_cn(base_summary, 10) or "-",
                    })
            # sort by time desc
            items.sort(key=lambda x: x.get("time") or "", reverse=True)
            return items[:20]

        def _build_meetings_md() -> str:
            items = _extract_meetings()
            if not items:
                return "# 会议路演信息\n- 信息有限"
            md = ["# 会议路演信息", f"- 记录到会议/路演：{len(items)} 场"]
            md.append("\n| 时间 | 形式 | 会议号 | 主讲人/机构 | 主题要点 |\n|---|---|---|---|---|")
            for it in items:
                md.append(f"| {it['time']} | {it['number']} | {it['speaker']} | {it['topic']} |")
            md.append("\n## 待处理事项\n- 核对会议号与参会方式，提前准备提问要点和资料。")
            return "\n".join(md)

        def _normalize_conflict_theme(m: dict) -> str:
            # 统一使用 summary 作为议题主题来源
            base = (m.get("summary") or m.get("content") or "").strip()
            if base.lower().startswith("ai:"):
                base = base[3:].strip()
            return _short(base, 40) or "未命名议题"

        def _classify_tone(m: dict) -> str:
            tone = (m.get("tone") or "").lower()
            text = (m.get("content") or "")
            if tone in ("positive", "negative"):
                return tone
            if _has_any(text, POS):
                return "positive"
            if _has_any(text, NEG):
                return "negative"
            return "neutral"

        def _extract_conflicts() -> list[dict]:
            topics: dict[str, dict[str, list[str]]] = {}
            for m in enriched_messages:
                tone = _classify_tone(m)
                if tone not in ("positive", "negative"):
                    continue
                summary = (m.get("summary") or m.get("content") or "").strip()
                if not summary:
                    continue
                # 主题归并：优先股票/行业/会议主题关键词，否则用摘要短句
                theme = _normalize_conflict_theme(m)
                entry = _short(summary, 160)
                bucket = topics.setdefault(theme, {"positive": [], "negative": []})
                bucket[tone].append(entry)
            conflicts: list[dict] = []
            for theme, bucket in topics.items():
                if bucket["positive"] and bucket["negative"]:
                    conflicts.append({
                    "theme": theme,
                    "positive": bucket["positive"][0],
                    "negative": bucket["negative"][0],
                    "positive_id": ids_map.get(theme, {}).get("positive", [""])[0] if ids_map.get(theme, {}).get("positive") else "",
                    "negative_id": ids_map.get(theme, {}).get("negative", [""])[0] if ids_map.get(theme, {}).get("negative") else "",
                })
            # 若严格二元分法产生空结果，退化为“争议线索”：提取包含对立词根的样本各3条
            if not conflicts:
                leads = []
                for m in enriched_messages:
                    txt = (m.get("content") or "")
                    if _has_any(txt, POS) and _has_any(txt, NEG):
                        leads.append(_short(txt, 120))
                if leads:
                    return [{"theme": "存在分歧/待核查", "positive": leads[0], "negative": leads[1] if len(leads)>1 else leads[0]}]
            return conflicts[:8]

        def _build_counter_md() -> str:
            conflicts = _extract_conflicts()
            if not conflicts:
                return "# 反驳观点分析\n- 暂未发现确凿的对立观点，建议持续收集证据。"
            md = ["# 反驳观点分析", f"总体：发现 {len(conflicts)} 个存在明显冲突的议题。"]
            for item in conflicts:
                md.append(f"\n## 议题：{_short(item['theme'], 40)}")
                md.append(f"- 观点：{item['positive']}")
                md.append(f"- 证据：已在消息中体现（可核查）")
                md.append(f"- 矛盾要点：{item['negative']}")
                md.append(f"- <span class=\"ask\">建议提问</span>：继续核查关键数据并保持证据导向的讨论。")
            md.append("\n## 总结\n- 上述议题仍存在分歧，建议按证据优先原则推进讨论，并跟踪高频联系人观点变动。")
            return "\n".join(md)

        def _build_contacts_md() -> str:
            lines = ["# 高评分联系人摘要"]
            if not high_contacts:
                lines.append("- 近3天暂无评分≥7.0且活跃的联系人")
                return "\n".join(lines)
            lines.append("以下按评分与活跃度排序：")
            for c in high_contacts[:20]:
                sender = c.get("name") or c.get("alias") or c.get("sender")
                rating = c.get("rating")
                act = c.get("activity")
                latest = next((m for m in reversed(enriched_messages) if (m.get("sender") or m.get("sender_name")) == c.get("sender")), None)
                summary = _short((latest or {}).get("summary") or (latest or {}).get("content") or "", 100)
                lines.append(f"### {sender}（评分 {rating:.1f} / 活跃 {act}）\n- 核心观点：{summary or '—'}\n- 最新动态：近期信息已记录于聊天摘要中\n- 跟进建议：针对其关注点准备问答并确认最新数据。")
            lines.append("\n## 关注联系人\n- 近3天评分处于次高分段的潜力对象建议提升触达频次。")
            return "\n".join(lines)

        # === HTML 版本（用于更好的交互与排版） ===
        def _build_meetings_html() -> str:
            items = _extract_meetings()
            if not items:
                return "<h1>会议路演信息</h1><p>信息有限</p>"
            rows = []
            for it in items:
                rows.append(
                    f"<tr data-msg-id=\"{html.escape(str(it.get('id') or ''))}\"><td>{html.escape(it['time'])}</td><td>{html.escape(it['number'])}</td>"
                    f"<td>{html.escape(it['speaker'])}</td><td><span class=\"msg-badge\" data-msg-id=\"{html.escape(str(it.get('id') or ''))}\">源</span> {html.escape(it['topic'])}</td></tr>"
                )
            table = """
            <h1>会议路演信息</h1>
            <p>记录到会议/路演：{n} 场</p>
            <table class=\"meeting-table\"><thead><tr><th>时间</th><th>会议号</th><th>主讲人/机构</th><th>主题要点</th></tr></thead>
            <tbody>{rows}</tbody></table>
            <h2>待处理事项</h2>
            <ul><li>核对会议号与参会方式，提前准备提问要点和资料。</li></ul>
            """.replace("{n}", str(len(items))).replace("{rows}", "\n".join(rows))
            return table

        def _build_counter_html() -> str:
            conflicts = _extract_conflicts()
            if not conflicts:
                return "<h1>矛盾观点分析</h1><p>暂无明确冲突。建议继续跟踪关键数据与风险点。</p>"
            rows = []
            for item in conflicts:
                rows.append(
                    f"<tr><td>{html.escape(item['positive'])}</td><td>{html.escape(item['negative'])}</td></tr>"
                )
            table = """
            <h1>矛盾观点分析</h1>
            <p>发现 {n} 个存在实质分歧的议题。</p>
            <table class=\"counter-table\"><thead><tr><th>主观点</th><th>冲突观点</th></tr></thead><tbody>{rows}</tbody></table>
            <h2>怀疑与结论</h2>
            <p>针对以上议题，建议核实相关数据来源，保持证据导向的讨论节奏。</p>
            """.replace("{n}", str(len(conflicts))).replace("{rows}", "\n".join(rows))
            return table

        def _build_contacts_html() -> str:
            if not high_contacts:
                return "<h1>高评分联系人摘要</h1><p>近3天暂无评分≥7.0且活跃的联系人</p>"
            items = []
            for c in high_contacts[:20]:
                sender = c.get("name") or c.get("alias") or c.get("sender")
                rating = c.get("rating")
                act = c.get("activity")
                latest = next((m for m in reversed(enriched_messages) if (m.get("sender") or m.get("sender_name")) == c.get("sender")), None)
                summary = _short((latest or {}).get("summary") or (latest or {}).get("content") or "", 120)
                items.append(f"<li><strong>{html.escape(sender)}</strong>（评分 {rating:.1f} / 活跃 {act}）<br><em>核心观点：</em><span class=\"msg-badge\" data-msg-id=\"{html.escape(str((latest or {}).get('id') or (latest or {}).get('message_id') or ''))}\">源</span> {html.escape(summary)}</li>")
            return "<h1>高评分联系人摘要</h1><ol>" + "".join(items) + "</ol>"

        if "market" in module_filter and not result.get("market_markdown"):
            result["market_markdown"] = _build_market_md()
            result["market_html"] = _build_market_html()
        if "meetings" in module_filter and not result.get("meetings_markdown"):
            result["meetings_markdown"] = _build_meetings_md()
            result["meetings_html"] = _build_meetings_html()
        if "counter" in module_filter and not result.get("counter_markdown"):
            result["counter_markdown"] = _build_counter_md()
            result["counter_html"] = _build_counter_html()
        if "contacts" in module_filter and not result.get("top_contacts_markdown"):
            result["top_contacts_markdown"] = _build_contacts_md()
            result["top_contacts_html"] = _build_contacts_html()

        active_modules = [m for m in module_map.keys() if m in module_filter]
        return {"status": "ok", "result": result, "modules": active_modules, "temperature": temperature, "meta": base_payload.get("meta") or {}}
    except Exception as exc:  # pragma: no cover
        safe = html.escape(str(exc))
        err_markdown = f"**summary-local error:** {safe}"
        empty = {
            "market_markdown": err_markdown if "market" in module_filter else "",
            "meetings_markdown": err_markdown if "meetings" in module_filter else "",
            "counter_markdown": err_markdown if "counter" in module_filter else "",
            "top_contacts_markdown": err_markdown if "contacts" in module_filter else "",
            "market_html": "",
            "meetings_html": "",
            "counter_html": "",
            "top_contacts_html": "",
        }
        return {"status": "error", "result": empty, "modules": [m for m in module_filter], "temperature": temperature}


@router.post("/summary-local")
def summary_local(payload: dict):
    return _run_summary_local(payload)
def _markdown_to_html(markdown_text: str) -> str:
    if not markdown_text:
        return ""
    # 将 `#123` 引用转换为可点击消息徽标
    import re
    md = re.sub(r"#(\d+)", r"<span class=\"msg-badge\" data-msg-id=\"\\1\">源</span>", markdown_text)
    lines = [line.rstrip() for line in md.strip().splitlines()]
    html_parts: list[str] = []
    list_open = False

    def close_list():
        nonlocal list_open
        if list_open:
            html_parts.append("</ul>")
            list_open = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            close_list()
            html_parts.append(f"<h3>{html.escape(stripped[4:].strip())}</h3>")
        elif stripped.startswith("## "):
            close_list()
            html_parts.append(f"<h2>{html.escape(stripped[3:].strip())}</h2>")
        elif stripped.startswith("# "):
            close_list()
            html_parts.append(f"<h1>{html.escape(stripped[2:].strip())}</h1>")
        elif stripped.startswith("- "):
            if not list_open:
                html_parts.append("<ul>")
                list_open = True
            html_parts.append(f"<li>{html.escape(stripped[2:].strip())}</li>")
        else:
            close_list()
            html_parts.append(f"<p>{html.escape(stripped)}</p>")

    close_list()
    return "\n".join(html_parts)
@router.get("/test-main")
def test_main_model():
    conf = load_ai_config()
    info = {
        "api_url": conf.get("api_url"),
        "model": conf.get("model"),
        "has_key": bool(conf.get("api_key")),
    }
    try:
        out = siliconflow_chat([
            {"role": "system", "content": "你是一个测试助手，回答四个字：连接成功。"},
            {"role": "user", "content": "请输出“连接成功”四个字"},
        ], temperature=0.0)
        return {"status": "ok", "output": out, "config": info}
    except Exception as e:
        return {"status": "error", "error": str(e), "config": info}


@router.get("/test-tool")
def test_tool_model():
    conf = load_ai_config()
    info = {
        "api_url": conf.get("api_url"),
        "tool_model": conf.get("tool_model"),
        "has_key": bool(conf.get("api_key")),
    }
    try:
        out = siliconflow_tool_chat([
            {"role": "system", "content": "你是一个测试助手，回答四个字：连接成功。"},
            {"role": "user", "content": "请输出“连接成功”四个字"},
        ], temperature=0.0)
        return {"status": "ok", "output": out, "config": info}
    except Exception as e:
        return {"status": "error", "error": str(e), "config": info}


@router.post("/test-tool-summary")
def test_tool_summary(payload: dict):
    from datetime import datetime

    text = str(payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text required")
    conf = load_ai_config()
    prompt_conf = (conf.get("tool_prompts") or {}).get("message_summary") or DEFAULT_TOOL_PROMPTS["message_summary"]
    system_prompt = prompt_conf.get("system") or DEFAULT_TOOL_PROMPTS["message_summary"]["system"]
    user_template = prompt_conf.get("user") or DEFAULT_TOOL_PROMPTS["message_summary"]["user"]
    sample = {
        "id": "demo",
        "time": datetime.utcnow().isoformat(),
        "sender": payload.get("sender") or "测试联系人",
        "content": text,
    }
    payload_json = json.dumps([sample], ensure_ascii=False)
    if "{{messages_json}}" in user_template:
        user_content = user_template.replace("{{messages_json}}", payload_json)
    else:
        user_content = user_template + "\n\n数据：\n" + payload_json
    messages_payload = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    try:
        raw = siliconflow_tool_chat(messages_payload, temperature=payload.get("temperature") or 0.1)
    except Exception as exc:
        # 直返错误文本，便于前端查看具体问题
        return {
            "status": "error",
            "error": str(exc),
            "raw": None,
            "config": {"tool_model": conf.get("tool_model"), "api_url": conf.get("api_url")},
        }

    # 尝试解析为 JSON；若失败也返回 200 并带上 raw，方便前端直观核对
    parsed = None
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    return {
        "status": "ok",
        "raw": raw,
        "parsed": parsed,
        "config": {"tool_model": conf.get("tool_model"), "api_url": conf.get("api_url")},
    }

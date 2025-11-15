from __future__ import annotations

from fastapi import APIRouter, Query
from typing import List, Dict, Any
import os
import re
import json
import hashlib
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import select

from ..db import SessionLocal
from ..models import SyncState
from ..services.llm_client import load_ai_config, DEFAULT_TOOL_PROMPTS
from ..services.ai_tools import extract_message_features


router = APIRouter(prefix="/api/minutes", tags=["minutes"])


TEXT_EXTS = {".txt", ".md", ".markdown"}
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".aac"}


def _default_minutes_dirs() -> list[str]:
    # 允许通过环境变量 MINUTES_DIRS 配置多个目录，逗号分隔
    env = os.getenv("MINUTES_DIRS", "")
    if env.strip():
        return [os.path.abspath(p.strip()) for p in env.split(",") if p.strip()]
    # 默认目录
    base = os.path.abspath(os.path.join(os.getcwd(), "data"))
    return [
        os.path.join(base, "minutes"),
        os.path.join(base, "recordings"),
    ]


def _read_text_file(path: str, limit: int = 200_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(limit)
    except Exception:
        return ""


def _hash_id(path: str, mtime: float) -> str:
    h = hashlib.sha1()
    h.update(path.encode("utf-8"))
    h.update(str(int(mtime)).encode("utf-8"))
    return h.hexdigest()[:16]


def _extract_time_from_name(name: str) -> str | None:
    # 支持 2025-11-10_14-30 或 2025_11_10 10:30 等
    m = re.search(r"(20\d{2})[-_]?(\d{1,2})[-_]?(\d{1,2})(?:[ _-](\d{1,2})[:._-]?(\d{1,2}))?", name)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4) or 9)
    mm = int(m.group(5) or 0)
    try:
        dt = datetime(y, mo, d, hh, mm)
        return dt.isoformat()
    except Exception:
        return None


def _guess_speaker(text: str, fallback: str) -> str:
    # 从正文首段猜测主讲人
    head = (text or "")[:1000]
    m = re.search(r"(主讲|主持|讲者|发言|分析师|嘉宾)[:：]\s*([^\n，,。:：]{2,20})", head)
    if m:
        return m.group(2).strip()
    # 从文件名猜测
    fb = fallback.replace("_", " ").replace("-", " ")
    m2 = re.search(r"(?:会议|纪要|路演)\s*([^\s]{2,20})", fb)
    if m2:
        return m2.group(1).strip()
    return fallback


def _classify_tone(text: str) -> str:
    t = text or ""
    if re.search(r"(看多|乐观|积极|超预期|改善|提价|增持|买入)", t):
        return "positive"
    if re.search(r"(看空|谨慎|负面|下行|风险|下滑|回调|卖出|减持)", t):
        return "negative"
    return "neutral"


def _summarize_locally(text: str, limit_cn: int = 300) -> str:
    # 简单本地摘要：取前几段非空行拼接到限定长度（中文近似按字符计）
    raw = (text or "").strip()
    if not raw:
        return "ai: 信息有限"
    parts = [p.strip() for p in raw.splitlines() if p.strip()]
    acc = []
    length = 0
    for p in parts[:12]:
        for ch in p:
            length += 1
            if length > limit_cn:
                acc.append("…")
                return "ai: " + "".join(acc)
            acc.append(ch)
        acc.append("。")
        if length > limit_cn:
            break
    return "ai: " + "".join(acc).strip("。")


def _cache_get(db: Session, key: str) -> dict | None:
    try:
        row = db.get(SyncState, key)
        if row and row.value:
            if isinstance(row.value, dict):
                return row.value  # type: ignore[return-value]
            return json.loads(row.value)
    except Exception:
        return None
    return None


def _cache_set(db: Session, key: str, val: dict) -> None:
    try:
        row = db.get(SyncState, key)
        if not row:
            row = SyncState(key=key, value=val)
        else:
            row.value = val
        db.add(row)
        db.commit()
    except Exception:
        db.rollback()


def _collect_minutes_items(refresh: bool = False, limit: int = 500) -> list[dict]:
    dirs = [p for p in _default_minutes_dirs() if os.path.isdir(p)]
    if not dirs:
        return []
    items: list[dict] = []
    for base in dirs:
        for root, _, files in os.walk(base):
            for fn in files:
                path = os.path.join(root, fn)
                ext = os.path.splitext(path)[1].lower()
                if ext not in TEXT_EXTS and ext not in AUDIO_EXTS:
                    continue
                try:
                    st = os.stat(path)
                except Exception:
                    continue
                mid = _hash_id(path, st.st_mtime)
                iso = _extract_time_from_name(fn) or datetime.fromtimestamp(st.st_mtime).isoformat()
                is_audio = ext in AUDIO_EXTS
                text = "" if is_audio else _read_text_file(path)
                speaker = _guess_speaker(text, os.path.splitext(fn)[0])
                item = {
                    "channel": "minutes",
                    "id": mid,
                    "path": path,
                    "time": iso,
                    "timestamp": iso,
                    "sender_name": speaker,  # 主讲人
                    "talker_name": None,
                    "message_type": "录音" if is_audio else "纪要",
                    "type": "minutes",
                    "content": text,
                    "content_text": text,
                    "derived": {
                        "category": "会议",
                        "tone": _classify_tone(text),
                    },
                    "meta": {
                        "need_transcript": bool(is_audio and not text),
                        "file_mtime": int(st.st_mtime),
                    },
                }
                items.append(item)
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break
        if len(items) >= limit:
            break
    # 按时间降序
    items.sort(key=lambda x: x.get("time") or "", reverse=True)
    return items


@router.get("")
def list_minutes(q: str | None = None, limit: int = Query(100, ge=1, le=500), refresh: bool = False) -> dict:
    """列出本地会议纪要/录音文件，生成≤300字的主题摘要。"""
    db: Session = SessionLocal()
    try:
        base_items = _collect_minutes_items(refresh=refresh, limit=limit)
        if q:
            ql = q.strip().lower()
            base_items = [it for it in base_items if (ql in (it.get("sender_name") or "").lower()) or (ql in (it.get("content_text") or "").lower()) or (ql in (os.path.basename(it.get("path") or "")).lower())]
        # 工具模型摘要（可选）
        conf = load_ai_config()
        has_key = bool(conf.get("api_key"))
        to_summarize: list[dict] = []
        out: list[dict] = []
        for it in base_items:
            cache_key = f"minutes:{it['id']}"
            cached = _cache_get(db, cache_key)
            if cached and isinstance(cached, dict) and "summary" in cached:
                it["summary"] = cached.get("summary")
                it["summary_origin"] = cached.get("summary_origin") or "tool"
                it["derived"]["tone"] = cached.get("tone") or it["derived"].get("tone")
                out.append(it)
                continue
            to_summarize.append({"id": it["id"], "time": it["time"], "sender": it["sender_name"], "content": it["content_text"]})
            out.append(it)
        # 调用小模型生成纪要主题与要点（<=300字）
        if to_summarize:
            try:
                if has_key:
                    # 定义 minutes_summary 提示词；若未在 ai_config 中定义，则给出默认
                    if "minutes_summary" not in DEFAULT_TOOL_PROMPTS:
                        pass  # 由 llm_client 的 DEFAULT_TOOL_PROMPTS 提供
                    feats = extract_message_features(to_summarize, batch_size=1, concurrency=4, temperature=0.1, prompt_key="minutes_summary")
                    for it in out:
                        fid = str(it["id"])
                        f = feats.get(fid) or {}
                        summ = (f.get("summary") or "").strip()
                        if summ and not summ.lower().startswith("ai:"):
                            summ = "ai: " + summ
                        # 限制 300 字
                        if summ:
                            txt = summ.replace("\n", " ")
                            if len(txt) > 302:
                                txt = txt[:300] + "…"
                            it["summary"] = txt
                            it["summary_origin"] = "tool"
                            it["derived"]["tone"] = f.get("tone") or it["derived"].get("tone")
                            _cache_set(db, f"minutes:{it['id']}", {"summary": txt, "summary_origin": "tool", "tone": it["derived"]["tone"]})
                        else:
                            local = _summarize_locally(it.get("content_text") or "")
                            it["summary"] = local
                            it["summary_origin"] = "fallback"
                            _cache_set(db, f"minutes:{it['id']}", {"summary": local, "summary_origin": "fallback", "tone": it["derived"]["tone"]})
                else:
                    for it in out:
                        local = _summarize_locally(it.get("content_text") or "")
                        it["summary"] = local
                        it["summary_origin"] = "fallback"
                        _cache_set(db, f"minutes:{it['id']}", {"summary": local, "summary_origin": "fallback", "tone": it["derived"]["tone"]})
            except Exception:
                for it in out:
                    local = _summarize_locally(it.get("content_text") or "")
                    it["summary"] = local
                    it["summary_origin"] = "fallback"
                    _cache_set(db, f"minutes:{it['id']}", {"summary": local, "summary_origin": "fallback", "tone": it["derived"]["tone"]})
        return {"items": out, "total": len(out), "dirs": _default_minutes_dirs()}
    finally:
        db.close()


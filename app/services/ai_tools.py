from __future__ import annotations

import json
import re
import logging
from typing import Any, Dict, Iterable, List
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy.orm import Session

from ..models import Message
from .llm_client import DEFAULT_TOOL_PROMPTS, load_ai_config, siliconflow_tool_chat

# Optional JSON5 for lenient parsing; fall back to stdlib json when unavailable
try:  # pragma: no cover - optional dependency
    import json5  # type: ignore
    _HAS_JSON5 = True
except Exception:  # pragma: no cover
    json5 = None  # type: ignore
    _HAS_JSON5 = False

logger = logging.getLogger(__name__)


# ----------------------------- helpers -----------------------------

def _batched(iterable: Iterable[Dict[str, Any]], size: int) -> Iterable[List[Dict[str, Any]]]:
    batch: List[Dict[str, Any]] = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _tool_prompt_payload(messages: List[Dict[str, Any]], prompt_conf: Dict[str, str]) -> List[Dict[str, str]]:
    """Build JSON payload for the tool model with defensive normalization.

    Some callers may pass `datetime` objects (e.g., from DB) in the `time` field,
    which are not JSON-serializable by default. Normalize typical fields to avoid
    `TypeError: Object of type datetime is not JSON serializable`.
    """
    system_prompt = prompt_conf.get("system") or DEFAULT_TOOL_PROMPTS["message_summary"]["system"]
    user_template = prompt_conf.get("user") or DEFAULT_TOOL_PROMPTS["message_summary"]["user"]

    norm_messages: List[Dict[str, Any]] = []
    for m in messages:
        try:
            mid = m.get("id")
            t = m.get("time") or m.get("timestamp")
            if hasattr(t, "isoformat"):
                t = t.isoformat()  # datetime -> ISO string
            sender = m.get("sender") or m.get("sender_name")
            content = m.get("content") or m.get("content_text") or m.get("text")
            norm_messages.append(
                {
                    "id": str(mid) if mid is not None else "",
                    "time": t,
                    "sender": str(sender) if sender is not None else "",
                    "content": str(content) if content is not None else "",
                }
            )
        except Exception:
            # Best-effort fallback: convert values that have isoformat(); else use raw
            try:
                norm_messages.append({k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in m.items()})  # type: ignore[arg-type]
            except Exception:
                norm_messages.append({"id": "", "time": None, "sender": "", "content": ""})

    payload_json = json.dumps(norm_messages, ensure_ascii=False)
    if "{{messages_json}}" in user_template:
        user_content = user_template.replace("{{messages_json}}", payload_json)
    else:
        user_content = user_template + "\n\n数据：\n" + payload_json
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


# ------------------------- tool extraction -------------------------

def extract_message_features(
    messages: List[Dict[str, Any]],
    batch_size: int = 1,  # 改为逐条调用
    concurrency: int = 8,
    temperature: float = 0.1,
    *,
    prompt_key: str = "message_summary",
    model_override: str | None = None,
    route_key: str | None = None,
) -> Dict[str, Dict[str, Any]]:
    """逐条调用小模型提取特征（不再批处理）"""

    if concurrency < 1:
        concurrency = 1

    conf = load_ai_config()
    tool_prompt_conf = (conf.get("tool_prompts") or {}).get(prompt_key) or DEFAULT_TOOL_PROMPTS.get(prompt_key) or DEFAULT_TOOL_PROMPTS["message_summary"]

    # 准备每条消息
    prepared: List[Dict[str, Any]] = []
    for msg in messages:
        msg_id = msg.get("id") or msg.get("time") or msg.get("message_id") or ""
        msg_id = str(msg_id)
        if not msg_id:
            continue
        prepared.append({
            "id": msg_id,
            "time": msg.get("time") or msg.get("timestamp"),
            "sender": msg.get("sender") or msg.get("sender_name"),
            "content": msg.get("content") or msg.get("content_text") or msg.get("text"),
        })

    errors: List[str] = []
    debug: List[Dict[str, Any]] = []

    def _process_single(single_msg: Dict[str, Any]) -> tuple[str, Dict[str, Any] | None, Dict[str, Any]]:
        """处理单条消息，返回 (msg_id, result)"""
        msg_id = single_msg["id"]
        content = None
        try:
            # 构造单条消息的 prompt（包装成数组以兼容现有格式）
            prompt = _tool_prompt_payload([single_msg], tool_prompt_conf)
            content = siliconflow_tool_chat(
                prompt,
                temperature=temperature,
                model_override=model_override,
                route_key=route_key,
            )
            
            if not content or not isinstance(content, str):
                raise ValueError(f"API返回为空或非字符串: {type(content)}")
            
            # 尝试从返回内容中提取JSON（可能被markdown代码块包围）
            content_clean = content.strip()
            original_content = content_clean  # 保存原始内容用于错误日志
            
            # 记录原始返回（前1000字符），便于诊断
            logger.debug("小模型原始返回 [%s] (前1000字符): %s", msg_id, original_content[:1000])
            
            # 移除可能的markdown代码块标记
            if content_clean.startswith("```"):
                # 找到第一个换行后的内容
                lines = content_clean.split("\n", 1)
                if len(lines) > 1:
                    content_clean = lines[1]
                # 也尝试移除开头标记
                if content_clean.startswith("json"):
                    content_clean = content_clean[4:].lstrip()
                elif content_clean.startswith("JSON"):
                    content_clean = content_clean[4:].lstrip()
            if content_clean.endswith("```"):
                content_clean = content_clean.rsplit("```", 1)[0].rstrip()
            
            # 尝试多种方式解析JSON
            data = None
            json_error = None
            
            # 方法1: 直接解析
            try:
                data = json.loads(content_clean)
            except json.JSONDecodeError as je:
                json_error = je
                # 尝试使用 json5（如可用）更宽松地解析（支持单引号、注释等）
                if data is None and _HAS_JSON5:
                    try:
                        data = json5.loads(content_clean)  # type: ignore
                    except Exception:
                        pass
                # 方法2: 查找JSON数组 [ ... ]
                array_match = re.search(r'\[[^\]]*(?:\{[^}]*\}[^\]]*)*\]', content_clean, re.DOTALL)
                if array_match:
                    try:
                        data = json.loads(array_match.group(0))
                    except json.JSONDecodeError:
                        # 再次尝试 json5
                        if _HAS_JSON5:
                            try:
                                data = json5.loads(array_match.group(0))  # type: ignore
                            except Exception:
                                pass
                
                # 方法3: 查找JSON对象 { ... }（更健壮的匹配）
                if data is None:
                    # 尝试找到最外层的大括号对
                    brace_start = content_clean.find('{')
                    if brace_start >= 0:
                        brace_count = 0
                        brace_end = -1
                        for i in range(brace_start, len(content_clean)):
                            if content_clean[i] == '{':
                                brace_count += 1
                            elif content_clean[i] == '}':
                                brace_count -= 1
                                if brace_count == 0:
                                    brace_end = i
                                    break
                        if brace_end > brace_start:
                            try:
                                json_str = content_clean[brace_start:brace_end+1]
                                data = json.loads(json_str)
                            except json.JSONDecodeError:
                                if _HAS_JSON5:
                                    try:
                                        data = json5.loads(json_str)  # type: ignore
                                    except Exception:
                                        pass
                
                # 方法4: 尝试提取所有可能的JSON对象
                if data is None:
                    json_matches = re.finditer(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', content_clean, re.DOTALL)
                    for match in json_matches:
                        try:
                            candidate = match.group(0)
                            data = json.loads(candidate)
                            break
                        except json.JSONDecodeError:
                            if _HAS_JSON5:
                                try:
                                    data = json5.loads(candidate)  # type: ignore
                                    break
                                except Exception:
                                    pass
                            continue
                
                # 如果所有方法都失败，抛出详细错误
                if data is None:
                    error_detail = f"JSON解析失败: {json_error.msg if json_error else '未找到有效JSON'} (位置 {json_error.pos if json_error else 'N/A'})"
                    logger.warning("小模型返回内容无法解析 [%s]: %s | 原始返回(前500字符): %s", msg_id, error_detail, original_content[:500])
                    raise ValueError(f"{error_detail}。原始返回(前300字符): {original_content[:300]}")
            
            # 统一成 dict：允许返回 list[str|dict] 或单个 string
            if isinstance(data, list) and len(data) > 0:
                item = data[0]
            elif isinstance(data, dict):
                # 某些模型会包一层 {"items": [...]}
                if "items" in data and isinstance(data["items"], list) and data["items"]:
                    item = data["items"][0]
                else:
                    item = data
            else:
                item = data

            if not isinstance(item, dict):
                # 宽松兼容：若返回 string，则当作 summary 文本
                if isinstance(item, str):
                    item = {"summary": item}
                else:
                    raise ValueError(f"返回元素不是dict: {type(item)}")
            
            # 提取核心字段
            summary = str(item.get("summary") or "").strip()
            if not summary:
                # 宽松兜底：有时模型仅返回了 key_info/markdown 等字段，或解析失败
                alt = str(item.get("key_info") or item.get("markdown") or "").strip()
                if alt:
                    summary = alt
                else:
                    # 最后兜底，给一个占位，避免上层视为失败
                    summary = "ai: 信息有限"
            if not summary.lower().startswith("ai:"):
                summary = f"ai: {summary}"

            # Optional: refined minutes / structured transcript
            refined = ""
            try:
                refined = str(
                    item.get("refined")
                    or item.get("content_refined")
                    or item.get("refined_content")
                    or item.get("minutes_refined")
                    or ""
                ).strip()
            except Exception:
                refined = ""
            
            # 禁止截断tool派生的文字，不设文字上限
            
            # 会议号处理
            meeting_number_raw = item.get("meeting_number") or ""
            meeting_number_digits = re.sub(r"\D", "", str(meeting_number_raw))
            meeting_number = meeting_number_digits if 9 <= len(meeting_number_digits) <= 13 else ""
            
            # tone 标准化（支持新增的 meeting 类型）
            tone = str(item.get("tone") or "neutral").lower()
            allowed_tones = {"bullish", "bearish", "neutral", "meeting", "positive", "negative"}
            if tone not in allowed_tones:
                tone = "neutral"
            
            # confidence
            confidence = float(item.get("confidence", 0.5))
            confidence = max(0.0, min(1.0, confidence))  # clamp to [0, 1]
            
            dbg = {"id": msg_id, "ok": True, "raw": (content[:500] if isinstance(content, str) else str(type(content))) }
            return msg_id, {
                "summary": summary,
                "meeting_number": meeting_number,
                "tone": tone,
                "confidence": confidence,
                "refined": refined,
                # 保留兼容字段（用于后续可能的扩展）
                "keywords": [],
                "platform": "",
                "category": "",
            }, dbg
        except Exception as exc:
            raw_preview = None
            try:
                # 尝试截取原始返回文本，便于调试
                if content:
                    raw_preview = content[:2000] if isinstance(content, str) else str(content)[:2000]
            except Exception:
                raw_preview = None
            
            error_msg = f"{msg_id}: {exc}"
            errors.append(error_msg)
            
            # 记录详细的错误日志（前20个错误全部记录，之后每10个记录一次）
            should_log = len(errors) <= 20 or (len(errors) % 10 == 0)
            if should_log:
                logger.warning(
                    "小模型提取失败 [%s]: %s | 原始返回(前800字符): %s",
                    msg_id,
                    str(exc),
                    raw_preview[:800] if raw_preview else "(无返回内容)"
                )
            
            dbg = {"id": msg_id, "ok": False, "error": str(exc)}
            if raw_preview:
                dbg["raw"] = raw_preview[:1000]  # 限制debug信息长度
            return msg_id, None, dbg

    results: Dict[str, Dict[str, Any]] = {}
    
    # 并发处理所有消息
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_map = {executor.submit(_process_single, msg): msg for msg in prepared}
        for future in as_completed(future_map):
            try:
                msg_id, result, dbg = future.result()
                if isinstance(dbg, dict):
                    debug.append(dbg)
                if result:
                    results[msg_id] = result
            except Exception as exc:
                errors.append(str(exc))

    if errors:
        results["__errors__"] = errors
    results["__debug__"] = debug
    logger.warning("小模型提取失败 (部分): %s", "; ".join(errors[:5]))  # 只记录前5个错误

    return results


# ---------------------------- adapters ----------------------------

def build_ai_input_messages(messages: List[Dict[str, Any]], features: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    for msg in messages:
        msg_id = str(msg.get("id") or msg.get("time") or msg.get("message_id") or len(enriched))
        feature = features.get(msg_id, {})
        enriched.append(
            {
                "id": msg_id,
                "time": msg.get("time") or msg.get("timestamp"),
                "sender": msg.get("sender_name") or msg.get("sender_id") or msg.get("sender"),
                "talker": msg.get("talker_name") or msg.get("chat_id"),
                "direction": msg.get("direction"),
                "message_type": msg.get("message_type") or msg.get("type"),
                "content": msg.get("content") or msg.get("content_text") or msg.get("text"),
                "importance": msg.get("importance_score"),
                "keywords": feature.get("keywords", []),
                "meeting_number": feature.get("meeting_number", ""),
                "platform": feature.get("platform", ""),
                "category": feature.get("category", ""),
                "summary": feature.get("summary", ""),
                # Normalize tone for downstream filters/modules
                "tone": str(feature.get("tone", "neutral")).lower(),
            }
        )
    return enriched


# ---------------------------- two-pass API ----------------------------

def ensure_message_features(
    db: Session,
    messages: List[Message],
    days_to_keep: int = 7,
    *,
    force: bool = False,
    batch_size: int = 20,
    concurrency: int = 3,
    temperature: float = 0.1,
) -> dict:
    """Overlay tool-model outputs onto Message.derived.

    Assumes populate_fallback_derived has already provided an initial snapshot.
    This function does NOT do any local fallback; it only updates rows where the tool
    produced results. summary_origin will be set to "tool" for updated rows.
    """
    if not messages:
        return

    cutoff = datetime.utcnow() - timedelta(days=days_to_keep)
    to_extract: List[Dict[str, Any]] = []
    updated = False
    updated_count = 0
    applied: List[Dict[str, Any]] = []

    def _vis_len(s: str) -> int:
        try:
            return len((s or '').replace('\n',' ').replace('\r',' ').replace('\t',' ').strip())
        except Exception:
            return len(s or '')

    for msg in messages:
        # Skip very old messages unless force=True (explicit derive request)
        if (not force) and msg.timestamp and msg.timestamp < cutoff:
            continue

        text = (msg.content_text or "").strip()
        if not text:
            try:
                meta = msg.meta or {}
                contents = meta.get("contents") if isinstance(meta, dict) else None
                parts: list[str] = []
                if isinstance(contents, dict):
                    # Prefer true body content first; title/url are last-resort hints only
                    for k in ("content", "desc", "title", "url"):
                        v = contents.get(k)
                        if isinstance(v, str) and v.strip():
                            parts.append(v.strip())
                text = " \n".join(parts).strip()
            except Exception:
                text = ""
        # Skip short/low-signal texts to reduce token usage
        if not text or _vis_len(text) < 20:
            continue

        # Skip non-text messages (image/file/video) entirely
        try:
            t = (msg.type or '').lower()
            if t in ('image','file','video'):
                continue
        except Exception:
            pass

        derived = msg.derived if isinstance(msg.derived, dict) else {}
        has_summary = bool(derived.get("summary"))
        origin = str(derived.get("summary_origin") or "").lower()
        if (not force) and has_summary and origin == "tool":
            continue

        to_extract.append({
            "id": str(msg.id),
            "content": text,
            "time": msg.timestamp.isoformat() if msg.timestamp else None,
        })

    if not to_extract:
        if updated:
            db.commit()
        return {"updated": 0, "errors": []}

    # Per-channel model override: prefer tool_model_messages when configured
    model_ovr = None
    try:
        conf = load_ai_config()
        model_ovr = conf.get("tool_model_messages") or conf.get("tool_model")
    except Exception:
        model_ovr = None

    features = extract_message_features(
        to_extract,
        batch_size=batch_size,
        concurrency=concurrency,
        temperature=temperature,
        prompt_key="message_summary",
        model_override=model_ovr,
        route_key="messages",
    )
    tool_errors = features.pop("__errors__", None)
    tool_debug = features.pop("__debug__", None)
    if tool_errors:
        logger.warning("小模型提取存在部分失败：%s", "; ".join(tool_errors))

    for msg in messages:
        fid = str(msg.id)
        data = features.get(fid)
        if not data:
            continue
        
        summary_text = str(data.get("summary") or "").strip()
        if not summary_text:
            continue
        if not summary_text.lower().startswith("ai:"):
            summary_text = f"ai: {summary_text}"
        
        meeting_number = str(data.get("meeting_number") or "").strip()
        tone = str(data.get("tone") or "neutral").lower()
        confidence = float(data.get("confidence", 0.5))
        
        # 从 summary/正文提取平台信息（辅助逻辑）
        platform = ""
        summary_lower = summary_text.lower()
        if "腾讯" in summary_text or "wemeet" in summary_lower:
            platform = "腾讯"
        elif "进门" in summary_text or "jinmen" in summary_lower:
            platform = "进门"
        elif "飞书" in summary_text or "feishu" in summary_lower:
            platform = "飞书"
        elif "zoom" in summary_lower:
            platform = "Zoom"
        elif "teams" in summary_lower:
            platform = "Teams"
        elif "钉钉" in summary_text:
            platform = "钉钉"
        elif "外呼" in summary_text or re.search(r"(?i)tel|电话|phone", summary_text):
            platform = "电话"
        
        # 前置平台与会议号到摘要中：ai: <platform> <number> <body>
        body = re.sub(r'^\s*ai:\s*', '', summary_text, flags=re.IGNORECASE).strip()
        prefix_parts = []
        if platform:
            prefix_parts.append(platform)
        if meeting_number:
            prefix_parts.append(meeting_number)
        prefix = ' '.join(prefix_parts).strip()
        display_summary = f"ai: {prefix} {body}".strip() if prefix else f"ai: {body}"

        new_part: Dict[str, Any] = {
            "summary": display_summary,
            "meeting_number": meeting_number,
            "platform": platform,
            "tone": tone,
            "confidence": confidence,
            "summary_origin": "tool",
            # 兼容字段（保持向后兼容）
            "keywords": data.get("keywords") or [],
            "category": data.get("category") or "",
        }
        
        # Always assign a new dict instance so SQLAlchemy marks column as changed
        before = msg.derived if isinstance(msg.derived, dict) else {}
        merged = dict(before)
        merged.update({k: v for k, v in new_part.items()})
        msg.derived = merged
        db.add(msg)
        updated = True
        updated_count += 1
        try:
            applied.append({
                "id": int(getattr(msg, 'id')),
                "summary": summary_text,
                "origin": "tool",
            })
        except Exception:
            pass

    if updated:
        db.commit()
    return {"updated": updated_count, "errors": tool_errors or [], "debug": tool_debug or [], "applied": applied}


def populate_fallback_derived(
    db: Session,
    messages: List[Message],
    days_to_keep: int = 7,
    *,
    force: bool = False,
    summary_limit: int = 50,
) -> int:
    """Write fallback snapshot first for instant UI.

    Skip rows that already have summary_origin=tool unless force is True.
    Returns number of rows updated.
    """
    cutoff = datetime.utcnow() - timedelta(days=days_to_keep)
    changed = 0

    def _fallback_keywords(text: str, topk: int = 5) -> List[str]:
        if not text:
            return []
        t = re.sub(r"https?://\S+", " ", text)
        t = re.sub(r"#[A-Za-z0-9_]+|@\S+", " ", t)
        t = re.sub(r"\b\d{5,}\b", " ", t)
        tokens = re.split(r"[^\w\u4e00-\u9fff]+", t)
        tokens = [k.strip().lower() for k in tokens if k.strip()]
        stop = {"的","了","和","是","在","对","及","与","于","以及","相关","我们","他们","你们","你","我","他","她","它","这个","那个","进行","公司","行业","板块","认为","建议","报告","最新","今天","明天","市场","影响","可能"}
        freq: Dict[str,int] = {}
        for k in tokens:
            if k in stop:
                continue
            if re.fullmatch(r"\d{5,}", k):
                continue
            freq[k] = freq.get(k, 0) + 1
        return [w for w,_ in sorted(freq.items(), key=lambda x:x[1], reverse=True)[:topk]]

    def _fallback_summary(text: str, limit: int) -> str:
        if not text:
            return ""
        t = re.sub(r"https?://\S+", "", text)
        t = re.sub(r"[\s]+", " ", t).strip()
        return (t[:limit] + ("…" if len(t) > limit else ""))

    def _fallback_meeting(text: str) -> tuple[str,str]:
        if not text:
            return "",""
        # Robust number detection: 9–13 digits, 9–10 digits, hyphenated forms, +86-, 400-xxx-xxxx
        patterns = [
            r"(?<!\d)(\d{9,13})(?!\d)",
            r"(?<!\d)(\d{9,10})(?!\d)",
            r"(\d{3}[-\s]?\d{3}[-\s]?\d{3,6})",
            r"\+?86[-\s]?(\d{3}[-\s]?\d{3}[-\s]?\d{3,6}|\d{8,12})",
            r"(400[-\s]?\d{3}[-\s]?\d{4})",
        ]
        number = ""
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                g = m.group(1) if m.groups() else m.group(0)
                number = re.sub(r"\D", "", g)
                break
        platform = ""
        low = text.lower()
        if "腾讯会议" in text or "wemeet" in low or "meeting.tencent.com" in low:
            platform = "腾讯"
        elif "进门财经" in text or "jinmen" in low:
            platform = "进门"
        elif "飞书" in text or "feishu" in low or "lark" in low:
            platform = "飞书"
        elif "zoom" in low:
            platform = "Zoom"
        elif "teams" in low or "microsoft.com" in low:
            platform = "Teams"
        elif "钉钉" in text or "dingtalk" in low:
            platform = "钉钉"
        elif "电话会" in text or "电话会议" in text or "外呼" in text or re.search(r"(?i)tel|电话|phone", text):
            platform = "电话"
        return number, platform

    def _infer_tone(text: str) -> str:
        low = (text or '').lower()
        pos = ('看多','利好','上调','增持','上涨','改善','超预期','提价','回暖','反弹','增长','积极','强势')
        neg = ('看空','利空','下调','减持','下跌','承压','不及预期','回落','下滑','风险','下行','疲弱')
        if any(p in text for p in pos) or any(p in low for p in ('bullish','positive')):
            return 'bullish'
        if any(n in text for n in neg) or any(n in low for n in ('bearish','negative')):
            return 'bearish'
        return 'neutral'

    def _extract_key_info(text: str) -> str:
        """Heuristic key_info from full body: prefer 观点/结论/主旨，再看 建议/下一步，兼顾标的/行业。"""
        if not text:
            return ''
        t = re.sub(r"\s+", " ", text)
        # 1) 明确标注的观点/结论/主旨
        m = re.search(r"(?:观点|结论|主旨|判断)[:：]\s*([^；。\n]{6,60})", t)
        if m:
            return m.group(1).strip()
        # 2) 建议/下一步
        m = re.search(r"(?:建议|下一步|行动|策略)[:：]\s*([^；。\n]{6,60})", t)
        if m:
            return m.group(1).strip()
        # 3) 简要抽取前一句较长语句
        m = re.search(r"([^；。\n]{6,60})(?:；|。|\n)", t)
        return (m.group(1).strip() if m else t[:60]).strip()

    for msg in messages:
        if msg.timestamp and msg.timestamp < cutoff:
            continue
        derived = msg.derived if isinstance(msg.derived, dict) else {}
        origin = str(derived.get("summary_origin") or "").lower()
        if origin == "tool" and not force:
            continue
        text = (msg.content_text or "").strip()
        if not text:
            meta = msg.meta or {}
            contents = meta.get("contents") if isinstance(meta, dict) else None
            parts: list[str] = []
            if isinstance(contents, dict):
                for k in ("title", "desc", "content", "url"):
                    v = contents.get(k)
                    if isinstance(v, str) and v.strip():
                        parts.append(v.strip())
            text = " \n".join(parts).strip()
        if not text:
            continue

        kws = _fallback_keywords(text, topk=5)
        summ = _fallback_summary(text, limit=summary_limit)
        num, plat = _fallback_meeting(text)
        # key_info: 倾向 观点/结论/主旨/建议，长于 6 字。
        key_src = _extract_key_info(text) or summ or text[:60]
        # 轻量实体增强：若 key_info 中未明显包含“行业/标的”，且空间允许，则补充首个行业/代码
        ents = _detect_entities(text)
        enrich_parts: list[str] = []
        base = key_src
        def _present(s: str, frag: str) -> bool:
            return frag and (frag in s)
        # prefer industry then ticker
        if ents.get("industries"):
            ind = ents["industries"][0]
            if not _present(base, ind):
                enrich_parts.append(ind)
        for group in ("a", "hk", "us"):
            codes = ents.get(group) or []
            if codes:
                code = codes[0]
                if not _present(base, code):
                    enrich_parts.append(code)
                    break
        if enrich_parts:
            candidate = (base + " | " + " ".join(enrich_parts)).strip()
            key_src = candidate
        def _clip_vis(s: str, limit: int) -> str:
            acc = []
            for ch in (s or "").strip():
                if len("".join(acc).replace(" ", "")) >= limit:
                    break
                acc.append(ch)
            return "".join(acc).strip()
        key_info = _clip_vis(key_src, 30)
        tone = _infer_tone(text)
        # 形成可阅读的 summary_full（结论/建议/要点拼接）
        parts = []
        if key_info:
            parts.append(f"结论：{key_info}")
        sug = re.search(r"(?:建议|下一步|行动)[:：]\s*([^；。\n]{4,60})", text)
        if sug:
            parts.append(f"建议：{sug.group(1).strip()}")
        # 选取一条依据
        ev = re.search(r"(?:依据|原因|背景)[:：]\s*([^；。\n]{4,60})", text)
        if ev:
            parts.append(f"依据：{ev.group(1).strip()}")
        summary_full = "；".join(parts)[:180]
        new_derived = {
            "keywords": kws,
            "meeting_number": num,
            "platform": plat,
            "tone": tone,
            "summary": f"fallback: {summ}" if summ else "fallback: ",
            "summary_origin": "fallback",
            "key_info": key_info,
            "key_info_origin": "fallback",
            "summary_full": summary_full,
        }
        before = msg.derived if isinstance(msg.derived, dict) else {}
        # Do not override tool results unless force=True
        try:
            if (not force) and isinstance(before, dict) and str(before.get("summary_origin") or '').lower() == 'tool':
                continue
        except Exception:
            pass
        if any(before.get(k) != v for k, v in new_derived.items()):
            merged = dict(before)
            merged.update(new_derived)
            msg.derived = merged
            db.add(msg)
            changed += 1

    if changed:
        db.commit()
    return changed
# ---------------------- lightweight entity dictionary ----------------------

_DEFAULT_INDUSTRIES: list[str] = [
    "半导体", "芯片", "集成电路", "算力", "人工智能", "AI", "云计算",
    "新能源", "光伏", "储能", "风电", "锂电", "动力电池",
    "煤炭", "石油", "有色", "钢铁", "化工", "机械",
    "汽车", "汽车零部件", "整车", "电动车",
    "银行", "券商", "保险",
    "白酒", "消费", "家电",
    "医药", "生物", "医疗",
    "军工", "国防",
    "地产", "房地产",
    "通信", "电力", "公用事业",
    "TMT", "软件", "游戏", "传媒", "互联网", "电商", "物流", "航运", "航空",
]

def _load_external_industries() -> list[str]:
    """Optionally load extra industries from data/entities.json: {"industries": [...]}"""
    try:
        import os, json
        path = os.path.abspath(os.path.join(os.getcwd(), 'data', 'entities.json'))
        if not os.path.exists(path):
            return []
        with open(path, 'r', encoding='utf-8') as f:
            j = json.load(f)
        inds = j.get('industries') if isinstance(j, dict) else None
        if isinstance(inds, list):
            return [str(x) for x in inds if isinstance(x, (str, int))]
        return []
    except Exception:
        return []

def _detect_entities(text: str) -> dict[str, list[str]]:
    """Detect lightweight entities: A/H/US tickers and industries.

    - A-share: 60x/00x/30x/68x patterns (strict 6 digits)
    - HK: 4 digits + .HK or HKxxxx
    - US: after prefixes (NASDAQ|NYSE|AMEX|US:|Ticker:|代码:) + 1-5 uppercase letters
    - industries: substring match from a small dictionary
    """
    if not text:
        return {"a": [], "hk": [], "us": [], "industries": []}
    low = text.lower()
    # A-share 6-digit codes
    a_pat = re.compile(r"(?<!\d)(?:60\d{4}|601\d{3}|603\d{3}|605\d{3}|000\d{3}|001\d{3}|002\d{3}|300\d{3}|301\d{3}|688\d{3})(?!\d)")
    a_codes = a_pat.findall(text)
    # HK codes
    hk_pat1 = re.compile(r"\b\d{4}\.(?:hk|HK)\b")
    hk_pat2 = re.compile(r"\b(?:hk|HK)\d{4}\b")
    hk_codes = sorted(set(hk_pat1.findall(text) + hk_pat2.findall(text)))
    # US tickers with context
    us_pat = re.compile(r"\b(?:NASDAQ|NYSE|AMEX|US:|Ticker[:：]|代码[:：])\s*([A-Z]{1,5})\b")
    us_codes = [m.group(1) for m in us_pat.finditer(text)]
    # industries (dedup preserve order)
    inds: list[str] = []
    seen = set()
    ext_inds = _load_external_industries()
    for ind in list(dict.fromkeys(_DEFAULT_INDUSTRIES + ext_inds)):
        if ind in text and ind not in seen:
            inds.append(ind)
            seen.add(ind)
    return {"a": a_codes[:3], "hk": hk_codes[:3], "us": us_codes[:3], "industries": inds[:3]}

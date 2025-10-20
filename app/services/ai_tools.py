from __future__ import annotations

import json
import re
import logging
from typing import Any, Dict, Iterable, List
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy.orm import Session

from ..models import Message

from .llm_client import siliconflow_tool_chat, load_ai_config, DEFAULT_TOOL_PROMPTS


logger = logging.getLogger(__name__)


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
    system_prompt = prompt_conf.get("system") or DEFAULT_TOOL_PROMPTS["message_summary"]["system"]
    user_template = prompt_conf.get("user") or DEFAULT_TOOL_PROMPTS["message_summary"]["user"]
    payload_json = json.dumps(messages, ensure_ascii=False)
    if "{{messages_json}}" in user_template:
        user_content = user_template.replace("{{messages_json}}", payload_json)
    else:
        user_content = user_template + "\n\n数据：\n" + payload_json
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def extract_message_features(
    messages: List[Dict[str, Any]],
    batch_size: int = 20,
    concurrency: int = 8,
    temperature: float = 0.1,
) -> Dict[str, Dict[str, Any]]:
    """Use the tool LLM to enrich messages with keywords/meetings/summaries."""

    if concurrency < 1:
        concurrency = 1

    conf = load_ai_config()
    tool_prompt_conf = (conf.get("tool_prompts") or {}).get("message_summary") or DEFAULT_TOOL_PROMPTS["message_summary"]

    prepared_batches: List[List[Dict[str, Any]]] = []
    for chunk in _batched(messages, batch_size):
        prepared: List[Dict[str, Any]] = []
        for msg in chunk:
            msg_id = msg.get("id") or msg.get("time") or msg.get("message_id") or ""
            msg_id = str(msg_id)
            if not msg_id:
                continue
            msg.setdefault("id", msg_id)
            prepared.append({
                "id": msg_id,
                "time": msg.get("time") or msg.get("timestamp"),
                "sender": msg.get("sender") or msg.get("sender_name"),
                "content": msg.get("content") or msg.get("content_text") or msg.get("text"),
            })
        if prepared:
            prepared_batches.append(prepared)

    errors: List[str] = []

    def _process(chunk: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        nonlocal errors
        chunk_results: Dict[str, Dict[str, Any]] = {}
        try:
            prompt = _tool_prompt_payload(chunk, tool_prompt_conf)
            content = siliconflow_tool_chat(prompt, temperature=temperature)
            data = json.loads(content)
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    msg_id = str(item.get("id") or "").strip()
                    if not msg_id:
                        continue
                    summary = str(item.get("summary") or "").strip()
                    if not summary:
                        raise ValueError(f"tool model未返回摘要: id={msg_id}")
                    if not summary.lower().startswith("ai:"):
                        summary = f"ai: {summary}"
                    keywords = item.get("keywords") or []
                    if not isinstance(keywords, list):
                        keywords = []
                    # Persist fields returned by the tool model. Some fields are optional in the
                    # prompt; keep sensible defaults if missing so downstream stays stable.
                    # Normalize category/tone to expected values to improve robustness.
                    allowed_categories = {"观点", "会议", "提问", "其他"}
                    raw_cat = str(item.get("category") or "").strip()
                    category = raw_cat if raw_cat in allowed_categories else ("其他" if raw_cat else "")
                    # extra fields for email key info
                    meeting_link = item.get("meeting_link") or ""
                    # 规范化会议号：仅保留数字，且长度限定在9–13位，避免将“15/13”等误识别为会议号
                    meeting_number_raw = item.get("meeting_id") or item.get("meeting_number") or ""
                    meeting_number_digits = re.sub(r"\D", "", str(meeting_number_raw))
                    meeting_number = meeting_number_digits if 9 <= len(meeting_number_digits) <= 13 else ""
                    appointment_time = item.get("appointment_time") or ""
                    analyst = item.get("analyst") or item.get("researcher") or ""
                    organizer = item.get("organizer") or item.get("预约人") or ""
                    main_point = item.get("main_point") or ""
                    summary_full = item.get("summary_full") or item.get("full_summary") or ""
                    # compose key_info if not provided
                    key_info = str(item.get("key_info") or "").strip()
                    if not key_info:
                        parts: list[str] = []
                        if main_point:
                            parts.append(f"观点:{main_point}")
                        if analyst:
                            parts.append(f"研究员:{analyst}")
                        if organizer:
                            parts.append(f"预约:{organizer}")
                        if meeting_link:
                            parts.append(f"链接:{meeting_link}")
                        if meeting_number:
                            parts.append(f"会议号:{meeting_number}")
                        if appointment_time:
                            parts.append(f"时间:{appointment_time}")
                        key_info = " | ".join(parts)[:120]
                    chunk_results[msg_id] = {
                        "keywords": keywords,
                        "meeting_number": meeting_number,
                        "platform": item.get("platform") or item.get("meeting_platform") or "",
                        "category": category,
                        "summary": summary,
                        "tone": (item.get("tone") or "neutral").lower(),
                        "key_info": key_info,
                        "meeting_link": meeting_link,
                        "appointment_time": appointment_time,
                        "analyst": analyst,
                        "organizer": organizer,
                        "main_point": main_point,
                        "summary_full": summary_full,
                    }
        except Exception as exc:
            errors.append(str(exc))
        return chunk_results

    results: Dict[str, Dict[str, Any]] = {}
    if concurrency <= 1 or len(prepared_batches) <= 1:
        for batch in prepared_batches:
            results.update(_process(batch))
        return results

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_map = {executor.submit(_process, batch): batch for batch in prepared_batches}
        for future in as_completed(future_map):
            try:
                chunk_results = future.result()
                results.update(chunk_results)
            except Exception:
                continue

    if errors:
        results["__errors__"] = errors
        logger.warning("小模型提取失败: %s", "; ".join(errors))

    return results


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
                "tone": feature.get("tone", "neutral"),
                "key_info": feature.get("key_info", ""),
            }
        )
    return enriched


def ensure_message_features(
    db: Session,
    messages: List[Message],
    days_to_keep: int = 7,
    *,
    force: bool = False,
    batch_size: int = 20,
    concurrency: int = 8,
    temperature: float = 0.1,
) -> None:
    """Populate Message.derived with keywords/summaries via the tool model."""

    if not messages:
        return

    cutoff = datetime.utcnow() - timedelta(days=days_to_keep)
    to_extract: List[Dict[str, Any]] = []
    updated = False

    for msg in messages:
        if msg.timestamp and msg.timestamp < cutoff:
            if msg.derived:
                msg.derived = None
                db.add(msg)
                updated = True
            continue

        text = (msg.content_text or "").strip()
        # Fallback: build text from meta.contents for link/structured messages
        if not text:
            try:
                meta = msg.meta or {}
                contents = meta.get("contents") if isinstance(meta, dict) else None
                parts: list[str] = []
                if isinstance(contents, dict):
                    for k in ("title", "desc", "content", "url"):
                        v = contents.get(k)
                        if isinstance(v, str) and v.strip():
                            parts.append(v.strip())
                text = " \n".join(parts).strip()
            except Exception:
                text = ""
        if not text:
            continue

        # Skip re-extraction only when we are confident that prior derived data is
        # complete and from the tool model. Historically we only checked for
        # keywords/summary/meeting_number which caused "key_info" to remain empty
        # forever for older rows, so the frontend kept showing fallback. Make the
        # condition stricter to ensure "key_info" exists and summary came from tool.
        derived = msg.derived if isinstance(msg.derived, dict) else {}
        has_keywords = bool(derived.get("keywords"))
        has_summary = bool(derived.get("summary"))
        has_key_info = bool(str(derived.get("key_info") or "").strip())
        origin = str(derived.get("summary_origin") or "").lower()  # 'tool' | 'fallback' | ''
        meeting_present = derived.get("meeting_number") is not None
        if (
            not force
            and has_keywords
            and has_summary
            and has_key_info
            and meeting_present
            and origin == "tool"
        ):
            # Good enough – keep existing derived features.
            continue

        to_extract.append({
            "id": str(msg.id),
            "content": text,
            "time": msg.timestamp.isoformat() if msg.timestamp else None,
        })

    if not to_extract:
        if updated:
            db.commit()
        return

    features = extract_message_features(
        to_extract,
        batch_size=batch_size,
        concurrency=concurrency,
        temperature=temperature,
    )
    tool_errors = features.pop("__errors__", None)
    if tool_errors:
        # 仅记录日志，不要全局降级，否则会让成功的条目也丢失AI结果
        logger.warning("小模型提取存在部分失败：%s", "; ".join(tool_errors))

    # 本地兜底：若工具模型不可用或返回缺失，为缺失项做简易关键词与摘要提取
    def _fallback_keywords(text: str, topk: int = 5) -> List[str]:
        if not text:
            return []
        # 去掉链接/ID/数字长串
        t = re.sub(r"https?://\S+", " ", text)
        t = re.sub(r"#[A-Za-z0-9_]+|@\S+", " ", t)
        t = re.sub(r"\b\d{5,}\b", " ", t)
        # 简单分词：按非字母数字汉字切分
        tokens = re.split(r"[^\w\u4e00-\u9fff]+", t)
        tokens = [k.strip().lower() for k in tokens if k.strip()]
        stop = {"的","了","和","是","在","对","及","与","于","与","以及","相关","我们","他们","你们","你","我","他","她","它","这个","那个","进行","公司","行业","板块","观点","认为","建议","报告","最新","今天","明天","市场","影响","可能"}
        freq: Dict[str,int] = {}
        for k in tokens:
            if len(k) <= 1 or k in stop:
                continue
            freq[k] = freq.get(k,0)+1
        return [w for w,_ in sorted(freq.items(), key=lambda kv: kv[1], reverse=True)[:topk]]

    def _fallback_summary(text: str, limit: int = 50) -> str:
        if not text:
            return ""
        t = re.sub(r"https?://\S+", "", text)
        t = re.sub(r"[\s]+", " ", t).strip()
        return (t[:limit] + ("…" if len(t) > limit else ""))

    def _fallback_meeting(text: str) -> tuple[str,str]:
        if not text:
            return "",""
        number = ""
        # Heuristic: 9–13 contiguous digits near meeting context tends to be a meeting ID.
        # (Looser than tool rules but good for offline fallback.)
        m = re.search(r"(?<!\d)(\d{9,13})(?!\d)", text)
        if m:
            number = m.group(1)
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
        elif "电话会" in text or "电话会议" in text or re.search(r"(?i)tel|电话|phone", text):
            platform = "电话"
        return number, platform
    
    # Remove personal names/mentions to keep key_info focused on观点/要点
    def _strip_person_refs(s: str) -> str:
        if not s:
            return s
        t = s
        # Remove @mentions
        t = re.sub(r"@[^\s:：]{1,20}", "", t)
        # Remove leading speaker labels like “张三/王总/李老师:”
        t = re.sub(r"^(?:[\u4e00-\u9fa5]{2,4}(?:[·•][\u4e00-\u9fa5]{1,3})?(?:总|老师|同学|经理|总监|董秘|博士|先生|女士|小姐)?)\s*[:：]\s*", "", t)
        # Remove common trailing ‘表示/说/提到’ patterns with short name prefix at start
        t = re.sub(r"^(?:[\u4e00-\u9fa5]{2,4})\s*(?:表示|说|提到|认为|建议)[:：]?\s*", "", t)
        return t.strip()
    for msg in messages:
        fid = str(msg.id)
        data = features.get(fid)
        origin = "tool"
        text = (msg.content_text or "").strip()
        if not data:
            # 工具模型无返回或整体失败时，做本地兜底
            kws = _fallback_keywords(text, topk=5)
            summ = _fallback_summary(text, limit=30)
            num, plat = _fallback_meeting(text)
            data = {
                "keywords": kws,
                "summary": f"fallback: {summ}" if summ else "fallback: ",
                "meeting_number": num,
                "tone": "neutral",
            }
            origin = "fallback"
        else:
            summary_text = str(data.get("summary") or "").strip()
            if not summary_text:
                summary_text = f"fallback: {_fallback_summary(text, 30)}"
                origin = "fallback"
            elif not summary_text.lower().startswith("ai:"):
                summary_text = f"ai: {summary_text}"
            data["summary"] = summary_text
        derived = msg.derived if isinstance(msg.derived, dict) else {}
        enriched = data.copy()
        meeting_number = enriched.get("meeting_number") or ""
        # Prefer platform from tool output if present; otherwise infer from content.
        platform = (enriched.get("platform") or "").strip() or None
        lowered = text.lower()
        if not platform:
            if "腾讯会议" in text or "wemeet" in lowered or "meeting.tencent.com" in lowered:
                platform = "腾讯"
            elif "进门财经" in text or "jinmen" in lowered:
                platform = "进门"
            elif "飞书" in text or "feishu" in lowered or "lark" in lowered:
                platform = "飞书"
            elif "zoom" in lowered:
                platform = "Zoom"
            elif "teams" in lowered or "microsoft.com" in lowered:
                platform = "Teams"
            elif "钉钉" in text or "dingtalk" in lowered:
                platform = "钉钉"
        key_parts: List[str] = []
        if meeting_number:
            key_parts.append(f"{(platform or '会议')}:{meeting_number}")
        elif platform:
            key_parts.append(platform)
        # Build key_info without keywords or truncation; emphasize viewpoint
        summary = enriched.get("summary") or ""
        summary_clean = _strip_person_refs(summary)
        # Prefer “平台:会议号 | 摘要” if meeting exists, else just 摘要
        if meeting_number and (platform or "会议"):
            key_parts.append(summary_clean)
            enriched["key_info"] = " | ".join([p for p in key_parts if p]).strip()
        else:
            enriched["key_info"] = summary_clean
        # Canonicalize and persist fields expected by frontend
        if platform:
            enriched["platform"] = platform
        if data.get("category") is not None:
            enriched["category"] = data.get("category") or ""
        enriched.setdefault("tone", (data.get("tone") or "neutral").lower())
        enriched["summary_origin"] = origin

        derived.update(enriched)
        msg.derived = derived
        db.add(msg)
        updated = True

    if updated:
        db.commit()

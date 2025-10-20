from __future__ import annotations

import re
from typing import Dict, List, Iterable

from sqlalchemy.orm import Session

from ..models import EmailMessage
from .ai_tools import extract_message_features


def _html_to_text(html: str | None) -> str:
    if not html:
        return ''
    try:
        text = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.IGNORECASE)
        text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;", " ", text)
        return re.sub(r"\s+", " ", text).strip()
    except Exception:
        return html or ''


def _normalize_line(line: str) -> str:
    return re.sub(r"^[0-9一二三四五六七八九十]+[）).、\.:\-]*\s*", "", line).strip()


def _build_summary(text: str) -> str:
    norm = (text or '').replace('\r', '\n')
    raw_lines = norm.split('\n')
    lines = [re.sub(r"[；。]+$", "", ln.strip()) for ln in raw_lines]
    result: List[str] = []
    skip_prefix = (
        "主题", "路演类型", "路演方式", "内部预约人", "预约人", "券商研究员", "分析师",
        "会议链接", "会议号", "时间", "路演平台", "会议平台"
    )
    for idx, line in enumerate(lines):
        if not line:
            continue
        if any(line.startswith(p) for p in skip_prefix):
            if line.startswith("观点") and ":" in line:
                val = _normalize_line(line.split(":", 1)[1].strip())
                if val:
                    result.append(val)
            continue
        if line.startswith("观点") and ":" in line:
            val = _normalize_line(line.split(":", 1)[1].strip())
            if val:
                result.append(val)
            continue
        if line.startswith("重点关注") and ":" in line:
            val = line.split(":", 1)[1].strip()
            if val:
                result.append(f"重点:{val}")
            for j in range(idx + 1, min(idx + 5, len(lines))):
                nxt = lines[j]
                if nxt.startswith(("T链", "国产链", "弹性", "核心", "低位", "海外", "A股", "B股")):
                    result.append(nxt.strip())
                else:
                    break
            continue
        if re.match(r"^[0-9一二三四五六七八九十]+[）).、\.:\-]", line):
            result.append(_normalize_line(line))
            continue
        if re.match(r"^[-•·]", line):
            result.append(_normalize_line(line))
            continue
        if len(result) < 4 and len(line) > 6:
            result.append(line)
    if not result:
        cleaned = re.sub(r"主题[:：].*?(?:\n|$)", "", norm, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned:
            result.append(cleaned[:100])
    dedup: List[str] = []
    seen = set()
    for item in result:
        if not item:
            continue
        if item in seen:
            continue
        seen.add(item)
        dedup.append(item)
    return "；".join(dedup)[:100]


def build_email_features(items: List[dict]) -> Dict[str, dict]:
    if not items:
        return {}

    prepared: List[dict] = []
    id_map: Dict[str, dict] = {}
    for it in items:
        mid = str(it.get('id')) if it.get('id') is not None else ''
        if not mid:
            continue
        text = (it.get('body_text') or _html_to_text(it.get('body_html')) or it.get('snippet') or it.get('subject') or '').strip()
        if not text:
            continue
        trimmed = text[:4000]
        prepared.append({
            'id': mid,
            'time': it.get('sent_at'),
            'sender': it.get('from_addr') or '',
            'content': trimmed,
        })
        id_map[mid] = {
            'raw_text': trimmed,
            'subject': it.get('subject') or '',
        }

    features = extract_message_features(prepared, batch_size=8, concurrency=6, temperature=0.1) if prepared else {}
    features.pop("__errors__", None)

    results: Dict[str, dict] = {}

    def _digits_meeting(text: str) -> str:
        m = re.search(r"(?:会议号[:：]?\s*)?(\d{3}[-\s]?\d{3}[-\s]?\d{3,6}|\d{8,12})", text)
        return m.group(1) if m else ""

    def _extract(field_pattern: str, text: str) -> str:
        m = re.search(field_pattern, text, flags=re.IGNORECASE)
        return (m.group(1) if m else "").strip()

    for item in items:
        mid = str(item.get('id')) if item.get('id') is not None else ''
        if not mid:
            continue
        base = id_map.get(mid, {})
        raw_text = base.get('raw_text', '')
        feat = features.get(mid, {}).copy() if isinstance(features, dict) else {}

        meeting_link = feat.get('meeting_link') or _extract(r"会议链接[:：]?\s*(https?://\S+)", raw_text)
        if not meeting_link:
            link_match = re.search(r"https?://\S+", raw_text)
            meeting_link = link_match.group(0) if link_match else ''

        meeting_number = feat.get('meeting_number') or feat.get('meeting_id') or _digits_meeting(raw_text)
        if meeting_number and not re.fullmatch(r"\d{3}[-\s]?\d{3}[-\s]?\d{3,6}|\d{8,12}", meeting_number):
            repl = _digits_meeting(raw_text)
            if repl:
                meeting_number = repl

        appointment_time = feat.get('appointment_time') or ''
        if not appointment_time:
            for pattern in [
                r"\b(20\d{2}[/-]\d{1,2}[/-]\d{1,2}\s+\d{1,2}:\d{2})\b",
                r"\b(\d{1,2}-\d{1,2}\s*\d{1,2}:\d{2})\b",
                r"(\d{1,2}月\d{1,2}日\s*\d{1,2}:\d{2})",
            ]:
                m = re.search(pattern, raw_text)
                if m:
                    appointment_time = m.group(1)
                    break

        analyst = feat.get('analyst') or feat.get('researcher') or _extract(r"(?:券商研究员|分析师)[:：]\s*([^\s;|，。\n]{1,20})", raw_text)
        organizer = feat.get('organizer') or _extract(r"(?:内部预约人|预约人)[:：]\s*([^\s;|，。\n]{1,20})", raw_text)

        main_point = feat.get('main_point')
        if not main_point:
            match = re.search(r"(?:观点|主题)[:：]\s*([\s\S]{4,400}?)\s*(?:内部预约人|预约人|券商研究员|分析师|会议链接|会议号|时间|路演|路演类型|路演方式|重点关注|重点|$)", raw_text)
            if match:
                main_point = re.sub(r"\s+", " ", match.group(1)).strip()
        if not main_point:
            main_point = base.get('subject', '')

        summary_full = feat.get('summary_full') or _build_summary(raw_text) or main_point or ''
        summary_full = summary_full.strip()
        summary_short = summary_full[:30]

        tone = (feat.get('tone') or '').lower()
        if tone not in {'bullish', 'bearish', 'neutral'}:
            tone = _infer_tone(raw_text)

        category = feat.get('category') or _infer_category(base.get('subject', ''), raw_text)

        # 强制保证 key_info 包含观点（main_point），否则将观点置于最前
        key_info = (feat.get('key_info') or '').strip()
        key_info_origin = 'tool' if key_info else 'fallback'
        if not key_info:
            parts: List[str] = []
            if summary_full:
                parts.append(summary_full)
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
            key_info = " | ".join([p for p in parts if p]).strip()[:160]
        else:
            vp = (main_point or '').strip()
            if vp and vp not in key_info:
                key_info = f"{vp} | {key_info}"

        summary_origin = 'tool' if feat.get('summary') or feat.get('summary_full') or feat.get('key_info') else 'fallback'
        # 摘要优先使用观点，确保前端第一眼看到观点
        summary_value = (main_point or '').strip()[:30] or summary_short or key_info[:30] or (raw_text[:30])
        if summary_origin == 'tool' and summary_value:
            summary_text = f"ai: {summary_value}"
        else:
            summary_text = f"fallback: {summary_value}" if summary_value else 'fallback:'
            summary_origin = 'fallback'
            key_info_origin = 'fallback'

        results[mid] = {
            'summary': summary_text,
            'summary_full': summary_full,
            'summary_origin': summary_origin,
            'key_info': key_info,
            'key_info_origin': key_info_origin,
            'tone': tone,
            'category': category,
            'meeting_link': meeting_link,
            'meeting_number': meeting_number,
            'platform': feat.get('platform') or feat.get('meeting_platform') or _infer_platform(raw_text),
            'appointment_time': appointment_time,
            'analyst': analyst,
            'organizer': organizer,
            'main_point': main_point,
            'keywords': feat.get('keywords') or [],
        }
    return results


def persist_email_features(
    db: Session,
    emails: Iterable[EmailMessage],
    *,
    precomputed: Dict[str, dict] | None = None,
    force: bool = False,
    commit: bool = False,
) -> Dict[str, dict]:
    emails = list(emails)
    if not emails:
        return {}

    items: List[dict] = []
    targets: List[EmailMessage] = []
    for em in emails:
        derived = em.derived if isinstance(em.derived, dict) else {}
        if derived and not force and derived.get('summary_origin') == 'tool' and derived.get('key_info'):
            continue
        items.append({
            'id': em.id,
            'sent_at': em.sent_at.isoformat() if em.sent_at else None,
            'from_addr': em.from_addr,
            'subject': em.subject,
            'body_text': em.body_text,
            'body_html': em.body_html,
            'snippet': em.snippet,
        })
        targets.append(em)

    if not items and precomputed is None:
        return {}

    features = precomputed or build_email_features(items)

    for em in targets:
        fid = str(em.id)
        feat = features.get(fid)
        if not feat:
            continue
        derived = em.derived if isinstance(em.derived, dict) else {}
        derived.update(feat)
        em.derived = derived
        db.add(em)

    if commit:
        db.commit()
    else:
        db.flush()

    return features


def _infer_platform(text: str) -> str:
    lower = text.lower()
    if "腾讯会议" in text or "wemeet" in lower or "meeting.tencent.com" in lower:
        return "腾讯"
    if "进门财经" in text or "jinmen" in lower:
        return "进门"
    if "飞书" in text or "feishu" in lower or "lark" in lower:
        return "飞书"
    if "zoom" in lower:
        return "Zoom"
    if "teams" in lower or "microsoft.com" in lower:
        return "Teams"
    if "钉钉" in text or "dingtalk" in lower:
        return "钉钉"
    if "电话会议" in text or "teleconference" in lower:
        return "电话"
    return ''


def _infer_category(subject: str, text: str) -> str:
    combined = subject + ' ' + text
    if re.search(r"会议|路演|会议号|报名|zoom|腾讯会议|飞书会议|进门财经|直播", combined, flags=re.IGNORECASE):
        return "会议"
    if re.search(r"观点|策略|简评|点评|研报|update|review|要闻|早报|日报|周报|月报", combined, flags=re.IGNORECASE):
        return "观点"
    return "其他"


def _infer_tone(text: str) -> str:
    lower = text.lower()
    pos = ['看多','利好','上涨','上调','增持','超配','改善','超预期','提价','回暖','反弹','突破','增长','积极','领先','强势']
    neg = ['看空','利空','下跌','下调','减持','不及预期','承压','恶化','回落','下滑','风险','下行','弱势','疲弱']
    if any(p.lower() in lower for p in pos):
        return 'bullish'
    if any(n.lower() in lower for n in neg):
        return 'bearish'
    return 'neutral'

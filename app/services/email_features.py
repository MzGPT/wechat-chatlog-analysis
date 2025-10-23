from __future__ import annotations

import re
import threading
from typing import Dict, List, Iterable

from sqlalchemy.orm import Session

from ..models import EmailMessage
from .ai_tools import extract_message_features


def _html_to_text(html: str | None) -> str:
    """智能HTML转文本（保留表格结构、段落分隔）"""
    if not html:
        return ''
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        
        # 移除噪音标签
        for tag in soup(['style', 'script', 'meta', 'link', 'head']):
            tag.decompose()
        
        # 表格转文本（保留结构）
        for table in soup.find_all('table'):
            rows = []
            for tr in table.find_all('tr'):
                cells = [td.get_text(strip=True) for td in tr.find_all(['td', 'th'])]
                if any(cells):  # 跳过空行
                    rows.append(' | '.join(cells))
            if rows:
                table_text = '\n'.join(rows)
                table.replace_with(soup.new_string('\n' + table_text + '\n'))
        
        # 段落和标题保留换行
        for tag in soup.find_all(['p', 'div', 'br', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
            if tag.name == 'br':
                tag.replace_with(soup.new_string('\n'))
            else:
                tag.append(soup.new_string('\n'))
        
        # 列表项保留结构
        for li in soup.find_all('li'):
            li.insert(0, soup.new_string('• '))
            li.append(soup.new_string('\n'))
        
        # 提取文本
        text = soup.get_text(separator=' ')
        # 清理空白（保留换行）
        lines = text.split('\n')
        cleaned_lines = [re.sub(r'\s+', ' ', line).strip() for line in lines]
        cleaned_lines = [line for line in cleaned_lines if line]  # 移除空行
        return '\n'.join(cleaned_lines)
    except Exception:
        # Fallback to simple regex cleanup
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
        
        # 优先策略：HTML body（完整） > body_text > snippet > subject
        text = ''
        if it.get('body_html'):
            text = _html_to_text(it.get('body_html'))
        if not text and it.get('body_text'):
            text = it.get('body_text').strip()
        if not text and it.get('snippet'):
            # snippet 通常被截断，尝试从 subject 补充上下文
            snippet = it.get('snippet', '').strip()
            subject = it.get('subject', '').strip()
            if subject and subject not in snippet:
                text = f"{subject}\n{snippet}"
            else:
                text = snippet
        if not text:
            text = it.get('subject', '').strip()
        
        if not text:
            continue
        
        # 智能截断：保留完整句子，避免切在句中
        trimmed = text[:4000]
        if len(text) > 4000:
            # 尝试在句号/换行处截断
            for sep in ['。\n', '。', '.\n', '.', '\n']:
                last_pos = trimmed.rfind(sep)
                if last_pos > 3000:  # 至少保留3000字符
                    trimmed = trimmed[:last_pos + len(sep)]
                    break
        
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

        # 两段式：这里产出 summary（短）与 key_info；其余字段尽量补齐
        summary_origin = 'tool' if feat.get('summary') or feat.get('summary_full') else 'fallback'
        summary_value = (main_point or '').strip()[:30] or summary_short or (raw_text[:30])
        key_info_src = feat.get('key_info') or main_point or base.get('subject', '') or raw_text[:50]
        key_info = key_info_src.strip()[:30]
        key_info_origin = 'tool' if feat.get('key_info') else 'fallback'
        if summary_origin == 'tool' and summary_value:
            summary_text = f"ai: {summary_value}"
        else:
            summary_text = f"fallback: {summary_value}" if summary_value else 'fallback:'
            summary_origin = 'fallback'

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


def _load_external_industries() -> List[str]:
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


def _detect_entities(text: str) -> Dict[str, List[str]]:
    if not text:
        return {"a": [], "hk": [], "us": [], "industries": []}
    a_pat = re.compile(r"(?<!\d)(?:60\d{4}|601\d{3}|603\d{3}|605\d{3}|000\d{3}|001\d{3}|002\d{3}|300\d{3}|301\d{3}|688\d{3})(?!\d)")
    a_codes = a_pat.findall(text)
    hk_pat1 = re.compile(r"\b\d{4}\.(?:hk|HK)\b")
    hk_pat2 = re.compile(r"\b(?:hk|HK)\d{4}\b")
    hk_codes = sorted(set(hk_pat1.findall(text) + hk_pat2.findall(text)))
    us_pat = re.compile(r"\b(?:NASDAQ|NYSE|AMEX|US:|Ticker[:：]|代码[:：])\s*([A-Z]{1,5})\b")
    us_codes = [m.group(1) for m in us_pat.finditer(text)]
    industries = []
    base_inds = [
        '半导体','芯片','集成电路','算力','人工智能','AI','云计算','新能源','光伏','储能','风电','锂电','动力电池',
        '煤炭','石油','有色','钢铁','化工','机械','汽车','汽车零部件','整车','电动车','银行','券商','保险','白酒',
        '消费','家电','医药','生物','医疗','军工','国防','地产','房地产','通信','电力','公用事业','TMT','软件',
        '游戏','传媒','互联网','电商','物流','航运','航空'
    ]
    ext_inds = _load_external_industries()
    for ind in list(dict.fromkeys(base_inds + ext_inds)):
        if ind in text and ind not in industries:
            industries.append(ind)
    return {"a": a_codes[:3], "hk": hk_codes[:3], "us": us_codes[:3], "industries": industries[:3]}


def build_email_fallback_features(items: List[dict]) -> Dict[str, dict]:
    """Fast, local fallback features without calling the tool model.

    Produces a minimal set of unified fields for immediate UI display:
    - meeting_number, platform, key_info, summary, summary_origin
    """
    results: Dict[str, dict] = {}
    for it in items:
        mid = str(it.get('id')) if it.get('id') is not None else ''
        if not mid:
            continue
        text = (it.get('body_text') or _html_to_text(it.get('body_html')) or it.get('snippet') or it.get('subject') or '').strip()
        subject = (it.get('subject') or '').strip()
        # meeting & platform
        m = re.search(r"(?:会议号[:：]?\s*)?(\d{3}[-\s]?\d{3}[-\s]?\d{3,6}|\d{8,12})", text)
        meeting_raw = m.group(1) if m else ''
        meeting_digits = re.sub(r"\D", "", meeting_raw)
        meeting_number = meeting_digits if 9 <= len(meeting_digits) <= 13 else ''
        platform = _infer_platform(text)
        # key_info
        key_info_src = ''
        # prefer explicit 观点/主题 段落
        mpt = re.search(r"(?:观点|主题)[:：]\s*([^\n；。]{4,50})", text)
        if mpt:
            key_info_src = mpt.group(1).strip()
        if not key_info_src:
            key_info_src = subject or text[:50]
        # entity enrichment: try to add industry/ticker if not present and space allows
        ents = _detect_entities(text)
        extras: List[str] = []
        if ents.get('industries'):
            ind = ents['industries'][0]
            if ind not in key_info_src:
                extras.append(ind)
        for group in ('a','hk','us'):
            codes = ents.get(group) or []
            if codes:
                code = codes[0]
                if code not in key_info_src:
                    extras.append(code)
                    break
        if extras:
            key_info_src = (key_info_src + ' | ' + ' '.join(extras)).strip()
        def _clip_vis(s: str, limit: int) -> str:
            compact = s.strip()
            acc = []
            for ch in compact:
                if len(''.join(acc).replace(' ', '')) >= limit:
                    break
                acc.append(ch)
            return ''.join(acc).strip()
        key_info = _clip_vis(key_info_src, 30)
        # summary (短)
        summ_src = _build_summary(text) or key_info_src
        summary = f"fallback: {_clip_vis(summ_src, 50)}"
        results[mid] = {
            'meeting_number': meeting_number,
            'platform': platform,
            'key_info': key_info,
            'summary': summary,
            'summary_origin': 'fallback',
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
        if derived and not force and derived.get('summary_origin') == 'tool':
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


def persist_email_fallback(
    db: Session,
    emails: Iterable[EmailMessage],
    *,
    force: bool = False,
    commit: bool = False,
) -> Dict[str, dict]:
    """Persist only fallback features for given emails.

    This runs quickly and should be invoked right after fetch so the UI can
    render grey fallback summaries immediately. The AI overlay can run
    asynchronously later to overwrite with tool results (orange).
    """
    emails = list(emails)
    if not emails:
        return {}
    items: List[dict] = []
    targets: List[EmailMessage] = []
    for em in emails:
        derived = em.derived if isinstance(em.derived, dict) else {}
        if derived and not force and derived.get('summary_origin') == 'tool':
            continue
        items.append({
            'id': em.id,
            'sent_at': em.sent_at.isoformat() if em.sent_at else None,
            'subject': em.subject,
            'body_text': em.body_text,
            'body_html': em.body_html,
            'snippet': em.snippet,
        })
        targets.append(em)
    if not items:
        return {}
    features = build_email_fallback_features(items)
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

from __future__ import annotations

import hashlib
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests

_CACHE: dict[str, tuple[float, Any]] = {}


@dataclass(frozen=True)
class NewsSource:
    id: str
    name: str
    url: str
    region: str = "global"
    category: str = "finance"


NEWS_SOURCES: tuple[NewsSource, ...] = (
    NewsSource("wallstreetcn-quick", "华尔街见闻", "https://api.wallstreetcn.com/apiv1/content/lives", "cn"),
    NewsSource("10jqka-stock", "同花顺", "https://news.10jqka.com.cn/tapp/news/push/stock/", "cn"),
    NewsSource("sina-finance", "新浪财经", "https://rss.sina.com.cn/roll/finance/hot_roll.xml", "cn"),
    NewsSource("stcn", "证券时报", "https://www.stcn.com/rss/gundong.xml", "cn"),
    NewsSource("yicai", "第一财经", "https://www.yicai.com/rss/pc/", "cn"),
    NewsSource("caixin", "财新", "https://file.caixin.com/m/caixin_rss.xml", "cn"),
    NewsSource("jiemian", "界面新闻", "https://www.jiemian.com/rss.html", "cn"),
    NewsSource("reuters-business", "Reuters Business", "https://feeds.reuters.com/reuters/businessNews", "global"),
    NewsSource("bbc-business", "BBC Business", "http://feeds.bbci.co.uk/news/business/rss.xml", "global"),
    NewsSource("cnbc", "CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html", "global"),
    NewsSource("bloomberg-markets", "彭博市场", "https://feeds.bloomberg.com/markets/news.rss", "global"),
    NewsSource("hackernews", "Hacker News", "https://hnrss.org/frontpage", "global", "technology"),
)

_POSITIVE = ("上涨", "增长", "突破", "回暖", "创新高", "利好", "扩张", "beat", "surge", "rise", "gain", "growth", "record")
_NEGATIVE = ("下跌", "下降", "风险", "承压", "亏损", "裁员", "调查", "制裁", "危机", "miss", "fall", "drop", "risk", "loss", "cut")
_RISK = ("监管", "制裁", "调查", "违约", "亏损", "裁员", "风险", "下跌", "crackdown", "probe", "default", "lawsuit")
_OPPORTUNITY = ("AI", "人工智能", "算力", "芯片", "新能源", "机器人", "出海", "增长", "突破", "record", "growth")
_TRANSLATION_RULES: tuple[tuple[str, str], ...] = (
    ("consumer spending", "消费者支出"),
    ("used car prices", "二手车价格"),
    ("gas prices", "汽油价格"),
    ("interest rates", "利率"),
    ("fed", "美联储"),
    ("stock market", "股市"),
    ("stocks", "股票"),
    ("shares", "股价"),
    ("ai bull market", "AI 牛市"),
    ("bull market", "牛市"),
    ("hedge trade", "对冲交易"),
    ("oil", "石油"),
    ("profits", "利润"),
    ("revenue", "营收"),
    ("earnings", "财报"),
    ("subscription prices", "订阅价格"),
    ("profitable quarter", "盈利季度"),
    ("family office", "家族办公室"),
    ("deal-making", "交易活动"),
    ("healthcare", "医疗健康"),
    ("college", "大学教育"),
    ("investment", "投资"),
    ("rail disruption", "铁路中断"),
    ("southern england", "英格兰南部"),
    ("vulnerability", "漏洞"),
    ("linux", "Linux"),
    ("cloudflare", "Cloudflare"),
    ("burning man", "火人节"),
    ("mcdonald", "麦当劳"),
    ("peloton", "Peloton"),
    ("warsh", "沃什"),
)
_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "into", "after", "over", "news", "says",
    "一个", "相关", "表示", "公司", "市场", "中国", "美国", "今日", "最新", "可能", "进行", "成为", "以及",
    "亿元", "万元", "同比", "同比增长", "增长", "美元", "亿美元", "人民币", "公告", "显示", "记者", "目前",
    "截至", "发布", "实现", "预计", "其中", "方面", "持续", "超过", "达到", "来看", "数据", "报告",
    "跌超", "涨超", "领涨", "领跌", "午评", "早盘", "收盘", "主力合约", "主力合约日内", "日内", "半日",
    "概念股", "集体", "爆发", "成交额", "股指期货", "沪深", "创业板指", "etf", "a股", "股票",
    "finance", "market", "markets", "business", "company", "companies", "report", "reports", "deal", "deals",
}
_TOPIC_TERMS = (
    "人形机器人", "机器人", "芯片", "半导体", "人工智能", "AI", "算力", "新能源", "光伏", "储能",
    "黄金", "原油", "霍尔木兹", "美联储", "降息", "通胀", "关税", "制裁", "ETF", "股指期货",
    "腾讯", "阿里巴巴", "英伟达", "Nvidia", "Cloudflare", "Datadog", "CoreWeave", "Coinbase",
    "房地产", "医药", "医疗", "消费", "出口", "汇率", "债券", "港股", "A股", "美股",
)


def _cache_get(key: str) -> Any | None:
    item = _CACHE.get(key)
    if not item:
        return None
    expires_at, value = item
    if expires_at > time.time():
        return value
    _CACHE.pop(key, None)
    return None


def _cache_set(key: str, value: Any, ttl: int = 600) -> None:
    _CACHE[key] = (time.time() + max(1, ttl), value)


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _parse_time(value: str) -> int:
    raw = (value or "").strip()
    if not raw:
        return 0
    try:
        return int(parsedate_to_datetime(raw).timestamp() * 1000)
    except Exception:
        pass
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def _item_id(source_id: str, title: str, url: str) -> str:
    digest = hashlib.sha1(f"{source_id}|{url}|{title}".encode("utf-8", "ignore")).hexdigest()[:16]
    return f"{source_id}:{digest}"


def _tone(title: str) -> str:
    lower = title.lower()
    if any(k.lower() in lower or k in title for k in _NEGATIVE):
        return "negative"
    if any(k.lower() in lower or k in title for k in _POSITIVE):
        return "positive"
    return "neutral"


def _has_chinese(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in str(text or ""))


def translate_title_to_zh(title: str) -> str:
    """Very light local Chinese rendering for English headlines.

    This intentionally avoids adding a heavy translation dependency. It preserves
    the original title in `title_original` and produces a readable Chinese display
    title for common finance/tech headlines. Full LLM translation can be layered
    later without changing the frontend contract.
    """
    raw = str(title or "").strip()
    if not raw or _has_chinese(raw):
        return raw
    text = raw
    for src, dst in sorted(_TRANSLATION_RULES, key=lambda x: len(x[0]), reverse=True):
        text = re.sub(re.escape(src), dst, text, flags=re.IGNORECASE)
    text = re.sub(r"\bCEO\b", "CEO", text, flags=re.IGNORECASE)
    text = re.sub(r"\bsays\b", "称", text, flags=re.IGNORECASE)
    text = re.sub(r"\bcould be\b", "可能", text, flags=re.IGNORECASE)
    text = re.sub(r"\bexpected\b", "预计", text, flags=re.IGNORECASE)
    text = re.sub(r"\brises?\b", "上涨", text, flags=re.IGNORECASE)
    text = re.sub(r"\bfalls?\b", "下跌", text, flags=re.IGNORECASE)
    text = re.sub(r"\bexpected in\b", "预计发生在", text, flags=re.IGNORECASE)
    text = re.sub(r"\bfor first time this year\b", "为今年首次", text, flags=re.IGNORECASE)
    text = re.sub(r"\banother year or two to run\b", "还可持续一两年", text, flags=re.IGNORECASE)
    text = re.sub(r"\bno chance\b", "没有机会", text, flags=re.IGNORECASE)
    text = re.sub(r"\btop 10 things to watch\b", "十大关注事项", text, flags=re.IGNORECASE)
    text = re.sub(r"\buntil end of day\b", "直至今日结束", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text



_TITLE_TRANSLATION_CACHE: dict[str, tuple[float, str]] = {}


def _translation_cache_get(title: str) -> str | None:
    key = title.strip().lower()
    hit = _TITLE_TRANSLATION_CACHE.get(key)
    if not hit:
        return None
    expires_at, value = hit
    if expires_at > time.time():
        return value
    _TITLE_TRANSLATION_CACHE.pop(key, None)
    return None


def _translation_cache_set(title: str, translated: str, ttl: int = 86400) -> None:
    key = title.strip().lower()
    _TITLE_TRANSLATION_CACHE[key] = (time.time() + max(60, ttl), translated)



def _translate_titles_google(titles: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    pending = []
    for title in titles:
        title = str(title or '').strip()
        if not title or _has_chinese(title):
            continue
        cached = _translation_cache_get(title)
        if cached:
            result[title] = cached
        else:
            pending.append(title)
    if not pending:
        return result
    session = requests.Session()
    for title in pending[:40]:
        try:
            resp = session.get(
                'https://translate.googleapis.com/translate_a/single',
                params={'client': 'gtx', 'sl': 'en', 'tl': 'zh-CN', 'dt': 't', 'q': title},
                timeout=4,
            )
            resp.raise_for_status()
            data = resp.json()
            translated = ''.join(part[0] for part in (data[0] or []) if part and part[0]).strip()
            if translated and translated != title:
                result[title] = translated
                _translation_cache_set(title, translated)
        except Exception:
            continue
    return result

def _translate_titles_batch(titles: list[str]) -> dict[str, str]:
    pending = [t for t in titles if t and not _has_chinese(t)]
    pending = [t for t in pending if not _translation_cache_get(t)]
    if not pending:
        return {}
    try:
        from .llm_client import load_ai_config, siliconflow_chat
        conf = load_ai_config()
        api_key = str(conf.get('api_key') or '').strip()
        if not api_key:
            return {}
        prompt = (
            '你是财经新闻标题翻译器。请把下面英文标题翻译成简洁自然的中文。'
            '只返回严格 JSON 对象，格式为 {"items":[{"source":"原文","translated":"中文"}, ...]}。'
            '要求：保留公司名/专有名词/数字/百分比；不要解释，不要扩写，不要输出多余文本。\n\n'
            + '\n'.join(f'- {t}' for t in pending[:20])
        )
        out = siliconflow_chat(
            [
                {'role': 'system', 'content': '你只做标题翻译，输出严格 JSON。'},
                {'role': 'user', 'content': prompt},
            ],
            temperature=0.1,
            route_kind='main',
            route_key='newswatch',
            force_json=True,
        )
        import json as _json
        data = _json.loads(out) if isinstance(out, str) else out
        result: dict[str, str] = {}
        if isinstance(data, dict):
            items = data.get('items') if isinstance(data.get('items'), list) else []
            for row in items:
                if not isinstance(row, dict):
                    continue
                src = str(row.get('source') or '').strip()
                tr = str(row.get('translated') or '').strip()
                if src and tr:
                    result[src] = tr
                    _translation_cache_set(src, tr)
        return result
    except Exception:
        return {}

def _localized_item(item: dict[str, Any]) -> dict[str, Any]:
    title = str(item.get("title") or "")
    title_zh = translate_title_to_zh(title)
    item["title_original"] = title
    item["title_zh"] = title_zh
    if not _has_chinese(title):
        item["title"] = title_zh
    derived = item.get("derived") if isinstance(item.get("derived"), dict) else {}
    derived["key_info"] = title_zh[:160]
    item["derived"] = derived
    return item


def _heat_score(item: dict[str, Any], *, duplicate_count: int = 1, source_count: int = 1) -> float:
    """TrendRadar-style heat score: recency + source weight + cluster resonance + signal words."""
    now_ms = int(time.time() * 1000)
    pub_ts = int(item.get("pub_ts") or 0)
    age_hours = max(0.0, (now_ms - pub_ts) / 3_600_000) if pub_ts else 24.0
    recency = max(0.0, 36.0 - age_hours) / 36.0 * 42.0
    source_id = str(item.get("source_id") or "")
    source_weight = {
        "wallstreetcn-quick": 18,
        "10jqka-stock": 16,
        "sina-finance": 15,
        "bloomberg-markets": 17,
        "cnbc": 14,
        "bbc-business": 11,
        "hackernews": 8,
    }.get(source_id, 10)
    title = str(item.get("title_original") or item.get("title") or "")
    lower = title.lower()
    signal = 0
    if any(k.lower() in lower or k in title for k in _RISK):
        signal += 10
    if any(k.lower() in lower or k in title for k in _OPPORTUNITY):
        signal += 8
    if re.search(r"\d+(?:\.\d+)?\s*%|涨停|跌停|创新高|新高|暴涨|暴跌|surge|plunge|soar|slump", title, re.I):
        signal += 8
    resonance = min(22, max(0, duplicate_count - 1) * 7 + max(0, source_count - 1) * 5)
    score = recency + source_weight + signal + resonance
    return round(max(0.0, min(100.0, score)), 1)


def _cluster_key(title: str) -> str:
    text = str(title or "").lower()
    text = re.sub(r"[\d\.]+[%％]?", "", text)
    words = re.findall(r"[A-Za-z][A-Za-z0-9+.-]{2,}|[\u4e00-\u9fff]{2,6}", text)
    useful = []
    for word in words:
        normalized = word.lower() if re.match(r"^[A-Za-z]", word) else word
        if normalized in _STOPWORDS:
            continue
        useful.append(normalized)
    return "|".join(useful[:4]) or text[:18]


def _category(title: str, source: NewsSource) -> str:
    text = title.lower()
    if any(k in title for k in ("AI", "人工智能", "芯片", "算力", "机器人")) or any(k in text for k in ("ai", "chip", "nvidia", "semiconductor")):
        return "technology"
    if any(k in title for k in ("央行", "利率", "通胀", "汇率", "GDP", "就业")) or any(k in text for k in ("fed", "inflation", "rate", "gdp")):
        return "macro"
    if any(k in title for k in ("A股", "港股", "美股", "债券", "期货")) or any(k in text for k in ("stocks", "market", "shares")):
        return "market"
    return source.category


def _fetch_source(source: NewsSource, timeout: int = 8) -> list[dict[str, Any]]:
    if source.id == "wallstreetcn-quick":
        return _fetch_wallstreetcn(source, timeout=timeout)
    if source.id == "10jqka-stock":
        return _fetch_10jqka(source, timeout=timeout)
    headers = {"User-Agent": "0913-news-engine/1.0 (+rss; lightweight)"}
    resp = requests.get(source.url, timeout=timeout, headers=headers)
    if source.id == "sina-finance":
        resp.encoding = "gb2312"
    text = resp.text
    root = ET.fromstring(text)
    rows: list[dict[str, Any]] = []

    def append_item(title: str, url: str, pub: str, summary: str = "") -> None:
        title = _strip_html(title)
        if not title:
            return
        pub_ts = _parse_time(pub) or int(time.time() * 1000)
        tone = _tone(title)
        category = _category(title, source)
        rows.append({
            "id": _item_id(source.id, title, url),
            "source_id": source.id,
            "source_name": source.name,
            "title": title,
            "url": url,
            "pub_ts": pub_ts,
            "region": source.region,
            "category": category,
            "summary": _strip_html(summary),
            "derived": {
                "key_info": title[:160],
                "category": category,
                "tone": tone,
                "summary_origin": "news_engine",
            },
        })

    for item in root.findall(".//item"):
        append_item(
            item.findtext("title") or "",
            item.findtext("link") or "",
            item.findtext("pubDate") or "",
            item.findtext("description") or "",
        )
    ns = "{http://www.w3.org/2005/Atom}"
    for entry in root.findall(f".//{ns}entry"):
        link_el = entry.find(f"{ns}link")
        append_item(
            entry.findtext(f"{ns}title") or "",
            (link_el.attrib.get("href") if link_el is not None else "") or "",
            entry.findtext(f"{ns}updated") or entry.findtext(f"{ns}published") or "",
            entry.findtext(f"{ns}summary") or "",
        )
    return rows


def _fetch_10jqka(source: NewsSource, timeout: int = 8) -> list[dict[str, Any]]:
    params = {"page": 1, "tag": "", "track": "website", "pagesize": 50}
    headers = {"User-Agent": "Mozilla/5.0 0913-news-engine/1.0"}
    data = requests.get(source.url, params=params, timeout=timeout, headers=headers).json()
    rows: list[dict[str, Any]] = []
    for item in (((data.get("data") or {}).get("list")) or []):
        title = _strip_html(str(item.get("title") or item.get("digest") or ""))
        if not title:
            continue
        digest = _strip_html(str(item.get("digest") or ""))
        url = str(item.get("url") or item.get("link") or "")
        raw_ts = item.get("ctime") or item.get("time") or item.get("rtime") or 0
        try:
            pub_ts = int(raw_ts)
            if pub_ts and pub_ts < 10_000_000_000:
                pub_ts *= 1000
        except Exception:
            pub_ts = int(time.time() * 1000)
        tone = _tone(title)
        category = _category(title, source)
        rows.append({
            "id": str(item.get("id") or item.get("seq") or _item_id(source.id, title, url)),
            "source_id": source.id,
            "source_name": source.name,
            "title": title,
            "url": url,
            "pub_ts": pub_ts,
            "region": source.region,
            "category": category,
            "summary": digest,
            "raw": item,
            "derived": {"key_info": title[:160], "category": category, "tone": tone, "summary_origin": "news_engine"},
        })
    return rows


def _fetch_wallstreetcn(source: NewsSource, timeout: int = 8) -> list[dict[str, Any]]:
    params = {"channel": "global", "limit": 50}
    headers = {"User-Agent": "0913-news-engine/1.0 (+json; lightweight)"}
    data = requests.get(source.url, params=params, timeout=timeout, headers=headers).json()
    rows: list[dict[str, Any]] = []
    for live in (data.get("data", {}).get("items") or []):
        content = _strip_html(str(live.get("content") or ""))
        if not content:
            continue
        title = content[:180]
        ts = live.get("display_time") or live.get("created_at") or live.get("updated_at") or 0
        try:
            pub_ts = int(ts) if isinstance(ts, int) else int(float(ts))
            if pub_ts and pub_ts < 10_000_000_000:
                pub_ts *= 1000
        except Exception:
            pub_ts = int(time.time() * 1000)
        url = ""
        try:
            article = live.get("article") or {}
            url = article.get("uri") or article.get("resource") or ""
        except Exception:
            url = ""
        tone = _tone(title)
        category = _category(title, source)
        rows.append({
            "id": str(live.get("id") or _item_id(source.id, title, url)),
            "source_id": source.id,
            "source_name": source.name,
            "title": title,
            "url": url,
            "pub_ts": pub_ts,
            "region": source.region,
            "category": category,
            "summary": content,
            "derived": {
                "key_info": title[:160],
                "category": category,
                "tone": tone,
                "summary_origin": "news_engine",
            },
        })
    return rows


def list_sources() -> list[dict[str, str]]:
    return [{"id": s.id, "name": s.name, "region": s.region, "category": s.category, "url": s.url} for s in NEWS_SOURCES]


def collect_news(*, limit: int = 80, q: str | None = None, source: str | None = None, force: bool = False) -> dict[str, Any]:
    limit = max(1, min(300, int(limit or 80)))
    cache_key = f"engine:{limit}:{q or ''}:{source or ''}"
    if not force:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    selected = [s for s in NEWS_SOURCES if not source or s.id == source]
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    per_source_limit = max(12, min(50, limit))
    for src in selected:
        try:
            items.extend(_fetch_source(src)[:per_source_limit])
        except Exception as exc:
            errors.append({"source_id": src.id, "source_name": src.name, "error": str(exc)[:160]})

    if q:
        query = str(q).strip().lower()
        items = [it for it in items if query in (it.get("title") or "").lower() or query in (it.get("source_name") or "").lower()]

    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    cluster_counts: dict[str, int] = {}
    cluster_sources: dict[str, set[str]] = {}
    for item in items:
        ck = _cluster_key(str(item.get("title") or ""))
        cluster_counts[ck] = cluster_counts.get(ck, 0) + 1
        cluster_sources.setdefault(ck, set()).add(str(item.get("source_id") or ""))

    def _rank_item(item: dict[str, Any]) -> tuple[float, int, int, int]:
        title = str(item.get("title") or "")
        source_id = str(item.get("source_id") or "")
        zh_bonus = 1 if _has_chinese(title) else 0
        cn_bonus = 1 if source_id in {"wallstreetcn-quick", "stcn", "yicai", "caixin", "jiemian"} else 0
        ck = _cluster_key(title)
        item["heat_score"] = _heat_score(item, duplicate_count=cluster_counts.get(ck, 1), source_count=len(cluster_sources.get(ck, set())))
        item["heat_cluster"] = ck
        return (float(item.get("heat_score") or 0), cn_bonus, zh_bonus, int(item.get("pub_ts") or 0))

    for it in sorted(items, key=_rank_item, reverse=True):
        key = str(it.get("id") or it.get("url") or it.get("title"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(_localized_item(it))
        if len(deduped) >= limit:
            break

    try:
        english_titles = [str(it.get('title_original') or it.get('title') or '') for it in deduped if str(it.get('title_original') or it.get('title') or '') and not _has_chinese(str(it.get('title_original') or it.get('title') or ''))]
        translations = _translate_titles_google(english_titles)
        missing_titles = [t for t in english_titles if t not in translations]
        if missing_titles:
            translations.update(_translate_titles_batch(missing_titles))
        if translations:
            for it in deduped:
                src_title = str(it.get('title_original') or it.get('title') or '')
                if src_title in translations:
                    zh = translations[src_title]
                    it['title_zh'] = zh
                    it['title'] = zh
                    derived = it.get('derived') if isinstance(it.get('derived'), dict) else {}
                    derived['key_info'] = zh[:160]
                    it['derived'] = derived
    except Exception:
        pass
    result = {"success": True, "total": len(deduped), "items": deduped, "errors": errors, "engine": "builtin-trend-radar-lite"}
    _cache_set(cache_key, result, ttl=600)
    return result


def _keywords(items: list[dict[str, Any]], top_n: int = 12) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for it in items:
        title = str(it.get("title") or "")
        original = str(it.get("title_original") or "")
        haystack = f"{title} {original}"
        for term in _TOPIC_TERMS:
            if term.lower() in haystack.lower():
                counts[term] = counts.get(term, 0) + 2
        words = re.findall(r"[A-Za-z][A-Za-z0-9+.-]{2,}|[\u4e00-\u9fff]{2,6}", title)
        for word in words:
            normalized = word.lower() if re.match(r"^[A-Za-z]", word) else word
            if normalized in _STOPWORDS or len(normalized) < 2:
                continue
            if any(stop in normalized for stop in _STOPWORDS if len(stop) >= 2):
                continue
            if _has_chinese(normalized) and normalized not in _TOPIC_TERMS and len(normalized) > 4:
                continue
            counts[normalized] = counts.get(normalized, 0) + 1
    return [{"keyword": k, "count": v} for k, v in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:top_n]]


def analyze_news(items: list[dict[str, Any]]) -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    six_h = now_ms - 6 * 3600 * 1000
    day = now_ms - 24 * 3600 * 1000
    tones = {"positive": 0, "neutral": 0, "negative": 0}
    categories: dict[str, int] = {}
    sources: dict[str, int] = {}
    risk_items: list[dict[str, Any]] = []
    opportunity_items: list[dict[str, Any]] = []
    recent_6h = 0
    recent_24h = 0
    for it in items:
        title = str(it.get("title") or "")
        derived = it.get("derived") or {}
        tone = str(derived.get("tone") or "neutral")
        tones[tone if tone in tones else "neutral"] += 1
        categories[str(it.get("category") or derived.get("category") or "other")] = categories.get(str(it.get("category") or derived.get("category") or "other"), 0) + 1
        sources[str(it.get("source_name") or "未知")] = sources.get(str(it.get("source_name") or "未知"), 0) + 1
        pub_ts = int(it.get("pub_ts") or 0)
        if pub_ts >= six_h:
            recent_6h += 1
        if pub_ts >= day:
            recent_24h += 1
        lower = title.lower()
        if any(k.lower() in lower or k in title for k in _RISK):
            risk_items.append(it)
        if any(k.lower() in lower or k in title for k in _OPPORTUNITY):
            opportunity_items.append(it)

    total = len(items)
    sentiment_score = 0 if not total else round((tones["positive"] - tones["negative"]) / total * 100, 1)
    velocity = round(recent_6h / max(1, recent_24h) * 100, 1) if recent_24h else 0
    if velocity >= 45:
        prediction = "热点正在加速扩散，建议进入高频跟踪与交叉验证。"
    elif velocity >= 20:
        prediction = "热度处于稳定上行阶段，适合纳入日内观察池。"
    else:
        prediction = "当前热度偏平稳，优先关注结构性分化与长尾议题。"

    return {
        "total": total,
        "sentiment": tones,
        "sentiment_score": sentiment_score,
        "velocity": velocity,
        "recent_6h": recent_6h,
        "recent_24h": recent_24h,
        "categories": sorted(({"name": k, "count": v} for k, v in categories.items()), key=lambda x: x["count"], reverse=True),
        "sources": sorted(({"name": k, "count": v} for k, v in sources.items()), key=lambda x: x["count"], reverse=True),
        "keywords": _keywords(items),
        "risks": risk_items[:8],
        "opportunities": opportunity_items[:8],
        "hot_items": sorted(items, key=lambda x: float(x.get("heat_score") or 0), reverse=True)[:10],
        "prediction": prediction,
    }


def engine_payload(*, limit: int = 80, q: str | None = None, source: str | None = None, force: bool = False) -> dict[str, Any]:
    payload = collect_news(limit=limit, q=q, source=source, force=force)
    items = payload.get("items") or []
    payload["analysis"] = analyze_news(items)
    payload["sources"] = list_sources()
    return payload

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..config import settings


_CACHE: Dict[str, Tuple[float, Any]] = {}


def _cache_get(key: str) -> Any | None:
    try:
        exp, val = _CACHE.get(key, (0, None))
        if exp and exp > time.time():
            return val
    except Exception:
        pass
    return None


def _cache_set(key: str, val: Any, ttl: int) -> None:
    try:
        _CACHE[key] = (time.time() + max(1, ttl), val)
    except Exception:
        pass


def _get(url: str, params: dict | None = None, *, timeout: int = 5) -> dict:
    for i in range(3):
        try:
            r = requests.get(url, params=params or {}, timeout=timeout)
            if r.status_code < 500:
                r.raise_for_status()
                return r.json()
        except Exception:
            if i == 2:
                raise
            time.sleep(0.3 * (i + 1))
    return {}


def _post(url: str, payload: dict | None = None, *, timeout: int = 8) -> dict:
    for i in range(2):
        try:
            r = requests.post(url, json=payload or {}, timeout=timeout)
            if r.status_code < 500:
                r.raise_for_status()
                return r.json()
        except Exception:
            if i == 1:
                raise
            time.sleep(0.5)
    return {}


def newsnow_health() -> dict:
    base = settings.NEWSNOW_API_BASE.rstrip("/")
    try:
        return _get(f"{base}/api/health")
    except Exception as e:
        return {"status": "error", "error": str(e)}


def newsnow_sources(force: bool = False) -> dict:
    base = settings.NEWSNOW_API_BASE.rstrip("/")
    key = "sources"
    if not force:
        val = _cache_get(key)
        if val is not None:
            return val
    try:
        j = _get(f"{base}/api/sources")
    except Exception as e:
        j = {"success": False, "error": str(e), "data": []}
    _cache_set(key, j, settings.NEWSNOW_CACHE_TTL)
    return j


def newsnow_news(keyword: str | None = None, source: str | None = None, limit: int = 50, simple: bool = True) -> dict:
    base = settings.NEWSNOW_API_BASE.rstrip("/")
    params: dict[str, Any] = {}
    if keyword:
        params["keyword"] = keyword
    if source:
        params["source"] = source
    if limit:
        params["limit"] = max(1, min(200, int(limit)))
    if simple:
        params["format"] = "simple"
    try:
        return _get(f"{base}/api/news", params=params)
    except Exception as e:
        return {"success": False, "error": str(e), "data": []}


def newsnow_search(q: str, limit: int = 20) -> dict:
    base = settings.NEWSNOW_API_BASE.rstrip("/")
    params = {"q": q}
    if limit:
        params["limit"] = max(1, min(200, int(limit)))
    try:
        return _get(f"{base}/api/search", params=params)
    except Exception as e:
        return {"success": False, "error": str(e), "data": []}


def newsnow_refresh() -> dict:
    base = settings.NEWSNOW_API_BASE.rstrip("/")
    try:
        return _post(f"{base}/api/refresh")
    except Exception as e:
        return {"success": False, "error": str(e)}


# --------------- finance filtering & normalization ---------------

_DEFAULT_FINANCE_KEYWORDS: list[str] = [
    "宏观", "央行", "利率", "通胀", "汇率", "财政", "货币政策", "降准", "降息",
    "地产", "房企", "土拍", "销售", "按揭",
    "半导体", "芯片", "算力", "AI", "人工智能", "光伏", "新能源", "储能", "风电", "锂电",
    "券商", "A股", "港股", "美股", "科创板", "创业板", "北交所",
    "业绩", "利润", "营收", "公告", "IPO", "回购", "减持", "增持", "限售解禁", "评级", "目标价",
]


def _load_finance_keywords() -> list[str]:
    import os
    path = os.path.abspath(os.path.join(os.getcwd(), 'data', 'entities.json'))
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                j = json.load(f)
            arr = j.get('finance_keywords') if isinstance(j, dict) else None
            if isinstance(arr, list) and arr:
                return [str(x) for x in arr if isinstance(x, (str, int))]
    except Exception:
        pass
    return _DEFAULT_FINANCE_KEYWORDS


def _is_finance(title: str, url: str | None = None) -> bool:
    if not title:
        return False
    kws = _load_finance_keywords()
    return any(k in title for k in kws) or any((k in (url or '')) for k in kws)


def _infer_news_category(title: str, source_name: str = "") -> str:
    t = (title or "")
    s = (source_name or "")
    macro_keys = ("央行","利率","通胀","财政","货币政策","降准","降息","贸易","关税","PMI","GDP")
    if any(k in t for k in macro_keys):
        return "宏观"
    industry_keys = ("半导体","芯片","算力","AI","人工智能","光伏","新能源","储能","风电","锂电","汽车","医药","军工","煤炭","有色","地产")
    if any(k in t for k in industry_keys):
        return "行业"
    stock_keys = ("股份","集团","公司","公告","业绩","回购","减持","增持","上市","IPO")
    if any(k in t for k in stock_keys):
        return "个股"
    sentiment_keys = ("热搜","热榜","舆情")
    if any(k in t for k in sentiment_keys) or any(k in s for k in sentiment_keys):
        return "舆情"
    return "观点"


def _infer_news_tone(title: str) -> str:
    t = (title or "").lower()
    pos = ("利好","上涨","上调","增持","改善","超预期","突破","创新高","回暖","反弹","大涨","涨停")
    neg = ("利空","下跌","下调","减持","承压","不及预期","下行","回落","疲弱","暴跌")
    if any(k.lower() in t for k in pos):
        return "positive"
    if any(k.lower() in t for k in neg):
        return "negative"
    return "neutral"


def normalize_items(raw: dict, *, finance_only: bool = True) -> dict:
    ok = bool(raw.get('success', True))
    data = raw.get('data') or []
    out: List[dict] = []
    for it in data:
        # Try both full and simple formats
        title = it.get('title') or ''
        url = it.get('url') or ''
        src_name = it.get('source') or it.get('sourceName') or it.get('name') or ''
        src_id = it.get('sourceId') or it.get('id') or ''
        nid = it.get('id') or url or title
        ts = it.get('pubDate') or it.get('timestamp') or it.get('updatedTime') or 0
        if finance_only and not _is_finance(str(title), str(url)):
            continue
        cat = _infer_news_category(str(title), str(src_name))
        tone = _infer_news_tone(str(title))
        out.append({
            'id': str(nid),
            'source_id': str(src_id),
            'source_name': str(src_name),
            'title': str(title),
            'url': str(url),
            'pub_ts': int(ts) if isinstance(ts, int) else 0,
            'tags': [],
            'category': cat,
            'summary': '',
            'raw': it,
            'derived': {
                'key_info': str(title)[:120],
                'category': cat,
                'tone': tone,
                'summary_origin': 'fallback',
            }
        })
    return {'total': len(out), 'items': out, 'upstream_ok': ok}

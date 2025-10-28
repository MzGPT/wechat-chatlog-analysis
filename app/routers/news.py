from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from typing import Optional

from ..config import settings
from ..services.news_client import (
    newsnow_health,
    newsnow_sources,
    newsnow_news,
    newsnow_search,
    newsnow_refresh,
    normalize_items,
)


router = APIRouter(prefix="/api/newsfeed", tags=["newsfeed"])


@router.get("/health")
def health():
    if not settings.NEWSNOW_ENABLED:
        return {"status": "disabled"}
    return newsnow_health()


@router.get("/sources")
def list_sources():
    if not settings.NEWSNOW_ENABLED:
        return {"success": False, "data": []}
    return newsnow_sources()


@router.get("/items")
def list_items(
    keyword: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    simple: bool = True,
    finance_only: bool = True,
):
    if not settings.NEWSNOW_ENABLED:
        return {"total": 0, "items": [], "disabled": True}
    raw = newsnow_news(keyword=keyword, source=source, limit=limit + offset, simple=simple)
    norm = normalize_items(raw, finance_only=finance_only)
    items = norm.get("items") or []
    # pagination on normalized list
    page = items[offset: offset + limit]
    return {"total": len(items), "items": page, "upstream_ok": norm.get("upstream_ok")}


@router.get("/search")
def search(q: str, limit: int = Query(20, ge=1, le=200), finance_only: bool = True):
    if not settings.NEWSNOW_ENABLED:
        return {"total": 0, "items": [], "disabled": True}
    raw = newsnow_search(q, limit=limit)
    norm = normalize_items(raw, finance_only=finance_only)
    return norm


@router.post("/refresh")
def refresh():
    if not settings.NEWSNOW_ENABLED:
        raise HTTPException(400, "news feed disabled")
    return newsnow_refresh()


@router.get("/stats")
def stats():
    # pass through for now (frontend can consume directly)
    if not settings.NEWSNOW_ENABLED:
        return {"success": False, "data": {}}
    try:
        import requests
        base = settings.NEWSNOW_API_BASE.rstrip("/")
        r = requests.get(f"{base}/api/stats", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"success": False, "error": str(e), "data": {}}


@router.post("/ai/summarize")
def summarize_news(ids: list[str] | None = None, q: Optional[str] = None, limit: int = 50):
    """生成新闻舆情监测markdown（默认使用模块提示词 newswatch）。

    - 数据来源：若提供 q 则优先 search；否则从 /api/news 拉取 limit 条
    - 输出：{ status: ok, markdown: str, used: n, model: modelName }
    """
    from ..services.llm_client import load_ai_config, DEFAULT_MODULE_PROMPTS, siliconflow_chat
    import json as _json
    # 取数据
    if q:
        raw = newsnow_search(q, limit=limit)
    else:
        raw = newsnow_news(limit=limit, simple=True)
    norm = normalize_items(raw, finance_only=True)
    items = norm.get('items') or []
    # 若传了 ids，仅过滤保留
    if ids:
        idset = {str(x) for x in ids}
        items = [it for it in items if str(it.get('id')) in idset]
    # 组装 messages_data（尽量精简以提升信噪比）
    msgs = []
    for it in items:
        msgs.append({
            'id': str(it.get('id')),
            'source': it.get('source_name') or it.get('source_id') or '',
            'title': it.get('title') or '',
            'url': it.get('url') or '',
            'time': it.get('pub_ts') or None,
        })
    # 构建提示词
    conf = load_ai_config()
    mp = (conf.get('module_prompts') or {}).get('newswatch') or DEFAULT_MODULE_PROMPTS['newswatch']
    system_prompt = mp.get('system') or DEFAULT_MODULE_PROMPTS['newswatch']['system']
    user_template = mp.get('user') or DEFAULT_MODULE_PROMPTS['newswatch']['user']
    payload_json = _json.dumps({'messages': msgs}, ensure_ascii=False)
    if '{{messages_data}}' in user_template:
        user_content = user_template.replace('{{messages_data}}', payload_json)
    else:
        user_content = user_template + "\n\n数据：\n" + payload_json
    # 调模型
    try:
        out = siliconflow_chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ], temperature=0.3)
        # 希望返回 {markdown: string}
        md = out
        try:
            j = _json.loads(out)
            if isinstance(j, dict) and 'markdown' in j:
                md = j.get('markdown') or md
        except Exception:
            pass
        return {"status": "ok", "markdown": md, "used": len(msgs), "model": conf.get('model')}
    except Exception as e:
        return {"status": "error", "error": str(e), "used": len(msgs)}

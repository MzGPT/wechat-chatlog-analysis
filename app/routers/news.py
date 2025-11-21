from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from typing import Optional

from ..config import settings
from fastapi import Body
from ..services.news_client import (
    direct_from_sources_json,
    normalize_items,
)


router = APIRouter(prefix="/api/newsfeed", tags=["newsfeed"])


@router.get("/health")
def health():
    # upstream removed; always ok
    return {"status": "ok"}


@router.get("/sources")
def list_sources():
    # upstream removed; empty list
    return {"success": True, "data": []}


@router.get("/items")
def list_items(
    keyword: Optional[str] = None,
    source: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    simple: bool = True,
    finance_only: bool = True,
    whitelist_only: bool = True,
):
    from ..services.news_client import _load_source_whitelist
    wl = _load_source_whitelist() if whitelist_only else []
    # 直接采集
    direct = direct_from_sources_json(limit=limit + offset, q=keyword)
    # 本地白名单与财经过滤（若白名单非空则忽略财经关键词）
    fo = finance_only if not wl else False
    norm = normalize_items({'success': True, 'data': direct.get('items', [])}, finance_only=fo, whitelist=wl)
    items = norm.get("items") or []
    # pagination on normalized list
    page = items[offset: offset + limit]
    return {"total": len(items), "items": page, "upstream_ok": norm.get("upstream_ok")}


@router.get("/search")
def search(q: str, limit: int = Query(20, ge=1, le=200), finance_only: bool = True, whitelist_only: bool = True):
    from ..services.news_client import _load_source_whitelist
    wl = _load_source_whitelist() if whitelist_only else []
    direct = direct_from_sources_json(limit=limit, q=q)
    fo = finance_only if not wl else False
    norm = normalize_items({'success': True, 'data': direct.get('items', [])}, finance_only=fo, whitelist=wl)
    return norm


@router.post("/refresh")
def refresh():
    # upstream removed; no-op
    return {"success": True}


@router.get("/by-ids")
def by_ids(ids: str, limit: int = Query(200, ge=1, le=1000)):
    """Fetch normalized news items by id list.

    IDs are matched as string equality on the normalized `id` field.
    """
    if not ids:
        return {"total": 0, "items": []}
    idset = {x.strip() for x in ids.split(',') if x.strip()}
    if not idset:
        return {"total": 0, "items": []}
    direct = direct_from_sources_json(limit=limit)
    norm = normalize_items({'success': True, 'data': direct.get('items', [])}, finance_only=True)
    items = norm.get('items') or []
    out = [it for it in items if str(it.get('id')) in idset]
    # keep input order if single id; otherwise arbitrary order is fine
    return {"total": len(out), "items": out}


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
def summarize_news(payload: dict = Body(default={})):  # accepts JSON body { ids?:[], q?:str, limit?:int, temperature?:float }
    """生成新闻舆情监测markdown（默认使用模块提示词 newswatch）。

    - 数据来源：若提供 q 则优先 search；否则从 /api/news 拉取 limit 条
    - 输出：{ status: ok, markdown: str, used: n, model: modelName }
    """
    from ..services.llm_client import load_ai_config, DEFAULT_MODULE_PROMPTS, siliconflow_chat
    import json as _json
    ids = payload.get('ids') if isinstance(payload, dict) else None
    q = payload.get('q') if isinstance(payload, dict) else None
    try:
        limit = int(payload.get('limit', 50))
    except Exception:
        limit = 50
    # 取数据：直接采集
    direct = direct_from_sources_json(limit=limit, q=q)
    # 使用来源白名单优先；若白名单非空，则不再按关键词 finance_only 过滤
    from ..services.news_client import _load_source_whitelist
    wl = _load_source_whitelist()
    fo = False if wl else True
    norm = normalize_items({'success': True, 'data': direct.get('items', [])}, finance_only=fo, whitelist=wl)
    raw_items = norm.get('items') or []
    # 仅保留近72小时新闻，避免模型被陈旧信息稀释
    from time import time as _time
    now_ms = int(_time() * 1000)
    cutoff = now_ms - 72 * 3600 * 1000
    items_72h = [it for it in raw_items if int(it.get('pub_ts') or 0) >= cutoff]
    items = items_72h or raw_items
    # 若传了 ids，仅过滤保留
    if ids and isinstance(ids, list):
        idset = {str(x) for x in ids if x is not None}
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
    # 调模型（温度优先用参数，其次用配置中的 model_temperature，默认 0.6）
    try:
        temp = float(payload.get('temperature')) if isinstance(payload, dict) and payload.get('temperature') is not None else None
    except Exception:
        temp = None
    if temp is None:
        try:
            temp = float(conf.get('model_temperature')) if conf.get('model_temperature') is not None else 0.6
        except Exception:
            temp = 0.6
    try:
        out = siliconflow_chat([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ], temperature=temp)
        # 希望返回 {markdown: string}
        md = out
        try:
            j = _json.loads(out)
            if isinstance(j, dict) and 'markdown' in j:
                md = j.get('markdown') or md
        except Exception:
            pass
        # 保存数据集到 datasets 目录
        try:
            import os, time
            ds_dir = os.path.abspath(os.path.join(os.getcwd(), 'data', 'datasets'))
            os.makedirs(ds_dir, exist_ok=True)
            fname = f"news_direct_{int(time.time())}.json"
            with open(os.path.join(ds_dir, fname), 'w', encoding='utf-8') as f:
                _json.dump({'items': items}, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return {"status": "ok", "markdown": md, "used": len(msgs), "model": conf.get('model')}
    except Exception as e:
        return {"status": "error", "error": str(e), "used": len(msgs)}

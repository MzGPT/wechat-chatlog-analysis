import os
import sys
import json


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def test_news_summarize_appends_quant(monkeypatch):
    from app.routers import news as news_router
    from app.services import llm_client
    from app.services import news_client

    # Avoid network: provide a single normalized news item within 72h.
    now_ms = 1_760_000_000_000
    fake_items = [
        {
            "id": "n1",
            "source_name": "X",
            "source_id": "x",
            "title": "黄金大涨",
            "url": "https://example.com",
            "pub_ts": now_ms,
        }
    ]

    monkeypatch.setattr(news_router, "direct_from_sources_json", lambda limit=50, q=None: {"items": fake_items})
    monkeypatch.setattr(news_router, "normalize_items", lambda raw, **kwargs: {"items": fake_items})
    monkeypatch.setattr(news_client, "_load_source_whitelist", lambda: [])

    def fake_chat(messages, temperature=0.3, model_override=None, force_json=False, **kwargs):
        return json.dumps(
            {
                "markdown": "# 新闻舆情监测\n- 总体基调：测试 #n1\n",
                "quant": {"topics": [{"topic": "黄金", "bullish_ids": ["n1"], "bearish_ids": [], "neutral_ids": []}]},
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(llm_client, "siliconflow_chat", fake_chat)

    res = news_router.summarize_news({"limit": 1, "temperature": 0.3})
    assert res["status"] == "ok"
    assert "## 量化分析" in (res.get("markdown") or "")

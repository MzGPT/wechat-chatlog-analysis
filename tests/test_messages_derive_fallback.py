import os
import sys
from datetime import datetime

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.schemas import MessageDeriveRequest


class _FakeMessage:
    def __init__(self, msg_id: int, content: str):
        self.id = msg_id
        self.timestamp = datetime(2026, 3, 20, 16, 0, 0)
        self.content_text = content
        self.type = "1"
        self.derived = None


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _ExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, messages):
        self._messages = messages
        self._calls = 0

    def execute(self, _query):
        self._calls += 1
        if self._calls == 1:
            return _ScalarResult(self._messages)
        return _ExecuteResult([(m.id, m.derived) for m in self._messages])


def test_messages_derive_prefills_fallback_before_tool_overlay(monkeypatch):
    from app.routers import messages as messages_router

    msg = _FakeMessage(101, "这是一个足够长的微信消息正文，用于验证在小模型没有产出时，仍然会先写入 fallback 摘要。")
    db = _FakeDB([msg])
    called = {"fallback": 0, "ensure": 0}

    def _fake_load_ai_config():
        return {"derive_defaults": {"batch_size": 20, "concurrency": 2, "temperature": 0.1, "force": False}}

    def _fake_populate_fallback(db_obj, rows, force=False, **kwargs):
        called["fallback"] += 1
        assert db_obj is db
        assert rows == [msg]
        assert force is False
        msg.derived = {"summary": "fallback: 这是一个兜底摘要", "summary_origin": "fallback"}
        return 1

    def _fake_ensure(db_obj, rows, **kwargs):
        called["ensure"] += 1
        assert db_obj is db
        assert rows == [msg]
        assert msg.derived["summary_origin"] == "fallback"
        return {"updated": 0, "errors": [], "debug": [], "applied": []}

    monkeypatch.setattr(messages_router, "load_ai_config", _fake_load_ai_config)
    monkeypatch.setattr(messages_router, "populate_fallback_derived", _fake_populate_fallback, raising=False)
    monkeypatch.setattr(messages_router, "ensure_message_features", _fake_ensure)

    body = MessageDeriveRequest(message_ids=[101])
    result = messages_router.derive_message_features(body=body, progress_key=None, db=db)

    assert result["status"] == "ok"
    assert called == {"fallback": 1, "ensure": 1}
    assert result["debug_readback"][0]["summary_origin"] == "fallback"

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.config import settings
from app.db import Base
from app.main import create_app
from app.models import Chat, Contact, Message, SyncState, WechatSubsession, WechatSubsessionMembership, WechatSubsessionTurn
from app.routers import wechat_gateway as wechat_gateway_router
from app.services.wechat_gateway import ingest_callback_event, save_config, save_trigger_rules


def _gateway_payload(**overrides):
    payload = {
        "enabled": True,
        "outbound_enabled": True,
        "allow_chat_ids": [],
        "block_chat_ids": [],
        "keyword_blocklist": [],
        "rate_limit_per_chat_per_minute": 30,
        "outbound_random_delay_min_seconds": 0,
        "outbound_random_delay_max_seconds": 0,
    }
    payload.update(overrides)
    return payload

API_HEADERS = ({"X-API-Token": str(settings.API_TOKEN).strip()} if str(getattr(settings, "API_TOKEN", "") or "").strip() else {})


def _session_factory(tmp_path: Path):
    db_path = tmp_path / "wechat-gateway-auto-reply.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(
        engine,
        tables=[
            Chat.__table__,
            Contact.__table__,
            Message.__table__,
            SyncState.__table__,
            WechatSubsession.__table__,
            WechatSubsessionMembership.__table__,
            WechatSubsessionTurn.__table__,
        ],
    )
    return TestingSession


def _private_callback(*, text: str, new_msg_id: int = 202, create_time: int = 1778036763) -> dict:
    return {
        "TypeName": "AddMsg",
        "Appid": "wx_app_test",
        "Wxid": "self_wxid",
        "Data": {
            "MsgId": 101,
            "NewMsgId": new_msg_id,
            "MsgType": 1,
            "CreateTime": create_time,
            "FromUserName": {"string": "wxid_friend"},
            "ToUserName": {"string": "self_wxid"},
            "Content": {"string": text},
        },
    }


def test_reply_local_uses_single_trigger_rule_layer(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        save_trigger_rules(
            db,
            {
                "enabled": True,
                "smart_reply_enabled": True,
                "group_enabled": True,
                "private_enabled": True,
                "prefixes": ["!"],
                "whitelist_chat_ids": [],
                "blacklist_chat_ids": [],
                "whitelist_sender_ids": [],
                "blacklist_sender_ids": [],
                "min_text_length": 2,
            },
        )
        save_config(
            db,
            _gateway_payload(),
        )
    finally:
        db.close()

    import app.services.reply_generation as reply_generation_service

    monkeypatch.setattr(
        reply_generation_service,
        "load_ai_config",
        lambda: {"api_key": "***", "tool_model": "fake-model", "tool_prompts": {}},
    )
    monkeypatch.setattr(reply_generation_service, "siliconflow_chat", lambda *args, **kwargs: "已生成回复")

    import app.routers.ai as ai_router

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = create_app()
    app.dependency_overrides[ai_router.get_db] = override_get_db
    client = TestClient(app)

    blocked = client.post(
        "/api/ai/reply-local",
        headers=API_HEADERS,
        json={
            "message_text": "你好",
            "chat_id": "wxid_friend",
            "sender_id": "wxid_friend",
            "sender_name": "好友",
            "talker_name": "好友",
            "is_group": False,
        },
    )
    assert blocked.status_code == 200
    blocked_data = blocked.json()
    assert blocked_data["status"] == "blocked"
    assert blocked_data["reason"] == "prefix_miss"

    ok = client.post(
        "/api/ai/reply-local",
        headers=API_HEADERS,
        json={
            "message_text": "!你好",
            "chat_id": "wxid_friend",
            "sender_id": "wxid_friend",
            "sender_name": "好友",
            "talker_name": "好友",
            "is_group": False,
        },
    )
    assert ok.status_code == 200
    ok_data = ok.json()
    assert ok_data["status"] == "ok"
    assert ok_data["reply"] == "已生成回复"


def test_private_reply_wakeup_allows_prefixless_followup_within_3_minutes(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        save_trigger_rules(
            db,
            {
                "enabled": True,
                "smart_reply_enabled": True,
                "group_enabled": True,
                "private_enabled": True,
                "prefixes": ["ai"],
                "whitelist_chat_ids": [],
                "blacklist_chat_ids": [],
                "whitelist_sender_ids": [],
                "blacklist_sender_ids": [],
                "min_text_length": 2,
            },
        )
        save_config(
            db,
            _gateway_payload(),
        )
    finally:
        db.close()

    import app.services.reply_generation as reply_generation_service

    monkeypatch.setattr(
        reply_generation_service,
        "load_ai_config",
        lambda: {"api_key": "***", "tool_model": "fake-model", "tool_prompts": {}},
    )
    monkeypatch.setattr(reply_generation_service, "siliconflow_chat", lambda *args, **kwargs: "已生成回复")

    import app.routers.ai as ai_router

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = create_app()
    app.dependency_overrides[ai_router.get_db] = override_get_db
    client = TestClient(app)

    wake = client.post(
        "/api/ai/reply-local",
        headers=API_HEADERS,
        json={
            "message_text": "ai 你好",
            "chat_id": "wxid_friend",
            "sender_id": "wxid_friend",
            "sender_name": "好友",
            "talker_name": "好友",
            "is_group": False,
            "message_time": "2026-05-08T10:00:00",
        },
    )
    assert wake.status_code == 200
    wake_data = wake.json()
    assert wake_data["status"] == "ok"

    followup = client.post(
        "/api/ai/reply-local",
        headers=API_HEADERS,
        json={
            "message_text": "继续说",
            "chat_id": "wxid_friend",
            "sender_id": "wxid_friend",
            "sender_name": "好友",
            "talker_name": "好友",
            "is_group": False,
            "message_time": "2026-05-08T10:02:59",
        },
    )
    assert followup.status_code == 200
    followup_data = followup.json()
    assert followup_data["status"] == "ok"
    assert followup_data["reply"] == "已生成回复"


def test_private_reply_wakeup_window_is_configurable(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        save_trigger_rules(
            db,
            {
                "enabled": True,
                "smart_reply_enabled": True,
                "group_enabled": True,
                "private_enabled": True,
                "prefixes": ["ai"],
                "private_wakeup_window_seconds": 30,
                "min_text_length": 2,
            },
        )
        save_config(db, _gateway_payload())
    finally:
        db.close()

    import app.services.reply_generation as reply_generation_service
    monkeypatch.setattr(reply_generation_service, "load_ai_config", lambda: {"api_key": "***", "tool_model": "fake-model", "tool_prompts": {}})
    monkeypatch.setattr(reply_generation_service, "siliconflow_chat", lambda *args, **kwargs: "已生成回复")
    import app.routers.ai as ai_router

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = create_app()
    app.dependency_overrides[ai_router.get_db] = override_get_db
    client = TestClient(app)

    wake = client.post("/api/ai/reply-local", headers=API_HEADERS, json={
        "message_text": "ai hi",
        "chat_id": "wxid_friend",
        "sender_id": "wxid_friend",
        "sender_name": "好友",
        "talker_name": "好友",
        "is_group": False,
        "message_time": "2026-05-08T10:00:00",
    })
    assert wake.status_code == 200
    assert wake.json()["status"] == "ok"

    allowed = client.post("/api/ai/reply-local", headers=API_HEADERS, json={
        "message_text": "继续",
        "chat_id": "wxid_friend",
        "sender_id": "wxid_friend",
        "sender_name": "好友",
        "talker_name": "好友",
        "is_group": False,
        "message_time": "2026-05-08T10:00:29",
    })
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "ok"

    cooled = client.post("/api/ai/reply-local", headers=API_HEADERS, json={
        "message_text": "继续",
        "chat_id": "wxid_friend",
        "sender_id": "wxid_friend",
        "sender_name": "好友",
        "talker_name": "好友",
        "is_group": False,
        "message_time": "2026-05-08T10:00:31",
    })
    assert cooled.status_code == 200
    assert cooled.json()["status"] == "blocked"
    assert cooled.json()["reason"] == "prefix_miss"


def test_private_reply_wakeup_can_be_limited_to_whitelisted_private_chats(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        save_trigger_rules(
            db,
            {
                "enabled": True,
                "smart_reply_enabled": True,
                "group_enabled": True,
                "private_enabled": True,
                "prefixes": ["ai"],
                "private_wakeup_whitelist_enabled": True,
                "private_wakeup_whitelist_chat_ids": ["wxid_allowed"],
                "min_text_length": 2,
            },
        )
        save_config(db, _gateway_payload())
    finally:
        db.close()

    import app.services.reply_generation as reply_generation_service
    monkeypatch.setattr(reply_generation_service, "load_ai_config", lambda: {"api_key": "***", "tool_model": "fake-model", "tool_prompts": {}})
    monkeypatch.setattr(reply_generation_service, "siliconflow_chat", lambda *args, **kwargs: "已生成回复")
    import app.routers.ai as ai_router

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = create_app()
    app.dependency_overrides[ai_router.get_db] = override_get_db
    client = TestClient(app)

    allowed_wake = client.post("/api/ai/reply-local", headers=API_HEADERS, json={
        "message_text": "ai hi",
        "chat_id": "wxid_allowed",
        "sender_id": "wxid_allowed",
        "sender_name": "好友A",
        "talker_name": "好友A",
        "is_group": False,
        "message_time": "2026-05-08T10:00:00",
    })
    assert allowed_wake.status_code == 200
    assert allowed_wake.json()["status"] == "ok"

    allowed_follow = client.post("/api/ai/reply-local", headers=API_HEADERS, json={
        "message_text": "继续",
        "chat_id": "wxid_allowed",
        "sender_id": "wxid_allowed",
        "sender_name": "好友A",
        "talker_name": "好友A",
        "is_group": False,
        "message_time": "2026-05-08T10:01:00",
    })
    assert allowed_follow.status_code == 200
    assert allowed_follow.json()["status"] == "ok"

    blocked_wake = client.post("/api/ai/reply-local", headers=API_HEADERS, json={
        "message_text": "ai hi",
        "chat_id": "wxid_blocked",
        "sender_id": "wxid_blocked",
        "sender_name": "好友B",
        "talker_name": "好友B",
        "is_group": False,
        "message_time": "2026-05-08T10:00:00",
    })
    assert blocked_wake.status_code == 200
    assert blocked_wake.json()["status"] == "ok"

    blocked_follow = client.post("/api/ai/reply-local", headers=API_HEADERS, json={
        "message_text": "继续",
        "chat_id": "wxid_blocked",
        "sender_id": "wxid_blocked",
        "sender_name": "好友B",
        "talker_name": "好友B",
        "is_group": False,
        "message_time": "2026-05-08T10:01:00",
    })
    assert blocked_follow.status_code == 200
    assert blocked_follow.json()["status"] == "blocked"
    assert blocked_follow.json()["reason"] == "prefix_miss"


def test_private_reply_wakeup_can_be_exited_by_command(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        save_trigger_rules(
            db,
            {
                "enabled": True,
                "smart_reply_enabled": True,
                "group_enabled": True,
                "private_enabled": True,
                "prefixes": ["ai"],
                "private_wakeup_exit_commands": ["暂停", "结束"],
                "min_text_length": 2,
            },
        )
        save_config(db, _gateway_payload())
    finally:
        db.close()

    import app.services.reply_generation as reply_generation_service
    monkeypatch.setattr(reply_generation_service, "load_ai_config", lambda: {"api_key": "***", "tool_model": "fake-model", "tool_prompts": {}})
    monkeypatch.setattr(reply_generation_service, "siliconflow_chat", lambda *args, **kwargs: "已生成回复")
    import app.routers.ai as ai_router

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = create_app()
    app.dependency_overrides[ai_router.get_db] = override_get_db
    client = TestClient(app)

    wake = client.post("/api/ai/reply-local", headers=API_HEADERS, json={
        "message_text": "ai hi",
        "chat_id": "wxid_friend",
        "sender_id": "wxid_friend",
        "sender_name": "好友",
        "talker_name": "好友",
        "is_group": False,
        "message_time": "2026-05-08T10:00:00",
    })
    assert wake.status_code == 200
    assert wake.json()["status"] == "ok"

    exit_resp = client.post("/api/ai/reply-local", headers=API_HEADERS, json={
        "message_text": "暂停",
        "chat_id": "wxid_friend",
        "sender_id": "wxid_friend",
        "sender_name": "好友",
        "talker_name": "好友",
        "is_group": False,
        "message_time": "2026-05-08T10:00:20",
    })
    assert exit_resp.status_code == 200
    exit_data = exit_resp.json()
    assert exit_data["status"] == "blocked"
    assert exit_data["reason"] == "private_wakeup_exited"

    follow = client.post("/api/ai/reply-local", headers=API_HEADERS, json={
        "message_text": "继续",
        "chat_id": "wxid_friend",
        "sender_id": "wxid_friend",
        "sender_name": "好友",
        "talker_name": "好友",
        "is_group": False,
        "message_time": "2026-05-08T10:00:30",
    })
    assert follow.status_code == 200
    assert follow.json()["status"] == "blocked"
    assert follow.json()["reason"] == "prefix_miss"


def test_private_reply_wakeup_expires_after_3_minutes_and_requires_retrigger(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        save_trigger_rules(
            db,
            {
                "enabled": True,
                "smart_reply_enabled": True,
                "group_enabled": True,
                "private_enabled": True,
                "prefixes": ["ai"],
                "whitelist_chat_ids": [],
                "blacklist_chat_ids": [],
                "whitelist_sender_ids": [],
                "blacklist_sender_ids": [],
                "min_text_length": 2,
            },
        )
        save_config(
            db,
            _gateway_payload(),
        )
    finally:
        db.close()

    import app.services.reply_generation as reply_generation_service

    monkeypatch.setattr(
        reply_generation_service,
        "load_ai_config",
        lambda: {"api_key": "***", "tool_model": "fake-model", "tool_prompts": {}},
    )
    monkeypatch.setattr(reply_generation_service, "siliconflow_chat", lambda *args, **kwargs: "已生成回复")

    import app.routers.ai as ai_router

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = create_app()
    app.dependency_overrides[ai_router.get_db] = override_get_db
    client = TestClient(app)

    wake = client.post(
        "/api/ai/reply-local",
        headers=API_HEADERS,
        json={
            "message_text": "ai 你好",
            "chat_id": "wxid_friend",
            "sender_id": "wxid_friend",
            "sender_name": "好友",
            "talker_name": "好友",
            "is_group": False,
            "message_time": "2026-05-08T10:00:00",
        },
    )
    assert wake.status_code == 200
    wake_data = wake.json()
    assert wake_data["status"] == "ok"

    cooled = client.post(
        "/api/ai/reply-local",
        headers=API_HEADERS,
        json={
            "message_text": "继续说",
            "chat_id": "wxid_friend",
            "sender_id": "wxid_friend",
            "sender_name": "好友",
            "talker_name": "好友",
            "is_group": False,
            "message_time": "2026-05-08T10:03:01",
        },
    )
    assert cooled.status_code == 200
    cooled_data = cooled.json()
    assert cooled_data["status"] == "blocked"
    assert cooled_data["reason"] == "prefix_miss"


def test_callback_auto_reply_sends_when_trigger_rules_pass(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        save_config(
            db,
            _gateway_payload(token="***", app_id="wx_app_test"),
        )
        save_trigger_rules(
            db,
            {
                "enabled": True,
                "smart_reply_enabled": True,
                "group_enabled": True,
                "private_enabled": True,
                "prefixes": ["!"],
                "whitelist_chat_ids": [],
                "blacklist_chat_ids": [],
                "whitelist_sender_ids": [],
                "blacklist_sender_ids": [],
                "min_text_length": 2,
            },
        )
    finally:
        db.close()

    send_calls = []

    class DummyWechatApiClient:
        def __init__(self, *args, **kwargs):
            pass

        def configured(self):
            return True

        def send_text(self, to_wxid: str, text: str):
            send_calls.append((to_wxid, text))
            return {"ret": 200, "msg": "操作成功", "data": {"toWxid": to_wxid, "msgId": 11, "newMsgId": 22, "type": 1}}

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(
        wechat_gateway_router,
        "generate_local_reply",
        lambda db, payload: {"status": "ok", "reply": "自动回复已发送", "prompt_key": "reply_da", "rule": {"allowed": True, "reason": "passed"}},
        raising=False,
    )
    monkeypatch.setattr(wechat_gateway_router, "WechatApiClient", DummyWechatApiClient, raising=False)

    app = create_app()
    app.dependency_overrides[wechat_gateway_router.get_db] = override_get_db
    client = TestClient(app)

    response = client.post("/api/wechat-gateway/callback", json=_private_callback(text="!你好"), headers=API_HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["stored"] is True
    assert data["auto_reply"]["status"] == "sent"
    assert data["auto_reply"]["reply"] == "自动回复已发送"
    assert send_calls == [("wxid_friend", "自动回复已发送")]

    verify = Session()
    try:
        rows = verify.query(Message).order_by(Message.id.asc()).all()
        assert len(rows) == 2
        inbound, outbound = rows
        assert inbound.direction == "in"
        assert outbound.direction == "out"
        assert outbound.chat_id == "wxid_friend"
        assert outbound.content_text == "自动回复已发送"
        assert (outbound.meta or {}).get("auto_reply", {}).get("trigger_message_id") == inbound.id
    finally:
        verify.close()


def test_generate_local_reply_prefers_subsession_prompt_and_route_over_global(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        save_trigger_rules(
            db,
            {
                "enabled": True,
                "smart_reply_enabled": True,
                "group_enabled": True,
                "private_enabled": True,
                "prefixes": ["ai"],
                "min_text_length": 2,
            },
        )
        db.add(
            WechatSubsession(
                id="wechat_gateway_default",
                channel="wechat_gateway",
                name="微信工作流分身",
                enabled=True,
                mode="fixed",
                system_prompt="你是 subsession 专属助手，只能按 subsession 规则回答。",
                model_route_kind="tool",
                model_route_key="reply_subsession",
                model_override="subsession-model",
            )
        )
        db.commit()
    finally:
        db.close()

    import app.services.reply_generation as reply_generation_service

    monkeypatch.setattr(
        reply_generation_service,
        "load_ai_config",
        lambda: {
            "api_key": "***",
            "tool_model": "global-model",
            "tool_prompts": {
                "reply_da": {
                    "system": "全局 system prompt",
                    "user": "全局用户模板：{{message_text}}",
                }
            },
        },
    )
    captured = {}

    def _fake_chat(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return "subsession reply"

    monkeypatch.setattr(reply_generation_service, "siliconflow_chat", _fake_chat)

    verify = Session()
    try:
        result = reply_generation_service.generate_local_reply(
            verify,
            {
                "message_text": "ai 请按 subsession 回复",
                "chat_id": "room_a@chatroom",
                "sender_id": "wxid_sender_a",
                "sender_name": "发送者A",
                "talker_name": "群A",
                "is_group": True,
                "subsession_id": "wechat_gateway_default",
            },
        )
        assert result["status"] == "ok"
        assert result["reply"] == "subsession reply"
        assert result["subsession_id"] == "wechat_gateway_default"
        assert result["prompt_key"] == "reply_da"
        assert captured["messages"][0]["content"] == "你是 subsession 专属助手，只能按 subsession 规则回答。"
        assert captured["messages"][1]["content"] == "全局用户模板：ai 请按 subsession 回复"
        assert captured["kwargs"]["model_override"] == "subsession-model"
        assert captured["kwargs"]["route_kind"] == "tool"
        assert captured["kwargs"]["route_key"] == "reply_subsession"
    finally:
        verify.close()


def test_generate_local_reply_returns_execution_metadata_for_subsession_route(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        save_trigger_rules(
            db,
            {
                "enabled": True,
                "smart_reply_enabled": True,
                "group_enabled": True,
                "private_enabled": True,
                "prefixes": ["ai"],
                "min_text_length": 2,
            },
        )
        db.add(
            WechatSubsession(
                id="wechat_gateway_default",
                channel="wechat_gateway",
                name="微信工作流分身",
                enabled=True,
                mode="fixed",
                system_prompt="你是 subsession 专属助手，只能按 subsession 规则回答。",
                model_route_kind="tool",
                model_route_key="reply_subsession",
                model_override="subsession-model",
            )
        )
        db.commit()
    finally:
        db.close()

    import app.services.reply_generation as reply_generation_service

    monkeypatch.setattr(
        reply_generation_service,
        "load_ai_config",
        lambda: {
            "api_key": "***",
            "tool_model": "global-model",
            "tool_prompts": {
                "reply_da": {
                    "system": "全局 system prompt",
                    "user": "全局用户模板：{{message_text}}",
                }
            },
        },
    )

    def _fake_chat(messages, **kwargs):
        return {
            "text": "subsession reply",
            "execution": {
                "route_kind": kwargs.get("route_kind"),
                "route_key": kwargs.get("route_key"),
                "final_model": "resolved-subsession-model",
                "provider": "api.siliconflow.cn",
                "channel_id": "tool-reply-primary",
            },
        }

    monkeypatch.setattr(reply_generation_service, "siliconflow_chat", _fake_chat)

    verify = Session()
    try:
        result = reply_generation_service.generate_local_reply(
            verify,
            {
                "message_text": "ai 请按 subsession 回复",
                "chat_id": "room_a@chatroom",
                "sender_id": "wxid_sender_a",
                "sender_name": "发送者A",
                "talker_name": "群A",
                "is_group": True,
                "subsession_id": "wechat_gateway_default",
            },
        )
        assert result["status"] == "ok"
        assert result["reply"] == "subsession reply"
        assert result["execution"]["route_kind"] == "tool"
        assert result["execution"]["route_key"] == "reply_subsession"
        assert result["execution"]["configured_model"] == "subsession-model"
        assert result["execution"]["final_model"] == "resolved-subsession-model"
        assert result["execution"]["provider"] == "api.siliconflow.cn"
        assert result["execution"]["channel_id"] == "tool-reply-primary"
    finally:
        verify.close()


def test_callback_auto_reply_passes_subsession_id_into_generate_local_reply(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        save_config(
            db,
            _gateway_payload(
                token="***",
                app_id="wx_app_test",
                sessionized_reply_enabled=True,
                fixed_subsession_enabled=True,
                fixed_subsession_id="wechat_gateway_default",
                fixed_subsession_name="微信工作流分身",
                auto_learn_subsession_members=True,
            ),
        )
        save_trigger_rules(
            db,
            {
                "enabled": True,
                "smart_reply_enabled": True,
                "group_enabled": True,
                "private_enabled": True,
                "prefixes": ["ai"],
                "min_text_length": 2,
            },
        )
    finally:
        db.close()

    captured = {}

    class DummyWechatApiClient:
        def __init__(self, *args, **kwargs):
            pass

        def configured(self):
            return True

        def send_text(self, to_wxid: str, text: str):
            return {"ret": 200, "msg": "操作成功", "data": {"toWxid": to_wxid, "msgId": 11, "newMsgId": 22, "type": 1}}

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    def _capture_generate(db, payload):
        captured.update(payload)
        return {"status": "ok", "reply": "subsession auto reply", "prompt_key": "reply_da", "rule": {"allowed": True, "reason": "passed"}, "subsession_id": payload.get("subsession_id")}

    monkeypatch.setattr(wechat_gateway_router, "generate_local_reply", _capture_generate, raising=False)
    monkeypatch.setattr(wechat_gateway_router, "WechatApiClient", DummyWechatApiClient, raising=False)

    app = create_app()
    app.dependency_overrides[wechat_gateway_router.get_db] = override_get_db
    client = TestClient(app)

    response = client.post("/api/wechat-gateway/callback", json=_private_callback(text="ai 进入 subsession", new_msg_id=303))
    assert response.status_code == 200
    data = response.json()
    assert data["stored"] is True
    assert data["subsession_id"] == "wechat_gateway_default"
    assert captured["subsession_id"] == "wechat_gateway_default"
    assert isinstance(captured.get("message_meta"), dict)
    assert (captured.get("message_meta") or {}).get("subsession", {}).get("id") == "wechat_gateway_default"


def test_callback_auto_reply_persists_execution_metadata_into_response_and_outbound_meta(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        save_config(
            db,
            _gateway_payload(token="***", app_id="wx_app_test"),
        )
        save_trigger_rules(
            db,
            {
                "enabled": True,
                "smart_reply_enabled": True,
                "group_enabled": True,
                "private_enabled": True,
                "prefixes": ["ai"],
                "whitelist_chat_ids": [],
                "blacklist_chat_ids": [],
                "whitelist_sender_ids": [],
                "blacklist_sender_ids": [],
                "min_text_length": 2,
            },
        )
    finally:
        db.close()

    class DummyWechatApiClient:
        def __init__(self, *args, **kwargs):
            pass

        def configured(self):
            return True

        def send_text(self, to_wxid: str, text: str):
            return {"ret": 200, "msg": "操作成功", "data": {"toWxid": to_wxid, "msgId": 11, "newMsgId": 22, "type": 1}}

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(
        wechat_gateway_router,
        "generate_local_reply",
        lambda db, payload: {
            "status": "ok",
            "reply": "自动回复已发送",
            "prompt_key": "reply_da",
            "rule": {"allowed": True, "reason": "passed"},
            "subsession_id": "wechat_gateway_default",
            "execution": {
                "route_kind": "tool",
                "route_key": "reply_subsession",
                "configured_model": "subsession-model",
                "final_model": "resolved-subsession-model",
                "provider": "api.siliconflow.cn",
                "channel_id": "tool-reply-primary",
            },
        },
        raising=False,
    )
    monkeypatch.setattr(wechat_gateway_router, "WechatApiClient", DummyWechatApiClient, raising=False)

    app = create_app()
    app.dependency_overrides[wechat_gateway_router.get_db] = override_get_db
    client = TestClient(app)

    response = client.post("/api/wechat-gateway/callback", json=_private_callback(text="ai 你好", new_msg_id=909), headers=API_HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["stored"] is True
    assert data["auto_reply"]["status"] == "sent"
    assert data["auto_reply"]["execution"]["route_key"] == "reply_subsession"
    assert data["auto_reply"]["execution"]["final_model"] == "resolved-subsession-model"

    verify = Session()
    try:
        rows = verify.query(Message).order_by(Message.id.asc()).all()
        assert len(rows) == 2
        outbound = rows[1]
        execution = ((outbound.meta or {}).get("auto_reply") or {}).get("execution") or {}
        assert execution["route_kind"] == "tool"
        assert execution["route_key"] == "reply_subsession"
        assert execution["configured_model"] == "subsession-model"
        assert execution["final_model"] == "resolved-subsession-model"
        assert execution["provider"] == "api.siliconflow.cn"
        assert execution["channel_id"] == "tool-reply-primary"
    finally:
        verify.close()


def test_callback_auto_reply_returns_execution_metadata_when_generation_errors(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        save_config(
            db,
            _gateway_payload(token="***", app_id="wx_app_test"),
        )
        save_trigger_rules(
            db,
            {
                "enabled": True,
                "smart_reply_enabled": True,
                "group_enabled": True,
                "private_enabled": True,
                "prefixes": ["ai"],
                "min_text_length": 2,
            },
        )
    finally:
        db.close()

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(
        wechat_gateway_router,
        "generate_local_reply",
        lambda db, payload: {
            "status": "error",
            "error": "provider timeout",
            "rule": {"allowed": True, "reason": "passed"},
            "execution": {
                "route_kind": "tool",
                "route_key": "reply_subsession",
                "configured_model": "subsession-model",
                "error": "provider timeout",
            },
        },
        raising=False,
    )

    app = create_app()
    app.dependency_overrides[wechat_gateway_router.get_db] = override_get_db
    client = TestClient(app)

    response = client.post("/api/wechat-gateway/callback", json=_private_callback(text="ai 你好", new_msg_id=910), headers=API_HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["stored"] is True
    assert data["auto_reply"]["status"] == "error"
    assert data["auto_reply"]["execution"]["route_key"] == "reply_subsession"
    assert data["auto_reply"]["execution"]["error"] == "provider timeout"


def test_callback_auto_reply_blocks_when_manual_reply_arrives_within_suppression_window(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        save_config(
            db,
            _gateway_payload(token="***", app_id="wx_app_test"),
        )
        save_trigger_rules(
            db,
            {
                "enabled": True,
                "smart_reply_enabled": True,
                "group_enabled": True,
                "private_enabled": True,
                "prefixes": ["ai"],
                "whitelist_chat_ids": [],
                "blacklist_chat_ids": [],
                "whitelist_sender_ids": [],
                "blacklist_sender_ids": [],
                "min_text_length": 2,
                "human_reply_suppression_seconds": 5,
            },
        )
    finally:
        db.close()

    send_calls = []

    class DummyWechatApiClient:
        def __init__(self, *args, **kwargs):
            pass

        def configured(self):
            return True

        def send_text(self, to_wxid: str, text: str):
            send_calls.append((to_wxid, text))
            return {"ret": 200, "msg": "操作成功", "data": {"toWxid": to_wxid, "msgId": 11, "newMsgId": 22, "type": 1}}

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    def _manual_reply_generate(db, payload):
        import threading
        import time
        inbound_message_time = datetime.fromisoformat(str(payload.get("message_time")))

        def _writer():
            time.sleep(0.2)
            session = Session()
            try:
                session.add(
                    Message(
                        chat_id="wxid_friend",
                        sender_id="self_wxid",
                        sender_name="self_wxid",
                        talker_name="wxid_friend",
                        timestamp=inbound_message_time + timedelta(seconds=0.2),
                        direction="out",
                        type="text",
                        content_text="人工接管",
                        meta={"source": "wechat_gateway", "manual": True},
                    )
                )
                session.commit()
            finally:
                session.close()

        threading.Thread(target=_writer, daemon=True).start()
        from app.services.reply_generation import generate_local_reply as real_generate
        return real_generate(db, payload)

    monkeypatch.setattr(wechat_gateway_router, "generate_local_reply", _manual_reply_generate, raising=False)
    monkeypatch.setattr(wechat_gateway_router, "WechatApiClient", DummyWechatApiClient, raising=False)

    app = create_app()
    app.dependency_overrides[wechat_gateway_router.get_db] = override_get_db
    client = TestClient(app)

    response = client.post(
        "/api/wechat-gateway/callback",
        json=_private_callback(text="ai 你好", new_msg_id=404, create_time=int(time.time())),
        headers=API_HEADERS,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["stored"] is True
    assert data["auto_reply"]["status"] == "blocked"
    assert data["auto_reply"]["reason"] == "human_reply_suppressed"
    assert send_calls == []


def test_callback_auto_reply_rechecks_human_takeover_before_sending(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        save_config(
            db,
            _gateway_payload(token="***", app_id="wx_app_test"),
        )
        save_trigger_rules(
            db,
            {
                "enabled": True,
                "smart_reply_enabled": True,
                "group_enabled": True,
                "private_enabled": True,
                "prefixes": ["ai"],
                "whitelist_chat_ids": [],
                "blacklist_chat_ids": [],
                "whitelist_sender_ids": [],
                "blacklist_sender_ids": [],
                "min_text_length": 2,
                "human_reply_suppression_seconds": 1,
            },
        )
    finally:
        db.close()

    send_calls = []

    class DummyWechatApiClient:
        def __init__(self, *args, **kwargs):
            pass

        def configured(self):
            return True

        def send_text(self, to_wxid: str, text: str):
            send_calls.append((to_wxid, text))
            return {"ret": 200, "msg": "操作成功", "data": {"toWxid": to_wxid, "msgId": 11, "newMsgId": 22, "type": 1}}

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    import app.services.reply_generation as reply_generation_service

    monkeypatch.setattr(
        reply_generation_service,
        "load_ai_config",
        lambda: {"api_key": "***", "tool_model": "fake-model", "tool_prompts": {}},
    )

    def _slow_chat(*args, **kwargs):
        import threading
        import time as _time
        message_time = kwargs.pop("_message_time", None)
        if message_time:
            inbound_message_time = datetime.fromisoformat(str(message_time))

            def _writer():
                _time.sleep(0.2)
                session = Session()
                try:
                    session.add(
                        Message(
                            chat_id="wxid_friend",
                            sender_id="self_wxid",
                            sender_name="self_wxid",
                            talker_name="wxid_friend",
                            timestamp=inbound_message_time + timedelta(seconds=1.2),
                            direction="out",
                            type="text",
                            content_text="人工接管",
                            meta={"source": "wechat_gateway", "manual": True},
                        )
                    )
                    session.commit()
                finally:
                    session.close()

            threading.Thread(target=_writer, daemon=True).start()
        _time.sleep(0.6)
        return "自动回复已发送"

    original_generate = wechat_gateway_router.generate_local_reply

    def _wrapped_generate(db, payload):
        original_chat = reply_generation_service.siliconflow_chat
        reply_generation_service.siliconflow_chat = lambda *args, **kwargs: _slow_chat(*args, _message_time=payload.get("message_time"), **kwargs)
        try:
            return original_generate(db, payload)
        finally:
            reply_generation_service.siliconflow_chat = original_chat

    monkeypatch.setattr(wechat_gateway_router, "generate_local_reply", _wrapped_generate, raising=False)
    monkeypatch.setattr(wechat_gateway_router, "WechatApiClient", DummyWechatApiClient, raising=False)

    app = create_app()
    app.dependency_overrides[wechat_gateway_router.get_db] = override_get_db
    client = TestClient(app)

    response = client.post(
        "/api/wechat-gateway/callback",
        json=_private_callback(text="ai 你好", new_msg_id=405, create_time=int(time.time())),
        headers=API_HEADERS,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["stored"] is True
    assert data["auto_reply"]["status"] == "blocked"
    assert data["auto_reply"]["reason"] == "human_reply_suppressed"
    assert send_calls == []


def test_callback_auto_reply_blocks_without_sending_when_trigger_rules_fail(tmp_path, monkeypatch):
    Session = _session_factory(tmp_path)
    db = Session()
    try:
        save_config(
            db,
            _gateway_payload(),
        )
        save_trigger_rules(
            db,
            {
                "enabled": True,
                "smart_reply_enabled": True,
                "group_enabled": True,
                "private_enabled": True,
                "prefixes": ["!"],
                "whitelist_chat_ids": [],
                "blacklist_chat_ids": [],
                "whitelist_sender_ids": [],
                "blacklist_sender_ids": [],
                "min_text_length": 2,
            },
        )
    finally:
        db.close()

    calls = {"generate": 0}

    def override_get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    def _blocked_generate(db, payload):
        calls["generate"] += 1
        return {"status": "blocked", "reason": "prefix_miss", "rule": {"allowed": False, "reason": "prefix_miss"}}

    monkeypatch.setattr(wechat_gateway_router, "generate_local_reply", _blocked_generate, raising=False)

    app = create_app()
    app.dependency_overrides[wechat_gateway_router.get_db] = override_get_db
    client = TestClient(app)

    response = client.post("/api/wechat-gateway/callback", json=_private_callback(text="你好", new_msg_id=303), headers=API_HEADERS)
    assert response.status_code == 200
    data = response.json()
    assert data["stored"] is True
    assert data["auto_reply"]["status"] == "blocked"
    assert data["auto_reply"]["reason"] == "prefix_miss"
    assert calls["generate"] == 1

    verify = Session()
    try:
        rows = verify.query(Message).order_by(Message.id.asc()).all()
        assert len(rows) == 1
        assert rows[0].direction == "in"
    finally:
        verify.close()


def test_concurrent_fixed_subsession_callback_ingest_is_idempotent(tmp_path):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    db_path = tmp_path / "wechat-gateway-concurrent-subsession.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        future=True,
        connect_args={"check_same_thread": False},
    )
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(
        engine,
        tables=[
            Chat.__table__,
            Contact.__table__,
            Message.__table__,
            SyncState.__table__,
            WechatSubsession.__table__,
            WechatSubsessionMembership.__table__,
            WechatSubsessionTurn.__table__,
        ],
    )
    db = Session()
    try:
        save_config(
            db,
            _gateway_payload(
                token="***",
                app_id="wx_app_test",
                sessionized_reply_enabled=True,
                fixed_subsession_enabled=True,
                fixed_subsession_id="wechat_gateway_default",
                fixed_subsession_name="微信工作流分身",
                auto_learn_subsession_members=True,
            ),
        )
    finally:
        db.close()

    def _ingest(idx: int):
        session = Session()
        try:
            return ingest_callback_event(
                session,
                _private_callback(text="ai 并发进入 subsession", new_msg_id=9000 + idx, create_time=1778036763 + idx),
            )
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(_ingest, idx) for idx in range(8)]
        results = [future.result() for future in as_completed(futures)]

    assert len(results) == 8
    assert all(item["stored"] is True for item in results)
    assert {item["subsession_id"] for item in results} == {"wechat_gateway_default"}

    verify = Session()
    try:
        assert verify.query(WechatSubsession).count() == 1
        memberships = verify.query(WechatSubsessionMembership).all()
        assert {(row.member_type, row.member_key) for row in memberships} == {
            ("chat", "wxid_friend"),
            ("sender", "wxid_friend"),
        }
        assert verify.query(WechatSubsessionTurn).count() == 8
    finally:
        verify.close()

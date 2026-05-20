from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.requests import Request

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.db import Base
from app.models import SendCampaign, SendDelivery
from app.routers import send
from app.schemas import SendCampaignCreateRequest, SendRequest
from app.services import send_dispatcher


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "scheme": "http",
            "server": ("testserver", 80),
            "headers": [],
            "path": "/api/send/campaigns",
            "query_string": b"",
        }
    )


def _db(tmp_path):
    db_path = tmp_path / "send-test.db"
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)
    Base.metadata.create_all(engine, tables=[SendCampaign.__table__, SendDelivery.__table__])
    return TestingSession()


def test_dispatch_send_item_wechat_gateway_media_renders_text_fallback(monkeypatch):
    monkeypatch.setattr(send_dispatcher, "load_ai_config", lambda: {"send_provider": "wechatapi_gateway"})

    class _DummyClient:
        def configured(self):
            return True

        def send_text(self, *, to_wxid, text, **_kwargs):
            return {"ret": "200", "to_wxid": to_wxid, "text": text}

    monkeypatch.setattr(send_dispatcher, "WechatApiClient", _DummyClient)
    monkeypatch.setattr(send_dispatcher, "load_wechat_gateway_config", lambda _db: {"outbound_enabled": True})
    monkeypatch.setattr(send_dispatcher, "evaluate_outbound_message", lambda _cfg, **_kwargs: {"allowed": True})
    monkeypatch.setattr(send_dispatcher, "apply_outbound_random_delay", lambda _cfg: None)
    monkeypatch.setattr(send_dispatcher, "record_outbound_message", lambda *_args, **_kwargs: None)

    result = send_dispatcher.dispatch_send_item(
        {
            "target": "filehelper",
            "content_parts": [{"type": "text", "text": "测试正文"}],
            "attachments": [{"type": "image", "name": "a.png", "url": "https://example.com/a.png", "kind": "image"}],
        }
    )

    assert result["ok"] is True
    assert result["provider"] == "wechatapi_gateway"
    assert result["mode"] == "text"
    assert "https://example.com/a.png" in result["rendered_text"]


def test_create_campaign_save_only_persists_deliveries(tmp_path):
    db = _db(tmp_path)
    try:
        body = SendCampaignCreateRequest(
            title="晨会群发",
            body_text="大家好",
            items=[
                {"target": "wxid_a", "text": "A"},
                {"target": "wxid_b", "text": "B"},
            ],
            send_now=False,
            save_only=True,
        )
        detail = send.create_send_campaign(body, _request(), db=db)
        assert detail.id > 0
        assert detail.status == "draft"
        assert detail.target_count == 2
        assert len(detail.deliveries) == 2
        assert {row.target_id for row in detail.deliveries} == {"wxid_a", "wxid_b"}
    finally:
        db.close()


def test_create_campaign_send_updates_counts(monkeypatch, tmp_path):
    db = _db(tmp_path)
    results = iter(
        [
            {"ok": True, "provider": "wechatpad_direct", "rendered_text": "成功"},
            {"ok": False, "provider": "wechatpad_direct", "rendered_text": "失败", "error": "network"},
        ]
    )
    monkeypatch.setattr(send, "dispatch_send_item", lambda _item: next(results))
    try:
        body = SendCampaignCreateRequest(
            title="晚报",
            body_text="测试",
            items=[
                {"target": "wxid_ok", "text": "hello"},
                {"target": "wxid_fail", "text": "hello"},
            ],
            send_now=True,
        )
        detail = send.create_send_campaign(body, _request(), db=db)
        assert detail.target_count == 2
        assert detail.success_count == 1
        assert detail.failed_count == 1
        assert detail.status == "partial"
        statuses = {row.target_id: row.status for row in detail.deliveries}
        assert statuses["wxid_ok"] == "sent"
        assert statuses["wxid_fail"] == "failed"
    finally:
        db.close()


def test_send_out_legacy_text_request(monkeypatch):
    monkeypatch.setattr(send, "load_ai_config", lambda: {"send_provider": "wechatpad_direct"})
    monkeypatch.setattr(
        send,
        "dispatch_send_items",
        lambda items, request=None: {"status": "ok", "results": [{"ok": True, "target": items[0].target}]},
    )
    body = SendRequest(items=[{"target": "filehelper", "text": "hello"}])
    payload = send.send_out(body, _request())
    assert payload["status"] == "ok"
    assert payload["results"][0]["target"] == "filehelper"


def test_save_send_upload_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(send_dispatcher.settings, "DATABASE_URL", f"sqlite:///{tmp_path / 'app.db'}", raising=False)
    upload = StarletteUploadFile(filename="brief.txt", file=open(Path(__file__), "rb"), headers=None)
    try:
        meta = send_dispatcher.save_send_upload(upload, _request())
        assert meta["file_id"]
        assert meta["name"] == "brief.txt"
        assert meta["url"].endswith(meta["file_id"])
        path = send_dispatcher.get_send_upload_path(meta["file_id"])
        assert path and path.exists()
    finally:
        upload.file.close()


def test_create_campaign_dedupes_duplicate_targets(tmp_path):
    db = _db(tmp_path)
    try:
        body = SendCampaignCreateRequest(
            title="重复目标",
            body_text="正文",
            items=[
                {"target": "wxid_a", "text": "first"},
                {"target": "wxid_a", "text": "second"},
                {"target": "wxid_b", "text": "third"},
            ],
            send_now=False,
            save_only=True,
        )
        detail = send.create_send_campaign(body, _request(), db=db)
        assert detail.target_count == 2
        assert [row.target_id for row in detail.deliveries] == ["wxid_a", "wxid_b"]
        assert detail.meta["deduped_targets"] == ["wxid_a"]
    finally:
        db.close()


def test_retry_does_not_resend_already_sent_delivery(monkeypatch, tmp_path):
    db = _db(tmp_path)
    calls = []

    def _fake_dispatch(item):
        calls.append(item["target"])
        return {"ok": True, "provider": "wechatpad_direct", "rendered_text": "ok"}

    monkeypatch.setattr(send, "dispatch_send_item", _fake_dispatch)
    try:
        body = SendCampaignCreateRequest(
            title="重试保护",
            body_text="正文",
            items=[{"target": "wxid_a", "text": "A"}, {"target": "wxid_b", "text": "B"}],
            send_now=False,
            save_only=True,
        )
        detail = send.create_send_campaign(body, _request(), db=db)
        sent = db.get(SendDelivery, detail.deliveries[0].id)
        failed = db.get(SendDelivery, detail.deliveries[1].id)
        sent.status = "sent"
        sent.sent_at = datetime.utcnow()
        failed.status = "failed"
        failed.error = "network"
        db.commit()

        retry = send.retry_send_campaign(
            detail.id,
            send.SendRetryRequest(delivery_ids=[sent.id, failed.id]),
            db=db,
        )

        assert calls == ["wxid_b"]
        by_id = {row.id: row for row in retry.deliveries}
        assert by_id[sent.id].status == "sent"
        assert by_id[failed.id].status == "sent"
        assert retry.meta["last_retry"]["skipped_already_sent"] == [sent.id]
    finally:
        db.close()

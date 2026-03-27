import os
import sys

from starlette.requests import Request

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.main import (
    _configured_api_tokens,
    _cors_options,
    _extract_api_token,
    _is_api_auth_exempt_path,
)
from app.routers import ai, contacts, email
from app.schemas import EmailAccountIn


class _DummyScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _DummyExecuteResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _DummyScalars(self._rows)


class _DummyDb:
    def __init__(self, rows=None, row=None, execute_results=None):
        self._rows = rows or []
        self._row = row
        self._execute_results = list(execute_results or [])
        self.committed = False
        self.refreshed = False

    def execute(self, *_args, **_kwargs):
        if self._execute_results:
            return self._execute_results.pop(0)
        return _DummyExecuteResult(self._rows)

    def get(self, *_args, **_kwargs):
        return self._row

    def commit(self):
        self.committed = True

    def refresh(self, _row):
        self.refreshed = True


class _EmailRow:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _ContactRow:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)



def test_get_ai_config_masks_router_secrets(monkeypatch):
    monkeypatch.setattr(
        ai,
        "load_ai_config",
        lambda: {
            "api_key": "top-secret",
            "api_url": "https://example.com/v1",
            "model": "main-model",
            "tool_model": "tool-model",
            "model_router": {
                "enabled": True,
                "main_channels": [
                    {
                        "id": "main-1",
                        "model": "model-a",
                        "api_url": "https://a.example/v1",
                        "api_key": "secret-a",
                        "weight": 1,
                        "enabled": True,
                    }
                ],
                "tool_channels": [
                    {
                        "id": "tool-1",
                        "model": "tool-a",
                        "api_key": "secret-b",
                        "weight": 1,
                        "enabled": True,
                    }
                ],
            },
        },
    )

    payload = ai.get_ai_config()
    assert payload["has_key"] is True
    assert "api_key" not in payload
    main_channel = payload["model_router"]["main_channels"][0]
    assert main_channel["api_key"] == ""
    assert main_channel["has_api_key"] is True
    tool_channel = payload["model_router"]["tool_channels"][0]
    assert tool_channel["api_key"] == ""
    assert tool_channel["has_api_key"] is True



def test_list_accounts_masks_password_but_keeps_username():
    row = _EmailRow(
        id=1,
        name="test",
        email_address="a@example.com",
        provider="custom",
        imap_host="imap.example.com",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.example.com",
        smtp_port=465,
        smtp_ssl=True,
        auth={"username": "alice", "password": "secret-pass"},
        enabled=True,
        last_sync_at=None,
    )
    out = email.list_accounts(db=_DummyDb(rows=[row]))
    assert out[0].auth["username"] == "alice"
    assert out[0].auth["password"] == ""
    assert out[0].auth["has_password"] is True



def test_update_account_preserves_existing_password_when_blank():
    row = _EmailRow(
        id=1,
        name="acct",
        email_address="a@example.com",
        provider="custom",
        imap_host="imap.old",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.old",
        smtp_port=465,
        smtp_ssl=True,
        auth={"username": "alice", "password": "kept-secret"},
        enabled=True,
        last_sync_at=None,
    )
    db = _DummyDb(row=row)
    body = EmailAccountIn(
        name="acct",
        email_address="a@example.com",
        provider="custom",
        imap_host="imap.new",
        imap_port=993,
        imap_ssl=True,
        smtp_host="smtp.new",
        smtp_port=465,
        smtp_ssl=True,
        auth={"username": "alice", "password": ""},
        enabled=True,
    )

    out = email.update_account(1, body=body, db=db)
    assert db.committed is True
    assert row.auth["password"] == "kept-secret"
    assert out.auth["password"] == ""
    assert out.auth["has_password"] is True


def test_list_email_messages_omits_bodies_by_default():
    row = _EmailRow(
        id=9,
        account_id=1,
        external_id="x",
        subject="subject",
        from_addr="from@example.com",
        to_addrs=["to@example.com"],
        cc_addrs=[],
        bcc_addrs=[],
        sent_at=None,
        direction="in",
        snippet="snippet",
        body_text="very long body",
        body_html="<p>very long body</p>",
        flags=[],
        meta=None,
        derived={},
    )
    db = _DummyDb(
        execute_results=[
            _DummyExecuteResult([object(), object()]),
            _DummyExecuteResult([row]),
        ]
    )
    out = email.list_email_messages(db=db)
    assert out["total"] == 2
    assert out["items"][0].body_text is None
    assert out["items"][0].body_html is None


def test_list_contacts_omits_labels_unless_requested():
    row = _ContactRow(id="wxid_a", name="Alice", alias="A", rating=88, labels={"tags": ["重点"]})
    db = _DummyDb(rows=[row])
    compact = contacts.list_contacts(db=db)
    assert compact[0].labels is None

    full = contacts.list_contacts(include_labels=True, db=_DummyDb(rows=[row]))
    assert full[0].labels == {"tags": ["重点"]}



def test_api_token_helpers_cover_core_api_and_exempt_paths(monkeypatch):
    monkeypatch.setattr("app.main.settings.API_TOKEN", "prod-token")
    assert _configured_api_tokens() == {"prod-token"}
    assert _is_api_auth_exempt_path("/api/health") is True
    assert _is_api_auth_exempt_path("/api/ready") is True
    assert _is_api_auth_exempt_path("/api/agent/invoke") is True
    assert _is_api_auth_exempt_path("/api/ai/config") is False

    bearer_request = Request({"type": "http", "headers": [(b"authorization", b"Bearer prod-token")]})
    assert _extract_api_token(bearer_request) == "prod-token"

    header_request = Request({"type": "http", "headers": [(b"x-api-token", b"prod-token")]})
    assert _extract_api_token(header_request) == "prod-token"



def test_cors_options_switch_between_dev_and_prod(monkeypatch):
    monkeypatch.setattr("app.main.settings.APP_ENV", "development")
    monkeypatch.setattr("app.main.settings.CORS_ALLOW_ORIGINS", None)
    dev = _cors_options()
    assert dev is not None
    assert dev["allow_origins"] == ["*"]

    monkeypatch.setattr("app.main.settings.APP_ENV", "production")
    monkeypatch.setattr("app.main.settings.CORS_ALLOW_ORIGINS", "https://a.example, https://b.example")
    prod = _cors_options()
    assert prod is not None
    assert prod["allow_origins"] == ["https://a.example", "https://b.example"]

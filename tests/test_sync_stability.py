import os
import sys
import json
import requests


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class _Row:
    def __init__(self, value):
        self.value = value


class _DB:
    def __init__(self, rows=None):
        self._rows = rows or {}

    def get(self, _model, key):
        v = self._rows.get(key)
        if v is None:
            return None
        return _Row(v)


def test_classify_sync_error_timeout_retryable():
    import app.services.sync_runtime as sync_runtime

    code, retryable = sync_runtime.classify_sync_error(requests.Timeout("timeout"))
    assert code == "SYNC-CHATLOG-TIMEOUT-001"
    assert retryable is True


def test_classify_sync_error_unreachable_retryable():
    import app.services.sync_runtime as sync_runtime

    code, retryable = sync_runtime.classify_sync_error(requests.ConnectionError("conn down"))
    assert code == "SYNC-CHATLOG-UNAVAILABLE-001"
    assert retryable is True


def test_run_with_retry_eventual_success():
    import app.services.sync_runtime as sync_runtime

    calls = {"n": 0}

    def _fn():
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.ConnectionError("temporary")
        return {"status": "ok"}

    result, attempts, err = sync_runtime.run_with_retry(_fn, max_attempts=3, sleep_seconds=0)
    assert err is None
    assert attempts == 3
    assert result == {"status": "ok"}


def test_build_sync_state_payload_with_last_run_json():
    import app.services.sync_runtime as sync_runtime

    run = {
        "run_id": "run-1",
        "status": "ok",
        "attempts": 1,
        "error_code": None,
        "fetched": 10,
        "inserted": 3,
        "duration_ms": 120,
    }
    db = _DB(
        {
            "chatlog_last_sync": "2026-03-07T11:30:00",
            "chatlog_sync_last_run": json.dumps(run, ensure_ascii=False),
        }
    )
    out = sync_runtime.build_sync_state_payload(db, _model_cls=object)
    assert out["last_sync"] == "2026-03-07T11:30:00"
    assert out["last_run"]["run_id"] == "run-1"
    assert out["last_run"]["fetched"] == 10


def test_build_sync_state_payload_handles_bad_json():
    import app.services.sync_runtime as sync_runtime

    db = _DB(
        {
            "chatlog_last_sync": "2026-03-07T11:30:00",
            "chatlog_sync_last_run": "{bad-json",
        }
    )
    out = sync_runtime.build_sync_state_payload(db, _model_cls=object)
    assert out["last_sync"] == "2026-03-07T11:30:00"
    assert out["last_run"] is None


def test_normalize_chatlog_sync_policy_defaults():
    import app.services.sync_runtime as sync_runtime

    p = sync_runtime.normalize_chatlog_sync_policy(None)
    assert p["max_attempts"] == 2
    assert p["sleep_seconds"] == 0.6


def test_normalize_chatlog_sync_policy_bounds():
    import app.services.sync_runtime as sync_runtime

    p = sync_runtime.normalize_chatlog_sync_policy({"max_attempts": 99, "sleep_seconds": -5})
    assert p["max_attempts"] == 5
    assert p["sleep_seconds"] == 0.0

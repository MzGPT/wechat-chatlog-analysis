from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, Callable

import requests


CHATLOG_LAST_SYNC_KEY = "chatlog_last_sync"
CHATLOG_LAST_RUN_KEY = "chatlog_sync_last_run"
CHATLOG_SYNC_POLICY_KEY = "chatlog_sync_policy"
DEFAULT_CHATLOG_SYNC_POLICY = {"max_attempts": 2, "sleep_seconds": 0.6}


def classify_sync_error(exc: Exception) -> tuple[str, bool]:
    """Map sync exceptions to stable error_code + retryable flag."""
    if isinstance(exc, (requests.Timeout, TimeoutError)):
        return "SYNC-CHATLOG-TIMEOUT-001", True
    if isinstance(exc, (requests.ConnectionError, ConnectionError, OSError)):
        return "SYNC-CHATLOG-UNAVAILABLE-001", True
    if isinstance(exc, requests.HTTPError):
        status = None
        try:
            status = int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
        except Exception:
            status = 0
        if status >= 500:
            return "SYNC-CHATLOG-UPSTREAM-5XX-001", True
        if status >= 400:
            return "SYNC-CHATLOG-UPSTREAM-4XX-001", False
    msg = str(exc).lower()
    if "timed out" in msg or "timeout" in msg:
        return "SYNC-CHATLOG-TIMEOUT-001", True
    if "remote end closed connection" in msg or "connection aborted" in msg or "connection reset" in msg:
        return "SYNC-CHATLOG-UNAVAILABLE-001", True
    return "SYNC-CHATLOG-UNKNOWN-001", False


def run_with_retry(
    fn: Callable[[], Any],
    *,
    max_attempts: int = 2,
    sleep_seconds: float = 0.6,
    on_error: Callable[[Exception], None] | None = None,
) -> tuple[Any | None, int, Exception | None]:
    """Run fn with error-code-aware retry strategy.

    Returns (result, attempts, last_error).
    """
    attempts = max(1, int(max_attempts or 1))
    last_error: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return fn(), i, None
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if on_error:
                try:
                    on_error(exc)
                except Exception:
                    pass
            _, retryable = classify_sync_error(exc)
            if i >= attempts or not retryable:
                break
            if sleep_seconds > 0:
                time.sleep(float(sleep_seconds))
    return None, attempts, last_error


def _get_row_value(db: Any, model_cls: Any, key: str) -> str | None:
    row = db.get(model_cls, key)
    return (row.value if row else None)


def _upsert_row_value(db: Any, model_cls: Any, key: str, value: str) -> None:
    row = db.get(model_cls, key)
    if not row:
        row = model_cls(key=key, value=value)
    else:
        row.value = value
        if hasattr(row, "updated_at"):
            row.updated_at = datetime.utcnow()
    db.add(row)


def persist_sync_run(db: Any, model_cls: Any, run_payload: dict[str, Any]) -> None:
    payload = json.dumps(run_payload or {}, ensure_ascii=False)
    _upsert_row_value(db, model_cls, CHATLOG_LAST_RUN_KEY, payload)


def _safe_json_dict(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def normalize_chatlog_sync_policy(raw: Any) -> dict[str, Any]:
    policy = dict(DEFAULT_CHATLOG_SYNC_POLICY)
    if not isinstance(raw, dict):
        return policy
    if "max_attempts" in raw:
        try:
            policy["max_attempts"] = max(1, min(5, int(raw.get("max_attempts") or 2)))
        except Exception:
            pass
    if "sleep_seconds" in raw:
        try:
            policy["sleep_seconds"] = max(0.0, min(3.0, float(raw.get("sleep_seconds") or 0.6)))
        except Exception:
            pass
    return policy


def get_chatlog_sync_policy(db: Any, *, model_cls: Any) -> dict[str, Any]:
    row = db.get(model_cls, CHATLOG_SYNC_POLICY_KEY)
    parsed = _safe_json_dict(row.value if row else None)
    return normalize_chatlog_sync_policy(parsed)


def save_chatlog_sync_policy(db: Any, *, model_cls: Any, payload: Any) -> dict[str, Any]:
    policy = normalize_chatlog_sync_policy(payload)
    _upsert_row_value(db, model_cls, CHATLOG_SYNC_POLICY_KEY, json.dumps(policy, ensure_ascii=False))
    return policy


def build_sync_state_payload(db: Any, *, _model_cls: Any) -> dict[str, Any]:
    last_sync = _get_row_value(db, _model_cls, CHATLOG_LAST_SYNC_KEY)
    last_run_raw = _get_row_value(db, _model_cls, CHATLOG_LAST_RUN_KEY)
    last_run = _safe_json_dict(last_run_raw)

    out: dict[str, Any] = {
        "last_sync": last_sync,
        "last_run": last_run,
    }

    # Best-effort lag estimate, compatible with existing callers.
    lag_seconds = None
    if isinstance(last_sync, str) and last_sync:
        try:
            dt = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            lag_seconds = max(0, int((datetime.now() - dt).total_seconds()))
        except Exception:
            lag_seconds = None
    out["lag_seconds"] = lag_seconds
    return out

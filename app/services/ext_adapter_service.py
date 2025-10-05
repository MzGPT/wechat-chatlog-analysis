from __future__ import annotations

"""External adapter ingestion service.

This module monitors a configured directory of adapter logs (e.g., produced by langbot
adapters) and imports messages into the DB. Each adapter can specify a subdirectory or
explicit log file path. We expect either:
- JSON Lines (*.jsonl) where each line is a JSON object
- Simple log lines with a leading JSON object {...}

Minimal expected JSON fields per line:
  { "id": str|int, "chat_id": str, "sender": str, "text": str, "timestamp": ISO8601|epoch, "direction": "in"|"out" }

Unknown shapes are skipped gracefully.
"""

import json
import os
from datetime import datetime
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ExtAdapter, AdapterMessage


def _to_dt(v) -> datetime | None:
    if not v:
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(v)
        except Exception:
            return None
    if isinstance(v, str):
        try:
            if v.endswith("Z"):
                v = v.replace("Z", "+00:00")
            return datetime.fromisoformat(v)
        except Exception:
            return None
    return None


def _parse_json_from_line(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    # best-effort: find first '{' and parse
    try:
        idx = line.find("{")
        if idx >= 0:
            return json.loads(line[idx:])
    except Exception:
        return None
    return None


def _iter_log_json(path: str) -> Iterable[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                obj = _parse_json_from_line(line)
                if obj:
                    yield obj
    except Exception:
        return []


def _iter_adapter_sources(base_dir: str, adapter_key: str) -> list[str]:
    sources: list[str] = []
    sub = os.path.join(base_dir, adapter_key)
    if os.path.isdir(sub):
        # prefer *.jsonl; fallback *.log
        for fn in sorted(os.listdir(sub)):
            if fn.endswith(".jsonl") or fn.endswith(".log"):
                sources.append(os.path.join(sub, fn))
    else:
        # maybe a single file named <key>.jsonl/log under base_dir
        for ext in (".jsonl", ".log"):
            p = os.path.join(base_dir, adapter_key + ext)
            if os.path.exists(p):
                sources.append(p)
    return sources


def ingest_adapter_logs(db: Session, adapter: ExtAdapter, base_dir: str) -> int:
    """Ingest messages from adapter logs. Returns number of new rows.

    Deduplication is by (adapter_key, external_id) when available, else content hash fallback.
    For simplicity, we use (adapter_key, external_id) only; if missing, we insert anyway and
    rely on the UI filter.
    """

    total_new = 0
    sources = _iter_adapter_sources(base_dir, adapter.key)
    if not sources:
        return 0

    existing_ids: set[str] = set(
        x[0]
        for x in db.execute(
            select(AdapterMessage.external_id).where(AdapterMessage.adapter_key == adapter.key)
        ).all()
        if x[0]
    )

    for path in sources:
        for obj in _iter_log_json(path):
            ext_id = str(obj.get("id")) if obj.get("id") is not None else None
            if ext_id and ext_id in existing_ids:
                continue
            msg = AdapterMessage(
                adapter_key=adapter.key,
                external_id=ext_id,
                chat_id=str(obj.get("chat_id") or ""),
                sender=str(obj.get("sender") or ""),
                timestamp=_to_dt(obj.get("timestamp")),
                direction=str(obj.get("direction") or "in"),
                content_text=str(obj.get("text") or obj.get("content") or ""),
                meta={k: v for k, v in obj.items() if k not in {"id", "chat_id", "sender", "timestamp", "direction", "text", "content"}},
            )
            db.add(msg)
            total_new += 1

    return total_new


from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import APIRouter


router = APIRouter(prefix="/api/langbot", tags=["langbot"])


def _default_langbot_db() -> Path | None:
    env_db = os.getenv("LANGBOT_DB", "").strip()
    if env_db:
        p = Path(env_db).expanduser().resolve()
        return p if p.exists() else None
    # Common local layout: ../LangBot/docker/data/langbot.db
    guess = (Path(os.getcwd()).resolve().parent / "LangBot" / "docker" / "data" / "langbot.db").resolve()
    return guess if guess.exists() else None


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    return con


@router.get("/bots")
def list_bots() -> dict[str, Any]:
    """List LangBot bots for importing send config (wechat08 adapter)."""
    dbp = _default_langbot_db()
    if not dbp:
        return {"items": [], "source": {"ok": False, "reason": "LANGBOT_DB not set and default not found"}}
    con = _connect(dbp)
    try:
        rows = con.execute(
            "SELECT uuid, name, description, adapter, adapter_config, enable, use_pipeline_name, use_pipeline_uuid, updated_at FROM bots ORDER BY updated_at DESC"
        ).fetchall()
        items: list[dict[str, Any]] = []
        for r in rows:
            cfg_raw = r["adapter_config"]
            cfg: dict[str, Any] = {}
            try:
                if isinstance(cfg_raw, str) and cfg_raw.strip():
                    cfg = json.loads(cfg_raw)
            except Exception:
                cfg = {}
            items.append(
                {
                    "uuid": r["uuid"],
                    "name": r["name"],
                    "description": r["description"],
                    "adapter": r["adapter"],
                    "enabled": bool(r["enable"] or 0),
                    "pipeline_name": r["use_pipeline_name"],
                    "pipeline_uuid": r["use_pipeline_uuid"],
                    # Only expose common wechat08 adapter config fields we can import.
                    "wechat08_api_base": cfg.get("wechat08_api_base") or "",
                    "wechat08_ws_base": cfg.get("wechat08_ws_base") or "",
                    "wxid": cfg.get("wxid") or "",
                }
            )
        return {"items": items, "source": {"ok": True, "db": str(dbp)}}
    finally:
        con.close()


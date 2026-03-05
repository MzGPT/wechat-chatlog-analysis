from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _default_we_mp_rss_db() -> Path | None:
    env_db = os.getenv("WE_MP_RSS_DB", "").strip()
    if env_db:
        p = Path(env_db).expanduser().resolve()
        return p if p.exists() else None
    env_dir = os.getenv("WE_MP_RSS_DIR", "").strip()
    if env_dir:
        p = (Path(env_dir).expanduser().resolve() / "data" / "db.db").resolve()
        return p if p.exists() else None
    guess = (Path(os.getcwd()).resolve().parent / "we-mp-rss" / "data" / "db.db").resolve()
    return guess if guess.exists() else None


def _iso_from_publish_time(v: Any) -> str | None:
    try:
        if v is None:
            return None
        if isinstance(v, str) and v.strip().isdigit():
            v = int(v.strip())
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(float(v), tz=timezone.utc).astimezone().replace(tzinfo=None).isoformat()
    except Exception:
        return None
    return None


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    return con


def list_mp_articles(*, limit: int = 100, offset: int = 0, q: str | None = None, db_path: str | None = None) -> dict:
    path = Path(db_path).expanduser().resolve() if db_path else _default_we_mp_rss_db()
    if not path:
        return {"items": [], "total": 0, "source": {"ok": False, "reason": "WE_MP_RSS_DB/WE_MP_RSS_DIR not set and default not found"}}
    if not path.exists():
        return {"items": [], "total": 0, "source": {"ok": False, "reason": "we-mp-rss db not found", "db": str(path)}}

    ql = (q or "").strip().lower()
    st = path.stat()
    con = _connect(path)
    try:
        where = "1=1"
        params: list[Any] = []
        if ql:
            where = "(lower(a.title) like ? OR lower(a.description) like ? OR lower(f.mp_name) like ?)"
            params.extend([f"%{ql}%", f"%{ql}%", f"%{ql}%"])
        sql = f"""
            SELECT
                a.id, a.mp_id, a.title, a.url, a.description, a.publish_time, a.created_at, a.updated_at, a.is_read,
                a.read_count, a.like_count, a.share_count, a.recommend_count,
                f.mp_name,
                i.summary AS insight_summary
            FROM articles a
            LEFT JOIN feeds f ON f.id = a.mp_id
            LEFT JOIN article_insights i ON i.article_id = a.id
            WHERE a.status != 0 AND {where}
            ORDER BY a.publish_time DESC
            LIMIT ? OFFSET ?
        """
        params.extend([int(limit), int(offset)])
        rows = con.execute(sql, params).fetchall()
        items: list[dict] = []
        for r in rows:
            title = (r["title"] or "").strip()
            summary = (r["insight_summary"] or r["description"] or "").strip()
            read_count = int(r["read_count"] or 0)
            like_count = int(r["like_count"] or 0)
            share_count = int(r["share_count"] or 0)
            recommend_count = int(r["recommend_count"] or 0)
            items.append(
                {
                    "id": r["id"],
                    "mp_id": r["mp_id"],
                    "channel_name": r["mp_name"] or r["mp_id"],
                    "title": title,
                    "url": r["url"] or "",
                    "summary": summary[:800],
                    "publish_time": _iso_from_publish_time(r["publish_time"]),
                    "is_read": bool(r["is_read"] or 0),
                    "read_count": read_count,
                    "like_count": like_count,
                    "share_count": share_count,
                    "recommend_count": recommend_count,
                }
            )
        return {
            "items": items,
            "total": len(items),
            "source": {"ok": True, "db": str(path), "mtime": int(st.st_mtime), "size": int(st.st_size)},
        }
    finally:
        con.close()


def get_mp_article(article_id: str, *, include_content: bool = False, db_path: str | None = None) -> dict:
    path = Path(db_path).expanduser().resolve() if db_path else _default_we_mp_rss_db()
    if not path:
        raise FileNotFoundError("we-mp-rss db not configured")
    con = _connect(path)
    try:
        row = con.execute(
            """
            SELECT
                a.id, a.mp_id, a.title, a.url, a.description, a.publish_time, a.created_at, a.updated_at, a.is_read, a.content,
                a.read_count, a.like_count, a.share_count, a.recommend_count,
                f.mp_name,
                i.summary AS insight_summary, i.key_points_json
            FROM articles a
            LEFT JOIN feeds f ON f.id = a.mp_id
            LEFT JOIN article_insights i ON i.article_id = a.id
            WHERE a.id = ?
            """,
            [article_id],
        ).fetchone()
        if not row:
            return {}
        item = {
            "id": row["id"],
            "mp_id": row["mp_id"],
            "channel_name": row["mp_name"] or row["mp_id"],
            "title": (row["title"] or "").strip(),
            "url": row["url"] or "",
            "publish_time": _iso_from_publish_time(row["publish_time"]),
            "summary": (row["insight_summary"] or row["description"] or "").strip(),
            "is_read": bool(row["is_read"] or 0),
            "read_count": int(row["read_count"] or 0),
            "like_count": int(row["like_count"] or 0),
            "share_count": int(row["share_count"] or 0),
            "recommend_count": int(row["recommend_count"] or 0),
        }
        if include_content:
            item["content"] = (row["content"] or "").strip()
            item["key_points_json"] = row["key_points_json"]
        return item
    finally:
        con.close()

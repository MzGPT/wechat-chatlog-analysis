from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import requests

from .llm_client import load_ai_config
from .wechatpad_client import WeChatPadClient
from ..config import settings


_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class LangBotGatewayClient:
    def __init__(
        self,
        *,
        base: str | None = None,
        bot_uuid: str | None = None,
        auth_token: str | None = None,
    ):
        conf: dict[str, Any] = {}
        if base is None or bot_uuid is None or auth_token is None:
            try:
                conf = load_ai_config()
            except Exception:
                conf = {}

        self.base = str(base or conf.get("langbot_gateway_base") or "").strip().rstrip("/")
        self.bot_uuid = str(bot_uuid or conf.get("langbot_gateway_bot_uuid") or "").strip()
        if auth_token is None:
            auth_token = conf.get("langbot_gateway_auth_token") or ""
        self.auth_token = str(auth_token or "").strip()

        try:
            self._session = requests.Session()
            self._session.trust_env = False
        except Exception:
            self._session = None

    def configured_base(self) -> bool:
        return bool(self.base)

    def configured(self) -> bool:
        # Base is optional: we can fallback to local LangBot DB (wechat08 adapter config).
        return bool(self.bot_uuid)

    @staticmethod
    def _default_langbot_db() -> Path | None:
        env_db = os.getenv("LANGBOT_DB", "").strip()
        if env_db:
            p = Path(env_db).expanduser().resolve()
            return p if p.exists() else None
        # Common local layout: ../LangBot/docker/data/langbot.db
        cwd = Path(os.getcwd()).resolve()
        candidates = [
            (cwd.parent / "LangBot" / "docker" / "data" / "langbot.db").resolve(),
            (cwd.parent / "LangBot" / "data" / "langbot.db").resolve(),
        ]
        for p in candidates:
            if p.exists():
                return p
        return None

    @staticmethod
    def _connect_sqlite(path: Path) -> sqlite3.Connection:
        con = sqlite3.connect(str(path))
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _strip_suffix_api(url: str) -> str:
        raw = (url or "").strip()
        if raw.endswith("/api"):
            return raw[:-4]
        return raw

    def _resolve_bot_from_langbot_db(self, identifier: str) -> dict[str, Any]:
        dbp = self._default_langbot_db()
        if not dbp:
            raise RuntimeError("LangBot DB not found (set LANGBOT_DB or place under ../LangBot/docker/data/langbot.db)")
        con = self._connect_sqlite(dbp)
        try:
            row = con.execute(
                "SELECT uuid, name, adapter, adapter_config FROM bots WHERE uuid = ? OR name = ? ORDER BY updated_at DESC LIMIT 1",
                (identifier, identifier),
            ).fetchone()
            if not row:
                raise RuntimeError(f"LangBot bot not found: {identifier}")
            cfg_raw = row["adapter_config"]
            cfg: dict[str, Any] = {}
            try:
                if isinstance(cfg_raw, str) and cfg_raw.strip():
                    cfg = json.loads(cfg_raw)
            except Exception:
                cfg = {}
            adapter = str(row["adapter"] or "").strip()
            return {
                "db": str(dbp),
                "uuid": str(row["uuid"] or "").strip(),
                "name": str(row["name"] or "").strip(),
                "adapter": adapter,
                "wechat08_api_base": str(cfg.get("wechat08_api_base") or "").strip(),
                "wechat08_ws_base": str(cfg.get("wechat08_ws_base") or "").strip(),
                "wxid": str(cfg.get("wxid") or "").strip(),
            }
        finally:
            con.close()

    def _candidate_wechat_text_paths(self, api_base: str) -> list[str]:
        try:
            conf = load_ai_config()
        except Exception:
            conf = {}
        configured = str(conf.get("wechatpad_text_path") or settings.WECHATPAD_TEXT_PATH or "/api/Msg/SendTxt").strip()
        defaults = [
            configured,
            "/api/Msg/SendTxt",
            "/api/msg/sendtxt",
            "/api/v1/message/sendText",
        ]
        parsed_path = ""
        try:
            from urllib.parse import urlparse

            parsed = urlparse(api_base)
            parsed_path = parsed.path or ""
        except Exception:
            parsed_path = ""

        out: list[str] = []
        for p in defaults:
            if not p:
                continue
            p = p if p.startswith("/") else ("/" + p)
            out.append(p)
            # If base already ends with "/api", avoid duplicating "/api" in the path.
            if parsed_path.rstrip("/") == "/api" and p.startswith("/api/"):
                out.append(p[len("/api") :])
        # Dedup while preserving order
        uniq: list[str] = []
        seen: set[str] = set()
        for p in out:
            if p in seen:
                continue
            seen.add(p)
            uniq.append(p)
        return uniq

    def _headers(self) -> dict[str, str]:
        if not self.auth_token:
            return {}
        return {"Authorization": f"Bearer {self.auth_token}"}

    @staticmethod
    def _looks_like_uuid(value: str) -> bool:
        return bool(_UUID_RE.match((value or "").strip()))

    def canonicalize_bot_uuid(self, identifier: str) -> str:
        """Best-effort convert a user-provided identifier (uuid or name) into a bot uuid."""
        ident = str(identifier or "").strip()
        if not ident:
            return ""
        if self._looks_like_uuid(ident):
            return ident
        # Prefer local LangBot DB mapping (uuid OR name).
        try:
            bot = self._resolve_bot_from_langbot_db(ident)
            uuid = str(bot.get("uuid") or "").strip()
            if uuid:
                return uuid
        except Exception:
            pass
        # Fallback: query gateway bot list (uuid OR name).
        if self.configured_base():
            try:
                url = f"{self.base}/v1/bots"
                if self._session is not None:
                    r = self._session.get(url, headers=self._headers(), timeout=10)
                else:
                    r = requests.get(url, headers=self._headers(), timeout=10, proxies={"http": None, "https": None})
                if r.ok:
                    data = r.json()
                    bots = data.get("bots") if isinstance(data, dict) else None
                    if isinstance(bots, list):
                        for b in bots:
                            if not isinstance(b, dict):
                                continue
                            uuid = str(b.get("uuid") or "").strip()
                            name = str(b.get("name") or "").strip()
                            if ident and (ident == uuid or ident == name):
                                return uuid or ident
            except Exception:
                pass
        return ident

    def health(self) -> Dict[str, Any]:
        http_err: Exception | None = None
        if self.configured_base():
            url = f"{self.base}/v1/health"
            try:
                if self._session is not None:
                    r = self._session.get(url, headers=self._headers(), timeout=5)
                else:
                    r = requests.get(url, headers=self._headers(), timeout=5, proxies={"http": None, "https": None})
                if not r.ok:
                    raise RuntimeError(f"gateway http {r.status_code}: {r.text[:200]}")
                try:
                    return r.json()
                except Exception:
                    return {"status": "ok", "raw": r.text}
            except Exception as exc:
                http_err = exc

        # Fallback: treat "gateway" as local LangBot bot resolver (DB), not an HTTP service.
        if not self.bot_uuid:
            raise RuntimeError(f"gateway unavailable: {http_err}" if http_err else "LangBot bot_uuid not configured")
        bot = self._resolve_bot_from_langbot_db(self.bot_uuid)
        return {
            "status": "ok",
            "mode": "langbot_db",
            "gateway_http_error": str(http_err) if http_err else "",
            "bot": {k: bot.get(k) for k in ("uuid", "name", "adapter", "wxid", "wechat08_api_base")},
            "db": bot.get("db"),
        }

    def list_bots(self) -> Dict[str, Any]:
        if self.configured_base():
            url = f"{self.base}/v1/bots"
            try:
                if self._session is not None:
                    r = self._session.get(url, headers=self._headers(), timeout=10)
                else:
                    r = requests.get(url, headers=self._headers(), timeout=10, proxies={"http": None, "https": None})
            except Exception as exc:
                raise RuntimeError(f"gateway request failed: {exc}") from exc
            if not r.ok:
                raise RuntimeError(f"gateway http {r.status_code}: {r.text[:200]}")
            try:
                return r.json()
            except Exception:
                return {"status": "ok", "raw": r.text}

        # Local DB fallback: expose minimal bot list.
        dbp = self._default_langbot_db()
        if not dbp:
            raise RuntimeError("LangBot gateway base not configured and LangBot DB not found")
        con = self._connect_sqlite(dbp)
        try:
            rows = con.execute("SELECT uuid, name, adapter, enable, updated_at FROM bots ORDER BY updated_at DESC").fetchall()
            items: list[dict[str, Any]] = []
            for r in rows:
                items.append(
                    {
                        "uuid": r["uuid"],
                        "name": r["name"],
                        "adapter": r["adapter"],
                        "enabled": bool(r["enable"] or 0),
                        "updated_at": r["updated_at"],
                    }
                )
            return {"status": "ok", "mode": "langbot_db", "items": items, "db": str(dbp)}
        finally:
            con.close()

    def send_text(
        self,
        *,
        target_id: str,
        text: str,
        bot_uuid: str | None = None,
        timeout_ms: int = 15_000,
    ) -> Dict[str, Any]:
        identifier = str(bot_uuid or self.bot_uuid or "").strip()
        if not identifier:
            raise RuntimeError("LangBot gateway bot_uuid not configured")
        resolved_bot_uuid = self.canonicalize_bot_uuid(identifier) if self.configured_base() else identifier
        target_id = str(target_id or "").strip()
        if not target_id:
            raise RuntimeError("target_id required")
        text = str(text or "").strip()
        if not text:
            raise RuntimeError("text required")
        target_type = "group" if target_id.endswith("@chatroom") else "person"
        http_err: Exception | None = None
        if self.configured_base():
            url = f"{self.base}/v1/send"
            payload = {
                "bot_uuid": resolved_bot_uuid,
                "target_type": target_type,
                "target_id": target_id,
                "text": text,
                "timeout_ms": int(timeout_ms),
            }
            timeout_s = max(3, int(timeout_ms / 1000) + 3)
            try:
                if self._session is not None:
                    r = self._session.post(url, json=payload, headers=self._headers(), timeout=timeout_s)
                else:
                    r = requests.post(
                        url, json=payload, headers=self._headers(), timeout=timeout_s, proxies={"http": None, "https": None}
                    )
                if not r.ok:
                    raise RuntimeError(f"gateway http {r.status_code}: {r.text[:200]}")
                try:
                    return r.json()
                except Exception:
                    return {"status": "ok", "raw": r.text}
            except Exception as exc:
                http_err = exc

        # Fallback: resolve bot from LangBot DB and send via its wechat08 API base.
        try:
            bot = self._resolve_bot_from_langbot_db(resolved_bot_uuid)
            adapter = str(bot.get("adapter") or "").strip()
            if adapter and adapter != "wechat08":
                raise RuntimeError(f"unsupported langbot adapter: {adapter}")
            api_base = str(bot.get("wechat08_api_base") or "").strip()
            wxid = str(bot.get("wxid") or "").strip()
            if not api_base:
                raise RuntimeError("wechat08_api_base not configured for this LangBot bot")

            last_exc: Exception | None = None
            for p in self._candidate_wechat_text_paths(api_base):
                try:
                    client = WeChatPadClient(base=api_base, text_path=p, wxid=wxid)
                    resp = client.send_text(target_id, text)
                    return {
                        "status": "ok",
                        "mode": "langbot_db_wechat08",
                        "bot": {"uuid": bot.get("uuid") or "", "name": bot.get("name") or "", "wxid": wxid},
                        "target_type": target_type,
                        "target_id": target_id,
                        "path": p,
                        "resp": resp,
                        "gateway_http_error": str(http_err) if http_err else "",
                    }
                except Exception as exc:
                    # If the endpoint replied with a structured failure (e.g., Code=-13 用户可能退出),
                    # that's the "real" error; don't keep probing other paths and masking it.
                    msg = str(exc)
                    if "Code=" in msg or "用户" in msg or "send failed" in msg:
                        raise RuntimeError(f"wechat08 send failed: {exc}") from exc
                    last_exc = exc
            raise RuntimeError(f"wechat08 send failed: {last_exc}")
        except Exception as exc:
            if http_err is not None:
                raise RuntimeError(f"gateway request failed: {http_err}; fallback failed: {exc}") from exc
            raise

    def send_batch(self, items: List[Dict[str, str]]) -> Dict[str, Any]:
        results: List[Dict[str, Any]] = []
        for it in items:
            target = it.get("target") or it.get("talker") or it.get("chat_id")
            text = it.get("text") or it.get("aiReply") or it.get("ai_reply")
            if not target or not text:
                results.append({"ok": False, "error": "missing target/text", "item": it})
                continue
            try:
                resp = self.send_text(target_id=target, text=text)
                results.append({"ok": True, "resp": resp})
            except Exception as exc:
                results.append({"ok": False, "error": str(exc)})
        return {"status": "ok", "results": results}

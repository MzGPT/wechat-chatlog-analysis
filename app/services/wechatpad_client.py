from __future__ import annotations

import requests
from typing import Dict, Any, List
from ..config import settings
from .llm_client import load_ai_config


class WeChatPadClient:
    def __init__(self, base: str | None = None, text_path: str | None = None):
        # Prefer explicit args; then dynamic ai_config; finally .env settings
        if base is None or text_path is None:
            try:
                conf = load_ai_config()
            except Exception:
                conf = {}
        else:
            conf = {}
        resolved_base = base or conf.get("wechatpad_http_base") or settings.WECHATPAD_HTTP_BASE or ""
        resolved_path = text_path or conf.get("wechatpad_text_path") or settings.WECHATPAD_TEXT_PATH or "/api/v1/message/sendText"
        self.base = str(resolved_base).rstrip("/")
        self.text_path = resolved_path
        # Disable environment proxies to avoid local proxy interfering with LAN endpoints
        try:
            self._session = requests.Session()
            self._session.trust_env = False
        except Exception:
            self._session = None

    def configured(self) -> bool:
        return bool(self.base)

    def send_text(self, to_user: str, text: str) -> Dict[str, Any]:
        if not self.configured():
            raise RuntimeError("WeChatPadPro base not configured")
        url = f"{self.base}{self.text_path}"
        # Try common payload shapes used by WeChatPad variants.
        is_room = to_user.endswith("@chatroom")
        payloads = [
            {"toUserName": to_user, "content": text},
            {"toUserName": to_user, "text": text},
            {"to": to_user, "content": text},
            {"wxid": to_user, "content": text},
        ]
        if is_room:
            payloads = [
                {"toUserName": to_user, "content": text, "isRoom": True},
                {"room": to_user, "content": text},
            ] + payloads
        last_err: Exception | None = None
        for data in payloads:
            try:
                if self._session is not None:
                    r = self._session.post(url, json=data, timeout=8)
                else:
                    r = requests.post(url, json=data, timeout=8, proxies={"http": None, "https": None})
                if r.ok:
                    # Consider JSON success conventions
                    try:
                        data = r.json()
                        ok = False
                        if isinstance(data, dict):
                            if data.get("ok") is True or data.get("success") is True:
                                ok = True
                            elif str(data.get("code")) in {"0", "200"}:
                                ok = True
                        return {"status": "ok" if ok else "unknown", "data": data}
                    except Exception:
                        return {"status": "ok", "raw": r.text}
            except Exception as e:
                last_err = e
        if last_err:
            raise last_err
        raise RuntimeError("failed to send text via WeChatPadPro")

    def send_batch(self, items: List[Dict[str, str]]) -> Dict[str, Any]:
        results: List[Dict[str, Any]] = []
        for it in items:
            target = it.get("target") or it.get("talker") or it.get("chat_id")
            text = it.get("text") or it.get("aiReply") or it.get("ai_reply")
            if not target or not text:
                results.append({"ok": False, "error": "missing target/text", "item": it})
                continue
            try:
                resp = self.send_text(target, text)
                results.append({"ok": True, "resp": resp})
            except Exception as e:
                results.append({"ok": False, "error": str(e)})
        return {"status": "ok", "results": results}

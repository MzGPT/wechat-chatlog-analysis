from __future__ import annotations

import requests
from typing import Dict, Any, List
from ..config import settings


class WeChatPadClient:
    def __init__(self, base: str | None = None, text_path: str | None = None):
        self.base = (base or settings.WECHATPAD_HTTP_BASE or "").rstrip("/")
        self.text_path = text_path or settings.WECHATPAD_TEXT_PATH or "/api/v1/message/sendText"

    def configured(self) -> bool:
        return bool(self.base)

    def send_text(self, to_user: str, text: str) -> Dict[str, Any]:
        if not self.configured():
            raise RuntimeError("WeChatPadPro base not configured")
        url = f"{self.base}{self.text_path}"
        # Try common payload shape used by multiple gateways
        payloads = [
            {"toUserName": to_user, "content": text},
            {"toUserName": to_user, "text": text},
            {"to": to_user, "content": text},
        ]
        last_err: Exception | None = None
        for data in payloads:
            try:
                r = requests.post(url, json=data, timeout=8)
                if r.ok:
                    try:
                        return r.json()
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


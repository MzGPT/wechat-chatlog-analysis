import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.routers import ai


def test_get_ai_config_masks_quan2go_provider_key(monkeypatch):
    monkeypatch.setattr(
        ai,
        "load_ai_config",
        lambda: {
            "api_url": "https://api.siliconflow.cn/v1",
            "api_key": "base-key",
            "model": "gpt-5.4",
            "tool_model": "THUDM/GLM-4-9B-0414",
            "tool_model_messages": "Qwen/Qwen3-8B",
            "tool_model_emails": "THUDM/GLM-4-9B-0414",
            "message_filters": {},
            "module_prompts": {},
            "tool_prompts": {},
            "derive_defaults": {},
            "model_router": {
                "enabled": True,
                "main_channels": [
                    {
                        "id": "main-quan2go-gpt55",
                        "name": "候选 Quan2Go GPT-5.5",
                        "model": "gpt-5.5",
                        "enabled": False,
                        "weight": 1,
                        "api_url": "https://capi.quan2go.com/openai",
                        "api_key": "secret-key",
                        "max_inflight": 4,
                    }
                ],
                "mid_channels": [],
                "tool_channels": [],
                "main_module_channels": {"default": ["main-quan2go-gpt55"]},
                "mid_route_channels": {},
                "tool_route_channels": {},
            },
        },
    )

    payload = ai.get_ai_config()
    main_channel = payload["model_router"]["main_channels"][0]
    assert main_channel["id"] == "main-quan2go-gpt55"
    assert main_channel["model"] == "gpt-5.5"
    assert main_channel["api_url"] == "https://capi.quan2go.com/openai"
    assert main_channel["api_key"] == ""
    assert main_channel["has_api_key"] is True

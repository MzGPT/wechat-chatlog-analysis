from __future__ import annotations

import os
import json
import requests
from typing import Optional, Dict, Any
from ..config import settings


DEFAULT_MODULE_PROMPTS: Dict[str, Dict[str, str]] = {
    "market": {
        "system": "\n".join([
            "你是一名资深投研首席，需要通读全部聊天记录后形成结构化的市场观点报告。",
            "需严格按照六大类进行归纳：宏观政策、行业板块、公司基本面、投资策略、市场情绪、其他观点。",
            "禁止逐条复述或拼接原始消息，只能在必要时引用不超过30字的短句，并注明来源联系人。",
            "当信息不足时可以省略该小类或标注‘信息有限’。",
        ]),
        "user": "\n".join([
            "请阅读 JSON 数据并输出 JSON 对象 {\"markdown\": string}：",
            "- markdown 顶部使用 `# 市场观点总结` 概述市场基调（2-3句话）。",
            "- 依次使用 `## 宏观政策`、`## 行业板块`、`## 公司基本面`、`## 投资策略`、`## 市场情绪`、`## 其他观点`，无信息的分类写 `- 信息有限`。",
            "- 每个分类下用无序列表列出倾向、关键依据（含联系人与评分）及风险提示。",
            "- 末尾增加 `## 行动建议` 与 `## 关注事项` 两段，给出策略与监控要点。",
            "- 忽略低信息术语（如“流通股本”“所属行业”等）。",
            "- 引用来源：当需要引用具体消息时，引用短句（<=20字），并在条目末尾标注 `#<id>`（消息 id）。",
            "数据：{{messages_data}}",
        ]),
    },
    "meetings": {
        "system": "\n".join([
            "你是一名会议情报分析师，要从大量聊天记录中抽取可靠的会议/路演安排 (真实、可核查)。",
            "请整合不同来源的信息，识别时间、平台/形式、会议号、讲者/机构与要点，必要时标注待确认的细节。",
            "只保留事实支撑的信息，禁止虚构与臆测。",
        ]),
        "user": "\n".join([
            "请阅读 JSON 数据并输出 JSON 对象 {\"markdown\": string}：",
            "- markdown 顶部使用 `# 会议路演信息`。",
            "- 使用 `## 概览` 段总结会议数量、行业焦点与信息缺口。",
            "- 使用 Markdown 表格（按时间倒序）：`| 时间(月-日 时:分) | 形式(简称) | 会议号 | 主讲人/机构 | 要点 |`。",
            "  - 平台简称示例：腾讯=腾，进门财经=进，飞书=飞，Zoom=ZM，Teams=TM，钉钉=钉，电话=电。",
            "  - 主题列直接取消息的 summary（去掉前缀），截断至 10 个字；保持整行两行以内的精要描述。",
            "- 以 `## 待处理事项` 列出需跟进的动作。",
            "- 引用来源：当需要引用具体消息时，引用短句（<=20字），并在条目末尾标注 `#<id>`（消息 id）。",
            "数据：{{messages_data}}",
        ]),
    },
    "counter": {
        "system": "\n".join([
            "你是一名投研风控专家，职责是审视聊天记录中的主流观点并产出严格基于事实的冲突分析。",
            "仅纳入确有证据支持的对立观点或事实，不得臆造。",
            "按议题输出结构化要点，格式保持稳定，便于复制与归档。",
        ]),
        "user": "\n".join([
            "请基于全部消息输出 JSON 对象 {\"markdown\": string}：",
            "- markdown 顶部使用 `# 反驳观点分析`，概括整体争议。",
            "- 对每个议题使用 `## 议题：主题`。",
            "  - 观点：归纳主流观点（一句话）。",
            "  - 证据：引用可核查的事实/数据/消息来源（可多条）。",
            "  - 反驳要点：指出对立证据或逻辑漏洞（可多条）。",
            "  - 结论/建议：给出行动建议或继续核查方向（1-2条）。",
            "- 仅纳入证据充分的真实冲突；不要生造争议。",
            "- 文末使用 `## 总结` 简述整体分歧与下一步。\n- 注意：保持条目清晰，避免表格，严格按上述层级输出。",
            "- 引用来源：当需要引用具体消息时，引用短句（<=20字），并在条目末尾标注 `#<id>`（消息 id）。",
        ]),
    },
    "contacts": {
        "system": "\n".join([
            "你是一名客户关系助理，需要梳理高评分联系人在聊天记录中的核心观点。",
            "请结合联系人评分、活跃度和消息摘要，提炼每位联系人的重点信息。",
            "禁止复制整条消息，只能引用必要短句，并标注时间或上下文。",
        ]),
        "user": "\n".join([
            "请通读全部消息并输出 JSON 对象 {\"markdown\": string}：",
            "- markdown 顶部使用 `# 高评分联系人摘要`，说明筛选标准。",
            "- 对每位联系人使用如下模版：",
            "  `### 姓名（评分 x.x / 活跃 y）`",
            "  `- 核心观点：一句话概括，必要时引用短句（注明上下文/时间）。`",
            "  `- 最新动态：列出其最近要点（2-4条）。`",
            "  `- 跟进建议：给出可执行的跟进动作（1-2条）。`",
            "- 可另加 `## 关注联系人` 段落列出潜力对象（同上模版）。",
            "- 可增加 `## 关注联系人` 段落列出潜力对象。",
            "- 引用来源：当需要引用具体消息时，引用短句（<=20字），并在条目末尾标注 `#<id>`（消息 id）。",
        ]),
    },
}

DEFAULT_TOOL_PROMPTS: Dict[str, Dict[str, str]] = {
    "message_summary": {
        "system": (
            "你是专业的投研信息提取助手。你的任务是：\n"
            "1. 仔细阅读每封邮件/消息的完整内容\n"
            "2. 理解其核心意图（路演邀请？观点分享？会议通知？）\n"
            "3. 提取关键事实（会议号/观点/建议），不要编造\n"
            "4. 用一句话概括最重要的信息（不超过30字）\n\n"
            "注意：\n"
            "- 必须通读完整正文，不要只看标题\n"
            "- 摘要要提炼实质内容，不要复读标题或拼凑关键词\n"
            "- 如果内容确实信息量很少，诚实标注'信息有限'"
        ),
        "user": (
            "请逐条分析以下消息（JSON格式：id/time/sender/content），返回JSON数组，每个元素结构：\n"
            "{\n"
            "  \"id\": string,                    // 必填：消息ID\n"
            "  \"summary\": string,               // 必填：<=30字自然语句，必须以'ai: '开头，概括核心内容\n"
            "  \"meeting_number\": string,        // 选填：9-13位纯数字会议号，无则留空\n"
            "  \"tone\": string,                  // 必填：bullish(看多)/bearish(看空)/neutral(中性)/meeting(会议)\n"
            "  \"confidence\": float              // 必填：0.0-1.0，你对提取准确性的信心\n"
            "}\n\n"
            "说明：\n"
            "- summary: 用自然语句概括，不要列举关键词。例如：'ai: XX证券邀约芯片板块路演，会议号123456789'\n"
            "- meeting_number: 只提取纯数字（9-13位），例如'123456789'或'123-456-789'统一写成'123456789'\n"
            "- tone: 会议邀请选'meeting'；观点看多选'bullish'；看空选'bearish'；中性或不明确选'neutral'\n"
            "- confidence: 内容完整且确定时>=0.8；信息不足或模糊时<=0.5\n"
            "- 如果内容太少无法提取有意义摘要，summary写'ai: 信息有限'，confidence设为0.3\n\n"
            "数据：{{messages_json}}"
        ),
    }
}


def load_ai_config() -> Dict[str, Any]:
    path = os.path.abspath(os.path.join(os.getcwd(), "data", "ai_config.json"))
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                conf = json.load(f)
        except Exception:
            conf = {}
    else:
        conf = {}

    if not conf:
        conf = {
            "api_key": settings.SILICONFLOW_API_KEY or "",
            "api_url": settings.SILICONFLOW_API_URL or "https://api.siliconflow.cn/v1",
            "model": settings.SILICONFLOW_MODEL or "Qwen/Qwen3-30B-A3B",
            "tool_model": settings.SILICONFLOW_TOOL_MODEL or "Qwen/Qwen3-8B",
            "max_tokens": 4000,
            "model_temperature": 0.7,
            "message_filters": {"external_only": True, "exclude_short": True, "exclude_system": True},
            "derive_defaults": {"batch_size": 20, "concurrency": 8, "temperature": 0.1, "force": False},
        }
    else:
        conf.setdefault("api_key", settings.SILICONFLOW_API_KEY or conf.get("api_key", ""))
        conf.setdefault("api_url", settings.SILICONFLOW_API_URL or conf.get("api_url", "https://api.siliconflow.cn/v1"))
        conf.setdefault("model", settings.SILICONFLOW_MODEL or conf.get("model", "Qwen/Qwen3-30B-A3B"))
        conf.setdefault("tool_model", settings.SILICONFLOW_TOOL_MODEL or conf.get("tool_model", "Qwen/Qwen3-8B"))
        conf.setdefault("max_tokens", conf.get("max_tokens", 4000))
        conf.setdefault("model_temperature", conf.get("model_temperature", 0.7))
        conf.setdefault("message_filters", conf.get("message_filters", {"external_only": True, "exclude_short": True, "exclude_system": True}))
        conf.setdefault("derive_defaults", conf.get("derive_defaults", {"batch_size": 20, "concurrency": 8, "temperature": 0.1, "force": False}))

    stored = conf.get("module_prompts") or {}
    merged_prompts: Dict[str, Dict[str, str]] = {}
    for key, defaults in DEFAULT_MODULE_PROMPTS.items():
        saved = stored.get(key) or {}
        merged_prompts[key] = {
            "system": saved.get("system") or defaults["system"],
            "user": saved.get("user") or defaults["user"],
        }
    conf["module_prompts"] = merged_prompts

    stored_tool = conf.get("tool_prompts") or {}
    merged_tool_prompts: Dict[str, Dict[str, str]] = {}
    for key, defaults in DEFAULT_TOOL_PROMPTS.items():
        saved = stored_tool.get(key) or {}
        merged_tool_prompts[key] = {
            "system": saved.get("system") or defaults["system"],
            "user": saved.get("user") or defaults["user"],
        }
    conf["tool_prompts"] = merged_tool_prompts
    return conf


def save_ai_config(conf: Dict[str, Any]) -> None:
    path_dir = os.path.abspath(os.path.join(os.getcwd(), "data"))
    os.makedirs(path_dir, exist_ok=True)
    path = os.path.join(path_dir, "ai_config.json")
    normalized = conf.copy()
    # clamp numeric config
    try:
        if "max_tokens" in normalized:
            normalized["max_tokens"] = max(256, int(normalized.get("max_tokens") or 4000))
    except Exception:
        normalized["max_tokens"] = 4000
    try:
        if "model_temperature" in normalized:
            t = float(normalized.get("model_temperature") or 0.7)
            normalized["model_temperature"] = 0.0 if t < 0 else (1.0 if t > 1 else t)
    except Exception:
        normalized["model_temperature"] = 0.7
    # clamp derive defaults
    dd = normalized.get("derive_defaults") or {}
    try:
        bs = int(dd.get("batch_size", 20))
        dd["batch_size"] = max(1, min(128, bs))
    except Exception:
        dd["batch_size"] = 20
    try:
        cc = int(dd.get("concurrency", 8))
        dd["concurrency"] = max(1, min(64, cc))
    except Exception:
        dd["concurrency"] = 8
    try:
        tp = float(dd.get("temperature", 0.1))
        dd["temperature"] = 0.0 if tp < 0 else (1.0 if tp > 1 else tp)
    except Exception:
        dd["temperature"] = 0.1
    dd["force"] = bool(dd.get("force", False))
    normalized["derive_defaults"] = dd
    # ensure module prompts always contain defaults when missing
    stored = normalized.get("module_prompts") or {}
    merged_prompts: Dict[str, Dict[str, str]] = {}
    for key, defaults in DEFAULT_MODULE_PROMPTS.items():
        saved = stored.get(key) or {}
        merged_prompts[key] = {
            "system": saved.get("system") or defaults["system"],
            "user": saved.get("user") or defaults["user"],
        }
    normalized["module_prompts"] = merged_prompts

    stored_tool = normalized.get("tool_prompts") or {}
    merged_tool_prompts: Dict[str, Dict[str, str]] = {}
    for key, defaults in DEFAULT_TOOL_PROMPTS.items():
        saved = stored_tool.get(key) or {}
        merged_tool_prompts[key] = {
            "system": saved.get("system") or defaults["system"],
            "user": saved.get("user") or defaults["user"],
        }
    normalized["tool_prompts"] = merged_tool_prompts
    with open(path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)


def siliconflow_chat(messages: list[dict], temperature: float | None = 0.3, model_override: str | None = None) -> str:
    """Call SiliconFlow once; do NOT fallback to other models.
    If it fails, caller should handle local fallback.
    """
    conf = load_ai_config()
    api_key = conf.get("api_key")
    api_url = conf.get("api_url", "https://api.siliconflow.cn/v1")
    model = model_override or conf.get("model", "Qwen/Qwen3-30B-A3B")
    if not api_key:
        raise RuntimeError("SILICONFLOW_API_KEY not configured")
    url = api_url.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    # resolve runtime params from config
    max_tokens = int(conf.get("max_tokens") or 4000)
    temp = float(temperature if temperature is not None else conf.get("model_temperature") or 0.7)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temp,
        "max_tokens": max_tokens,
        "stream": False,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=180)
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:  # type: ignore[name-defined]
        detail = resp.text.strip()
        message = f"LLM request failed: {exc}"
        if detail:
            message += f" | detail: {detail[:500]}"
        raise RuntimeError(message) from exc
    data = resp.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    return content or ""


def siliconflow_tool_chat(messages: list[dict], temperature: float = 0.2) -> str:
    conf = load_ai_config()
    tool_model = conf.get("tool_model") or "Qwen/Qwen3-8B"
    return siliconflow_chat(messages, temperature=temperature, model_override=tool_model)


def siliconflow_chat_stream(messages: list, temperature: float = 0.7, model_override: str = None) -> str:
    """硅基流动流式对话接口"""
    conf = load_ai_config()
    api_key = conf.get("siliconflow_api_key")
    if not api_key:
        raise ValueError("SiliconFlow API key not configured")
    
    model = model_override or conf.get("model") or "Qwen/Qwen3-8B"
    
    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    data = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "stream": True
    }
    
    try:
        response = requests.post(url, headers=headers, json=data, stream=True)
        response.raise_for_status()
        
        for line in response.iter_lines():
            if line:
                line = line.decode('utf-8')
                if line.startswith('data: '):
                    data_str = line[6:]  # 去掉 'data: ' 前缀
                    if data_str.strip() == '[DONE]':
                        break
                    try:
                        data_obj = json.loads(data_str)
                        if 'choices' in data_obj and len(data_obj['choices']) > 0:
                            delta = data_obj['choices'][0].get('delta', {})
                            if 'content' in delta:
                                yield delta['content']
                    except json.JSONDecodeError:
                        continue
    except requests.RequestException as e:
        raise RuntimeError(f"SiliconFlow API request failed: {e}")

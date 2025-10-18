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
            "  - 主题列直接取消息的 key_info/summary，去掉前缀后截断至 10 个字；保持整行两行以内的精要描述。",
            "- 以 `## 待处理事项` 列出需跟进的动作。",
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
            "数据：{{messages_data}}",
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
            "数据：{{messages_data}}",
        ]),
    },
}

DEFAULT_TOOL_PROMPTS: Dict[str, Dict[str, str]] = {
    "message_summary": {
        "system": (
            "你是一名专业的投研小模型助手，擅长从邮件/消息中提取用于快速浏览的关键信息。"
            "你的任务是：对每条输入提取‘关键信息’（主要观点、分析师/研究员、会议链接、会议号、预约时间），"
            "并给出类别与情绪。输出必须严格遵循指定 JSON 结构。"
        ),
        "user": (
            "请处理以下 JSON 数组，每个元素代表一条消息，字段包含 id/time/sender/content。"
            "输出 JSON 数组，元素结构如下：\n"
            "{\n"
            "  \"id\": string,\n"
            "  \"summary\": string (<=30字，且必须以 'ai: ' 前缀开头，用于简要提示),\n"
            "  \"keywords\": [string<=5],\n"
            "  \"category\": string (观点/会议/提问/其他),\n"
            "  \"tone\": string (bullish/neutral/bearish),\n"
            "  \"platform\": string,\n"
            "  \"meeting_number\": string,\n"
            "  \"meeting_link\": string,\n"
            "  \"appointment_time\": string,\n"
            "  \"analyst\": string,\n"
            "  \"organizer\": string,\n"
            "  \"main_point\": string,\n"
            "  \"key_info\": string (<=120字，整合 main_point/analyst/meeting_link/meeting_number/appointment_time 为便于浏览的一行摘要)\n"
            "}\n"
            "规则与示例：\n"
            "- 常见邮件格式示例：‘主题: 【国金医药】特应性皮炎线上路演\n路演类型: 行业路演\n路演方式: 线上\n观点: ……\n内部预约人: 魏家裕\n券商研究员: 唐玉青\n会议链接: https://meeting.tencent.com/dm/xxxx\n会议号: 487-364-622\n时间: 2025-10-16 15:00’；从中提取 main_point/analyst/organizer/meeting_link/meeting_number/appointment_time。\n"
            "- category 仅选 观点/会议/提问/其他；tone 仅选 bullish/neutral/bearish。\n"
            "- summary 必须以 'ai: ' 开头；key_info 请压缩为便于快速浏览的一行，字段缺失可省略。\n"
            "- meeting_number 支持 3*3 形式(***-***-***) 或连续8~12位数字；meeting_link 提取 http/https 链接。\n"
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

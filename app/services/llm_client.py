from __future__ import annotations

import os
import json
import time
import random
import threading
import requests
from typing import Optional, Dict, Any
from ..config import settings


DEFAULT_MODULE_PROMPTS: Dict[str, Dict[str, str]] = {
    "market": {
        "system": "\n".join([
            "你是面向投委会的首席策略官。请基于全部消息给出高信噪比的市场解读。",
            "- 必须通览所有消息，合并相近主题，输出综合判断；",
            "- 开头一句话给出整体点评并提示关键风险；",
            "- 各子主题用概括性语言总结主要内容与关键信息，不摘抄原文，不逐条罗列消息；",
            "- 可在句尾用短标 `#<id>` 标注代表消息来源。",
        ]),
        "user": "\n".join([
            "请输出 {\\\"markdown\\\": string}：",
            "# 市场观点总览",
            "- 整体点评：<一句话概括市场基调/主要驱动/核心风险>",
            "- 关键风险：<一句话指出需关注的触发因素与潜在影响>",
            "## 宏观政策",
            "- <主题A>：<综合结论>；<关键证据/逻辑>；<边际变化/不确定性> (#<id> 可选)",
            "## 行业板块",
            "- <主题A>：<综合结论>；<关键证据/逻辑>；<边际变化/不确定性>",
            "## 公司基本面",
            "- <主题A>：<综合结论>；<关键证据/逻辑>；<风险/待验证>",
            "## 投资策略",
            "- <建议>：<依据>；<风险控制>",
            "## 市场情绪",
            "- <情绪概括>：<迹象/成交/流向/分布>；<演变可能>",
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
            "- 使用 Markdown 表格（按时间倒序）：`| 时间(月-日 时:分) | 形式 | 会议号 | 主题 |`。",
            "  - 平台简称示例：腾讯=腾，进门财经=进，飞书=飞，Zoom=ZM，Teams=TM，钉钉=钉，电话=电。",
            "  - 主题列必须取小模型生成的 summary（去掉 ai: 前缀），不要复制原文。",
            "  - 时间列：若无法从消息中提取明确的会议时间，该列必须留空，严禁使用消息发送时间填充。",
            "- 以 `## 待处理事项` 列出需跟进的动作。",
            "- 引用来源：当需要引用具体消息时，引用短句（<=20字），并在条目末尾标注 `#<id>`（消息 id）。",
            "数据：{{messages_data}}",
        ]),
    },
    "counter": {
        "system": "\n".join([
            "你是分歧聚合分析师。请通读全部消息，将相近主题归为同一议题，输出正反双方要点。",
            "- 每个议题必须包含标题（## 议题：...）；",
            "- 每个议题用 Markdown 表格给出 正方/反方 的 结论/建议、主要依据、代表消息(#id)；",
            "- 另列 冲突点 与 疑问点；不摘抄原文，不罗列标题。",
        ]),
        "user": "\n".join([
            "请输出 {\\\"markdown\\\": string}：",
            "# 分歧观点分析",
            "- 整体概览：<本次涉及的议题数/主要分歧方向/高风险议题>",
            "",
            "## 议题：<概括性主题>",
            "| 立场 | 结论/建议 | 主要依据 | 代表 #id |",
            "| --- | --- | --- | --- |",
            "| 正方 | <综合结论/建议> | <证据/逻辑链条> | #123 #456 |",
            "| 反方 | <综合结论/建议> | <证据/逻辑链条> | #789 |",
            "",
            "### 冲突点",
            "- <关键分歧1；触发条件/边际变化>\\n- <关键分歧2>",
            "### 疑问点",
            "- <待核查信息/数据缺口/下一步验证方向>",
            "（注意：第一个议题也必须包含 '## 议题：...' 标题，不可省略）",
            "数据：{{messages_data}}",
        ]),
    },
    "contacts": {
        "system": "\n".join([
            "你是社交网络分析师。请严格基于数据中提供的 'rating' 字段识别高价值联系人（评分>=60）。",
            "仅基于提供的联系人列表进行分析；列表外的联系人一律忽略。",
            "必须使用消息摘要（summary字段）进行总结，严禁复制原文内容。",
            "禁止复制整条消息，只能使用摘要的概括性内容，必要时引用短句，并标注时间或上下文。",
        ]),
        "user": "\n".join([
            "请通读全部消息摘要并输出 JSON 对象 {\"markdown\": string}：",
            "- markdown 顶部必须包含标题 `# 高评分联系人摘要`。",
            "- 仅分析提供的联系人列表中的人物，不要自行添加其他人。",
            "- 严禁使用 wxid，必须使用数据中提供的 'sender' (已解析为姓名/备注)。",
            "- 严禁自行估算评分，必须使用数据中每条消息携带的 'rating' 字段。",
            "- 仅列出 rating>=60 且有实质内容的联系人。",
            "- 对每位联系人使用如下模版：",
            "  `## 姓名`",
            "  `- 核心观点：基于摘要的一句话概括，必要时引用摘要短句（注明上下文/时间）。`",
            "  `- 最新动态：列出其最近要点（2-4条），优先使用摘要内容。`",
            "  `- 跟进建议：给出可执行的跟进动作（1-2条）。`",
            "  `- 引用来源：当需要引用具体消息时，引用短句（<=20字），并在条目末尾标注 `#<id>`（消息 id）。`",
            "- 重要：必须优先使用消息的summary字段（摘要），不要直接复制content（原文）。",
        ]),
    },
        "newswatch": {
        "system": "你是舆情风控分析师。将多来源新闻整合为面向投研/交易的简报。要求：1) 仅纳入近72小时内的新闻（优先近24小时）；2) 合并相近主题，去重，不复述标题；3) 用概括性语言总结影响路径/受益与受损/待验证；4) 句尾可用 #(id) 标注来源；5) 输出结构清晰、可执行。",
        "user": """
请输出 {\"markdown\": string}：
# 新闻舆情监测
- 总体基调：<一句话概括主线/风险/催化>
- 关键风险：<1-2条>
## 主题脉络
- <主题A>：<综合结论>；<影响路径/受益与受损>；<待验证/数据点>；<时间敏感性> (#<id> 可选)
- <主题B> ...
## 关注动作
- <动作1>：<目的/触发条件>；<跟踪指标>
- <动作2> ...
（仅整合近72小时新闻。避免罗列标题/链接，使用归纳性语言。）
数据：{{messages_data}}
"""
    },

    "socialwatch": {
        "system": "你是一名自媒体舆情分析师，关注雪球/微博/公众号/视频号等内容对市场带来的情绪与潜在风险/机会。",
        "user": "请阅读 JSON 数据并输出 JSON 对象{\"markdown\": string}：\n- 标题：`# 自媒体舆情监测`。\n- `## 负面舆情`：3-6条，指出对象/证据/扩散度/潜在冲击。\n- `## 正面催化`：2-5条，说明触发条件与可量化观察指标。\n- `## 伪信息/谣言`：列出可核查的证伪证据与建议声明。\n- `## 建议`：给风控与投研的具体提醒。\n数据：{{messages_data}}"
    },
}

DEFAULT_TOOL_PROMPTS: Dict[str, Dict[str, str]] = {
    "message_summary": {
        "system": (
            "你是专业的投研信息提取助手。你的任务是：\n"
            "1. 仔细阅读每封邮件/消息的完整内容；\n"
            "2. 理解其核心意图（路演邀请？观点分享？会议通知？）；\n"
            "3. 提取关键事实（平台/会议号/会议开始时间/观点/建议/论据/关键数据），不要编造；\n"
            "4. 用一句话概括最重要的信息（不超过50字），除概括主旨外，把出现的核心结论、观点、推荐、论据、关键数据等要点尽量囊括到这句话中。\n\n"
            "注意：\n"
            "- 必须通读完整正文，不要只看标题；\n"
            "- 摘要要提炼实质内容，不要复读标题或拼凑关键词；\n"
            "- 如果内容确实信息量很少，诚实标注'信息有限'。"
        ),
        "user": (
            "请逐条分析以下消息（JSON格式：id/time/sender/content），返回JSON数组，每个元素结构：\n"
            "{\n"
            "  \"id\": string,                    // 必填：消息ID\n"
            "  \"summary\": string,               // 必填：<=50字自然语句，必须以'ai: '开头；格式必须为'ai: [时间] [平台/会议号] 摘要'（时间/平台/会议号如有则前置），示例'ai: [11-23 14:30] [腾讯 123456789] 路演XX'\n"
            "  \"meeting_number\": string,        // 选填：会议号（归一化为纯数字），支持 9-13 位或 9-10 位；支持'123-456-789'、'+86-xxx-xxx-xxxx'、'400-xxx-xxxx' 等形式\n"
            "  \"platform\": string,              // 选填：会议平台，常见有'腾讯'、'进门'、'飞书'、'钉钉'、'Zoom'、'Teams'、'电话'（含'外呼'/'tel'/'phone'）\n"
            "  \"start_time\": string,            // 选填：会议开始时间，格式如 '11-23 14:30'，若文中未提及则留空\n"
            "  \"tone\": string,                  // 必填：bullish(看多)/bearish(看空)/neutral(中性)/meeting(会议)\n"
            "  \"confidence\": float              // 必填：0.0-1.0，你对提取准确性的信心\n"
            "}\n\n"
            "说明：\n"
            "- meeting_number: 只保留数字；'123-456-789'、'+86-010-8888-6666'、'400-820-5555' 等需去除非数字。\n"
            "- platform: 从文本中识别（含'进门'、'腾讯'、'飞书'、'钉钉'、'Zoom'、'Teams'、'电话'/'外呼'）。\n"
            "- summary: 若有会议时间，务必加在最前，如 '[11-23 14:30]'。\n"
            "- 若无法提取有用信息，summary写'ai: 信息有限'，confidence设为0.3。\n\n"
            "数据：{{messages_json}}"
        ),
    }
    ,
    "email_message_summary": {
        "system": (
            "你是一名专业的投研小模型助手，负责基于邮件正文提炼摘要。\n"
            "要求：\n"
            "- 严禁复读/引用邮件主题或标题；只看正文内容；\n"
            "- 摘要需覆盖：核心观点(简洁)、关键信息(要点)、若文中出现则包含分析师/预约人等角色信息；\n"
            "- 如为会议或路演邮件，识别会议号、平台与开始时间（同样允许 +86/400/连字符形式，归一化为纯数字）；tone 选 'meeting'；category 选 '会议'。\n"
        ),
        "user": (
            "请逐条分析以下邮件正文（JSON格式：id/time/sender/content），返回JSON数组，每个元素结构：\n"
            "{\n"
            "  \"id\": string,\n"
            "  \"summary\": string,               // 必填：<=50字自然语句，必须以'ai: '开头；格式必须为'ai: [时间] [平台/会议号] [预约人:姓名] 摘要'（时间/平台/会议号/预约人如有则前置）\n"
            "  \"meeting_number\": string,        // 选填：会议号（纯数字），允许 9-13 位或 9-10 位；支持 123-456-789, +86-xxx-..., 400-xxx-xxxx 等\n"
            "  \"platform\": string,              // 选填：会议平台（'腾讯'/'进门'/'飞书'/'钉钉'/'Zoom'/'Teams'/'电话'/'外呼'）\n"
            "  \"start_time\": string,            // 选填：会议开始时间，格式如 '11-23 14:30'\n"
            "  \"organizer\": string,             // 选填：内部预约人/联系人姓名\n"
            "  \"tone\": string,                  // 必填：meeting/neutral/bullish/bearish 中选；会议邀请用'meeting'\n"
            "  \"confidence\": float,             // 必填：0.0-1.0\n"
            "  \"category\": string              // 必填：会议/观点/其他 中选；当检测到会议信息时选'会议'\n"
            "}\n\n"
            "说明：\n"
            "- meeting_number 只保留数字；如 '+86-010-8888-6666' -> '01088886666'；'400-820-5555' -> '4008205555'；\n"
            "- summary: 若有会议时间，务必加在最前，如 '[11-23 14:30]'；若有预约人，加在会议号后，如 '[预约人:张三]'。\n"
            "- 再次强调：不要把邮件主题（Subject）当作摘要，请从正文中提取实质内容。\n\n"
            "数据：{{messages_json}}"
        ),
    }
    ,
    "minutes_summary": {
        "system": (
            "你是会议纪要压缩助手，负责把会议纪要或录音文本压缩成<=300字的主题摘要。\n"
            "要求：\n"
            "- 不要复述文件标题；从正文提炼'主题、核心结论、主要依据/要点'；\n"
            "- 如出现明确的会议/路演安排信息（平台/会议号/时间），可简短保留；\n"
            "- 输出须以'ai: '开头；禁止编造信息；"
        ),
        "user": (
            "请逐条总结以下会议纪要文本（JSON数组：id/time/sender/content），返回JSON数组，每个元素：\n"
            "{\n"
            "  \"id\": string,\n"
            "  \"summary\": string,     // 必填，<=300字，自然语句；必须以'ai: '开头\n"
            "  \"tone\": string         // 可选：positive/negative/neutral\n"
            "}\n\n"
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
            "tool_model_messages": settings.SILICONFLOW_TOOL_MODEL or "Qwen/Qwen3-8B",
            "tool_model_emails": settings.SILICONFLOW_TOOL_MODEL or "Qwen/Qwen3-8B",
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
        conf.setdefault("tool_model_messages", conf.get("tool_model_messages") or conf.get("tool_model", "Qwen/Qwen3-8B"))
        conf.setdefault("tool_model_emails", conf.get("tool_model_emails") or conf.get("tool_model", "Qwen/Qwen3-8B"))
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


_LLM_MAX_PARALLEL = max(1, int(os.getenv("AI_MAX_PARALLEL", "3") or 3))
# Global semaphore to throttle concurrent LLM calls across the process
_LLM_SEMAPHORE = threading.BoundedSemaphore(_LLM_MAX_PARALLEL)


def _post_with_backoff(url: str, headers: dict, payload: dict, *, timeout: int = 180) -> requests.Response:
    """POST with basic exponential backoff on 429/5xx.

    This reduces flakiness under provider TPM/RPM limits while preserving caller simplicity.
    """
    attempts = 5
    backoff = 0.6
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            # Happy path
            if resp.status_code < 400:
                return resp
            # Backoff on 429 or 5xx
            if resp.status_code in (429, 500, 502, 503, 504):
                # Honor Retry-After if present
                ra = resp.headers.get("Retry-After")
                try:
                    wait = float(ra) if ra is not None else None
                except Exception:
                    wait = None
                # Base backoff with jitter
                if wait is None:
                    wait = backoff * (2 ** (attempt - 1)) + random.uniform(0.05, 0.25)
                time.sleep(min(wait, 8.0))
                last_exc = requests.HTTPError(f"status={resp.status_code}")
                continue
            # Other client errors: bubble up immediately
            resp.raise_for_status()
            return resp  # pragma: no cover
        except requests.RequestException as e:  # network errors -> retry
            last_exc = e
            time.sleep(backoff * (2 ** (attempt - 1)) + random.uniform(0.05, 0.25))
            continue
    # Exhausted retries
    if last_exc:
        raise last_exc
    raise RuntimeError("LLM request failed after retries")


def siliconflow_chat(
    messages: list[dict],
    temperature: float | None = 0.3,
    model_override: str | None = None,
    *,
    force_json: bool = False,
) -> str:
    """Call SiliconFlow once; auto‑retry with gentle backoff on rate limits.
    If it still fails, caller should handle local fallback.
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
    # Encourage JSON-only output for tool calls when supported
    if force_json:
        try:
            payload["response_format"] = {"type": "json_object"}
        except Exception:
            pass
    # Throttle concurrent calls globally to reduce 429 spikes
    with _LLM_SEMAPHORE:
        # Allow configuring HTTP timeout via ai_config (defaults to 90s to avoid long hangs)
        try:
            http_timeout = int(conf.get("http_timeout") or 90)
        except Exception:
            http_timeout = 90
        resp = _post_with_backoff(url, headers, payload, timeout=http_timeout)
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


def siliconflow_tool_chat(messages: list[dict], temperature: float = 0.2, model_override: str | None = None) -> str:
    conf = load_ai_config()
    tool_model = model_override or conf.get("tool_model") or "Qwen/Qwen3-8B"
    # Tool calls expect strict JSON; request JSON object formatting when supported
    return siliconflow_chat(messages, temperature=temperature, model_override=tool_model, force_json=True)


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

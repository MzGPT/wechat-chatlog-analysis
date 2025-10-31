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
            "你是一名面向投委会的首席策略官，职责是把大量噪声消息整合成高信噪比的领导简报。",
            "必须像智能体一样通览全文，理解大段文字的核心意义，提炼趋势、驱动与影响，给出可执行的提醒。",
            "严禁复述原文、严禁罗列机构或时间、严禁提取泛用名词作为关键词。",
            "必须真正概括主旨，用自然语言总结核心观点和逻辑，而不是简单提取词汇。",
            "如需引用来源，只能在句尾用 `(来源:#123 #456)` 短标，正文不得出现券商、人名、日期。",
        ]),
        "user": "\n".join([
            "请通读所有消息（包括摘要和正文上下文），理解每段内容的核心意义，然后输出 JSON 对象 {\"markdown\": string}，格式严格如下：",
            "`# 市场观点总览`",
            "`- 核心基调：<一句话概括整体市场多空平衡/驱动因素/核心风险，必须基于对全部内容的深入理解>`",
            "`- 今日关键风险：<1-2 个最需关注的风险，含触发条件和影响>`",
            "`## 宏观政策`",
            "`1. <概括性主题名（避免泛用词）>：核心结论；关键证据与逻辑链条；风险点/待验证事项` (共 2-4 条，按重要度降序)",
            "`## 行业板块` (2-4 条，突出热点/拐点/行业对比，用概括性语言描述趋势)",
            "`## 公司基本面` (2-4 条，聚焦业绩/订单/估值变化，总结核心要点)",
            "`## 投资策略` (给出资产配置/仓位/风格建议，基于整体判断)",
            "`## 市场情绪` (量化描述情绪分布、资金流向或成交特征，总结情绪特征)`",
            "`## 今日重点提示` (3-4 条行动建议或跟踪事项，基于综合分析)`",
            "重要要求：",
            "- 必须通览所有消息，不要遗漏任何重要信息；确保覆盖所有相关消息的要点。",
            "- 每条观点必须基于对原文的深入理解，用概括性语言总结核心意义，而非抄写原文或提取泛用名词。",
            "- 每条观点最多两行，先结论后依据，用分号连接；强调逻辑链。",
            "- 同一主题只保留一条，合并重复表述；如有分歧，用`(存在分歧：...)` 标注。",
            "- 严禁出现'观点'、'认为'、'建议'等泛用名词作为主题；要用能体现核心内容的概括性表述。",
            "- 数据请写成'同比+/-x%'等结构化描述，但整体表述要自然流畅。",
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
            "你是一名风控合规负责人，需要识别同一议题下的分歧观点，帮助管理层快速定位争议点。",
            "必须呈现每个主题下的双方立场、依据与待补充信息，严禁罗列原文或未经核实的臆测。",
        ]),
        "user": "\n".join([
            "请将输入消息整合为 {\"markdown\": string}，结构如下：",
            "`# 分歧观点分析`",
            "`- 整体概览：共发现 X 个存在明显分歧的议题，列出需重点核查的两个方向。`",
            "`## 议题：<主题>` (按影响力排序，最多 5 个)",
            "`- 主流观点：<主结论；依据(简短)；乐观侧重点>`",
            "`- 对立观点：<反方结论；依据(简短)；担忧点>`",
            "`- 待核查：<需要补充的数据/事件/联系人>`",
            "- 如有更多分支观点，可在末尾补充 `其他声音：...`，但同一段落长度需控制在两行内。",
            "- 不得出现人名/机构/时间，只可在句尾用 `(来源:#123 #456)` 简短标注。",
            "- 文末 `## 行动建议`：列出 2-3 条聚焦在调查、沟通或风控动作的建议。",
            "数据：{{messages_data}}",
        ]),
    },
    "contacts": {
        "system": "\n".join([
            "你是一名客户关系助理，需要梳理高评分联系人在聊天记录中的核心观点。",
            "请结合联系人评分、活跃度和消息摘要，提炼每位联系人的重点信息。",
            "必须使用消息摘要（summary字段）进行总结，严禁复制原文内容。",
            "禁止复制整条消息，只能使用摘要的概括性内容，必要时引用短句，并标注时间或上下文。",
        ]),
        "user": "\n".join([
            "请通读全部消息摘要并输出 JSON 对象 {\"markdown\": string}：",
            "- markdown 顶部使用 `# 高评分联系人摘要`，说明筛选标准（评分≥60分）。",
            "- 对每位联系人使用如下模版：",
            "  `### 姓名（评分 x.x / 活跃 y）`",
            "  `- 核心观点：基于摘要的一句话概括，必要时引用摘要短句（注明上下文/时间）。`",
            "  `- 最新动态：列出其最近要点（2-4条），优先使用摘要内容。`",
            "  `- 跟进建议：给出可执行的跟进动作（1-2条）。`",
            "- 可另加 `## 关注联系人` 段落列出潜力对象（同上模版）。",
            "- 引用来源：当需要引用具体消息时，引用短句（<=20字），并在条目末尾标注 `#<id>`（消息 id）。",
            "- 重要：必须优先使用消息的summary字段（摘要），不要直接复制content（原文）。",
        ]),
    },
        "newswatch": {
        "system": "你是一名舆情风控分析师，要将多来源新闻归纳为交易层面的日报。必须通览全文，理解每篇新闻的核心意义，概括要点，而不是简单复制标题或内容。",
        "user": """
请通读所有新闻的全文内容，理解每篇新闻的核心意思和关键要点，然后输出 {"markdown": string}，遵循以下结构：
`# 新闻舆情监测`
`- 数据概览：今日收录 <总条数> 条，主要来源 <前3家媒体>；类别分布（宏观/行业/个股/...）`
`## 全局判断`
`- <总结1: 基于全部新闻理解的主线/谈判/政策进展 + 对市场的影响>`
`- <总结2: 业绩披露/资金面/风险事件的核心要点 + 影响>`
`## 主题脉络`
`1. <主题A>(<条数>条): 基于全文理解的核心结论；可能影响；关注要点`
`2. <主题B>...` (列3-5个，按影响力排序，必要时合并同义主题，每个主题必须概括核心意思)
`## 舆情温度`
`- 正面/中性/负面条数对比，指出情绪倾向变化`
`- 监测到的风险点或舆情扩散路径`
`## 关注动作`
`- 3-4 条可执行的跟踪/沟通/仓位建议`
重要约束：
- 必须通览全文，理解每篇新闻的核心意思，用概括性语言总结要点，严禁直接复制新闻标题或内容。
- 只能写综合总结，不得粘贴原新闻标题或媒体名；引用来源仅能在句尾用 `(来源:#123 #456)` 简短标注。
- 同一主题若多条新闻，务必合并，写明"X 条报道集中于..."，并概括这些报道的核心意思。
- 避免无意义的客套语，重点描述趋势、驱动因素、潜在催化与风险，用自然语言概括而非抄写。
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
            "3. 提取关键事实（平台/会议号/观点/建议/论据/关键数据），不要编造；\n"
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
            "  \"summary\": string,               // 必填：<=50字自然语句，必须以'ai: '开头；格式推荐'平台 会议号 摘要'（如有其一则前置），示例'ai: 腾讯 123456789 路演XX'\n"
            "  \"meeting_number\": string,        // 选填：会议号（归一化为纯数字），支持 9-13 位或 9-10 位；支持'123-456-789'、'+86-xxx-xxx-xxxx'、'400-xxx-xxxx' 等形式\n"
            "  \"platform\": string,              // 选填：会议平台，常见有'腾讯'、'进门'、'飞书'、'钉钉'、'Zoom'、'Teams'、'电话'（含'外呼'/'tel'/'phone'）\n"
            "  \"tone\": string,                  // 必填：bullish(看多)/bearish(看空)/neutral(中性)/meeting(会议)\n"
            "  \"confidence\": float              // 必填：0.0-1.0，你对提取准确性的信心\n"
            "}\n\n"
            "说明：\n"
            "- meeting_number: 只保留数字；'123-456-789'、'+86-010-8888-6666'、'400-820-5555' 等需去除非数字。\n"
            "- platform: 从文本中识别（含'进门'、'腾讯'、'飞书'、'钉钉'、'Zoom'、'Teams'、'电话'/'外呼'）。\n"
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
            "- 如为会议或路演邮件，识别会议号与平台（同样允许 +86/400/连字符形式，归一化为纯数字）；tone 选 'meeting'；category 选 '会议'。\n"
        ),
        "user": (
            "请逐条分析以下邮件正文（JSON格式：id/time/sender/content），返回JSON数组，每个元素结构：\n"
            "{\n"
            "  \"id\": string,\n"
            "  \"summary\": string,               // 必填：<=50字自然语句，必须以'ai: '开头；推荐'平台 会议号 摘要'的顺序（如可识别）\n"
            "  \"meeting_number\": string,        // 选填：会议号（纯数字），允许 9-13 位或 9-10 位；支持 123-456-789, +86-xxx-..., 400-xxx-xxxx 等\n"
            "  \"platform\": string,              // 选填：会议平台（'腾讯'/'进门'/'飞书'/'钉钉'/'Zoom'/'Teams'/'电话'/'外呼'）\n"
            "  \"tone\": string,                  // 必填：meeting/neutral/bullish/bearish 中选；会议邀请用'meeting'\n"
            "  \"confidence\": float,             // 必填：0.0-1.0\n"
            "  \"category\": string              // 必填：会议/观点/其他 中选；当检测到会议信息时选'会议'\n"
            "}\n\n"
            "说明：\n"
            "- meeting_number 只保留数字；如 '+86-010-8888-6666' -> '01088886666'；'400-820-5555' -> '4008205555'；\n"
            "- summary 开头（如适用）建议包含平台与会议号以便前端渲染。\n\n"
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

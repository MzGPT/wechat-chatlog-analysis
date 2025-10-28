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
            "你是资深投研首席，要在高噪声样本中形成‘综述级’结论而非逐条摘录。",
            "你的职责：去重、合并同义、提炼趋势与因果链，保留关键信号与不确定性。",
            "只在必要时引用极短证据，并标注来源 `#<id>`，禁止罗列长段原文。",
        ]),
        "user": "\n".join([
            "请阅读 JSON，并输出 JSON 对象 {\"markdown\": string}：",
            "- 开头 `# 市场观点总结`：用2-3句话给出‘本周市场基调+主因’（必须是你自己的归纳）。",
            "- 分类：`## 宏观政策`、`## 行业板块`、`## 公司基本面`、`## 投资策略`、`## 市场情绪`、`## 其他观点`。",
            "  - 每类只保留3-6条去重后的要点；每条=‘结论/倾向 + 关键依据(简短) + 风险/不确定性’，必要处用 `#<id>` 标注来源。",
            "- 末尾 `## 行动建议` 与 `## 关注事项`：聚焦可执行的仓位/跟踪项。",
            "- 严禁逐条拼接消息；必须合并同义、消除重复，使用统一表述；对矛盾信息给出‘以何为准’的判断或待证。",
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
    "newswatch": {
        "system": "你是一名舆情风控分析师，擅长从新闻与快讯中识别风险、黑天鹅与突发利好，输出面向投研/交易的预警摘要。",
        "user": "请阅读 JSON 数据并输出 JSON 对象{\"markdown\": string}：\n- 标题：`# 新闻舆情监测`，用一段话概述总体风险基调。\n- `## 重大风险`：列出3-8条，包含事件、涉及标的/行业、潜在影响、证据来源（引用 #id 或来源字段）。\n- `## 突发利好`：列出2-6条，说明逻辑链与催化窗口。\n- `## 需跟踪`：列出待确认信息与下一步动作。\n要求：\n- 去除营销/低信噪信息；强调可验证来源；避免夸大。\n数据：{{messages_data}}"
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
            "1. 仔细阅读每封邮件/消息的完整内容\n"
            "2. 理解其核心意图（路演邀请？观点分享？会议通知？）\n"
            "3. 提取关键事实（会议号/观点/建议/论据/关键数据），不要编造\n"
            "4. 用一句话概括最重要的信息（不超过50字），除概括主旨外，把出现的核心结论、观点、推荐、论据、关键数据等要点尽量囊括到这句话中（用分号或逗号自然连接）\n\n"
            "注意：\n"
            "- 必须通读完整正文，不要只看标题\n"
            "- 摘要要提炼实质内容，不要复读标题或拼凑关键词\n"
            "- 如果内容确实信息量很少，诚实标注'信息有限'"
        ),
        "user": (
            "请逐条分析以下消息（JSON格式：id/time/sender/content），返回JSON数组，每个元素结构：\n"
            "{\n"
            "  \"id\": string,                    // 必填：消息ID\n"
            "  \"summary\": string,               // 必填：<=50字自然语句，必须以'ai: '开头，概括主旨并包含关键要点（结论/观点/推荐/论据/关键数据）\n"
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
    ,
    "email_message_summary": {
        "system": (
            "你是一名专业的投研小模型助手，负责基于邮件正文提炼摘要。\n"
            "要求：\n"
            "- 严禁复读/引用邮件主题或标题；只看正文内容\n"
            "- 摘要需覆盖：核心观点(简洁)、关键信息(要点)、若文中出现则包含分析师/预约人等角色信息\n"
            "- 如为会议或路演邮件，识别会议号(9-13位数字)与平台；tone 选 'meeting'；category 选 '会议'\n"
        ),
        "user": (
            "请逐条分析以下邮件正文（JSON格式：id/time/sender/content），返回JSON数组，每个元素结构：\n"
            "{\n"
            "  \"id\": string,\n"
            "  \"summary\": string,               // 必填：<=50字自然语句，必须以'ai: '开头；基于正文概括观点与关键信息，若有则点名分析师/预约人\n"
            "  \"meeting_number\": string,        // 选填：从正文抽取的9-13位纯数字会议号，无则''\n"
            "  \"tone\": string,                  // 必填：meeting/neutral/bullish/bearish 中选；会议邀请用'meeting'\n"
            "  \"confidence\": float,             // 必填：0.0-1.0\n"
            "  \"category\": string              // 必填：会议/观点/其他 中选；当检测到会议信息时选'会议'\n"
            "}\n\n"
            "说明：\n"
            "- 严禁使用标题或主题信息；仅使用正文进行判断与生成\n"
            "- meeting_number 只保留数字；tone=meeting 当存在会议/路演述求或会议号/平台\n"
            "- 如果正文信息不足，summary 写'ai: 信息有限'，confidence=0.3\n\n"
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


def siliconflow_chat(messages: list[dict], temperature: float | None = 0.3, model_override: str | None = None) -> str:
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
    # Throttle concurrent calls globally to reduce 429 spikes
    with _LLM_SEMAPHORE:
        resp = _post_with_backoff(url, headers, payload, timeout=180)
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

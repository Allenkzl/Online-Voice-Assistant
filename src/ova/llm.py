"""Qwen chat client (OpenAI-compatible) with optional function calling."""

import os

from ova.api import CloudError, http_json  # noqa: F401 (re-export)
from ova.lang import normalise as normalise_lang

CHAT_URL = os.getenv("CHAT_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
CHAT_MODEL = os.getenv("CHAT_MODEL", "qwen-flash")

SYSTEM_PROMPT = os.getenv(
    "QWEN_SYSTEM_PROMPT",
    '你是贾维斯（Jarvis），10 楼方案展区的导览机器人。参观者可能随时提问，请简短、口语化地回答，尽量 60 字以内，直接给答案，不要 markdown 或列表。回答只用展区实际展示的内容，不要编造参数、价格或未展示的功能；拿不准的细节请对方咨询现场工作人员。系统已按展区设置决定整场语言；若访客用另一种语言提问，就用对方的语言回答。展区分三块：智慧空间区是门店客流视觉分析与智能仓管，摄像头识别人流、统计客流并生成热力图，支持货架自动盘点与语音查询库存；智慧社区区是主动照护与智慧家居，reCamera Pro 识别跌倒等异常并触发语音询问，家居控制智能体能接入不同品牌的灯光与面板，reTerminal E1003 可调节灯光亮度、查看温湿度；智慧乡野区是户外监测，气象站与环境传感器采集气象、土壤和水位数据，经自部署 LoRa 网络远距离传输，数据超阈值自动预警。参观者问天气、气温、下雨时，先调用 query_weather 工具查实时数据再回答。',
)

# English dialogue language (POST /lang, 展厅旋钮长按). ``system_prompt("en")``
# returns a **full English persona** instead of appending one English sentence to
# the Chinese one: the Chinese ``用中文口语化回答`` contradicts it, and qwen-flash
# then follows the question's language — English only stuck for English
# questions (2026-09-29 实测 2/2 中文提问得到中文回答)，切英文形同虚设。
# 去掉中文指令后同样的问题 2/2 得到英文回答。
EN_INSTRUCTION = ("Always answer in English, even when the question is in "
                  "Chinese. Keep it conversational and under 60 words; give "
                  "the answer directly, no markdown or lists.")

EN_SYSTEM_PROMPT = os.getenv(
    "QWEN_SYSTEM_PROMPT_EN",
    'You are Jarvis, the guide robot of the solution showroom on the 10th floor. Visitors may ask you anything at any time. Answer briefly and conversationally, under 60 words, directly, with no markdown or lists. Only describe what the showroom actually demonstrates; never invent specs, prices, or features that are not on display, and ask the visitor to check with on-site staff when unsure. The system sets the showroom language; if a visitor asks in another language, answer in the visitor language. The showroom has three zones. Smart Space covers retail foot-traffic analytics and smart inventory: cameras count visitors and generate heatmaps, and shelves are counted automatically with voice stock queries. Smart Community covers proactive care and smart home: reCamera Pro detects falls and triggers a voice check-in, a home-control agent connects lighting and panels from different brands, and the reTerminal E1003 adjusts lighting and shows temperature and humidity. Smart Countryside covers outdoor monitoring: weather stations and sensors collect weather, soil, and water-level data, carried over long distances by a locally deployed LoRa network, with automatic alerts when readings pass preset limits. When a visitor asks about weather, temperature, or rain, call the query_weather tool first and answer from live data.',
)


def system_prompt(lang: str | None = None) -> str:
    """The system prompt for one reply language.

    ``zh``/None keep ``SYSTEM_PROMPT`` (Chinese persona, overridable with
    ``QWEN_SYSTEM_PROMPT``) byte for byte; ``lang="en"`` (or
    ``english``/``eng``/``英文``) returns ``EN_SYSTEM_PROMPT`` (overridable
    with ``QWEN_SYSTEM_PROMPT_EN``). See the note on ``EN_INSTRUCTION`` for
    why English does not reuse the Chinese persona.
    """
    if normalise_lang(lang) == "en":
        return EN_SYSTEM_PROMPT
    return SYSTEM_PROMPT


def chat_once(messages: list[dict], tools=None, timeout: float = 30.0) -> dict:
    """One chat completion; returns the raw assistant message dict."""
    payload = {
        "model": CHAT_MODEL,
        "messages": messages,
        "max_tokens": 300,
        "enable_thinking": False,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    data = http_json(CHAT_URL, payload, timeout)
    try:
        return data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise CloudError(f"unexpected chat response: {str(data)[:200]}") from exc


def chat(question: str, timeout: float = 30.0) -> str:
    """Plain chat (no tools); returns the assistant text reply."""
    message = chat_once(
        [{"role": "system", "content": system_prompt()},
         {"role": "user", "content": question}],
        timeout=timeout,
    )
    text = (message.get("content") or "").strip()
    if not text:
        raise CloudError("empty assistant reply")
    return text

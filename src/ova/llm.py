"""Qwen chat client (OpenAI-compatible) with optional function calling."""

import os

from ova.api import CloudError, http_json  # noqa: F401 (re-export)
from ova.lang import normalise as normalise_lang

CHAT_URL = os.getenv("CHAT_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions")
CHAT_MODEL = os.getenv("CHAT_MODEL", "qwen-flash")

SYSTEM_PROMPT = os.getenv(
    "QWEN_SYSTEM_PROMPT",
    "你是语音助手 Jarvis。用户询问天气、气温、下雨等问题时，必须调用 "
    "query_weather 工具查实时数据后再回答；其他问题（攻略、常识、闲聊等）"
    "直接正常回答，不要说自己是受限的或只能查天气。"
    "用中文口语化回答，尽量60字以内，直接给答案，不要markdown或列表。",
)

# Appended when the dialogue language is English (POST /lang, 展厅旋钮长按) so
# the whole conversation is answered in English, still short and speakable.
EN_INSTRUCTION = ("Answer in English, conversational, ≤60 words, "
                  "no markdown or lists.")


def system_prompt(lang: str | None = None) -> str:
    """The system prompt for one reply language.

    ``SYSTEM_PROMPT`` (Chinese persona, overridable with ``QWEN_SYSTEM_PROMPT``)
    is always the base; ``lang="en"`` (or ``english``/``eng``/``英文``) appends
    the English instruction. ``zh``/None keep the existing Chinese wording
    byte for byte.
    """
    if normalise_lang(lang) == "en":
        return f"{SYSTEM_PROMPT} {EN_INSTRUCTION}"
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

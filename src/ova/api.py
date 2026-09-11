"""Shared HTTP client for cloud model APIs (DashScope/Qwen, Zhipu/GLM)."""

import json
import urllib.error
import urllib.request


class CloudError(RuntimeError):
    """Raised when a cloud call fails (network, HTTP error, empty result)."""


def http_json(url: str, payload: dict, timeout: float = 25.0,
              api_key_env: str = "DASHSCOPE_API_KEY") -> dict:
    """POST JSON with a bearer key from the environment; returns parsed JSON.

    ``api_key_env`` names the variable holding the key, so one helper serves
    DashScope (Qwen) and other providers (e.g. ZHIPUAI_API_KEY for GLM).
    """
    import os
    key = os.environ.get(api_key_env, "")
    if not key:
        raise CloudError(f"{api_key_env} is not set")
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        raise CloudError(f"HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise CloudError(f"network error: {exc.reason}") from exc

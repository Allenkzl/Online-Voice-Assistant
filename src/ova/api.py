"""Shared HTTP client for DashScope (Qwen) cloud services."""

import json
import urllib.error
import urllib.request


class CloudError(RuntimeError):
    """Raised when a cloud call fails (network, HTTP error, empty result)."""


def http_json(url: str, payload: dict, timeout: float = 25.0) -> dict:
    """POST JSON with the DashScope bearer key; returns parsed JSON."""
    import os
    key = os.environ.get("DASHSCOPE_API_KEY", "")
    if not key:
        raise CloudError("DASHSCOPE_API_KEY is not set")
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

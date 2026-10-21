#!/usr/bin/env python3
"""把展区人设从 systemd EnvironmentFile 同步进 ``src/ova/llm.py`` 的默认值（幂等）。

背景：现场 Reachy Mini 的人设由设备 ``/etc/ova.env`` 的
``QWEN_SYSTEM_PROMPT`` / ``QWEN_SYSTEM_PROMPT_EN`` 覆盖；代码默认值必须与它保持
一致，否则换机/重装/新部署会退回旧人设（"隐式配置"漂移）。

用法（仓库根目录）：
    python3 tools/sync_persona_to_llm.py --persona <persona.env 路径>     # 写入
    python3 tools/sync_persona_to_llm.py --persona <...> --check          # 只比对，退出码 1=需要同步

persona.env 的权威副本在 infra-network 仓
``hall/button-bridge/docs/ops/reachy-mini/persona.env``（含部署/回滚说明）。
"""
from __future__ import annotations

import argparse
import ast
import re
import shlex
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LLM = REPO / "src" / "ova" / "llm.py"
KEYS = ("QWEN_SYSTEM_PROMPT", "QWEN_SYSTEM_PROMPT_EN")


def read_persona(path: Path) -> dict[str, str]:
    """从 EnvironmentFile 取人设；单引号内是字面值（systemd 不做转义）。"""
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        for key in KEYS:
            if line.startswith(f"{key}="):
                out[key] = shlex.split(line.split("=", 1)[1])[0]
    missing = [k for k in KEYS if not out.get(k)]
    if missing:
        raise SystemExit(f"{path}: 缺少 {', '.join(missing)}")
    return out


def default_span(src: str, key: str) -> tuple[int, int]:
    """``key = os.getenv("KEY", <默认>)`` 里默认值表达式的 [start, end)。"""
    start = src.index(f'    "{key}",\n') + len(f'    "{key}",\n')
    for m in re.finditer(r",\s*\n", src[start:]):
        return start, start + m.start()
    raise SystemExit(f"llm.py: 无法定位 {key} 默认值的结尾")


def literal(text: str, indent: str = "    ") -> str:
    return "\n".join(indent + ln for ln in repr(text).splitlines())


def load_module(src: str) -> dict:
    """在 stub 掉 os / ova.* 的环境里执行源码，读出常量做语义比对。"""
    os_stub = types.ModuleType("os")
    os_stub.getenv = lambda key, default=None: default  # type: ignore[attr-defined]
    ova = types.ModuleType("ova")
    ova.__path__ = []  # type: ignore[attr-defined]
    lang = types.ModuleType("ova.lang")
    lang.normalise = lambda value: value or "zh"  # type: ignore[attr-defined]
    api = types.ModuleType("ova.api")
    api.CloudError = type("CloudError", (Exception,), {})  # type: ignore[attr-defined]
    api.http_json = lambda *a, **k: {}  # type: ignore[attr-defined]
    saved = {k: sys.modules.get(k) for k in ("os", "ova", "ova.lang", "ova.api")}
    sys.modules.update({"os": os_stub, "ova": ova, "ova.lang": lang, "ova.api": api})
    try:
        ns: dict = {}
        exec(compile(src, str(LLM), "exec"), ns)  # noqa: S102 本地自检
        return ns
    finally:
        for key, mod in saved.items():
            if mod is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", required=True, type=Path,
                    help="persona.env 路径（infra-network 仓 ops/reachy-mini/ 下）")
    ap.add_argument("--check", action="store_true", help="只比对，不写入")
    args = ap.parse_args()

    persona = read_persona(args.persona)
    src = LLM.read_text(encoding="utf-8")
    for key in KEYS:
        start, end = default_span(src, key)
        src = src[:start] + literal(persona[key]) + src[end:]
    ast.parse(src)

    ns = load_module(src)
    checks = {
        "SYSTEM_PROMPT": ns["SYSTEM_PROMPT"] == persona["QWEN_SYSTEM_PROMPT"],
        "EN_SYSTEM_PROMPT": ns["EN_SYSTEM_PROMPT"] == persona["QWEN_SYSTEM_PROMPT_EN"],
    }
    if not all(checks.values()):
        print(f"自检失败：{checks}", file=sys.stderr)
        return 2
    changed = src != LLM.read_text(encoding="utf-8")
    print(f"人设一致（zh {len(persona['QWEN_SYSTEM_PROMPT'])}B / "
          f"en {len(persona['QWEN_SYSTEM_PROMPT_EN'])}B）；需要同步={changed}")
    if args.check:
        return 1 if changed else 0
    if changed:
        LLM.write_text(src, encoding="utf-8")
        print(f"已写入 {LLM.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
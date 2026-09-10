"""Fixed showroom exhibit introductions.

The robot should read approved exhibit copy verbatim instead of asking the LLM
to improvise. This module maps recognized voice commands such as "介绍智慧零售"
or "introduce smart retail" to a pre-generated local WAV file.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import string


DEFAULT_CONFIG = "config/solutions.json"
CN_DIGITS = {"一": "1", "二": "2", "三": "3", "四": "4", "五": "5"}


@dataclass(frozen=True)
class SolutionIntro:
    id: str
    name: str
    language: str
    text: str
    audio_path: Path


def _project_root() -> Path:
    return Path(os.getenv("OVA_HOME", Path.cwd()))


def _resolve_path(value: str | os.PathLike, base: Path) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return base / path


def _normalise(text: str) -> str:
    text = text.lower().strip()
    table = str.maketrans("", "", string.whitespace + string.punctuation)
    text = text.translate(table)
    text = re.sub(r"[，。！？、：；“”‘’（）【】《》]", "", text)
    for cn, digit in CN_DIGITS.items():
        text = text.replace(cn, digit)
    return text


def _variant(item: dict, lang: str, root: Path) -> SolutionIntro:
    text = str(item.get(f"text_{lang}") or item.get("text") or "")
    audio = item.get(f"audio_{lang}") or item.get("audio")
    if not audio:
        raise KeyError(f"missing audio_{lang} for {item.get('id')}")
    return SolutionIntro(
        id=str(item["id"]),
        name=str(item.get(f"name_{lang}") or item.get("name") or item["id"]),
        language=lang,
        text=text,
        audio_path=_resolve_path(audio, root),
    )


def _aliases(item: dict, lang: str) -> list[str]:
    if lang == "zh":
        values = [item.get("name_zh") or item.get("name") or ""]
    else:
        values = [item.get("name_en") or ""]
    values.extend(item.get(f"aliases_{lang}") or [])
    if lang == "zh":
        values.extend(item.get("aliases") or [])
    return [str(value) for value in values if str(value).strip()]


def load_solution_intros(config_path: str | os.PathLike | None = None) -> list[SolutionIntro]:
    root = _project_root()
    cfg_path = _resolve_path(
        config_path or os.getenv("OVA_SOLUTIONS_CONFIG", DEFAULT_CONFIG),
        root,
    )
    if not cfg_path.is_file():
        return []
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    intros: list[SolutionIntro] = []
    for item in data.get("solutions", []):
        has_variants = False
        for lang in ("zh", "en"):
            if f"audio_{lang}" in item or f"text_{lang}" in item:
                intros.append(_variant(item, lang, root))
                has_variants = True
        if not has_variants:
            intros.append(_variant(item, "zh", root))
    return intros


def match_solution_intro(
    command: str,
    config_path: str | os.PathLike | None = None,
) -> SolutionIntro | None:
    """Return the requested intro when the recognized text names a solution."""
    query = _normalise(command)
    if not query:
        return None
    cfg_path = _resolve_path(
        config_path or os.getenv("OVA_SOLUTIONS_CONFIG", DEFAULT_CONFIG),
        _project_root(),
    )
    if not cfg_path.is_file():
        return None
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    for item in data.get("solutions", []):
        for lang in ("zh", "en"):
            for alias in _aliases(item, lang):
                if _normalise(str(alias)) in query:
                    return _variant(item, lang, _project_root())
    return None

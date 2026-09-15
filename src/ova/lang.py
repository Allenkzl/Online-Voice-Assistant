#!/usr/bin/env python3
"""Persistent dialogue language for the whole conversation (stdlib only).

The showroom keyboard switches the conversation language by long-pressing a
knob; the choice is not a one-request hint but "the language this robot talks
now". It is kept in a small state file (the same idea as
``speaking_state_file``) so every later turn follows it — injected text,
showroom intro variants and the LLM answer language — without restarting
``ova-wake``:

    POST /lang {"toggle": true}    # 中 <-> 英
    POST /lang {"lang": "en"}      # 直接设置
    GET  /lang                     # 读当前语言

The state file is best effort: a missing, unreadable or garbage file falls back
to ``cfg["dialogue_lang"]`` (built-in default ``zh``) and never raises, so a bad
/tmp can never break the microphone path.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ova.config import svc_event

LOG = logging.getLogger("ova.lang")

DEFAULT_LANG = "zh"
LANGS = ("zh", "en")
DEFAULT_LANG_FILE = "/tmp/ova_lang.state"

# Accepted spellings: what a keypad, a curl call or a config file may send.
ALIASES = {
    "zh": "zh",
    "cn": "zh",
    "chinese": "zh",
    "中文": "zh",
    "en": "en",
    "eng": "en",
    "english": "en",
    "英文": "en",
}


def normalise(value) -> str | None:
    """Map any accepted spelling to ``zh``/``en``; None when unrecognized.

    Case and whitespace are ignored, so ``ENGLISH``, ``  en `` and ``英文`` all
    work. ``None`` (and anything else unknown) returns None.
    """
    if value is None:
        return None
    text = "".join(ch for ch in str(value).lower() if not ch.isspace())
    return ALIASES.get(text)


def state_path(cfg: dict) -> Path:
    """The state file holding the language somebody switched to."""
    return Path(str(cfg.get("dialogue_lang_file") or DEFAULT_LANG_FILE))


def configured(cfg: dict) -> str:
    """The language configured at startup (``zh`` when unset or invalid)."""
    return normalise(cfg.get("dialogue_lang")) or DEFAULT_LANG


def _has_settings(cfg: dict) -> bool:
    """True when this cfg really carries the dialogue-language settings.

    A caller that passes a bare cfg (unit tests, small tools) has no persistent
    language, so nothing on disk is consulted and the built-in default applies.
    The wake service always carries both keys, because they are in
    ``wake.DEFAULTS``.
    """
    return "dialogue_lang_file" in cfg or "dialogue_lang" in cfg


def _stored(cfg: dict) -> str | None:
    """The language in the state file, or None when it is missing/garbage."""
    if not _has_settings(cfg):
        return None
    try:
        raw = state_path(cfg).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        LOG.debug("LANG_STATE_UNREADABLE path=%s: %s", state_path(cfg), exc)
        return None
    return normalise(raw)


def current(cfg: dict) -> str:
    """The language in force now: state file first, config second, ``zh`` last.

    Never raises: an unreadable or invalid state file only costs the switch.
    """
    return _stored(cfg) or configured(cfg)


def chosen(cfg: dict) -> str | None:
    """The language somebody explicitly picked, or None while nobody did.

    The state file means "the knob was used on site"; a ``dialogue_lang`` that
    differs from the built-in default means "configured on purpose". Callers
    that have their own per-request default (the showroom intros, where the
    matched keyword picks the language) use this so an untouched robot keeps
    its old behaviour; :func:`current` is the always-a-language view.
    """
    stored = _stored(cfg)
    if stored is not None:
        return stored
    want = normalise(cfg.get("dialogue_lang"))
    return want if want and want != DEFAULT_LANG else None


def set_lang(cfg: dict, value) -> tuple[str, str]:
    """Persist one language switch; returns ``(previous, new)``.

    Raises ValueError for an unknown language (the HTTP entrance turns that
    into 400). Writing the state file is best effort: a read-only or bogus path
    warns and still reports the switch, because the answer of this process must
    not depend on /tmp being writable.
    """
    new = normalise(value)
    if new is None:
        raise ValueError(f"unsupported lang {value!r}; expected one of "
                         f"{', '.join(LANGS)}")
    previous = current(cfg)
    path = state_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new + "\n", encoding="utf-8")
    except OSError as exc:
        LOG.warning("LANG_STATE_ERROR path=%s %s: %s", path, type(exc).__name__,
                    exc)
    LOG.info("LANG_SET previous=%s lang=%s", previous, new)
    svc_event("system", f"对话语言已切换: {previous} → {new}", "ok", lang=new)
    return previous, new


def toggle(cfg: dict) -> tuple[str, str]:
    """Switch the conversation language back and forth (knob long-press)."""
    return set_lang(cfg, "en" if current(cfg) == DEFAULT_LANG else DEFAULT_LANG)
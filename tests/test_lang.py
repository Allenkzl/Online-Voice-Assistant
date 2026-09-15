#!/usr/bin/env python3
"""Dialogue-language tests: normalisation, state file, toggle — no hardware.

``ova.lang`` is the persistent "what language does this robot talk now" state
behind ``POST /lang`` (showroom knob long-press). Everything here runs on a
throwaway state file, so the real ``/tmp/ova_lang.state`` is never touched.

Run with pytest or directly:

    python3 tests/test_lang.py
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:      # allow running without `pip install -e .`
    sys.path.insert(0, str(SRC))

os.environ.setdefault("OVA_HOME", str(ROOT))

from ova import lang as ova_lang                                   # noqa: E402
from ova import wake                                               # noqa: E402


@contextlib.contextmanager
def temp_cfg(**extra):
    """Config whose language state file lives in a throwaway directory."""
    with tempfile.TemporaryDirectory(prefix="ova_lang_") as tmp:
        cfg = {
            "dialogue_lang": ova_lang.DEFAULT_LANG,
            "dialogue_lang_file": str(Path(tmp) / "ova_lang.state"),
        }
        cfg.update(extra)
        yield cfg


@contextlib.contextmanager
def capture_logs():
    """Collect log messages of every ova logger."""
    messages: list[str] = []

    class _Handler(logging.Handler):
        def emit(self, record):  # noqa: D102 - logging API
            messages.append(record.getMessage())

    handler = _Handler()
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield messages
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


# --- normalise ---------------------------------------------------------------

def test_normalise_accepts_every_accepted_spelling():
    assert ova_lang.normalise("zh") == "zh"
    assert ova_lang.normalise("en") == "en"
    assert ova_lang.normalise("cn") == "zh"
    assert ova_lang.normalise("eng") == "en"
    assert ova_lang.normalise("chinese") == "zh"
    assert ova_lang.normalise("english") == "en"
    assert ova_lang.normalise("中文") == "zh"
    assert ova_lang.normalise("英文") == "en"
    # 大小写与空白都忽略
    assert ova_lang.normalise("  ENGLISH \n") == "en"
    assert ova_lang.normalise("Chinese") == "zh"
    assert ova_lang.normalise(" ZH ") == "zh"


def test_normalise_rejects_anything_else():
    for value in (None, "", "  ", "jp", "auto", "zh-en", "français", 3, True):
        assert ova_lang.normalise(value) is None, value


# --- current / state file ----------------------------------------------------

def test_current_defaults_to_config_without_a_state_file():
    with temp_cfg() as cfg:
        assert ova_lang.current(cfg) == "zh"
    with temp_cfg(dialogue_lang="en") as cfg:
        assert ova_lang.current(cfg) == "en"      # 没切换过时按配置默认值
    with temp_cfg(dialogue_lang="klingon") as cfg:
        assert ova_lang.current(cfg) == "zh"      # 配置值非法 → 内置默认


def test_a_cfg_without_the_settings_never_reads_the_disk():
    """裸 cfg（工具/测试）不带状态文件，也不受 /tmp 里遗留文件影响。"""
    assert ova_lang.current({}) == "zh"
    assert ova_lang.chosen({}) is None


def test_set_lang_writes_the_state_file_and_current_follows():
    with temp_cfg() as cfg:
        assert ova_lang.set_lang(cfg, "en") == ("zh", "en")
        assert ova_lang.state_path(cfg).read_text(encoding="utf-8").strip() == "en"
        assert ova_lang.current(cfg) == "en"
        # 别名写法同样落盘成规范值
        assert ova_lang.set_lang(cfg, " 中文 ") == ("en", "zh")
        assert ova_lang.current(cfg) == "zh"


def test_set_lang_rejects_an_unknown_value():
    with temp_cfg() as cfg:
        for value in ("jp", "", None):
            try:
                ova_lang.set_lang(cfg, value)
            except ValueError as exc:
                assert "unsupported lang" in str(exc)
            else:  # pragma: no cover - defensive
                raise AssertionError(f"expected ValueError for {value!r}")
        assert ova_lang.current(cfg) == "zh"      # 状态文件没被写脏
        assert not ova_lang.state_path(cfg).exists()


def test_set_lang_creates_the_parent_directory_and_logs():
    with tempfile.TemporaryDirectory(prefix="ova_lang_") as tmp:
        cfg = {"dialogue_lang_file": str(Path(tmp) / "sub" / "ova_lang.state")}
        with capture_logs() as logs:
            assert ova_lang.set_lang(cfg, "en") == ("zh", "en")
        assert ova_lang.current(cfg) == "en"
        assert any("LANG_SET previous=zh lang=en" in msg for msg in logs)


def test_corrupt_or_unwritable_state_never_raises():
    with temp_cfg() as cfg:
        path = ova_lang.state_path(cfg)
        # 1. 内容非法：回退到配置默认值
        path.write_text("?? gibberish ??\n", encoding="utf-8")
        assert ova_lang.current(cfg) == "zh"
        assert ova_lang.chosen(cfg) is None       # 没人真正切换过
        # 2. 路径是个目录：读不出来也不能抛
        path.unlink()
        path.mkdir()
        assert ova_lang.current(cfg) == "zh"
        # 3. 写不进去（父目录是个普通文件）：只记 warning，返回值照给
        bad = Path(str(path.parent / "afile") + "/ova_lang.state")
        bad.parent.write_text("not a directory", encoding="utf-8")
        with capture_logs() as logs:
            assert ova_lang.set_lang({"dialogue_lang_file": str(bad)}, "en") == \
                ("zh", "en")
        assert any("LANG_STATE_ERROR" in msg for msg in logs)
        assert ova_lang.current({"dialogue_lang_file": str(bad)}) == "zh"


def test_toggle_round_trip():
    with temp_cfg() as cfg:
        assert ova_lang.toggle(cfg) == ("zh", "en")
        assert ova_lang.current(cfg) == "en"
        assert ova_lang.toggle(cfg) == ("en", "zh")
        assert ova_lang.current(cfg) == "zh"
        # 配置默认英文时，第一次 toggle 也是中英互切
        with temp_cfg(dialogue_lang="en") as cfg_en:
            assert ova_lang.toggle(cfg_en) == ("en", "zh")


# --- chosen ------------------------------------------------------------------

def test_chosen_is_none_until_somebody_picks_a_language():
    """讲解选版的"是否有人切换过"判据：默认配置不算切换。"""
    with temp_cfg() as cfg:                       # 默认 zh，没切换
        assert ova_lang.chosen(cfg) is None
    with temp_cfg(dialogue_lang="en") as cfg:     # 部署时特意配成英文
        assert ova_lang.chosen(cfg) == "en"
    with temp_cfg() as cfg:                       # 现场切换过（有状态文件）
        ova_lang.set_lang(cfg, "en")
        assert ova_lang.chosen(cfg) == "en"
        ova_lang.set_lang(cfg, "zh")              # 又切回中文，也算显式选择
        assert ova_lang.chosen(cfg) == "zh"


# --- config wiring -----------------------------------------------------------

def test_defaults_and_env_vars_are_wired():
    old = {k: os.environ.get(k) for k in
           ("WAKE_DIALOGUE_LANG", "WAKE_DIALOGUE_LANG_FILE")}
    os.environ["WAKE_DIALOGUE_LANG"] = "en"
    os.environ["WAKE_DIALOGUE_LANG_FILE"] = "/tmp/somewhere_else.state"
    try:
        cfg = wake.resolve_config(argparse.Namespace(config=None))
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    assert cfg["dialogue_lang"] == "en"
    assert cfg["dialogue_lang_file"] == "/tmp/somewhere_else.state"
    assert wake.DEFAULTS["dialogue_lang"] == "zh"
    assert wake.DEFAULTS["dialogue_lang_file"] == "/tmp/ova_lang.state"
    assert wake.ENV_MAP["dialogue_lang"] == "WAKE_DIALOGUE_LANG"
    assert wake.ENV_MAP["dialogue_lang_file"] == "WAKE_DIALOGUE_LANG_FILE"


# --- direct runner (no pytest required) -------------------------------------

def _main() -> int:
    import traceback

    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception:  # noqa: BLE001 - report and keep going
            failed += 1
            print(f"FAIL: {test.__name__}")
            traceback.print_exc()
        else:
            print(f"PASS: {test.__name__}")
    print(f"\n{'FAILED' if failed else 'OK'}: {len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_main())
"""Command-line entry points for the Online Voice Assistant.

Usage (from the project root, or after ``pip install -e .``):

    python -m ova wake        [wake-word listener args, e.g. --threshold 0.2]
    python -m ova console     [--port 8080]
    python -m ova calibrate   [--record 30 | --analyze FILE]
"""

import argparse
import sys


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("wake", "console", "calibrate"):
        cmd, rest = argv[0], argv[1:]
    else:
        cmd, rest = ("wake", argv)  # backward compatible: bare args = wake
    if cmd == "wake":
        from ova import wake
        return wake.main(rest)
    if cmd == "console":
        from ova import console
        return console.console_main(rest)
    if cmd == "calibrate":
        from ova import calibrate
        return calibrate.calibrate_main(rest)
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

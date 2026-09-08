#!/usr/bin/env python3
"""
Run every design in this repo end to end.

    python run_all.py            # run all 8
    python run_all.py chess atm  # run the ones whose folder matches

This is the repo's smoke test: if a file stops running, this fails loudly.
Every design is self-contained and has a `_demo()` in a `__main__` block, so
"does it still work?" is just "does it still run?".
"""

import argparse
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent

DESIGNS = [
    ("01_parking_lot", "parking_lot.py"),
    ("02_atm", "atm.py"),
    ("03_amazon_locker", "amazon_locker.py"),
    ("04_logger_system", "logger_system.py"),
    ("05_rate_limiter", "rate_limiter.py"),
    ("06_splitwise", "splitwise.py"),
    ("07_book_my_show", "book_my_show.py"),
    ("08_chess_game", "chess_game.py"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("filters", nargs="*", help="substrings to match folders")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="only print pass/fail, not the demo output")
    args = parser.parse_args()

    selected = [
        (folder, script)
        for folder, script in DESIGNS
        if not args.filters or any(f.lower() in folder for f in args.filters)
    ]
    if not selected:
        print(f"nothing matched {args.filters}")
        return 1

    failures = []
    for folder, script in selected:
        path = ROOT / folder / script
        print(f"\n{'#' * 72}\n#  {folder}/{script}\n{'#' * 72}")
        result = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
        )
        if not args.quiet:
            print(result.stdout, end="")
        if result.returncode != 0:
            failures.append(folder)
            print(result.stderr, file=sys.stderr)
            print(f"  FAILED (exit {result.returncode})")
        else:
            print(f"  -> {folder} OK")

    print(f"\n{'=' * 72}")
    print(f"{len(selected) - len(failures)}/{len(selected)} designs ran cleanly")
    if failures:
        print(f"FAILED: {', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

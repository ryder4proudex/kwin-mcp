#!/usr/bin/env python3
"""Reject CJK characters in committed files (outside allowed paths) and commit messages.

Usage:
  cjk.py --base DIR --list FILE [--allow FILE]   # FILE: NUL-separated repo paths
  cjk.py --message FILE                          # commit-msg hook
"""

import argparse
import fnmatch
import re
import sys
from pathlib import Path

# Ranges are escapes on purpose: this file must pass its own gate.
CJK = re.compile(
    "["
    r"\u2e80-\u2fdf"  # CJK radicals, Kangxi radicals
    r"\u2ff0-\u303f"  # ideographic description, CJK symbols and punctuation
    r"\u3040-\u31ff"  # kana, bopomofo, Hangul compatibility jamo, kanbun, CJK strokes
    r"\u3200-\u33ff"  # enclosed CJK letters, CJK compatibility
    r"\u3400-\u4dbf"  # CJK extension A
    r"\u4e00-\u9fff"  # CJK unified ideographs
    r"\ua960-\ua97f"  # Hangul jamo extended A
    r"\uac00-\ud7ff"  # Hangul syllables, jamo extended B
    r"\uf900-\ufaff"  # CJK compatibility ideographs
    r"\ufe30-\ufe4f"  # CJK compatibility forms
    r"\uff00-\uffef"  # fullwidth and halfwidth forms
    r"\U00020000-\U0003ffff"  # CJK extensions B and later
    "]"
)
MAX_REPORTS = 20


def allowed_patterns(path):
    if not path or not Path(path).is_file():
        return []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]


def scan(text):
    for number, line in enumerate(text.splitlines(), 1):
        match = CJK.search(line)
        if match:
            yield number, match.start() + 1, len(CJK.findall(line))


def check_files(base, paths, patterns):
    problems = []
    for rel in paths:
        if any(fnmatch.fnmatch(rel, p) for p in patterns):
            continue
        file = Path(base, rel)
        if file.is_symlink() or not file.is_file():
            continue
        data = file.read_bytes()
        if b"\0" in data[:8192]:
            continue  # binary
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            problems.append(f"{rel}: not valid UTF-8 text")
            continue
        problems += [
            f"{rel}:{n}:{col}: {count} CJK char(s)" for n, col, count in scan(text)
        ]
    return problems


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base", default=".")
    parser.add_argument("--list")
    parser.add_argument("--allow")
    parser.add_argument("--message")
    args = parser.parse_args()
    if args.message:
        text = Path(args.message).read_text(encoding="utf-8", errors="replace")
        body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
        problems = [
            f"commit message:{n}:{col}: {count} CJK char(s)"
            for n, col, count in scan(body)
        ]
        hint = "Write the commit message in English."
    else:
        raw = Path(args.list).read_bytes() if args.list else b""
        paths = [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]
        problems = check_files(args.base, paths, allowed_patterns(args.allow))
        hint = (
            "Move user-facing CJK text into i18n/locale files (allowed by .gates/cjk-allow), "
            "or add a glob there for a repository dedicated to CJK documents."
        )
    if not problems:
        return 0
    for line in problems[:MAX_REPORTS]:
        print(line, file=sys.stderr)
    if len(problems) > MAX_REPORTS:
        print(f"... and {len(problems) - MAX_REPORTS} more", file=sys.stderr)
    print(f"CJK gate: {len(problems)} problem(s). {hint}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

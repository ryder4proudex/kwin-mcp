#!/usr/bin/env python3
"""Reject files whose token count exceeds the limits in limits.json (tokenu via npx).

Usage: tokens.py --base DIR --list FILE   # FILE: NUL-separated repo paths
"""

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

TOKENU = "tokenu@1.1.0"
POLICY = Path(__file__).resolve().with_name("limits.json")


def allowed_patterns(path):
    """Globs from an allow file: one per line, "#" comments, fnmatch with "*" crossing "/"."""
    if not path or not Path(path).is_file():
        return []
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]


def classify(rel, policy):
    path = Path(rel)
    if path.name == "SKILL.md":
        return "skill_entry"
    if path.name == "AGENTS.md":
        return "agents"
    if path.suffix.lower() == ".md":
        return "markdown"
    if (
        path.suffix.lower() in policy["code_extensions"]
        or path.name in policy["code_filenames"]
    ):
        return "code"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=".")
    parser.add_argument("--list", required=True)
    parser.add_argument("--allow", help="file of fnmatch globs exempt from the limits")
    args = parser.parse_args()
    policy = json.loads(POLICY.read_text())
    raw = Path(args.list).read_bytes()
    paths = [p.decode("utf-8", "surrogateescape") for p in raw.split(b"\0") if p]
    exempt = allowed_patterns(args.allow)
    paths = [p for p in paths if not any(fnmatch.fnmatch(p, g) for g in exempt)]
    kinds = {rel: classify(rel, policy) for rel in paths}
    targets = [
        rel for rel, kind in kinds.items() if kind and Path(args.base, rel).is_file()
    ]
    if not targets:
        return 0
    command = [
        "npx",
        "--yes",
        TOKENU,
        "--all",
        "--json",
        "--encoding",
        policy["encoding"],
        *targets,
    ]
    try:
        result = subprocess.run(
            command, cwd=args.base, check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as error:
        print(
            f"token gate: could not run tokenu (needs Node.js/npx): {error}",
            file=sys.stderr,
        )
        return 1
    over = []
    for entry in json.loads(result.stdout)["results"]:
        if entry["type"] != "file":
            continue
        rel = str(Path(entry["path"]))
        limit = policy["limits"][kinds.get(rel) or classify(rel, policy)]
        if entry["tokens"] > limit:
            over.append(f"{rel}: {entry['tokens']} tokens > limit {limit}")
    if not over:
        return 0
    for line in over:
        print(line, file=sys.stderr)
    print(
        f"Token gate: {len(over)} file(s) too large. Split each one into smaller cohesive "
        "modules or documents (link them from the original); do not truncate content.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

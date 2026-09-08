#!/usr/bin/env python3
"""Bump cache-bust versions on static asset references in index.html.

Usage:
    scripts/bump_static_versions.py styles.css app.js notes.js ...
    scripts/bump_static_versions.py --all   # bump every referenced asset

Each file gets a version string of the form YYYYMMDD-<slug>-N where N increments
each time the same file is bumped on the same day. Date uses local time.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "static" / "index.html"

_REF_RE = re.compile(
    r'(href="|/static/)(?P<path>[A-Za-z0-9_.-]+\.(?:css|js))(?:\?v=(?P<ver>[^"]*))?',
)


def _slug(filename: str) -> str:
    name = filename.rsplit(".", 1)[0]
    return name.replace("_", "-")


def _today() -> str:
    return _dt.date.today().strftime("%Y%m%d")


def _bump(filename: str, current: str | None) -> str:
    today = _today()
    slug = _slug(filename)
    counter = 1
    if current:
        parts = current.split("-")
        if len(parts) >= 3 and parts[0] == today and parts[-2] == slug:
            try:
                counter = int(parts[-1]) + 1
            except ValueError:
                counter = 1
    return f"{today}-{slug}-{counter}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="*", help="filenames to bump")
    parser.add_argument("--all", action="store_true", help="bump every referenced asset")
    args = parser.parse_args()

    if not INDEX.exists():
        print(f"index.html not found: {INDEX}", file=sys.stderr)
        return 1

    text = INDEX.read_text(encoding="utf-8")

    referenced = set()
    for match in _REF_RE.finditer(text):
        referenced.add(match.group("path"))

    if args.all:
        targets = sorted(referenced)
    else:
        targets = [f for f in args.files if f in referenced]
        if not targets:
            print("No matching assets referenced in index.html", file=sys.stderr)
            return 1

    new_text = text
    bumped = []
    for filename in targets:
        current = None
        for match in _REF_RE.finditer(text):
            if match.group("path") == filename:
                current = match.group("ver")
                break
        new_ver = _bump(filename, current)
        pattern = re.compile(
            r'((?:href="|/static/)' + re.escape(filename) + r')(?:\?v=[^"]*)?',
        )
        new_text, n = pattern.subn(rf'\1?v={new_ver}', new_text)
        if n:
            bumped.append((filename, current, new_ver))

    if bumped:
        INDEX.write_text(new_text, encoding="utf-8")
    for filename, old, new in bumped:
        old_str = old or "(none)"
        print(f"  {filename:24} {old_str:35} -> {new}")
    if not bumped:
        print("Nothing to bump")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""从 CHANGELOG.md 抽出某个版本的 ``##`` 节, 作为 GitHub Release 正文.

用法::

    uv run python scripts/changelog.py extract 0.5.0
    uv run python scripts/changelog.py extract v0.5.0 --file CHANGELOG.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CHANGELOG = ROOT / "CHANGELOG.md"

# ``## v0.5.0`` / ``## [0.5.0] - 2026-08-27`` / ``## 0.5.0``
_H2 = re.compile(r"^##[ \t]+(\S.*?)\s*$")
_VERSION_TITLE = re.compile(r"^\[?v?(\d+\.\d+\.\d+(?:-custom\.\d+)?)\]?(?:\s+.*)?$", re.IGNORECASE)


class ChangelogError(Exception):
    """用户可见的失败, CLI 打印到 stderr 并以 1 退出."""


def normalize_version(raw: str) -> str:
    text = raw.strip()
    if text.startswith(("v", "V")):
        text = text[1:]
    return text


def heading_version(title: str) -> str | None:
    match = _VERSION_TITLE.match(title.strip())
    if match is None:
        return None
    return match.group(1)


def split_h2_sections(text: str) -> list[tuple[str, str]]:
    """每项为 ``(标题不含 ##, 整节含标题行)``."""
    sections: list[tuple[str, str]] = []
    current_title: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal current_title, buf
        if current_title is not None:
            sections.append((current_title, "".join(buf)))
        current_title = None
        buf = []

    for line in text.splitlines(keepends=True):
        match = _H2.match(line.rstrip("\r\n"))
        if match is not None:
            flush()
            current_title = match.group(1).strip()
            buf = [line]
        elif current_title is not None:
            buf.append(line)
    flush()
    return sections


def extract_section(text: str, version: str) -> str:
    want = normalize_version(version)
    if not want:
        raise ChangelogError("版本号为空")
    for title, section in split_h2_sections(text):
        if heading_version(title) != want:
            continue
        stripped = section.strip()
        rest = stripped.split("\n", 1)
        if len(rest) < 2 or not rest[1].strip():
            raise ChangelogError(f"CHANGELOG 中 v{want} 节为空")
        return stripped + "\n"
    raise ChangelogError(f"CHANGELOG 中没有 v{want} 节")


def extract_file(path: Path, version: str) -> str:
    if not path.is_file():
        raise ChangelogError(f"缺少 {path.name}")
    return extract_section(path.read_text(encoding="utf-8"), version)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    extract = sub.add_parser("extract", help="打印指定版本的 CHANGELOG 节")
    extract.add_argument("version", help="0.5.0 或 v0.5.0")
    extract.add_argument("--file", type=Path, default=DEFAULT_CHANGELOG, help="CHANGELOG 路径")

    args = parser.parse_args(argv)
    try:
        if args.cmd == "extract":
            sys.stdout.write(extract_file(args.file, args.version))
        else:
            raise ChangelogError(f"未知命令: {args.cmd}")
    except ChangelogError as e:
        print(e, file=sys.stderr)  # noqa: T201
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Fail when a Markdown file links to a repository file or heading that does not exist.

Usage: python tools/ci/check_links.py [ROOT]
Only relative links are checked; web links are left to the reader.
"""

from __future__ import annotations

import re
import subprocess
import sys
import unicodedata
from pathlib import Path

LINK = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")
# Broken in upstream Handy itself; left alone so upstream files merge cleanly.
KNOWN_UPSTREAM = {("README.md", "#3-configure-the-webkit-renderer")}


def slug(text: str) -> str:
    """GitHub's heading anchor: lower case, punctuation removed, spaces to hyphens."""
    text = re.sub(r"<[^>]+>|`", "", text).strip().lower()
    out = []
    for ch in text:
        if ch in " -":
            out.append("-")
        elif ch == "_" or unicodedata.category(ch)[0] in "LNM":
            out.append(ch)
    return "".join(out)


def lines_outside_code(path: Path) -> list[str]:
    keep, fenced = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            fenced = not fenced
            keep.append("")
            continue
        keep.append("" if fenced else line)
    return keep


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    found = set()
    for line in lines_outside_code(path):
        m = HEADING.match(line)
        if not m:
            continue
        base = slug(m.group(1))
        n = seen.get(base, 0)
        seen[base] = n + 1
        found.add(base if n == 0 else f"{base}-{n}")
    found.update(re.findall(r'<a\s+(?:name|id)="([^"]+)"', path.read_text(encoding="utf-8")))
    return found


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    files = subprocess.run(
        ["git", "ls-files", "*.md"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.split()
    cache: dict[Path, set[str]] = {}
    errors = 0
    for name in files:
        md = root / name
        for no, line in enumerate(lines_outside_code(md), 1):
            for m in LINK.finditer(re.sub(r"`[^`]*`", "", line)):
                target = m.group(1).strip("<>")
                # Web links, and site-root paths the app resolves itself (release notes).
                if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("/"):
                    continue
                if (name, target) in KNOWN_UPSTREAM:
                    continue
                path_part, _, frag = target.partition("#")
                dest = (md.parent / path_part).resolve() if path_part else md
                if not dest.exists():
                    print(f"{name}:{no}: missing file {path_part}")
                    errors += 1
                    continue
                if frag and dest.suffix.lower() == ".md":
                    if dest not in cache:
                        cache[dest] = anchors(dest)
                    if frag.lower() not in cache[dest]:
                        print(f"{name}:{no}: missing heading #{frag} in {dest.relative_to(root).as_posix()}")
                        errors += 1
    print(f"checked {len(files)} Markdown files, {errors} broken link(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

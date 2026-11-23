"""Fail when tracked files (or, with --history, any commit) contain private details.

Usage: python tools/ci/scrub_check.py [--history [--base REF] | --files DIR] [ROOT]

Patterns are case-insensitive regular expressions. A few generic ones are built in; the
maintainer's own names, hosts and proxies are supplied one per line in the SCRUB_PATTERNS
environment variable (a repository secret), so the public tree never spells them out.
Matches are reported by file and line only, never by content, because CI logs are public.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path

GENERIC = [
    r"OneDrive - ",  # synced business folders
    r"session-state[\\/]",  # agent session folders
    r"[A-Z]:[\\/]Users[\\/](?!<|%|\$|\{|you\b|user\b|name\b|me\b|Public\b|Default\b|runneradmin\b)[A-Za-z0-9._-]+[\\/]",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\bgh[pousr]_[A-Za-z0-9]{36,}\b",  # GitHub tokens
    r"\bAKIA[0-9A-Z]{16}\b",  # AWS access keys
]
SELF = "tools/ci/scrub_check.py"


def patterns() -> list[re.Pattern[str]]:
    extra = [p.strip() for p in os.environ.get("SCRUB_PATTERNS", "").splitlines()]
    return [re.compile(p, re.I) for p in GENERIC + [p for p in extra if p and not p.startswith("#")]]


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout


def scan_data(name: str, data: bytes, pats: list[re.Pattern[str]]) -> list[str]:
    hits = []
    if any(p.search(name) for p in pats):
        hits.append(f"{name}: file name matches")
    if b"\0" in data[:8192]:
        texts = [data.decode("latin-1"), data.decode("utf-16-le", errors="ignore"),
                 data[1:].decode("utf-16-le", errors="ignore")]
        if any(p.search(text) for p in pats for text in texts):
            hits.append(f"{name}: binary content matches")
    else:
        for no, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if any(p.search(line) for p in pats):
                hits.append(f"{name}:{no}")
    if name.lower().endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for entry in archive.infolist():
                if not entry.is_dir():
                    hits.extend(scan_data(f"{name}!{entry.filename}", archive.read(entry), pats))
    return hits


def scan_tree(root: Path, pats: list[re.Pattern[str]], files: Path | None = None) -> list[str]:
    hits = []
    names = (str(p.relative_to(root)) for p in files.rglob("*") if p.is_file()) if files else git(root, "ls-files", "-z").split("\0")
    for name in names:
        if not name or name == SELF:
            continue
        path = root / name
        if path.is_symlink() or not path.is_file():
            continue
        hits.extend(scan_data(name, path.read_bytes(), pats))
    return hits


def scan_history(root: Path, pats: list[re.Pattern[str]], base: str | None = None) -> list[str]:
    hits = []
    revision = f"{base}..HEAD" if base else "HEAD"
    log = git(root, "log", "-p", "--no-color", "--no-ext-diff", "--text", "--format=@@commit %H%n%an <%ae>%n%cn <%ce>%n%B", revision, "--")
    commit, file, in_diff = "", "", False
    for line in log.splitlines():
        if line.startswith("@@commit "):
            commit, file, in_diff = line[9:21], "(message)", False
            continue
        if line.startswith("diff --git "):
            in_diff, file = True, line.rsplit(" b/", 1)[-1]
            continue
        if in_diff:
            if not line.startswith(("+", "-")) or line.startswith(("+++ ", "--- ")):
                continue
        if file == SELF:
            continue
        if any(p.search(line) for p in pats):
            hits.append(f"{commit} {file}")
    return sorted(set(hits))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--history", action="store_true")
    mode.add_argument("--files", type=Path)
    parser.add_argument("--base")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    files = args.files.resolve() if args.files else None
    if files:
        if not files.is_dir():
            parser.error("--files must be an existing directory")
        root = files
    pats = patterns()
    hits = scan_history(root, pats, args.base) if args.history else scan_tree(root, pats, files)
    for h in hits:
        print(h)
    print(f"{len(pats)} patterns ({len(pats) - len(GENERIC)} private), {len(hits)} match(es)")
    if len(pats) == len(GENERIC):
        print("note: SCRUB_PATTERNS is empty; only the generic patterns were checked")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())

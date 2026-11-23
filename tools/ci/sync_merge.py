"""Helpers for upstream-sync.yml: finish an upstream merge without a maintainer.

resolve  Resolves each conflict hunk (diff3 style) in the given files with a GitHub Models chat model.
         Every hunk is sent with its surrounding lines; the reply replaces the hunk. Files that cannot be
         resolved (no hunk markers, oversized hunk, model error, markers left in the reply) are listed
         and the command exits 1, so the workflow falls back to a conflict issue.
locales  Adds keys that exist in the English UI strings but are missing from another locale, with the
         English text, which is the fork's convention for strings it has not translated yet. Upstream
         often adds a new locale that has none of the fork's keys, and check:translations would fail.

The merged result is never trusted on its own: the sync workflow runs the fork's full CI on it and
only merges and releases when every check passes.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://models.github.ai/inference/chat/completions"
CONTEXT_LINES = 40
# The free GitHub Models tier accepts about 8000 input tokens per request; keep a margin.
MAX_PROMPT_CHARS = 20000

SYSTEM = (
    "You resolve git merge conflicts in Handy Remote, a fork of the Handy speech-to-text app. "
    "OURS is the fork, THEIRS is a new upstream Handy release, BASE is their common ancestor. "
    "Keep every fork feature (remote recognition, caret paste, refine, cloud providers, updater, "
    "settings it added) and take every upstream change (fixes, new settings, renames, refactors). "
    "When both sides changed the same lines, combine them so the code compiles and both behaviours "
    "remain; adapt fork code to upstream renames. Never add explanations, comments about the merge, "
    "or conflict markers. Reply with only the resolved lines that replace the conflict, inside one "
    "fenced block that starts with ```resolved and ends with ```."
)


class Hunk:
    def __init__(self, start, end, ours, base, theirs):
        self.start, self.end = start, end  # line indexes of <<<<<<< and >>>>>>>
        self.ours, self.base, self.theirs = ours, base, theirs


def parse_hunks(lines):
    hunks, i = [], 0
    while i < len(lines):
        if not lines[i].startswith("<<<<<<< "):
            i += 1
            continue
        start, part, ours, base, theirs = i, "ours", [], [], []
        i += 1
        while i < len(lines) and not lines[i].startswith(">>>>>>> "):
            line = lines[i]
            if line.startswith("||||||| ") and part == "ours":
                part = "base"
            elif line.rstrip("\r\n") == "=======" and part in ("ours", "base"):
                part = "theirs"
            else:
                {"ours": ours, "base": base, "theirs": theirs}[part].append(line)
            i += 1
        if i >= len(lines) or part != "theirs":
            raise ValueError(f"unterminated conflict at line {start + 1}")
        hunks.append(Hunk(start, i, ours, base, theirs))
        i += 1
    return hunks


def build_prompt(path, lines, hunk):
    before = "".join(lines[max(0, hunk.start - CONTEXT_LINES) : hunk.start])
    after = "".join(lines[hunk.end + 1 : hunk.end + 1 + CONTEXT_LINES])
    return (
        f"File: {path}\n\nLines before the conflict:\n```\n{before}```\n\n"
        f"OURS (fork):\n```\n{''.join(hunk.ours)}```\n\n"
        f"BASE:\n```\n{''.join(hunk.base)}```\n\n"
        f"THEIRS (upstream):\n```\n{''.join(hunk.theirs)}```\n\n"
        f"Lines after the conflict:\n```\n{after}```\n"
    )


def extract_resolution(reply):
    match = re.search(r"```resolved[^\n]*\n(.*?)```", reply, re.S)
    if not match:
        raise ValueError("reply has no ```resolved block")
    text = match.group(1)
    if re.search(r"^(<<<<<<<|=======$|>>>>>>>|\|\|\|\|\|\|\|)", text, re.M):
        raise ValueError("reply still contains conflict markers")
    return text


def resolve_text(path, text, ask):
    lines = text.splitlines(keepends=True)
    hunks = parse_hunks(lines)
    if not hunks:
        raise ValueError("no conflict hunks (binary, deleted or renamed file)")
    newline = "\r\n" if "\r\n" in text else "\n"
    for hunk in reversed(hunks):
        prompt = build_prompt(path, lines, hunk)
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ValueError(f"conflict at line {hunk.start + 1} is too large for the model")
        resolved = extract_resolution(ask(prompt))
        if resolved and not resolved.endswith("\n"):
            resolved += "\n"
        if newline == "\r\n":
            resolved = re.sub(r"(?<!\r)\n", "\r\n", resolved)
        lines[hunk.start : hunk.end + 1] = resolved.splitlines(keepends=True)
    return "".join(lines), len(hunks)


def github_models(model, token):
    def ask(prompt):
        body = json.dumps(
            {
                "model": model,
                "temperature": 0,
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            }
        ).encode()
        for attempt in range(4):
            request = urllib.request.Request(
                ENDPOINT,
                data=body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    return json.load(response)["choices"][0]["message"]["content"]
            except urllib.error.HTTPError as error:
                if error.code not in (429, 500, 502, 503) or attempt == 3:
                    raise
                time.sleep(int(error.headers.get("Retry-After") or 30))
        raise RuntimeError("unreachable")

    return ask


def cmd_resolve(args):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is not set", file=sys.stderr)
        return 1
    ask = github_models(args.model, token)
    failed = []
    for name in args.files:
        path = Path(name)
        try:
            text = path.read_text(encoding="utf-8")
            merged, count = resolve_text(name, text, ask)
            path.write_text(merged, encoding="utf-8", newline="")
            print(f"resolved {name}: {count} conflict(s)")
        except Exception as error:  # report every file, then fail once
            print(f"::warning::could not resolve {name}: {error}")
            failed.append(name)
    if failed:
        print("unresolved: " + " ".join(failed))
        return 1
    return 0


def fill_missing(reference, target):
    added = 0
    for key, value in reference.items():
        if key not in target:
            target[key] = value
            added += 1 if not isinstance(value, dict) else sum(1 for _ in _leaves(value))
        elif isinstance(value, dict) and isinstance(target[key], dict):
            added += fill_missing(value, target[key])
    return added


def _leaves(tree):
    for value in tree.values():
        if isinstance(value, dict):
            yield from _leaves(value)
        else:
            yield value


def cmd_locales(args):
    root = Path(args.dir)
    reference = json.loads((root / "en" / "translation.json").read_text(encoding="utf-8"))
    for path in sorted(root.glob("*/translation.json")):
        if path.parent.name == "en":
            continue
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        added = fill_missing(reference, data)
        if added:
            indent = 4 if re.search(r'^\{\r?\n    "', raw) else 2
            out = json.dumps(data, ensure_ascii=False, indent=indent) + "\n"
            if "\r\n" in raw:
                out = out.replace("\n", "\r\n")
            path.write_text(out, encoding="utf-8", newline="")
            print(f"{path.parent.name}: added {added} English fallback string(s)")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    resolve = sub.add_parser("resolve")
    resolve.add_argument("--model", default=os.environ.get("SYNC_AI_MODEL") or "openai/gpt-4.1")
    resolve.add_argument("files", nargs="+")
    locales = sub.add_parser("locales")
    locales.add_argument("--dir", default="src/i18n/locales")
    args = parser.parse_args(argv)
    return cmd_resolve(args) if args.command == "resolve" else cmd_locales(args)


if __name__ == "__main__":
    sys.exit(main())

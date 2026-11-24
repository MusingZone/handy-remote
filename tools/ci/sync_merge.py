"""Helpers for upstream-sync.yml: finish an upstream merge without a maintainer.

resolve  Resolves each conflict hunk (diff3 style) in the given files. Independent JSON object-key
         conflicts (such as upstream bumping version next to fork identity keys in tauri.conf.json)
         are merged deterministically; every other hunk is sent with surrounding context to an
         OpenAI-compatible chat model (GitHub Models by default, or SYNC_AI_ENDPOINT). Files that
         cannot be resolved are listed with safe diagnostics and the command exits 1, so the
         workflow reports an issue and fails.
locales  Adds keys that exist in the English UI strings but are missing from another locale, with the
         English text, which is the fork's convention for strings it has not translated yet. Upstream
         often adds a new locale that has none of the fork's keys, and check:translations would fail.

The merged result is never trusted on its own: the sync workflow runs the fork's full CI on it and
only merges and releases when every check passes.
"""

import argparse
from datetime import datetime, timezone
import email.utils
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINT = "https://models.github.ai/inference/chat/completions"
GITHUB_MODELS_HOST = "models.github.ai"
GITHUB_API_VERSION = "2022-11-28"
USER_AGENT = "handy-remote-upstream-sync"
DEFAULT_MODEL = "openai/gpt-4.1"
CONTEXT_LINES = 40
# The free GitHub Models tier accepts about 8000 input tokens per request; keep a margin.
MAX_PROMPT_CHARS = 20000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 64 * 1024
RETRYABLE_STATUSES = (429, 500, 502, 503, 504)
REDIRECT_STATUSES = (301, 302, 303, 307, 308)

_SECRET_PATTERNS = (
    re.compile(r"\bBearer\s+\S+", re.I),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]+\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]+\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9:._-]{1,80}$")

SYSTEM = (
    "You resolve git merge conflicts in Handy Remote, a fork of the Handy speech-to-text app. "
    "OURS is the fork, THEIRS is a new upstream Handy release, BASE is their common ancestor. "
    "Keep every fork feature (remote recognition, caret paste, refine, cloud providers, updater, "
    "settings it added) and take every upstream change (fixes, new settings, renames, refactors). "
    "In src-tauri/tauri.conf.json, keep the fork's productName, identifier, window title, and "
    "updater pubkey/endpoints, and take upstream's version in source (release builds override it). "
    "When both sides changed the same lines, combine them so the code compiles and both behaviours "
    "remain; adapt fork code to upstream renames. Never add explanations, comments about the merge, "
    "or conflict markers. Reply with only the resolved lines that replace the conflict, inside one "
    "fenced block that starts with ```resolved and ends with ```."
)


class AIRequestError(RuntimeError):
    """Safe, diagnostic error raised when the AI inference request fails."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse HTTP redirects so POST requests never become GET or leak Authorization."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Hunk:
    def __init__(self, start, end, ours, base, theirs, has_base=True):
        self.start, self.end = start, end  # line indexes of <<<<<<< and >>>>>>>
        self.ours, self.base, self.theirs = ours, base, theirs
        self.has_base = has_base


def parse_hunks(lines):
    hunks, i = [], 0
    while i < len(lines):
        if not lines[i].startswith("<<<<<<< "):
            i += 1
            continue
        start, part, ours, base, theirs, has_base = i, "ours", [], [], [], False
        i += 1
        while i < len(lines) and not lines[i].startswith(">>>>>>> "):
            line = lines[i]
            if line.startswith("||||||| ") and part == "ours":
                part = "base"
                has_base = True
            elif line.rstrip("\r\n") == "=======" and part in ("ours", "base"):
                part = "theirs"
            else:
                {"ours": ours, "base": base, "theirs": theirs}[part].append(line)
            i += 1
        if i >= len(lines) or part != "theirs":
            raise ValueError(f"unterminated conflict at line {start + 1}")
        hunks.append(Hunk(start, i, ours, base, theirs, has_base=has_base))
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
    if not isinstance(reply, str):
        raise ValueError("reply has no ```resolved block")
    match = re.search(r"```resolved[^\n]*\n(.*?)```", reply, re.S)
    if not match:
        raise ValueError("reply has no ```resolved block")
    text = match.group(1)
    if re.search(r"^(<<<<<<<|=======$|>>>>>>>|\|\|\|\|\|\|\|)", text, re.M):
        raise ValueError("reply still contains conflict markers")
    return text


def _parse_json_object_members(block_lines):
    """Parse a diff3 hunk fragment as formatted JSON object members without losing layout."""
    text = "".join(block_lines)
    if not text.strip():
        return {}, False
    stripped = text.rstrip()
    trailing_comma = stripped.endswith(",")
    body = stripped[:-1] if trailing_comma else stripped
    wrapped = "{\n" + body + "\n}"
    decoder = json.JSONDecoder()
    members = {}
    pos = 2
    length = len(wrapped)
    while True:
        while pos < length and wrapped[pos] in " \t\r\n":
            pos += 1
        if pos >= length:
            return None
        if wrapped[pos] == "}":
            break
        line_start = wrapped.rfind("\n", 0, pos) + 1
        if wrapped[line_start:pos].strip():
            return None
        if wrapped[pos] != '"':
            return None
        try:
            key, after_key = decoder.raw_decode(wrapped, pos)
        except json.JSONDecodeError:
            return None
        if not isinstance(key, str) or key in members:
            return None
        p = after_key
        while p < length and wrapped[p] in " \t\r\n":
            p += 1
        if p >= length or wrapped[p] != ":":
            return None
        p += 1
        while p < length and wrapped[p] in " \t\r\n":
            p += 1
        try:
            value, val_end = decoder.raw_decode(wrapped, p)
        except json.JSONDecodeError:
            return None
        members[key] = (wrapped[line_start:val_end], value)
        pos = val_end
        while pos < length and wrapped[pos] in " \t\r\n":
            pos += 1
        if pos >= length:
            return None
        if wrapped[pos] == ",":
            pos += 1
            while pos < length and wrapped[pos] in " \t\r\n":
                pos += 1
            if pos >= length or wrapped[pos] == "}":
                return None
            continue
        if wrapped[pos] == "}":
            break
        return None
    if wrapped[pos + 1 :].strip():
        return None
    return members, trailing_comma


def _merge_key_order(base_keys, ours_keys, theirs_keys):
    order = list(ours_keys)
    ours_set = set(order)
    base_set = set(base_keys)
    theirs_only = [key for key in theirs_keys if key not in base_set and key not in ours_set]
    for key in theirs_only:
        idx = theirs_keys.index(key)
        insert_at = len(order)
        for prev in reversed(theirs_keys[:idx]):
            if prev in order:
                insert_at = order.index(prev) + 1
                break
        order.insert(insert_at, key)
    return order


def try_resolve_json_hunk(path, lines, hunk):
    """Deterministically resolve a diff3 JSON object hunk when OURS and THEIRS changed disjoint keys.

    Returns the replacement text (using LF newlines) or None if the hunk should go to the AI model.
    """
    if Path(path).suffix.lower() != ".json" or not hunk.has_base:
        return None
    before_tail = "".join(lines[: hunk.start]).rstrip()
    after_head = "".join(lines[hunk.end + 1 :]).lstrip()
    if not before_tail or before_tail[-1] not in "{,":
        return None
    if after_head.startswith("}"):
        need_trailing_comma = False
    elif after_head.startswith('"'):
        need_trailing_comma = True
    else:
        return None

    parsed_ours = _parse_json_object_members(hunk.ours)
    parsed_base = _parse_json_object_members(hunk.base)
    parsed_theirs = _parse_json_object_members(hunk.theirs)
    if parsed_ours is None or parsed_base is None or parsed_theirs is None:
        return None

    ours, _ = parsed_ours
    base, _ = parsed_base
    theirs, _ = parsed_theirs

    order = _merge_key_order(list(base), list(ours), list(theirs))
    entries = []
    for key in order:
        in_base = key in base
        in_ours = key in ours
        in_theirs = key in theirs
        if in_base and in_ours and in_theirs:
            ours_raw, ours_val = ours[key]
            _, base_val = base[key]
            theirs_raw, theirs_val = theirs[key]
            if ours_val == theirs_val:
                entries.append(ours_raw)
            elif ours_val == base_val:
                entries.append(theirs_raw)
            elif theirs_val == base_val:
                entries.append(ours_raw)
            else:
                return None
        elif in_base and in_ours and not in_theirs:
            if ours[key][1] != base[key][1]:
                return None
        elif in_base and in_theirs and not in_ours:
            if theirs[key][1] != base[key][1]:
                return None
        elif in_ours and not in_base and not in_theirs:
            entries.append(ours[key][0])
        elif in_theirs and not in_base and not in_ours:
            entries.append(theirs[key][0])
        elif in_ours and in_theirs and not in_base:
            if ours[key][1] == theirs[key][1]:
                entries.append(ours[key][0])
            else:
                return None

    if not entries:
        if before_tail[-1] == "," and not need_trailing_comma:
            return None
        return ""
    joined = ",\n".join(entries)
    if need_trailing_comma:
        joined += ","
    return joined + "\n"


def resolve_text_detailed(path, text, ask):
    lines = text.splitlines(keepends=True)
    hunks = parse_hunks(lines)
    if not hunks:
        raise ValueError("no conflict hunks (binary, deleted or renamed file)")
    newline = "\r\n" if "\r\n" in text else "\n"
    deterministic_count = 0
    ai_count = 0
    for hunk in reversed(hunks):
        resolved = try_resolve_json_hunk(path, lines, hunk)
        if resolved is not None:
            deterministic_count += 1
        else:
            prompt = build_prompt(path, lines, hunk)
            if len(prompt) > MAX_PROMPT_CHARS:
                raise ValueError(f"conflict at line {hunk.start + 1} is too large for the model")
            resolved = extract_resolution(ask(prompt))
            ai_count += 1
        if resolved and not resolved.endswith("\n"):
            resolved += "\n"
        if newline == "\r\n":
            resolved = re.sub(r"(?<!\r)\n", "\r\n", resolved)
        lines[hunk.start : hunk.end + 1] = resolved.splitlines(keepends=True)
    merged = "".join(lines)
    if Path(path).suffix.lower() == ".json":
        try:
            json.loads(merged)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"resolved JSON is invalid at line {error.lineno} column {error.colno}"
            ) from None
    return merged, len(hunks), deterministic_count, ai_count


def resolve_text(path, text, ask):
    merged, count, _, _ = resolve_text_detailed(path, text, ask)
    return merged, count


def _endpoint_label(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("AI endpoint must be a valid http(s) URL")
    if parts.username or parts.password:
        raise ValueError("AI endpoint URL must not contain embedded credentials")
    host = parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"
    return f"{host}{parts.path or '/'}"


def _safe_location(location, base_url):
    if not location:
        return None
    joined = urllib.parse.urljoin(base_url, location.strip())
    parts = urllib.parse.urlsplit(joined)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "(invalid redirect target)"
    host = parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"
    return f"{host}{parts.path or '/'}"


def _sanitize_text(text, token=None, max_len=160):
    cleaned = str(text or "")
    if token:
        cleaned = cleaned.replace(token, "[redacted]")
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    for stop in ("```", "<<<<<<<", "=======", ">>>>>>>"):
        if stop in cleaned:
            cleaned = cleaned.split(stop, 1)[0]
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 3].rstrip() + "..."
    return cleaned


def _extract_request_id(headers):
    if not headers:
        return None
    for name in ("x-github-request-id", "x-request-id", "apim-request-id"):
        value = headers.get(name)
        if value:
            candidate = value.strip()
            if _REQUEST_ID_RE.match(candidate):
                return candidate
    return None


def _extract_content_type(headers):
    if not headers:
        return "(missing)"
    raw = headers.get("Content-Type")
    if not raw:
        return "(missing)"
    media = raw.split(";", 1)[0].strip().lower()
    return media or "(missing)"


def _parse_retry_after(value, default=30, maximum=120):
    if not value:
        return default
    raw = str(value).strip()
    try:
        seconds = float(raw)
        return max(1, min(maximum, int(seconds)))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = int((dt - datetime.now(timezone.utc)).total_seconds())
        return max(1, min(maximum, delta))
    except (TypeError, ValueError, OverflowError):
        return default


def _decode_response_body(raw_bytes, headers):
    encoding = ""
    if headers:
        encoding = (headers.get("Content-Encoding") or "").strip().lower()
    if not encoding or encoding == "identity":
        return raw_bytes
    if encoding == "gzip":
        try:
            data = gzip.decompress(raw_bytes)
        except (OSError, EOFError):
            raise AIRequestError("invalid gzip response body") from None
        if len(data) > MAX_RESPONSE_BYTES:
            raise AIRequestError("decompressed response exceeded size limit")
        return data
    raise AIRequestError(f"unsupported Content-Encoding: {_sanitize_text(encoding, max_len=40)}")


def _extract_safe_json_error(raw_bytes, headers, token=None):
    if not raw_bytes:
        return None
    try:
        decoded = _decode_response_body(raw_bytes, headers)
        payload = json.loads(decoded.decode("utf-8"))
    except (AIRequestError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    parts = []
    err = payload.get("error")
    if isinstance(err, dict):
        for field in ("code", "type"):
            val = err.get(field)
            if isinstance(val, (str, int)) and str(val).strip():
                parts.append(f"{field}={_sanitize_text(val, token=token, max_len=60)}")
        msg = err.get("message")
        if isinstance(msg, str) and msg.strip():
            parts.append(f"message={_sanitize_text(msg, token=token, max_len=160)}")
    elif isinstance(err, str) and err.strip():
        parts.append(f"error={_sanitize_text(err, token=token, max_len=160)}")
    else:
        for field in ("code", "status"):
            val = payload.get(field)
            if isinstance(val, (str, int)) and str(val).strip():
                parts.append(f"{field}={_sanitize_text(val, token=token, max_len=60)}")
        msg = payload.get("message")
        if isinstance(msg, str) and msg.strip():
            parts.append(f"message={_sanitize_text(msg, token=token, max_len=160)}")
    return ", ".join(parts) if parts else None


def _retired_models_hint(endpoint_url):
    if urllib.parse.urlsplit(endpoint_url).hostname == GITHUB_MODELS_HOST:
        return (
            "; GitHub Models (models.github.ai) was retired on 2026-07-30; "
            "set SYNC_AI_ENDPOINT and SYNC_AI_API_KEY to an OpenAI-compatible provider"
        )
    return ""


def request_headers(endpoint_url, token):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": USER_AGENT,
    }
    if urllib.parse.urlsplit(endpoint_url).hostname == GITHUB_MODELS_HOST:
        headers["Accept"] = "application/vnd.github+json"
        headers["X-GitHub-Api-Version"] = GITHUB_API_VERSION
    else:
        headers["Accept"] = "application/json"
    return headers


def resolve_ai_config(model=None, endpoint=None, env=None):
    source = os.environ if env is None else env
    resolved_model = (model or source.get("SYNC_AI_MODEL") or DEFAULT_MODEL).strip()
    resolved_endpoint = (endpoint or source.get("SYNC_AI_ENDPOINT") or ENDPOINT).strip()
    _endpoint_label(resolved_endpoint)
    host = urllib.parse.urlsplit(resolved_endpoint).hostname
    api_key = (source.get("SYNC_AI_API_KEY") or "").strip()
    if api_key:
        token = api_key
    elif host == GITHUB_MODELS_HOST:
        token = (source.get("GITHUB_TOKEN") or "").strip()
        if not token:
            raise ValueError("GITHUB_TOKEN (or SYNC_AI_API_KEY) is not set")
    else:
        raise ValueError("SYNC_AI_API_KEY must be set when using a custom SYNC_AI_ENDPOINT")
    return resolved_model, resolved_endpoint, token


def _extract_choice_content(payload, prefix, token):
    if not isinstance(payload, dict):
        raise AIRequestError(
            f"{prefix}: HTTP 200 unsupported JSON response (expected object, got {type(payload).__name__})"
        )
    if payload.get("error"):
        detail = _extract_safe_json_error(json.dumps(payload).encode("utf-8"), None, token=token)
        raise AIRequestError(f"{prefix}: HTTP 200 API error ({detail or 'unspecified error'})")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise AIRequestError(f"{prefix}: HTTP 200 unsupported response (missing choices)")
    choice = choices[0]
    finish_reason = _sanitize_text(choice.get("finish_reason") or "unknown", max_len=40)
    message = choice.get("message")
    if not isinstance(message, dict):
        raise AIRequestError(
            f"{prefix}: HTTP 200 unsupported response (missing message, finish_reason={finish_reason})"
        )
    content = message.get("content")
    if isinstance(content, list):
        text_parts = [
            part["text"]
            for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        ]
        content = "".join(text_parts)
    if not isinstance(content, str) or not content.strip():
        refusal = "refusal present, " if message.get("refusal") else ""
        raise AIRequestError(
            f"{prefix}: HTTP 200 empty completion content ({refusal}finish_reason={finish_reason})"
        )
    return content


def github_models(model, token, endpoint=None, opener=None, sleep=time.sleep):
    target_endpoint = (endpoint or ENDPOINT).strip()
    label = _endpoint_label(target_endpoint)
    active_opener = opener or urllib.request.build_opener(_NoRedirectHandler())
    prefix = f"AI request failed (model={model}, endpoint={label})"

    def ask(prompt):
        body = json.dumps(
            {
                "model": model,
                "temperature": 0,
                "stream": False,
                "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
            }
        ).encode("utf-8")
        headers = request_headers(target_endpoint, token)
        for attempt in range(4):
            request = urllib.request.Request(target_endpoint, data=body, headers=headers, method="POST")
            try:
                with active_opener.open(request, timeout=180) as response:
                    status = getattr(response, "status", None) or response.getcode() or 200
                    resp_headers = response.headers
                    content_type = _extract_content_type(resp_headers)
                    req_id = _extract_request_id(resp_headers)
                    id_part = f", request-id={req_id}" if req_id else ""
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise AIRequestError(
                            f"{prefix}: HTTP {status} response exceeded {MAX_RESPONSE_BYTES} bytes{id_part}"
                        )
                    try:
                        raw = _decode_response_body(raw, resp_headers)
                    except AIRequestError as dec_err:
                        raise AIRequestError(
                            f"{prefix}: HTTP {status} {dec_err} (content-type={content_type}{id_part})"
                        ) from None
                    if status != 200:
                        raise AIRequestError(
                            f"{prefix}: HTTP {status} unexpected status "
                            f"(content-type={content_type}, {len(raw)} bytes{id_part})"
                            f"{_retired_models_hint(target_endpoint)}"
                        )
                    if not raw.strip():
                        raise AIRequestError(
                            f"{prefix}: HTTP 200 empty response "
                            f"(content-type={content_type}, {len(raw)} bytes{id_part})"
                            f"{_retired_models_hint(target_endpoint)}"
                        )
                    if content_type == "text/event-stream":
                        raise AIRequestError(
                            f"{prefix}: HTTP 200 unsupported streaming response "
                            f"(content-type=text/event-stream, {len(raw)} bytes{id_part})"
                        )
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        raise AIRequestError(
                            f"{prefix}: HTTP 200 non-JSON response "
                            f"(content-type={content_type}, {len(raw)} bytes{id_part})"
                            f"{_retired_models_hint(target_endpoint)}"
                        ) from None
                    return _extract_choice_content(payload, prefix, token)
            except urllib.error.HTTPError as error:
                err_headers = error.headers
                try:
                    err_raw = error.read(MAX_ERROR_BODY_BYTES)
                except OSError:
                    err_raw = b""
                finally:
                    try:
                        error.close()
                    except OSError:
                        pass
                if error.code in RETRYABLE_STATUSES and attempt < 3:
                    sleep(_parse_retry_after(err_headers.get("Retry-After") if err_headers else None))
                    continue
                reason = _sanitize_text(getattr(error, "reason", "") or "", token=token, max_len=60)
                status_part = f"HTTP {error.code} {reason}".strip()
                meta = []
                if error.code in REDIRECT_STATUSES:
                    target = _safe_location(
                        err_headers.get("Location") if err_headers else None, target_endpoint
                    )
                    if target:
                        meta.append(f"redirect to {target} (redirects are not followed)")
                    else:
                        meta.append("redirects are not followed")
                content_type = _extract_content_type(err_headers)
                if content_type != "(missing)":
                    meta.append(f"content-type={content_type}")
                req_id = _extract_request_id(err_headers)
                if req_id:
                    meta.append(f"request-id={req_id}")
                api_err = _extract_safe_json_error(err_raw, err_headers, token=token)
                if api_err:
                    meta.append(api_err)
                elif not err_raw.strip():
                    meta.append("empty error body")
                hint = ""
                if error.code in (401, 403):
                    hint = "; check token permissions (models:read or SYNC_AI_API_KEY)"
                elif error.code in (*REDIRECT_STATUSES, 404, 410):
                    hint = _retired_models_hint(target_endpoint)
                detail = f" ({'; '.join(meta)})" if meta else ""
                raise AIRequestError(f"{prefix}: {status_part}{detail}{hint}") from None
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                reason = _sanitize_text(getattr(error, "reason", error), token=token, max_len=120)
                raise AIRequestError(f"{prefix}: network error ({reason})") from None
        raise AIRequestError(f"{prefix}: request exhausted retries")

    return ask


def _write_diagnostics(path_str, lines):
    if not path_str:
        return
    Path(path_str).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def cmd_resolve(args):
    ask_fn = None

    def lazy_ask(prompt):
        nonlocal ask_fn
        if ask_fn is None:
            model, endpoint, token = resolve_ai_config(args.model, args.endpoint)
            ask_fn = github_models(model, token, endpoint=endpoint)
        return ask_fn(prompt)

    failed = []
    diagnostics = []
    for name in args.files:
        path = Path(name)
        try:
            text = path.read_text(encoding="utf-8")
            merged, count, det_count, ai_count = resolve_text_detailed(name, text, lazy_ask)
            path.write_text(merged, encoding="utf-8", newline="")
            method = f"{det_count} deterministic JSON, {ai_count} AI"
            summary = f"resolved {name}: {count} conflict(s) ({method})"
            print(summary)
            diagnostics.append(f"- `{name}`: resolved {count} conflict(s) ({method})")
        except Exception as error:  # report every file, then fail once
            safe_error = _sanitize_text(str(error), max_len=300)
            print(f"::error::could not resolve {name}: {safe_error}")
            diagnostics.append(f"- `{name}`: {safe_error}")
            failed.append(name)
    _write_diagnostics(args.diagnostics_file, diagnostics)
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
    resolve.add_argument("--model", default=os.environ.get("SYNC_AI_MODEL") or DEFAULT_MODEL)
    resolve.add_argument("--endpoint", default=os.environ.get("SYNC_AI_ENDPOINT") or ENDPOINT)
    resolve.add_argument("--diagnostics-file", default="")
    resolve.add_argument("files", nargs="+")
    locales = sub.add_parser("locales")
    locales.add_argument("--dir", default="src/i18n/locales")
    args = parser.parse_args(argv)
    return cmd_resolve(args) if args.command == "resolve" else cmd_locales(args)


if __name__ == "__main__":
    sys.exit(main())

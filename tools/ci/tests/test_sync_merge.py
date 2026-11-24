import email.message
import gzip
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location("sync_merge", Path(__file__).parents[1] / "sync_merge.py")
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)

CONFLICTED = (
    "fn a() {}\r\n"
    "<<<<<<< HEAD\r\n"
    "let x = fork();\r\n"
    "||||||| base\r\n"
    "let x = old();\r\n"
    "=======\r\n"
    "let x = upstream();\r\n"
    ">>>>>>> v9.9.9\r\n"
    "fn b() {}\r\n"
    "<<<<<<< HEAD\r\n"
    "one\r\n"
    "||||||| base\r\n"
    "=======\r\n"
    "two\r\n"
    ">>>>>>> v9.9.9\r\n"
)

TAURI_CONF_CONFLICT = """{
  "$schema": "https://schema.tauri.app/config/2",
\x3c<<<<<< HEAD
  "productName": "Handy Remote",
  "version": "0.10.1",
  "identifier": "com.musingzone.handy-remote",
\x7c|||||| base
  "productName": "Handy",
  "version": "0.10.1",
  "identifier": "com.pais.handy",
\x3d======
  "productName": "Handy",
  "version": "0.10.2",
  "identifier": "com.pais.handy",
\x3e>>>>>> v0.10.2
  "app": {
    "security": {
      "assetProtocol": {
        "scope": {
          "allow": ["**"]
        }
      }
    }
  },
  "plugins": {
    "updater": {
      "pubkey": "FORK_PUBKEY",
      "endpoints": [
        "https://github.com/MusingZone/handy-remote/releases/latest/download/latest.json"
      ]
    }
  }
}
"""


def _headers(mapping):
    msg = email.message.Message()
    for key, value in mapping.items():
        msg[key] = value
    return msg


class _FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self._body = body
        self.status = status
        self.headers = _headers(headers or {"Content-Type": "application/json"})

    def read(self, amt=-1):
        if amt is None or amt < 0:
            return self._body
        return self._body[:amt]

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeOpener:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _http_error(code, reason, body=b"", headers=None, url=sync.ENDPOINT):
    return urllib.error.HTTPError(
        url,
        code,
        reason,
        _headers(headers or {}),
        io.BytesIO(body),
    )


class ResolveTest(unittest.TestCase):
    def test_replaces_every_hunk_with_the_reply_and_keeps_crlf(self):
        prompts = []

        def ask(prompt):
            prompts.append(prompt)
            body = "merged-2\n" if "two" in prompt.split("THEIRS")[1] else "merged-1a\nmerged-1b\n"
            return "Sure.\n```resolved\n" + body + "```\n"

        merged, count = sync.resolve_text("src/x.rs", CONFLICTED, ask)
        self.assertEqual(count, 2)
        self.assertEqual(merged, "fn a() {}\r\nmerged-1a\r\nmerged-1b\r\nfn b() {}\r\nmerged-2\r\n")
        self.assertIn("let x = fork();", prompts[1])
        self.assertIn("let x = upstream();", prompts[1])

    def test_rejects_replies_that_keep_markers_or_lack_the_block(self):
        with self.assertRaisesRegex(ValueError, "markers"):
            sync.resolve_text("x", CONFLICTED, lambda p: "```resolved\n<<<<<<< HEAD\n```")
        with self.assertRaisesRegex(ValueError, "no ```resolved"):
            sync.resolve_text("x", CONFLICTED, lambda p: "merged")

    def test_files_without_hunks_or_with_huge_hunks_are_not_resolved(self):
        with self.assertRaisesRegex(ValueError, "no conflict hunks"):
            sync.resolve_text("x", "plain\n", lambda p: "")
        huge = "<<<<<<< a\n" + "x\n" * sync.MAX_PROMPT_CHARS + "=======\ny\n>>>>>>> b\n"
        with self.assertRaisesRegex(ValueError, "too large"):
            sync.resolve_text("x", huge, lambda p: "")

    def test_deterministic_json_merge_for_tauri_conf_keeps_identity_takes_upstream_version(self):
        ai_called = False

        def fail_if_called(_prompt):
            nonlocal ai_called
            ai_called = True
            raise AssertionError("AI should not be called for disjoint JSON keys")

        merged, count, det_count, ai_count = sync.resolve_text_detailed(
            "src-tauri/tauri.conf.json", TAURI_CONF_CONFLICT, fail_if_called
        )
        self.assertFalse(ai_called)
        self.assertEqual((count, det_count, ai_count), (1, 1, 0))
        parsed = json.loads(merged)
        self.assertEqual(parsed["productName"], "Handy Remote")
        self.assertEqual(parsed["version"], "0.10.2")
        self.assertEqual(parsed["identifier"], "com.musingzone.handy-remote")
        self.assertEqual(parsed["plugins"]["updater"]["pubkey"], "FORK_PUBKEY")
        # Formatting outside the hunk stays untouched (Prettier single-line array preserved).
        self.assertIn('"allow": ["**"]', merged)

    def test_deterministic_json_merge_handles_independent_delete_and_add(self):
        conflicted = (
            "{\n"
            '  "windows": {\n'
            "<<<<<<< HEAD\n"
            '    "nsis": {\n'
            '      "installerIcon": "icons/icon.ico"\n'
            "    }\n"
            "||||||| base\n"
            '    "signCommand": "upstream-sign %1",\n'
            '    "nsis": {\n'
            '      "installerIcon": "icons/icon.ico"\n'
            "    }\n"
            "=======\n"
            '    "signCommand": "upstream-sign %1",\n'
            '    "nsis": {\n'
            '      "installerIcon": "icons/icon.ico"\n'
            "    },\n"
            '    "webviewInstallMode": "embedBootstrapper"\n'
            ">>>>>>> v0.10.2\n"
            "  }\n"
            "}\n"
        )
        merged, count, det_count, ai_count = sync.resolve_text_detailed(
            "src-tauri/tauri.conf.json", conflicted, lambda _p: self.fail("unexpected AI call")
        )
        self.assertEqual((count, det_count, ai_count), (1, 1, 0))
        data = json.loads(merged)
        self.assertNotIn("signCommand", data["windows"])
        self.assertEqual(data["windows"]["webviewInstallMode"], "embedBootstrapper")

    def test_overlapping_same_json_key_falls_back_to_ai(self):
        conflicted = (
            "{\n"
            "<<<<<<< HEAD\n"
            '  "version": "0.10.1-custom"\n'
            "||||||| base\n"
            '  "version": "0.10.1"\n'
            "=======\n"
            '  "version": "0.10.2"\n'
            ">>>>>>> v0.10.2\n"
            "}\n"
        )
        prompts = []

        def ask(prompt):
            prompts.append(prompt)
            return '```resolved\n  "version": "0.10.2"\n```'

        merged, count, det_count, ai_count = sync.resolve_text_detailed(
            "src-tauri/tauri.conf.json", conflicted, ask
        )
        self.assertEqual((count, det_count, ai_count), (1, 0, 1))
        self.assertEqual(len(prompts), 1)
        self.assertEqual(json.loads(merged), {"version": "0.10.2"})


class ModelsClientTest(unittest.TestCase):
    def test_request_protocol_headers_and_non_streaming_payload(self):
        secret = "ghs_superSecretTokenValue1234567890"
        body = json.dumps({"choices": [{"message": {"content": "```resolved\nok\n```"}, "finish_reason": "stop"}]}).encode()
        opener = _FakeOpener([_FakeResponse(body)])
        ask = sync.github_models("openai/gpt-4.1", secret, opener=opener)
        self.assertEqual(ask("merge this"), "```resolved\nok\n```")

        req, timeout = opener.requests[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(timeout, 180)
        hdrs = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(hdrs["authorization"], f"Bearer {secret}")
        self.assertEqual(hdrs["accept"], "application/vnd.github+json")
        self.assertEqual(hdrs["x-github-api-version"], sync.GITHUB_API_VERSION)
        self.assertEqual(hdrs["content-type"], "application/json")
        self.assertEqual(hdrs["accept-encoding"], "identity")
        self.assertIn("handy-remote", hdrs["user-agent"])
        payload = json.loads(req.data.decode("utf-8"))
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["model"], "openai/gpt-4.1")

    def test_custom_endpoint_uses_standard_headers_and_refuses_github_token_fallback(self):
        with self.assertRaisesRegex(ValueError, "SYNC_AI_API_KEY"):
            sync.resolve_ai_config(
                endpoint="https://api.example.com/v1/chat/completions",
                env={"GITHUB_TOKEN": "ghs_should_not_leak"},
            )
        model, endpoint, token = sync.resolve_ai_config(
            endpoint="https://api.example.com/v1/chat/completions",
            env={"SYNC_AI_API_KEY": "sk-custom-key-1234567890", "SYNC_AI_MODEL": "gpt-4.1"},
        )
        self.assertEqual((model, endpoint, token), ("gpt-4.1", "https://api.example.com/v1/chat/completions", "sk-custom-key-1234567890"))
        hdrs = {k.lower(): v for k, v in sync.request_headers(endpoint, token).items()}
        self.assertEqual(hdrs["accept"], "application/json")
        self.assertNotIn("x-github-api-version", hdrs)

    def test_diagnoses_empty_non_json_streaming_and_gzip_responses_safely(self):
        secret = "ghs_secretTokenDoNotPrint9876543210"
        # 1. Empty 200 body (the real-world Expecting value failure mode)
        opener = _FakeOpener([_FakeResponse(b"", status=200, headers={"Content-Type": "text/plain", "x-github-request-id": "REQ-123"})])
        with self.assertRaises(sync.AIRequestError) as ctx:
            sync.github_models("openai/gpt-4.1", secret, opener=opener)("p")
        msg = str(ctx.exception)
        self.assertIn("HTTP 200 empty response", msg)
        self.assertIn("0 bytes", msg)
        self.assertIn("REQ-123", msg)
        self.assertIn("2026-07-30", msg)
        self.assertNotIn(secret, msg)

        # 2. Non-JSON HTML body must NOT leak HTML/code content
        html = b"<html><body>SECRET_CODE_SNIPPET_42</body></html>"
        opener = _FakeOpener([_FakeResponse(html, status=200, headers={"Content-Type": "text/html; charset=utf-8"})])
        with self.assertRaises(sync.AIRequestError) as ctx:
            sync.github_models("openai/gpt-4.1", secret, opener=opener)("p")
        msg = str(ctx.exception)
        self.assertIn("non-JSON response", msg)
        self.assertIn("content-type=text/html", msg)
        self.assertNotIn("SECRET_CODE_SNIPPET_42", msg)
        self.assertNotIn(secret, msg)

        # 3. SSE streaming response
        sse = b'data: {"choices":[{"delta":{"content":"PRIVATE_CODE"}}]}\n\n'
        opener = _FakeOpener([_FakeResponse(sse, status=200, headers={"Content-Type": "text/event-stream"})])
        with self.assertRaises(sync.AIRequestError) as ctx:
            sync.github_models("openai/gpt-4.1", secret, opener=opener)("p")
        msg = str(ctx.exception)
        self.assertIn("unsupported streaming response", msg)
        self.assertNotIn("PRIVATE_CODE", msg)

        # 4. Gzip-encoded valid JSON is transparently decompressed
        raw_json = json.dumps({"choices": [{"message": {"content": [{"type": "text", "text": "```resolved\nok\n```"}]}, "finish_reason": "stop"}]}).encode()
        opener = _FakeOpener([_FakeResponse(gzip.compress(raw_json), status=200, headers={"Content-Type": "application/json", "Content-Encoding": "gzip"})])
        self.assertEqual(sync.github_models("openai/gpt-4.1", secret, opener=opener)("p"), "```resolved\nok\n```")

    def test_diagnoses_unsupported_json_shapes_and_empty_content(self):
        opener = _FakeOpener([
            _FakeResponse(json.dumps({"choices": [{"message": {"content": None, "refusal": "policy"}, "finish_reason": "content_filter"}]}).encode())
        ])
        with self.assertRaisesRegex(sync.AIRequestError, "empty completion content.*refusal present.*content_filter"):
            sync.github_models("openai/gpt-4.1", "tok", opener=opener)("p")

        opener = _FakeOpener([
            _FakeResponse(json.dumps({"error": {"code": "model_not_found", "message": "bad model"}}).encode())
        ])
        with self.assertRaisesRegex(sync.AIRequestError, "HTTP 200 API error.*model_not_found"):
            sync.github_models("openai/gpt-4.1", "tok", opener=opener)("p")

    def test_http_errors_redirects_and_retries_never_leak_tokens_or_code(self):
        secret = "ghs_secretTokenNeverReveal1122334455"
        # Redirect handler must return None so redirects are not followed
        handler = sync._NoRedirectHandler()
        self.assertIsNone(handler.redirect_request(None, None, 302, "Found", {}, "https://docs.github.com/retired?token=abc"))

        # 302 redirect surfaces target host/path without query params
        opener = _FakeOpener([
            _http_error(302, "Found", headers={"Location": "https://docs.github.com/retired?secret=xyz"})
        ])
        with self.assertRaises(sync.AIRequestError) as ctx:
            sync.github_models("openai/gpt-4.1", secret, opener=opener)("p")
        msg = str(ctx.exception)
        self.assertIn("HTTP 302 Found", msg)
        self.assertIn("docs.github.com/retired", msg)
        self.assertNotIn("secret=xyz", msg)
        self.assertNotIn(secret, msg)

        # 410 Gone with JSON error containing token and code fence gets scrubbed
        err_body = json.dumps({
            "error": {
                "code": "service_retired",
                "message": f"retired for Bearer {secret} ```\nfn leaked_code() {{}}\n```",
            }
        }).encode()
        opener = _FakeOpener([_http_error(410, "Gone", body=err_body, headers={"Content-Type": "application/json", "x-github-request-id": "ABCD-99"})])
        with self.assertRaises(sync.AIRequestError) as ctx:
            sync.github_models("openai/gpt-4.1", secret, opener=opener)("p")
        msg = str(ctx.exception)
        self.assertIn("HTTP 410 Gone", msg)
        self.assertIn("service_retired", msg)
        self.assertIn("ABCD-99", msg)
        self.assertNotIn(secret, msg)
        self.assertNotIn("leaked_code", msg)

        # 429 retry with non-integer Retry-After does not crash and succeeds on next attempt
        sleeps = []
        ok_body = json.dumps({"choices": [{"message": {"content": "```resolved\ndone\n```"}, "finish_reason": "stop"}]}).encode()
        opener = _FakeOpener([
            _http_error(429, "Too Many Requests", headers={"Retry-After": "not-a-number"}),
            _FakeResponse(ok_body),
        ])
        reply = sync.github_models("openai/gpt-4.1", secret, opener=opener, sleep=sleeps.append)("p")
        self.assertEqual(reply, "```resolved\ndone\n```")
        self.assertEqual(sleeps, [30])

    def test_cmd_resolve_writes_safe_diagnostics_on_deterministic_and_failed_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tauri = root / "tauri.conf.json"
            tauri.write_text(TAURI_CONF_CONFLICT, encoding="utf-8")
            diag = root / "diag.txt"
            # Deterministic JSON conflict succeeds even without any AI token set
            with mock.patch.dict(os.environ, {}, clear=True):
                rc = sync.main(["resolve", "--diagnostics-file", str(diag), str(tauri)])
            self.assertEqual(rc, 0)
            self.assertIn("1 deterministic JSON, 0 AI", diag.read_text(encoding="utf-8"))
            self.assertEqual(json.loads(tauri.read_text(encoding="utf-8"))["version"], "0.10.2")

            # General conflict with missing token fails and writes safe diagnostic
            rust_file = root / "lib.rs"
            rust_file.write_text(CONFLICTED, encoding="utf-8")
            with mock.patch.dict(os.environ, {}, clear=True), mock.patch("builtins.print"):
                rc = sync.main(["resolve", "--diagnostics-file", str(diag), str(rust_file)])
            self.assertEqual(rc, 1)
            diag_text = diag.read_text(encoding="utf-8")
            self.assertIn("GITHUB_TOKEN", diag_text)
            self.assertNotIn("let x = fork()", diag_text)


class LocalesTest(unittest.TestCase):
    def test_fills_missing_keys_with_english_and_keeps_existing_translations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for lang, data in {
                "en": {"a": "A", "group": {"b": "B", "c": "C"}},
                "th": {"a": "ก", "group": {"b": "ข"}},
                "zh": {"a": "甲", "group": {"b": "乙", "c": "丙"}},
            }.items():
                (root / lang).mkdir()
                (root / lang / "translation.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
            zh_before = (root / "zh" / "translation.json").read_bytes()
            sync.main(["locales", "--dir", directory])
            th = json.loads((root / "th" / "translation.json").read_text(encoding="utf-8"))
            self.assertEqual(th, {"a": "ก", "group": {"b": "ข", "c": "C"}})
            self.assertEqual((root / "zh" / "translation.json").read_bytes(), zh_before)


if __name__ == "__main__":
    unittest.main()

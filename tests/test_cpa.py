"""Unit tests for the CLIProxyAPI bridge core (no Hermes import required)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cpa  # noqa: E402


class ParseConfigTests(unittest.TestCase):
    def test_declared_instance_builds_one_spec_per_group(self):
        specs, errors = cpa.parse_config(
            {
                "cliproxyapi": {
                    "instances": {
                        "fast": {
                            "base_url": "http://127.0.0.1:8317",
                            "key_env": "CPA_FAST_API_KEY",
                            "groups": {
                                "claude": {"models": ["claude-*"]},
                                "codex": {"models": ["gpt-*"]},
                            },
                        }
                    }
                }
            }
        )
        self.assertEqual(errors, [])
        by_name = {s.name: s for s in specs}
        self.assertEqual(set(by_name), {"cpa-fast-claude", "cpa-fast-codex"})
        claude = by_name["cpa-fast-claude"]
        self.assertEqual(claude.api_mode, "anthropic_messages")
        self.assertEqual(claude.base_url, "http://127.0.0.1:8317")
        self.assertEqual(claude.key_env, "CPA_FAST_API_KEY")
        self.assertEqual(claude.patterns, ("claude-*",))
        codex = by_name["cpa-fast-codex"]
        self.assertEqual(codex.api_mode, "codex_responses")
        self.assertEqual(codex.base_url, "http://127.0.0.1:8317/v1")

    def test_trailing_v1_and_slash_are_normalized_to_root(self):
        specs, _ = cpa.parse_config(
            {
                "cliproxyapi": {
                    "instances": {
                        "a": {
                            "base_url": "http://127.0.0.1:8317/v1/",
                            "key_env": "K",
                            "groups": {"claude": {"models": ["*"]}},
                        }
                    }
                }
            }
        )
        self.assertEqual(specs[0].root, "http://127.0.0.1:8317")
        self.assertEqual(specs[0].base_url, "http://127.0.0.1:8317")

    def test_wire_map_rows(self):
        expected = {
            "claude": "anthropic_messages",
            "codex": "codex_responses",
            "gemini": "chat_completions",
            "antigravity": "chat_completions",
            "openai-compatibility": "chat_completions",
            "something-new": "chat_completions",
        }
        for group, mode in expected.items():
            self.assertEqual(cpa.wire_for_group(group), mode, group)

    def test_group_api_mode_override(self):
        specs, errors = cpa.parse_config(
            {
                "cliproxyapi": {
                    "instances": {
                        "pre": {
                            "base_url": "http://127.0.0.1:8317",
                            "key_env": "K",
                            "groups": {
                                "xai": {"models": ["grok-*"], "api_mode": "codex_responses"}
                            },
                        }
                    }
                }
            }
        )
        self.assertEqual(errors, [])
        self.assertEqual(specs[0].api_mode, "codex_responses")

    def test_rejects_unknown_api_mode(self):
        specs, errors = cpa.parse_config(
            {
                "cliproxyapi": {
                    "instances": {
                        "pre": {
                            "base_url": "http://127.0.0.1:8317",
                            "key_env": "K",
                            "groups": {"xai": {"models": ["*"], "api_mode": "bogus"}},
                        }
                    }
                }
            }
        )
        self.assertEqual(specs, [])
        self.assertTrue(errors)

    def test_rejects_bad_names_and_missing_fields(self):
        bad = [
            {
                "Bad Name": {
                    "base_url": "http://127.0.0.1:1",
                    "key_env": "K",
                    "groups": {"claude": {"models": ["*"]}},
                }
            },
            {"a": {"key_env": "K", "groups": {"claude": {"models": ["*"]}}}},
            {"a": {"base_url": "http://127.0.0.1:1", "groups": {"claude": {"models": ["*"]}}}},
            {
                "a": {
                    "base_url": "http://127.0.0.1:1",
                    "key_env": "bad key",
                    "groups": {"claude": {"models": ["*"]}},
                }
            },
            {
                "a": {
                    "base_url": "http://127.0.0.1:1",
                    "key_env": "K",
                    "groups": {"claude": {"models": []}},
                }
            },
            {
                "a": {
                    "base_url": "ftp://127.0.0.1:1",
                    "key_env": "K",
                    "groups": {"claude": {"models": ["*"]}},
                }
            },
        ]
        for instances in bad:
            specs, errors = cpa.parse_config({"cliproxyapi": {"instances": instances}})
            self.assertEqual(specs, [], instances)
            self.assertTrue(errors, instances)

    def test_missing_or_malformed_section_is_empty_not_error(self):
        for config in ({}, None, {"cliproxyapi": None}, {"cliproxyapi": {"instances": None}}):
            self.assertEqual(cpa.parse_config(config), ([], []))

    def test_errors_never_contain_urls(self):
        _, errors = cpa.parse_config(
            {
                "cliproxyapi": {
                    "instances": {
                        "a": {
                            "base_url": "http://user:pw@8.8.8.8:1",
                            "key_env": "K",
                            "groups": {"claude": {"models": ["*"]}},
                        }
                    }
                }
            }
        )
        self.assertTrue(errors)
        for err in errors:
            self.assertNotIn("8.8.8.8", err)
            self.assertNotIn("pw", err)


class UrlPolicyTests(unittest.TestCase):
    def test_https_always_allowed(self):
        self.assertTrue(cpa.transport_allowed("https://cpa.example.com"))

    def test_http_private_literals_allowed(self):
        for url in (
            "http://127.0.0.1:8317",
            "http://10.1.2.3:8317",
            "http://172.20.0.5:8317",
            "http://192.168.1.2",
            "http://100.100.1.2:8317",
            "http://[::1]:8317",
            "http://[fd00::1]:8317",
            "http://localhost:8317",
        ):
            self.assertTrue(cpa.transport_allowed(url), url)

    def test_http_public_or_link_local_refused(self):
        for url in (
            "http://8.8.8.8",
            "http://169.254.169.254",
            "http://172.32.0.1",
            "http://100.128.0.1",
            "http://[2001:4860::1]",
            "http://[::ffff:8.8.8.8]",
        ):
            self.assertFalse(cpa.transport_allowed(url), url)

    def test_http_hostname_uses_resolver_and_fails_closed(self):
        self.assertTrue(cpa.transport_allowed("http://cpa:8317", resolver=lambda h: ["10.0.0.5"]))
        self.assertFalse(
            cpa.transport_allowed("http://cpa:8317", resolver=lambda h: ["10.0.0.5", "8.8.8.8"])
        )
        self.assertFalse(cpa.transport_allowed("http://cpa:8317", resolver=lambda h: []))

        def boom(_host):
            raise OSError("no dns")

        self.assertFalse(cpa.transport_allowed("http://cpa:8317", resolver=boom))

    def test_userinfo_refused(self):
        self.assertFalse(cpa.transport_allowed("https://u:p@cpa.example.com"))


class _Gateway(BaseHTTPRequestHandler):
    """Fake CPA public model lists keyed by request shape."""

    routes: dict = {}
    seen: list = []

    def log_message(self, *args):  # silence
        pass

    def do_GET(self):  # noqa: N802
        type(self).seen.append(
            {
                "path": self.path,
                "ua": self.headers.get("User-Agent", ""),
                "auth": self.headers.get("Authorization", ""),
            }
        )
        if self.headers.get("Authorization") != "Bearer test-key":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"error":"secret-body"}')
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()
            return
        key = self.path
        if key == "/v1/models" and "grok-shell" in self.headers.get("User-Agent", ""):
            key = "grok"
        body = type(self).routes.get(key)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


OPENAI_LIST = {
    "object": "list",
    "data": [
        {"id": "claude-opus-5-5", "object": "model", "owned_by": "anthropic"},
        {"id": "gpt-5.6", "object": "model", "owned_by": "openai"},
        {"id": "gemini-3.8-flash-high", "object": "model", "owned_by": "google"},
    ],
}
GROK_LIST = {
    "object": "list",
    "data": [
        {
            "id": "claude-opus-5-5",
            "model": "claude-opus-5-5",
            "name": "Claude Opus 5.5",
            "context_window": 1000000,
            "api_backend": "responses",
            "supported_in_api": True,
            "reasoning_efforts": [
                {"value": "low"},
                {"value": "high"},
                {"value": "max"},
                {"value": "auto"},
            ],
        },
        {
            "id": "gpt-5.6",
            "model": "gpt-5.6",
            "name": "gpt-5.6",
            "api_backend": "responses",
            "supported_in_api": True,
        },
        {
            "id": "gemini-3.8-flash-high",
            "model": "gemini-3.8-flash-high",
            "name": "Gemini",
            "context_window": 1048576,
            "api_backend": "responses",
            "supported_in_api": True,
        },
    ],
}
GEMINI_LIST = {
    "models": [
        {
            "name": "models/gemini-3.8-flash-high",
            "outputTokenLimit": 65536,
            "supportedInputModalities": ["TEXT", "IMAGE"],
        },
        {"name": "models/gpt-5.6", "supportedInputModalities": ["TEXT"]},
        {"name": "models/claude-opus-5-5"},
    ]
}


class GatewayTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _Gateway.routes = {
            "/v1/models": OPENAI_LIST,
            "grok": GROK_LIST,
            "/v1beta/models": GEMINI_LIST,
        }
        cls.server = HTTPServer(("127.0.0.1", 0), _Gateway)
        cls.root = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Gateway.seen = []


class PublicCatalogTests(GatewayTestCase):
    def test_fetch_catalog_merges_three_public_lists(self):
        catalog = cpa.fetch_public_catalog(self.root, "test-key", timeout=5)
        self.assertEqual(catalog.ids, ["claude-opus-5-5", "gpt-5.6", "gemini-3.8-flash-high"])
        opus = catalog.meta["claude-opus-5-5"]
        self.assertEqual(opus["context_window"], 1000000)
        self.assertEqual(opus["reasoning_efforts"], ["low", "high", "max"])
        self.assertTrue(opus["supports_reasoning"])
        self.assertNotIn("supports_vision", opus)  # unknown stays unknown
        gpt = catalog.meta["gpt-5.6"]
        self.assertNotIn("context_window", gpt)
        self.assertNotIn("supports_reasoning", gpt)
        self.assertFalse(gpt["supports_vision"])
        gem = catalog.meta["gemini-3.8-flash-high"]
        self.assertEqual(gem["context_window"], 1048576)
        self.assertEqual(gem["max_output_tokens"], 65536)
        self.assertTrue(gem["supports_vision"])

    def test_requests_send_identifying_user_agent_and_bearer(self):
        cpa.fetch_public_catalog(self.root, "test-key", timeout=5)
        self.assertEqual(len(_Gateway.seen), 3)
        for req in _Gateway.seen:
            self.assertTrue(req["ua"].startswith("hermes-cliproxyapi-provider/"), req["ua"])
            self.assertEqual(req["auth"], "Bearer test-key")
        self.assertTrue(any("grok-shell" in r["ua"] for r in _Gateway.seen))

    def test_auth_failure_raises_safe_error(self):
        with self.assertRaises(cpa.GatewayError) as ctx:
            cpa.fetch_public_catalog(self.root, "wrong", timeout=5)
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.category, "auth")
        self.assertNotIn("secret-body", str(ctx.exception))
        self.assertNotIn("127.0.0.1", str(ctx.exception))

    def test_metadata_lists_are_optional(self):
        _Gateway.routes = {"/v1/models": OPENAI_LIST}
        try:
            catalog = cpa.fetch_public_catalog(self.root, "test-key", timeout=5)
        finally:
            _Gateway.routes = {
                "/v1/models": OPENAI_LIST,
                "grok": GROK_LIST,
                "/v1beta/models": GEMINI_LIST,
            }
        self.assertEqual(len(catalog.ids), 3)
        self.assertEqual(catalog.meta.get("claude-opus-5-5", {}), {})

    def test_redirects_are_refused(self):
        with self.assertRaises(cpa.GatewayError) as ctx:
            cpa.get_json(self.root + "/redirect", "test-key", timeout=5)
        self.assertEqual(ctx.exception.category, "redirect")

    def test_refuses_public_http_before_sending_key(self):
        with self.assertRaises(cpa.GatewayError) as ctx:
            cpa.fetch_public_catalog("http://8.8.8.8:9", "test-key", timeout=1)
        self.assertEqual(ctx.exception.category, "transport_policy")


class CacheTests(unittest.TestCase):
    def test_round_trip_and_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            catalog = cpa.Catalog(
                ids=["claude-opus-5-5", "gpt-5.6"],
                meta={
                    "claude-opus-5-5": {
                        "context_window": 1000000,
                        "supports_reasoning": True,
                        "reasoning_efforts": ["low", "high"],
                    },
                    "gpt-5.6": {"supports_vision": False, "max_output_tokens": 128000},
                },
            )
            cpa.save_cache(home, "fast", catalog)
            loaded = cpa.load_cache(home, "fast")
            self.assertEqual(loaded.ids, catalog.ids)
            self.assertEqual(loaded.meta, catalog.meta)
            caps = cpa.capabilities_for(loaded, ("claude-*",))
            self.assertEqual(
                caps,
                {"claude-opus-5-5": {"context_window": 1000000, "supports_reasoning": True}},
            )
            mode = os.stat(cpa.cache_path(home, "fast")).st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_missing_or_corrupt_cache_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(cpa.load_cache(home, "nope").ids, [])
            path = cpa.cache_path(home, "bad")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{not json")
            self.assertEqual(cpa.load_cache(home, "bad").ids, [])


class MatchTests(unittest.TestCase):
    def test_glob_filter_preserves_order_and_is_case_sensitive(self):
        ids = ["claude-a", "gpt-b", "Claude-c", "claude-d"]
        self.assertEqual(cpa.match_models(ids, ("claude-*",)), ["claude-a", "claude-d"])
        self.assertEqual(cpa.match_models(ids, ("gpt-b", "claude-a")), ["claude-a", "gpt-b"])


if __name__ == "__main__":
    unittest.main()

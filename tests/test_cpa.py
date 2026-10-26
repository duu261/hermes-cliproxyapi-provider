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
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cpa  # noqa: E402

API_KEY = "test-key"
MGMT_KEY = "mgmt-key"


def _instance(**body):
    base = {"base_url": "http://127.0.0.1:8317", "key_env": "CPA_A_API_KEY"}
    base.update(body)
    return {"cliproxyapi": {"instances": {"a": base}}}


class ParseConfigTests(unittest.TestCase):
    def test_minimal_instance_needs_only_url_and_key(self):
        specs, errors = cpa.parse_config(_instance())
        self.assertEqual(errors, [])
        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertEqual(spec.name, "a")
        self.assertEqual(spec.root, "http://127.0.0.1:8317")
        self.assertEqual(spec.key_env, "CPA_A_API_KEY")
        self.assertEqual(spec.mgmt_key_env, "")
        self.assertEqual(spec.overrides, {})
        self.assertEqual(spec.wide_name, "cpa-a")
        self.assertEqual(spec.group_name("claude"), "cpa-a-claude")

    def test_trailing_v1_and_slash_are_normalized_to_root(self):
        specs, _ = cpa.parse_config(_instance(base_url="http://127.0.0.1:8317/v1/"))
        self.assertEqual(specs[0].root, "http://127.0.0.1:8317")

    def test_no_instances_gives_default_local(self):
        for config in ({}, None, {"cliproxyapi": None}, {"cliproxyapi": {"instances": {}}}):
            specs, errors = cpa.parse_config(config)
            self.assertEqual(errors, [])
            self.assertEqual([s.name for s in specs], ["local"])
            self.assertEqual(specs[0].root, cpa.DEFAULT_ROOT)
            self.assertEqual(specs[0].key_env, cpa.DEFAULT_KEY_ENV)
            self.assertEqual(specs[0].url_env, cpa.DEFAULT_URL_ENV)

    def test_management_key_and_overrides(self):
        specs, errors = cpa.parse_config(
            _instance(
                mgmt_key_env="CPA_A_MGMT_KEY",
                groups={
                    "xai": {"api_mode": "chat_completions"},
                    "custom": {"models": ["foo-*"], "display_name": "Foo"},
                },
            )
        )
        self.assertEqual(errors, [])
        spec = specs[0]
        self.assertEqual(spec.mgmt_key_env, "CPA_A_MGMT_KEY")
        self.assertEqual(spec.overrides["xai"].api_mode, "chat_completions")
        self.assertEqual(spec.overrides["xai"].models, ())
        self.assertEqual(spec.overrides["custom"].models, ("foo-*",))
        self.assertEqual(spec.overrides["custom"].display_name, "Foo")

    def test_rejects_bad_input_without_leaking_values(self):
        bad = [
            {"Bad Name": {"base_url": "http://127.0.0.1:1", "key_env": "K"}},
            {"a": {"key_env": "K"}},
            {"a": {"base_url": "http://127.0.0.1:1"}},
            {"a": {"base_url": "http://127.0.0.1:1", "key_env": "bad key"}},
            {"a": {"base_url": "http://127.0.0.1:1", "key_env": "K", "mgmt_key_env": "x y"}},
            {"a": {"base_url": "ftp://127.0.0.1:1", "key_env": "K"}},
            {"a": {"base_url": "http://user:pw@8.8.8.8:1", "key_env": "K"}},
            {"a": {"base_url": "http://8.8.8.8:1", "key_env": "K"}},
            {
                "a": {
                    "base_url": "http://127.0.0.1:1",
                    "key_env": "K",
                    "groups": {"xai": {"api_mode": "bogus"}},
                }
            },
            {"a": {"base_url": "http://127.0.0.1:1", "key_env": "K", "groups": {"Bad": {}}}},
        ]
        for instances in bad:
            specs, errors = cpa.parse_config({"cliproxyapi": {"instances": instances}})
            self.assertEqual(specs, [], instances)
            self.assertEqual(len(errors), 1, instances)
            self.assertNotIn("8.8.8.8", errors[0])
            self.assertNotIn("pw", errors[0])

    def test_one_bad_instance_does_not_drop_the_others(self):
        specs, errors = cpa.parse_config(
            {
                "cliproxyapi": {
                    "instances": {
                        "good": {"base_url": "http://127.0.0.1:1", "key_env": "K"},
                        "bad": {"base_url": "http://8.8.8.8:1", "key_env": "K"},
                    }
                }
            }
        )
        self.assertEqual([s.name for s in specs], ["good"])
        self.assertEqual(len(errors), 1)

    def test_billing_route_parsed(self):
        specs, errors = cpa.parse_config(
            _instance(
                route_via="https://gateway.example/v1/",
                route_key_env="GW_A",
                groups={"xai": {"route_key_env": "GW_B", "api_mode": "chat_completions"}},
            )
        )
        self.assertEqual(errors, [])
        spec = specs[0]
        self.assertTrue(spec.routed)
        self.assertEqual(spec.route_root, "https://gateway.example")
        self.assertEqual(spec.route_key_env, "GW_A")
        self.assertEqual(spec.route_key_for("xai"), "GW_B")
        self.assertEqual(spec.route_key_for("claude"), "GW_A")
        self.assertEqual(spec.route_key_for(None), "GW_A")
        self.assertEqual(spec.route_key_envs(), ["GW_A", "GW_B"])

    def test_unrouted_instance_is_unchanged(self):
        spec = cpa.parse_config(_instance())[0][0]
        self.assertFalse(spec.routed)
        self.assertEqual(spec.route_key_envs(), [])

    def test_billing_route_rejects_unsafe_or_ambiguous_config(self):
        bad = [
            {"route_via": "https://gw.example"},  # no route key
            {"route_key_env": "GW_A"},  # key without a route
            {"groups": {"xai": {"route_key_env": "GW_B"}}},  # group key without a route
            {"route_via": "http://8.8.8.8", "route_key_env": "GW_A"},  # public plain http
            {"route_via": "https://u:pw@gw.example", "route_key_env": "GW_A"},
            {"route_via": "https://gw.example", "route_key_env": "bad name"},
            # The CPA or management key must never be the gateway key.
            {"route_via": "https://gw.example", "route_key_env": "CPA_A_API_KEY"},
            {
                "route_via": "https://gw.example",
                "route_key_env": "GW_A",
                "mgmt_key_env": "GW_A",
            },
            {
                "route_via": "https://gw.example",
                "route_key_env": "GW_A",
                "groups": {"xai": {"route_key_env": "CPA_A_API_KEY"}},
            },
        ]
        for body in bad:
            specs, errors = cpa.parse_config(_instance(**body))
            self.assertEqual(specs, [], body)
            self.assertEqual(len(errors), 1, body)
            self.assertNotIn("8.8.8.8", errors[0])
            self.assertNotIn("pw", errors[0])


class WireMapTests(unittest.TestCase):
    def test_every_cpa_provider_has_a_sourced_wire(self):
        expected = {
            "claude": "anthropic_messages",
            "kimi": "anthropic_messages",
            "kiro": "anthropic_messages",
            "codex": "codex_responses",
            "xai": "codex_responses",
            "meta": "codex_responses",
            "gemini": "chat_completions",
            "vertex": "chat_completions",
            "aistudio": "chat_completions",
            "gemini-interactions": "chat_completions",
            "antigravity": "chat_completions",
            "devin": "chat_completions",
            "opencode-go": "chat_completions",
            "something-new": "chat_completions",
        }
        for group, mode in expected.items():
            self.assertEqual(cpa.wire_for(group), mode, group)

    def test_override_wins(self):
        self.assertEqual(
            cpa.wire_for("xai", cpa.GroupOverride(api_mode="chat_completions")),
            "chat_completions",
        )

    def test_base_url_per_wire(self):
        self.assertEqual(cpa.base_url_for("http://h", "anthropic_messages"), "http://h")
        self.assertEqual(cpa.base_url_for("http://h", "codex_responses"), "http://h/v1")
        self.assertEqual(cpa.base_url_for("http://h", "chat_completions"), "http://h/v1")

    def test_owner_and_provider_normalization(self):
        self.assertEqual(cpa.provider_for_owner("anthropic"), "claude")
        self.assertEqual(cpa.provider_for_owner("openai"), "codex")
        self.assertEqual(cpa.provider_for_owner("moonshot"), "kimi")
        self.assertEqual(cpa.provider_for_owner("google"), "gemini")
        self.assertEqual(cpa.provider_for_owner("Opencode-go"), "opencode-go")
        self.assertEqual(cpa.provider_for_owner("google-antigravity"), "google-antigravity")
        self.assertEqual(cpa.provider_for_owner(""), "")
        for raw in ("kimi", "kimi-ai", "kimi.ai", "kimi.com"):
            self.assertEqual(cpa.normalize_provider(raw), "kimi")
        for raw, want in (
            ("x-ai", "xai"),
            ("grok", "xai"),
            ("muse", "meta"),
            ("gemini-interactions", "gemini-interactions"),
            ("kimi-k2", "kimi-k2"),
        ):
            self.assertEqual(cpa.normalize_provider(raw), want)
        self.assertIn(cpa.normalize_provider("gemini-interactions"), cpa.DEFINITION_CHANNEL)
        self.assertEqual(
            cpa.normalize_provider("openai-compatible-OpenRouter"), "openai-compatible-openrouter"
        )


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


# ── Fake CPA ────────────────────────────────────────────────────────────────

OPENAI_LIST = {
    "object": "list",
    "data": [
        {"id": "claude-opus-5-5", "object": "model", "owned_by": "anthropic"},
        {"id": "gpt-5.6", "object": "model", "owned_by": "openai"},
        {"id": "gemini-3.8-flash-high", "object": "model", "owned_by": "google"},
        {"id": "grok-4.6", "object": "model", "owned_by": "xai"},
        {"id": "glm-5.3", "object": "model", "owned_by": "Opencode-go"},
    ],
}
GROK_LIST = {
    "object": "list",
    "data": [
        {
            "id": "claude-opus-5-5",
            "context_window": 1000000,
            "reasoning_efforts": [
                {"value": "low"},
                {"value": "high"},
                {"value": "max"},
                {"value": "auto"},
            ],
        },
        {"id": "gpt-5.6"},
        {"id": "gemini-3.8-flash-high", "context_window": 1048576},
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
# Anthropic-format /v1/models (Anthropic-Version header). CPA fills max_tokens
# with its 64000 default when a model has none, and cloaks non-claude ids as
# "claude-fable-5-dd-" + the id reversed (internal/client/claude/models).
ANTHROPIC_LIST = {
    "data": [
        {"id": "claude-opus-5-5", "max_input_tokens": 999, "max_tokens": 128000},
        {
            "id": "claude-fable-5-dd-" + "gpt-5.6"[::-1],
            "max_input_tokens": 400000,
            "max_tokens": 128000,
        },
        {"id": "claude-fable-5-dd-" + "gemini-3.8-flash-high"[::-1], "max_tokens": 64000},
        {
            "id": "claude-fable-5-dd-" + "glm-5.3"[::-1],
            "max_input_tokens": 200000,
            "max_tokens": 64000,
        },
        {"id": "claude-fable-5-dd-" + "not-served"[::-1], "max_tokens": 32000},
        {"id": "claude-not-served", "max_tokens": 32000},
        {"id": "claude-fable-5-dd-", "max_tokens": 32000},
    ],
    "has_more": False,
}
CREDENTIALS = {
    "observed_at": "now",
    "files": [
        # Devin serves Claude ids but labels them "anthropic" on /v1/models.
        {"name": "devin-1.json", "provider": "devin", "disabled": False, "api_key": "leak-me"},
        {"name": "claude-1.json", "provider": "claude", "disabled": False},
        {"name": "claude-2.json", "provider": "claude", "disabled": True},
        {"name": "kimi-a.json", "provider": "kimi-ai", "unavailable": True},
        {"name": "x-1.json", "provider": "xai"},
        {"name": "x-2.json", "provider": "xai"},
        {"name": "x-3.json", "provider": "xai"},
        {"name": "x-4.json", "provider": "xai"},
        # A CPA plugin provider: no static channel, definitions from the plugin (v8.0.34+).
        {"name": "kiro-1.json", "provider": "kiro"},
        # No static channel and no definitions on this CPA: 400 "unknown channel".
        {"name": "oc-1.json", "provider": "opencode-go"},
    ],
}
ACCOUNT_MODELS = {
    "devin-1.json": ["claude-opus-5-5", "gpt-5.6"],
    "claude-1.json": ["claude-opus-5-5"],
    "kimi-a.json": ["kimi-k3"],
    "x-1.json": ["grok-4.6"],
    "x-2.json": ["grok-4.6"],
    "x-3.json": ["grok-4.6"],
    "x-4.json": ["grok-4.6"],
    "kiro-1.json": ["claude-sonnet-5.5"],
    "oc-1.json": ["glm-5.3"],
}
DEFINITIONS = {
    "claude": {
        "channel": "claude",
        "models": [
            {
                "id": "claude-opus-5-5",
                "context_length": 1000000,
                "max_completion_tokens": 64000,
                "supportedInputModalities": ["text", "image"],
                "thinking": {"levels": ["low", "medium", "high", "xhigh", "max"]},
            }
        ],
    },
    "xai": {"channel": "xai", "models": [{"id": "grok-4.6", "context_length": 2000000}]},
    "kiro": {
        "channel": "kiro",
        "models": [
            {
                "id": "claude-sonnet-5.5",
                "context_length": 200000,
                "max_completion_tokens": 64000,
                "supportedInputModalities": ["text", "image"],
            }
        ],
    },
}


class _Gateway(BaseHTTPRequestHandler):
    """Fake CPA: public lists for API_KEY, management routes for MGMT_KEY."""

    routes: dict = {}
    seen: list = []
    mgmt_status: int = 200  # force a status on every management request
    v8: bool = True

    def log_message(self, *args):  # silence
        pass

    def _send(self, status, body=None):
        data = json.dumps(body if body is not None else {"error": "secret-body"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        cls = type(self)
        parts = urlsplit(self.path)
        auth = self.headers.get("Authorization", "")
        cls.seen.append(
            {
                "path": parts.path,
                "query": parts.query,
                "ua": self.headers.get("User-Agent", ""),
                "auth": auth,
                "anthropic_version": self.headers.get("Anthropic-Version", ""),
                "method": "GET",
            }
        )
        if "/management/" in parts.path:
            return self._management(parts, auth)
        if auth != f"Bearer {API_KEY}":
            return self._send(401)
        if parts.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
            self.end_headers()
            return
        key = parts.path
        if key == "/v1/models" and "grok-shell" in self.headers.get("User-Agent", ""):
            key = "grok"
        elif key == "/v1/models" and self.headers.get("Anthropic-Version"):
            key = "anthropic"
        body = cls.routes.get(key)
        return self._send(200, body) if body is not None else self._send(404)

    def _management(self, parts, auth):
        cls = type(self)
        if cls.mgmt_status != 200:
            return self._send(cls.mgmt_status)
        if auth != f"Bearer {MGMT_KEY}":
            return self._send(401)
        version = "v8" if parts.path.startswith("/v8/") else "v0"
        if (version == "v8") != cls.v8:
            return self._send(404)
        tail = parts.path.split("/management/", 1)[1]
        if tail in ("credentials", "auth-files"):
            return self._send(200, CREDENTIALS)
        if tail in ("credentials/models", "auth-files/models"):
            name = parse_qs(parts.query).get("name", [""])[0]
            ids = ACCOUNT_MODELS.get(name, [])
            return self._send(
                200, {"models": [{"id": i, "type": "x", "owned_by": "y"} for i in ids]}
            )
        for prefix in ("routing/model-definitions/", "model-definitions/"):
            if tail.startswith(prefix):
                body = DEFINITIONS.get(tail[len(prefix) :])
                return (
                    self._send(200, body) if body else self._send(400, {"error": "unknown channel"})
                )
        return self._send(404)


PUBLIC_ROUTES = {
    "/v1/models": OPENAI_LIST,
    "grok": GROK_LIST,
    "/v1beta/models": GEMINI_LIST,
    "anthropic": ANTHROPIC_LIST,
}

GW_KEY_A = "gw-key-a"
GW_KEY_B = "gw-key-b"
# What a billing gateway sells per key: each key is bound to one billing group.
GW_LISTS = {
    GW_KEY_A: ["gpt-5.6", "claude-opus-5-5", "not-on-cpa"],
    GW_KEY_B: ["grok-4.6"],
}


class _BillingGateway(BaseHTTPRequestHandler):
    """Fake New API: lists models per gateway key, rejects anything else."""

    seen: list = []
    status: int = 200

    def log_message(self, *args):
        pass

    def do_GET(self):  # noqa: N802
        cls = type(self)
        auth = self.headers.get("Authorization", "")
        cls.seen.append({"path": urlsplit(self.path).path, "auth": auth})
        key = auth.removeprefix("Bearer ")
        if cls.status != 200:
            status, body = cls.status, {"error": "secret-body"}
        elif urlsplit(self.path).path != "/v1/models":
            status, body = 404, {"error": "secret-body"}
        elif key not in GW_LISTS:
            status, body = 401, {"error": "secret-body"}
        else:
            status = 200
            body = {
                "object": "list",
                "data": [{"id": i, "owned_by": "custom"} for i in GW_LISTS[key]],
            }
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class GatewayTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _Gateway.routes = dict(PUBLIC_ROUTES)
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
        _Gateway.routes = dict(PUBLIC_ROUTES)
        _Gateway.mgmt_status = 200
        _Gateway.v8 = True

    def mgmt_requests(self):
        return [r for r in _Gateway.seen if "/management/" in r["path"]]


class PublicCatalogTests(GatewayTestCase):
    def test_fetch_catalog_merges_three_public_lists_and_owners(self):
        catalog = cpa.fetch_public_catalog(self.root, API_KEY, timeout=5)
        self.assertEqual(
            catalog.ids,
            ["claude-opus-5-5", "gpt-5.6", "gemini-3.8-flash-high", "grok-4.6", "glm-5.3"],
        )
        self.assertEqual(catalog.owners["glm-5.3"], "Opencode-go")
        opus = catalog.meta["claude-opus-5-5"]
        self.assertEqual(opus["context_window"], 1000000)
        self.assertEqual(opus["reasoning_efforts"], ["low", "high", "max"])
        self.assertTrue(opus["supports_reasoning"])
        self.assertNotIn("supports_vision", opus)  # unknown stays unknown
        self.assertFalse(catalog.meta["gpt-5.6"]["supports_vision"])
        gem = catalog.meta["gemini-3.8-flash-high"]
        self.assertEqual(gem["context_window"], 1048576)
        self.assertEqual(gem["max_output_tokens"], 65536)
        self.assertTrue(gem["supports_vision"])

    def test_requests_send_identifying_user_agent_and_bearer(self):
        cpa.fetch_public_catalog(self.root, API_KEY, timeout=5)
        self.assertEqual(len(_Gateway.seen), 4)
        for req in _Gateway.seen:
            self.assertTrue(req["ua"].startswith("hermes-cliproxyapi-provider/"), req["ua"])
            self.assertEqual(req["auth"], f"Bearer {API_KEY}")
        self.assertTrue(any("grok-shell" in r["ua"] for r in _Gateway.seen))
        anthropic = [r for r in _Gateway.seen if r["anthropic_version"]]
        self.assertEqual(len(anthropic), 1)
        self.assertEqual(anthropic[0]["path"], "/v1/models")
        self.assertNotIn("grok-shell", anthropic[0]["ua"])

    def test_anthropic_list_adds_only_real_max_output(self):
        catalog = cpa.fetch_public_catalog(self.root, API_KEY, timeout=5)
        meta = catalog.meta
        # A real limit is taken; the context in this list is never used.
        self.assertEqual(meta["claude-opus-5-5"]["max_output_tokens"], 128000)
        self.assertEqual(meta["claude-opus-5-5"]["context_window"], 1000000)
        # Cloaked ids are decoded back to the served id.
        self.assertEqual(meta["gpt-5.6"]["max_output_tokens"], 128000)
        self.assertNotIn("context_window", meta["gpt-5.6"])
        # CPA's 64000 fill-in is unknown: the Gemini list's real limit stays.
        self.assertEqual(meta["gemini-3.8-flash-high"]["max_output_tokens"], 65536)
        # A model with no output limit gets 64000 from CPA; never taken, so
        # Hermes falls back to models.dev (a real 64000 limit is lost the same way).
        self.assertNotIn("glm-5.3", meta)
        # Ids CPA does not serve on /v1/models add nothing.
        self.assertEqual(
            set(catalog.ids),
            {"claude-opus-5-5", "gpt-5.6", "gemini-3.8-flash-high", "grok-4.6", "glm-5.3"},
        )
        self.assertFalse(any("not-served" in key for key in meta))

    def test_auth_failure_raises_safe_error(self):
        with self.assertRaises(cpa.GatewayError) as ctx:
            cpa.fetch_public_catalog(self.root, "wrong", timeout=5)
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.category, "auth")
        self.assertNotIn("secret-body", str(ctx.exception))
        self.assertNotIn("127.0.0.1", str(ctx.exception))

    def test_metadata_lists_are_optional(self):
        _Gateway.routes = {"/v1/models": OPENAI_LIST}
        catalog = cpa.fetch_public_catalog(self.root, API_KEY, timeout=5)
        self.assertEqual(len(catalog.ids), 5)
        self.assertEqual(catalog.meta, {})

    def test_redirects_are_refused(self):
        with self.assertRaises(cpa.GatewayError) as ctx:
            cpa.get_json(self.root + "/redirect", API_KEY, timeout=5)
        self.assertEqual(ctx.exception.category, "redirect")

    def test_refuses_public_http_before_sending_key(self):
        with self.assertRaises(cpa.GatewayError) as ctx:
            cpa.fetch_public_catalog("http://8.8.8.8:9", API_KEY, timeout=1)
        self.assertEqual(ctx.exception.category, "transport_policy")


class ManagementTests(GatewayTestCase):
    def test_groups_by_account_provider_and_reads_definitions(self):
        result = cpa.fetch_management(self.root, MGMT_KEY, timeout=5)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.groups["devin"], ["claude-opus-5-5", "gpt-5.6"])
        self.assertEqual(result.groups["claude"], ["claude-opus-5-5"])
        self.assertEqual(result.groups["kimi"], ["kimi-k3"])
        self.assertEqual(result.groups["xai"], ["grok-4.6"])
        opus = result.definitions["claude-opus-5-5"]
        self.assertEqual(opus["context_window"], 1000000)
        self.assertEqual(opus["max_output_tokens"], 64000)
        self.assertTrue(opus["supports_vision"])
        self.assertEqual(opus["reasoning_efforts"], ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(result.definitions["grok-4.6"], {"context_window": 2000000})

    def test_plugin_provider_definitions_and_unknown_channel(self):
        result = cpa.fetch_management(self.root, MGMT_KEY, timeout=5)
        self.assertEqual(result.status, "ok")
        # CPA v8.0.34+ serves plugin providers' definitions on the same route.
        self.assertEqual(result.groups["kiro"], ["claude-sonnet-5.5"])
        kiro = result.definitions["claude-sonnet-5.5"]
        self.assertEqual(kiro["context_window"], 200000)
        self.assertTrue(kiro["supports_vision"])
        # A provider with no definitions (older CPA, or none published) answers
        # 400 "unknown channel": its group stays, its metadata stays unknown.
        self.assertEqual(result.groups["opencode-go"], ["glm-5.3"])
        self.assertNotIn("glm-5.3", result.definitions)
        asked = {
            r["path"].rsplit("/", 1)[1]
            for r in self.mgmt_requests()
            if "model-definitions/" in r["path"]
        }
        self.assertEqual(asked, {"claude", "devin", "kimi", "kiro", "opencode-go", "xai"})

    def test_static_channels_are_read_before_plugin_providers(self):
        cpa.fetch_management(self.root, MGMT_KEY, timeout=5)
        order = [
            r["path"].rsplit("/", 1)[1]
            for r in self.mgmt_requests()
            if "model-definitions/" in r["path"]
        ]
        self.assertEqual(order, ["claude", "devin", "kimi", "xai", "kiro", "opencode-go"])

    def test_samples_accounts_and_skips_disabled(self):
        cpa.fetch_management(self.root, MGMT_KEY, timeout=5, per_provider=3)
        names = [r["query"] for r in self.mgmt_requests() if r["path"].endswith("/models")]
        self.assertNotIn("name=claude-2.json", names)  # disabled
        self.assertEqual(sum(1 for n in names if n.startswith("name=x-")), 3)  # capped

    def test_only_read_only_allowlisted_gets(self):
        cpa.fetch_management(self.root, MGMT_KEY, timeout=5)
        allowed = (
            "/v8/management/credentials",
            "/v8/management/credentials/models",
            "/v8/management/routing/model-definitions/",
        )
        for req in self.mgmt_requests():
            self.assertEqual(req["method"], "GET")
            self.assertTrue(
                req["path"] in allowed[:2] or req["path"].startswith(allowed[2]), req["path"]
            )
            self.assertEqual(req["auth"], f"Bearer {MGMT_KEY}")

    def test_wrong_key_stops_after_one_request(self):
        result = cpa.fetch_management(self.root, "wrong", timeout=5)
        self.assertEqual(result.status, "auth_failed")
        self.assertEqual(len(self.mgmt_requests()), 1)

    def test_remote_disabled_is_unavailable_after_one_request(self):
        _Gateway.mgmt_status = 403
        result = cpa.fetch_management(self.root, MGMT_KEY, timeout=5)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(len(self.mgmt_requests()), 1)

    def test_v0_fallback_on_404_only(self):
        _Gateway.v8 = False
        result = cpa.fetch_management(self.root, MGMT_KEY, timeout=5)
        self.assertEqual(result.status, "ok")
        paths = [r["path"] for r in self.mgmt_requests()]
        self.assertEqual(paths[0], "/v8/management/credentials")
        self.assertTrue(all(p.startswith("/v0/") for p in paths[1:]))

    def test_call_cap_is_hard(self):
        result = cpa.fetch_management(self.root, MGMT_KEY, timeout=5, max_calls=3)
        self.assertLessEqual(len(self.mgmt_requests()), 3)
        self.assertEqual(result.status, "ok")

    def test_refuses_public_http(self):
        result = cpa.fetch_management("http://8.8.8.8:9", MGMT_KEY, timeout=1)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.calls, 0)

    def test_choose_accounts_reads_only_routing_fields(self):
        chosen = cpa.choose_accounts(CREDENTIALS, 2)
        self.assertEqual(chosen["claude"], ["claude-1.json"])
        self.assertEqual(chosen["kimi"], ["kimi-a.json"])
        self.assertEqual(chosen["xai"], ["x-1.json", "x-2.json"])
        self.assertNotIn("leak-me", json.dumps(chosen))


class GroupingTests(unittest.TestCase):
    def _catalog(self, **kw):
        return cpa.Catalog(
            ids=["claude-opus-5-5", "gpt-5.6", "gemini-3.8-flash-high", "grok-4.6", "glm-5.3"],
            owners={
                "claude-opus-5-5": "anthropic",
                "gpt-5.6": "openai",
                "gemini-3.8-flash-high": "google",
                "grok-4.6": "xai",
                "glm-5.3": "Opencode-go",
            },
            **kw,
        )

    def test_public_grouping_by_owned_by(self):
        groups = cpa.compute_groups(self._catalog())
        self.assertEqual(
            groups,
            {
                "claude": ["claude-opus-5-5"],
                "codex": ["gpt-5.6"],
                "gemini": ["gemini-3.8-flash-high"],
                "xai": ["grok-4.6"],
                "opencode-go": ["glm-5.3"],
            },
        )

    def test_management_membership_wins_and_ids_can_be_shared(self):
        catalog = self._catalog(
            mgmt_groups={
                "devin": ["claude-opus-5-5", "gpt-5.6", "not-served"],
                "claude": ["claude-opus-5-5"],
            }
        )
        groups = cpa.compute_groups(catalog)
        self.assertEqual(groups["devin"], ["claude-opus-5-5", "gpt-5.6"])
        self.assertEqual(groups["claude"], ["claude-opus-5-5"])
        self.assertNotIn("codex", groups)  # gpt-5.6 is covered by management
        self.assertEqual(groups["xai"], ["grok-4.6"])  # not covered: owned_by fallback

    def test_override_patterns_replace_a_group(self):
        groups = cpa.compute_groups(self._catalog(), {"custom": cpa.GroupOverride(models=("g*",))})
        self.assertEqual(
            groups["custom"], ["gpt-5.6", "gemini-3.8-flash-high", "grok-4.6", "glm-5.3"]
        )

    def test_merge_meta_prefers_definitions_and_handles_prefixes(self):
        merged = cpa.merge_meta(
            ["claude-opus-5-5", "pro/claude-opus-5-5"],
            {"claude-opus-5-5": {"context_window": 200000, "supports_vision": False}},
            {"claude-opus-5-5": {"context_window": 1000000}},
        )
        self.assertEqual(
            merged["claude-opus-5-5"], {"context_window": 1000000, "supports_vision": False}
        )
        self.assertEqual(merged["pro/claude-opus-5-5"], {"context_window": 1000000})

    def test_capabilities_keep_only_hermes_keys(self):
        catalog = cpa.Catalog(
            ids=["m"],
            meta={
                "m": {
                    "context_window": 5,
                    "max_output_tokens": 9,
                    "reasoning_efforts": ["low"],
                    "supports_reasoning": True,
                }
            },
        )
        self.assertEqual(
            cpa.capabilities_for(catalog, ["m"]),
            {"m": {"context_window": 5, "supports_reasoning": True}},
        )


class RefreshTests(GatewayTestCase):
    def setUp(self):
        super().setUp()
        cpa._REJECTED.clear()

    def test_rejected_key_is_not_sent_again_from_another_instance(self):
        # Two config instances pointing at one CPA share the process-wide latch.
        first = cpa.refresh_catalog(
            self.root, API_KEY, "wrong", cpa.Catalog(), timeout=5, now=10**9
        )
        self.assertEqual(first.mgmt_status, "auth_failed")
        _Gateway.seen = []
        other = cpa.refresh_catalog(
            self.root, API_KEY, "wrong", cpa.Catalog(), timeout=5, now=10**9
        )
        self.assertEqual(other.mgmt_status, "auth_failed")
        self.assertTrue(other.mgmt_auth_failed)  # persisted by the second instance too
        self.assertEqual(self.mgmt_requests(), [])

    def test_management_off_uses_public_owners(self):
        catalog = cpa.refresh_catalog(self.root, API_KEY, "", cpa.Catalog(), timeout=5, now=1000)
        self.assertEqual(catalog.mgmt_status, "off")
        self.assertEqual(self.mgmt_requests(), [])
        self.assertEqual(cpa.compute_groups(catalog)["claude"], ["claude-opus-5-5"])

    def test_management_ok_enriches_and_regroups(self):
        catalog = cpa.refresh_catalog(
            self.root, API_KEY, MGMT_KEY, cpa.Catalog(), timeout=5, now=1000
        )
        self.assertEqual(catalog.mgmt_status, "ok")
        self.assertEqual(catalog.meta["claude-opus-5-5"]["max_output_tokens"], 64000)
        self.assertEqual(catalog.meta["grok-4.6"]["context_window"], 2000000)
        groups = cpa.compute_groups(catalog)
        self.assertIn("claude-opus-5-5", groups["devin"])
        self.assertNotIn("kimi-k3", groups.get("kimi", []))  # not served on /v1/models

    def test_management_ttl_reuses_previous_result(self):
        first = cpa.refresh_catalog(
            self.root, API_KEY, MGMT_KEY, cpa.Catalog(), timeout=5, now=1000
        )
        _Gateway.seen = []
        second = cpa.refresh_catalog(self.root, API_KEY, MGMT_KEY, first, timeout=5, now=1100)
        self.assertEqual(second.mgmt_status, "cached")
        self.assertEqual(self.mgmt_requests(), [])
        self.assertEqual(second.mgmt_groups, first.mgmt_groups)

    def test_rejected_key_is_never_retried_until_it_changes(self):
        first = cpa.refresh_catalog(self.root, API_KEY, "wrong", cpa.Catalog(), timeout=5, now=1000)
        self.assertEqual(first.mgmt_status, "auth_failed")
        self.assertNotIn("wrong", first.mgmt_auth_failed)
        self.assertEqual(len(self.mgmt_requests()), 1)
        _Gateway.seen = []
        later = cpa.refresh_catalog(self.root, API_KEY, "wrong", first, timeout=5, now=10**9)
        self.assertEqual(later.mgmt_status, "auth_failed")
        self.assertEqual(self.mgmt_requests(), [])
        fixed = cpa.refresh_catalog(self.root, API_KEY, MGMT_KEY, later, timeout=5, now=10**9)
        self.assertEqual(fixed.mgmt_status, "ok")
        self.assertEqual(fixed.mgmt_auth_failed, "")

    def test_unavailable_keeps_previous_results(self):
        good = cpa.refresh_catalog(self.root, API_KEY, MGMT_KEY, cpa.Catalog(), timeout=5, now=1000)
        _Gateway.mgmt_status = 403
        later = cpa.refresh_catalog(
            self.root, API_KEY, MGMT_KEY, good, timeout=5, now=1000 + cpa.MANAGEMENT_TTL + 1
        )
        self.assertEqual(later.mgmt_status, "unavailable")
        self.assertEqual(later.mgmt_groups, good.mgmt_groups)

    def test_configured_but_unreadable_key_keeps_previous_results(self):
        good = cpa.refresh_catalog(self.root, API_KEY, MGMT_KEY, cpa.Catalog(), timeout=5, now=1000)
        _Gateway.seen = []
        later = cpa.refresh_catalog(
            self.root, API_KEY, "", good, mgmt_configured=True, timeout=5, now=10**9
        )
        self.assertEqual(later.mgmt_status, "missing_key")
        self.assertEqual(later.mgmt_groups, good.mgmt_groups)
        self.assertEqual(self.mgmt_requests(), [])


class RouteListTests(unittest.TestCase):
    """The billing gateway's model list: client endpoint only, per gateway key."""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _BillingGateway)
        cls.root = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _BillingGateway.seen = []
        _BillingGateway.status = 200

    def test_lists_ids_for_the_gateway_key_only(self):
        self.assertEqual(
            cpa.fetch_route_ids(self.root, GW_KEY_A, timeout=5),
            ["gpt-5.6", "claude-opus-5-5", "not-on-cpa"],
        )
        self.assertEqual(cpa.fetch_route_ids(self.root, GW_KEY_B, timeout=5), ["grok-4.6"])
        self.assertEqual([r["path"] for r in _BillingGateway.seen], ["/v1/models", "/v1/models"])

    def test_errors_carry_status_and_category_only(self):
        with self.assertRaises(cpa.GatewayError) as ctx:
            cpa.fetch_route_ids(self.root, "wrong", timeout=5)
        self.assertEqual((ctx.exception.status, ctx.exception.category), (401, "auth"))
        self.assertNotIn("secret-body", str(ctx.exception))
        self.assertNotIn("127.0.0.1", str(ctx.exception))

    def test_refuses_public_plain_http_before_connecting(self):
        with self.assertRaises(cpa.GatewayError) as ctx:
            cpa.fetch_route_ids("http://8.8.8.8:1", GW_KEY_A, timeout=5)
        self.assertEqual(ctx.exception.category, "transport_policy")

    def test_route_filter_keeps_cpa_order_and_drops_unsold(self):
        groups = {"claude": ["claude-opus-5-5"], "codex": ["gpt-5.6"], "xai": ["grok-4.6"]}
        sold = {"GW_A": ["gpt-5.6", "claude-opus-5-5", "not-on-cpa"], "GW_B": ["grok-4.6"]}
        spec = cpa.parse_config(
            _instance(
                route_via="https://gw.example",
                route_key_env="GW_A",
                groups={"xai": {"route_key_env": "GW_B"}},
            )
        )[0][0]
        self.assertEqual(
            cpa.filter_routed(groups, spec, sold),
            {"claude": ["claude-opus-5-5"], "codex": ["gpt-5.6"], "xai": ["grok-4.6"]},
        )
        # A key whose gateway list was never read keeps CPA's ids until it is;
        # a known list filters strictly, even when it is empty.
        self.assertEqual(
            cpa.filter_routed(groups, spec, {"GW_A": ["gpt-5.6"]}),
            {"claude": [], "codex": ["gpt-5.6"], "xai": ["grok-4.6"]},
        )
        self.assertEqual(
            cpa.filter_routed(groups, spec, {"GW_A": [], "GW_B": []}),
            {"claude": [], "codex": [], "xai": []},
        )


class FingerprintTests(unittest.TestCase):
    def test_salted_and_matching(self):
        one = cpa.management_fingerprint("k")
        two = cpa.management_fingerprint("k")
        self.assertNotEqual(one, two)
        self.assertTrue(cpa.fingerprint_matches("k", one))
        self.assertFalse(cpa.fingerprint_matches("other", one))
        self.assertFalse(cpa.fingerprint_matches("k", "garbage"))

    def test_corrupt_stored_values_never_raise(self):
        good = cpa.management_fingerprint("k")
        salt = good.split(":")[0]
        for stored in (f"{salt}:short", f"{salt}:é", "", ":", "zz:00", good[:-1]):
            self.assertFalse(cpa.fingerprint_matches("k", stored), stored)


class CacheTests(unittest.TestCase):
    def test_round_trip_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            catalog = cpa.Catalog(
                ids=["claude-opus-5-5", "gpt-5.6"],
                meta={"claude-opus-5-5": {"context_window": 1000000}},
                owners={"claude-opus-5-5": "anthropic", "gpt-5.6": "openai"},
                fetched_at=12.5,
                mgmt_groups={"devin": ["gpt-5.6"]},
                mgmt_defs={"gpt-5.6": {"max_output_tokens": 128000}},
                mgmt_checked_at=11.0,
                mgmt_auth_failed="",
            )
            cpa.save_cache(home, "fast", catalog)
            loaded = cpa.load_cache(home, "fast")
            for attr in (
                "ids",
                "meta",
                "owners",
                "fetched_at",
                "mgmt_groups",
                "mgmt_defs",
                "mgmt_checked_at",
                "mgmt_auth_failed",
            ):
                self.assertEqual(getattr(loaded, attr), getattr(catalog, attr), attr)
            mode = os.stat(cpa.cache_path(home, "fast")).st_mode & 0o777
            self.assertEqual(mode, 0o600)

    def test_reads_v1_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            path = cpa.cache_path(home, "old")
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"version": 1, "ids": ["a"], "meta": {"a": {"x": 1}}}))
            loaded = cpa.load_cache(home, "old")
            self.assertEqual(loaded.ids, ["a"])
            self.assertEqual(loaded.owners, {})

    def test_route_lists_round_trip_and_are_per_key_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            catalog = cpa.Catalog(ids=["a"], route_ids={"GW_A": ["a"], "GW_B": []})
            cpa.save_cache(home, "r", catalog)
            self.assertEqual(cpa.load_cache(home, "r").route_ids, {"GW_A": ["a"], "GW_B": []})
            text = cpa.cache_path(home, "r").read_text()
            self.assertNotIn(GW_KEY_A, text)

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


class VersionTests(unittest.TestCase):
    def test_versions_match(self):
        # The catalog User-Agent carries cpa.VERSION; keep every copy in step.
        root = Path(__file__).resolve().parent.parent
        manifest = (root / "plugin.yaml").read_text(encoding="utf-8")
        project = (root / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(f"\nversion: {cpa.VERSION}\n", manifest)
        self.assertIn(f'\nversion = "{cpa.VERSION}"\n', project)
        self.assertTrue(cpa.USER_AGENT.endswith("/" + cpa.VERSION))


if __name__ == "__main__":
    unittest.main()

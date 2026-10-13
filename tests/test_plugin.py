"""Integration tests against a real Hermes checkout.

Run with Hermes importable, for example:
    PYTHONPATH=/path/to/hermes-agent python -m unittest discover -s tests -v
Skipped when Hermes is not importable.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import providers  # noqa: F401

    HAVE_HERMES = True
except ImportError:
    HAVE_HERMES = False

from test_cpa import GEMINI_LIST, GROK_LIST, OPENAI_LIST, _Gateway  # noqa: E402

import cpa  # noqa: E402


def _load_plugin(module_name: str):
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(HAVE_HERMES, "Hermes not importable")
class PluginRegistrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _Gateway.routes = {
            "/v1/models": OPENAI_LIST,
            "grok": GROK_LIST,
            "/v1beta/models": GEMINI_LIST,
        }
        cls.server = HTTPServer(("127.0.0.1", 0), _Gateway)
        cls.root = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._old_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = str(self.home)
        config = {
            "cliproxyapi": {
                "instances": {
                    "test": {
                        "base_url": self.root + "/v1",
                        "key_env": "CPA_TEST_API_KEY",
                        "groups": {
                            "claude": {"models": ["claude-*"]},
                            "codex": {"models": ["gpt-*"]},
                            "gemini": {"models": ["gemini-*"]},
                        },
                    },
                    "broken": {"base_url": "http://8.8.8.8", "key_env": "X", "groups": {}},
                }
            }
        }
        (self.home / "config.yaml").write_text(json.dumps(config))  # JSON is valid YAML

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = self._old_home
        self.tmp.cleanup()

    def _capture(self):
        captured = []
        import providers as hermes_providers

        original = hermes_providers.register_provider
        hermes_providers.register_provider = captured.append
        try:
            module = _load_plugin(f"cpa_plugin_test_{id(self)}")
        finally:
            hermes_providers.register_provider = original
        return module, {p.name: p for p in captured}

    def test_registers_one_profile_per_declared_group_and_skips_broken(self):
        with self.assertLogs(level="WARNING") as logs:
            _, profiles = self._capture()
        self.assertEqual(
            set(profiles), {"cpa-local", "cpa-test-claude", "cpa-test-codex", "cpa-test-gemini"}
        )
        self.assertTrue(any("broken" in line for line in logs.output))
        claude = profiles["cpa-test-claude"]
        self.assertEqual(claude.api_mode, "anthropic_messages")
        self.assertEqual(claude.base_url, self.root)
        self.assertEqual(claude.env_vars, ("CPA_TEST_API_KEY",))
        self.assertEqual(profiles["cpa-test-codex"].base_url, self.root + "/v1")
        self.assertEqual(profiles["cpa-test-gemini"].api_mode, "chat_completions")
        self.assertEqual(claude.fallback_models, ())

    def test_fetch_models_filters_caches_and_enriches(self):
        _, profiles = self._capture()
        claude = profiles["cpa-test-claude"]
        self.assertEqual(claude.fetch_models(api_key="test-key"), ["claude-opus-5-5"])
        self.assertEqual(
            claude.model_capabilities,
            {"claude-opus-5-5": {"context_window": 1000000, "supports_reasoning": True}},
        )
        self.assertEqual(
            claude.supported_reasoning_efforts("claude-opus-5-5"), ("low", "high", "max")
        )
        gemini = profiles["cpa-test-gemini"]
        self.assertEqual(gemini.fetch_models(api_key="test-key"), ["gemini-3.8-flash-high"])
        self.assertEqual(gemini.get_max_tokens("gemini-3.8-flash-high"), 65536)
        self.assertIsNone(gemini.get_max_tokens("unknown"))
        self.assertTrue(cpa.cache_path(self.home, "test").exists())

        # A fresh process registers from the cache with no network.
        _, again = self._capture()
        self.assertEqual(again["cpa-test-codex"].fallback_models, ("gpt-5.6",))
        self.assertEqual(
            again["cpa-test-gemini"].model_capabilities["gemini-3.8-flash-high"]["context_window"],
            1048576,
        )

    def test_fetch_models_without_key_or_with_bad_key_returns_none(self):
        _, profiles = self._capture()
        codex = profiles["cpa-test-codex"]
        self.assertIsNone(codex.fetch_models(api_key=None))
        with self.assertLogs(level="WARNING") as logs:
            self.assertIsNone(codex.fetch_models(api_key="wrong"))
        joined = "\n".join(logs.output)
        self.assertIn("auth", joined)
        self.assertNotIn("127.0.0.1", joined)
        self.assertNotIn("wrong", joined)

    def test_no_config_registers_only_default(self):
        (self.home / "config.yaml").write_text("model: {}\n")
        _, profiles = self._capture()
        self.assertEqual(set(profiles), {"cpa-local"})
        default = profiles["cpa-local"]
        self.assertEqual(default.api_mode, "chat_completions")
        self.assertEqual(default.base_url, "http://127.0.0.1:8317/v1")
        self.assertEqual(default.env_vars, ("CPA_LOCAL_API_KEY", "CPA_LOCAL_BASE_URL"))

    def test_fetch_models_honours_effective_base_url(self):
        _, profiles = self._capture()
        default = profiles["cpa-local"]
        ids = default.fetch_models(api_key="test-key", base_url=self.root + "/v1")
        self.assertEqual(ids, ["claude-opus-5-5", "gpt-5.6", "gemini-3.8-flash-high"])
        self.assertTrue(cpa.cache_path(self.home, "_local").exists())

    def test_instance_cannot_take_default_name(self):
        _, errors = cpa.parse_config(
            {
                "cliproxyapi": {
                    "instances": {
                        "local": {
                            "base_url": "http://127.0.0.1:8317",
                            "key_env": "K",
                            "groups": {"all": {"models": ["*"]}},
                        }
                    }
                }
            }
        )
        self.assertEqual(errors, [])  # cpa-local-all, not cpa-local

    def test_hyphen_name_collision_is_refused(self):
        group = {"models": ["*"]}
        specs, errors = cpa.parse_config(
            {
                "cliproxyapi": {
                    "instances": {
                        "a": {
                            "base_url": "http://127.0.0.1:1",
                            "key_env": "K",
                            "groups": {"b-c": group},
                        },
                        "a-b": {
                            "base_url": "http://127.0.0.1:2",
                            "key_env": "K",
                            "groups": {"c": group},
                        },
                    }
                }
            }
        )
        self.assertEqual([s.name for s in specs], ["cpa-a-b-c"])
        self.assertEqual(len(errors), 1)
        self.assertIn("a-b", errors[0])
        self.assertNotIn("127.0.0.1", errors[0])

    def test_reads_config_without_pyyaml(self):
        # The Hermes CLI interpreter ships ruamel via hermes_yaml, not PyYAML.
        saved = sys.modules.get("yaml")
        sys.modules["yaml"] = None  # import yaml -> ImportError
        try:
            _, profiles = self._capture()
        finally:
            if saved is None:
                sys.modules.pop("yaml", None)
            else:
                sys.modules["yaml"] = saved
        self.assertIn("cpa-test-claude", profiles)

    def test_profiles_have_no_create_client_override(self):
        _, profiles = self._capture()
        from providers.base import ProviderProfile

        for profile in profiles.values():
            self.assertIs(type(profile).create_client, ProviderProfile.create_client)

    def test_cache_file_is_private(self):
        _, profiles = self._capture()
        profiles["cpa-test-claude"].fetch_models(api_key="test-key")
        mode = os.stat(cpa.cache_path(self.home, "test")).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        data = json.loads(cpa.cache_path(self.home, "test").read_text())
        self.assertNotIn("test-key", json.dumps(data))


if __name__ == "__main__":
    unittest.main()

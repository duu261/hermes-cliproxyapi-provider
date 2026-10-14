"""Integration tests against a real Hermes checkout.

Run with Hermes importable, for example:
    PYTHONPATH=/path/to/hermes-agent python -m unittest discover -s tests -v
Skipped when Hermes is not importable.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
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

from test_cpa import (  # noqa: E402
    API_KEY,
    GW_KEY_A,
    GW_KEY_B,
    MGMT_KEY,
    PUBLIC_ROUTES,
    _BillingGateway,
    _Gateway,
)

import cpa  # noqa: E402

_COUNTER = [0]


def _load_plugin():
    _COUNTER[0] += 1
    name = f"cpa_plugin_test_{_COUNTER[0]}"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _mgmt(seen):
    return [r for r in seen if "/management/" in r["path"]]


@unittest.skipUnless(HAVE_HERMES, "Hermes not importable")
class PluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Gateway)
        cls.root = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Gateway.seen = []
        _Gateway.routes = dict(PUBLIC_ROUTES)
        _Gateway.mgmt_status = 200
        _Gateway.v8 = True
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self._env = {k: os.environ.get(k) for k in ("HERMES_HOME", "T_MGMT")}
        os.environ["HERMES_HOME"] = str(self.home)
        os.environ.pop("T_MGMT", None)
        self.write_config({"t": {"base_url": self.root + "/v1", "key_env": "T_KEY"}})

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def write_config(self, instances):
        config = {"model": {"default": "x"}, "cliproxyapi": {"instances": instances}}
        (self.home / "config.yaml").write_text(json.dumps(config))  # JSON is valid YAML

    def load(self):
        """Import the plugin with register_provider captured; returns (module, profiles)."""
        import providers as hermes_providers

        captured = {}

        def capture(profile):
            captured[profile.name] = profile

        original = hermes_providers.register_provider
        hermes_providers.register_provider = capture
        try:
            module = _load_plugin()
        finally:
            hermes_providers.register_provider = original
        # Late registrations (from fetch_models) also land in `captured`.
        module.register_provider = capture
        return module, captured

    # ── Registration ──

    def test_first_run_registers_only_instance_wide_chat_provider(self):
        _, profiles = self.load()
        self.assertEqual(set(profiles), {"cpa-t"})
        wide = profiles["cpa-t"]
        self.assertEqual(wide.api_mode, "chat_completions")
        self.assertEqual(wide.base_url, self.root + "/v1")
        self.assertEqual(wide.models_url, self.root + "/v1/models")
        self.assertEqual(wide.env_vars, ("T_KEY",))
        self.assertEqual(_Gateway.seen, [])

    def test_fetch_models_groups_and_registers_new_providers(self):
        _, profiles = self.load()
        ids = profiles["cpa-t"].fetch_models(api_key=API_KEY)
        self.assertEqual(len(ids), 5)
        self.assertEqual(
            set(profiles),
            {
                "cpa-t",
                "cpa-t-claude",
                "cpa-t-codex",
                "cpa-t-gemini",
                "cpa-t-xai",
                "cpa-t-opencode-go",
            },
        )
        claude = profiles["cpa-t-claude"]
        self.assertEqual(claude.api_mode, "anthropic_messages")
        self.assertEqual(claude.base_url, self.root)
        self.assertEqual(claude.fallback_models, ("claude-opus-5-5",))
        self.assertEqual(claude.model_capabilities["claude-opus-5-5"]["context_window"], 1000000)
        self.assertEqual(
            claude.supported_reasoning_efforts("claude-opus-5-5"), ("low", "high", "max")
        )
        self.assertEqual(profiles["cpa-t-codex"].api_mode, "codex_responses")
        self.assertEqual(profiles["cpa-t-xai"].api_mode, "codex_responses")
        self.assertEqual(profiles["cpa-t-xai"].base_url, self.root + "/v1")
        self.assertEqual(profiles["cpa-t-opencode-go"].api_mode, "chat_completions")
        gemini = profiles["cpa-t-gemini"]
        self.assertEqual(gemini.get_max_tokens("gemini-3.8-flash-high"), 65536)
        self.assertIsNone(gemini.get_max_tokens("unknown"))
        self.assertEqual(profiles["cpa-t"].fallback_models[0], "claude-opus-5-5")

    def test_restart_registers_groups_from_cache_without_network(self):
        _, first = self.load()
        first["cpa-t"].fetch_models(api_key=API_KEY)
        _Gateway.seen = []
        _, second = self.load()
        self.assertIn("cpa-t-claude", second)
        self.assertEqual(second["cpa-t-claude"].fallback_models, ("claude-opus-5-5",))
        self.assertEqual(_Gateway.seen, [])
        path = cpa.cache_path(self.home, "t")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertNotIn(API_KEY, path.read_text())

    def test_profiles_of_one_instance_share_one_refresh(self):
        _, profiles = self.load()
        profiles["cpa-t"].fetch_models(api_key=API_KEY)
        _Gateway.seen = []
        self.assertEqual(
            profiles["cpa-t-claude"].fetch_models(api_key=API_KEY), ["claude-opus-5-5"]
        )
        self.assertEqual(_Gateway.seen, [])

    def test_fetch_models_without_key_or_with_bad_key_returns_none(self):
        _, profiles = self.load()
        wide = profiles["cpa-t"]
        self.assertIsNone(wide.fetch_models(api_key=None))
        with self.assertLogs(level="WARNING") as logs:
            self.assertIsNone(wide.fetch_models(api_key="wrong"))
        joined = "\n".join(logs.output)
        self.assertIn("auth", joined)
        self.assertNotIn("127.0.0.1", joined)
        self.assertNotIn("wrong", joined)

    def test_fetch_models_honours_effective_base_url(self):
        self.write_config({"t": {"base_url": "http://127.0.0.1:1", "key_env": "T_KEY"}})
        _, profiles = self.load()
        ids = profiles["cpa-t"].fetch_models(api_key=API_KEY, base_url=self.root + "/v1")
        self.assertEqual(len(ids), 5)

    def test_no_config_registers_default_local(self):
        (self.home / "config.yaml").write_text("model: {}\n")
        _, profiles = self.load()
        self.assertEqual(set(profiles), {"cpa-local"})
        local = profiles["cpa-local"]
        self.assertEqual(local.base_url, "http://127.0.0.1:8317/v1")
        self.assertEqual(local.env_vars, ("CPA_LOCAL_API_KEY", "CPA_LOCAL_BASE_URL"))

    def test_broken_instance_is_skipped_with_safe_warning(self):
        self.write_config(
            {
                "t": {"base_url": self.root, "key_env": "T_KEY"},
                "broken": {"base_url": "http://8.8.8.8", "key_env": "X"},
            }
        )
        with self.assertLogs(level="WARNING") as logs:
            _, profiles = self.load()
        self.assertEqual(set(profiles), {"cpa-t"})
        self.assertTrue(any("broken" in line for line in logs.output))
        self.assertFalse(any("8.8.8.8" in line for line in logs.output))

    def test_overrides_change_wire_and_membership(self):
        self.write_config(
            {
                "t": {
                    "base_url": self.root,
                    "key_env": "T_KEY",
                    "groups": {
                        "xai": {"api_mode": "chat_completions"},
                        "picks": {"models": ["gpt-*", "glm-*"], "display_name": "Picks"},
                    },
                }
            }
        )
        _, profiles = self.load()
        profiles["cpa-t"].fetch_models(api_key=API_KEY)
        self.assertEqual(profiles["cpa-t-xai"].api_mode, "chat_completions")
        picks = profiles["cpa-t-picks"]
        self.assertEqual(picks.fallback_models, ("gpt-5.6", "glm-5.3"))
        self.assertEqual(picks.display_name, "Picks")
        self.assertEqual(picks.api_mode, "chat_completions")

    def test_hyphen_name_collision_is_refused(self):
        self.write_config(
            {
                "a": {
                    "base_url": self.root,
                    "key_env": "T_KEY",
                    "groups": {"b-c": {"models": ["gpt-*"]}},
                },
                "a-b": {
                    "base_url": self.root,
                    "key_env": "T_KEY",
                    "groups": {"c": {"models": ["glm-*"]}},
                },
            }
        )
        _, profiles = self.load()
        with self.assertLogs(level="WARNING") as logs:
            profiles["cpa-a"].fetch_models(api_key=API_KEY)
            profiles["cpa-a-b"].fetch_models(api_key=API_KEY)
        self.assertEqual(profiles["cpa-a-b-c"].fallback_models, ("gpt-5.6",))
        self.assertTrue(any("already used" in line for line in logs.output))

    # ── Management ──

    def test_management_regroups_and_enriches(self):
        os.environ["T_MGMT"] = MGMT_KEY
        self.write_config(
            {"t": {"base_url": self.root, "key_env": "T_KEY", "mgmt_key_env": "T_MGMT"}}
        )
        _, profiles = self.load()
        profiles["cpa-t"].fetch_models(api_key=API_KEY)
        devin = profiles["cpa-t-devin"]
        self.assertEqual(devin.api_mode, "chat_completions")
        self.assertEqual(devin.fallback_models, ("claude-opus-5-5", "gpt-5.6"))
        claude = profiles["cpa-t-claude"]
        self.assertEqual(claude.get_max_tokens("claude-opus-5-5"), 64000)
        self.assertTrue(claude.model_capabilities["claude-opus-5-5"]["supports_vision"])
        cache = cpa.cache_path(self.home, "t").read_text()
        self.assertNotIn(MGMT_KEY, cache)
        self.assertNotIn("leak-me", cache)
        self.assertNotIn("devin-1.json", cache)

    def test_rejected_management_key_is_tried_once_across_restarts(self):
        os.environ["T_MGMT"] = "wrong-mgmt"
        self.write_config(
            {"t": {"base_url": self.root, "key_env": "T_KEY", "mgmt_key_env": "T_MGMT"}}
        )
        _, profiles = self.load()
        with self.assertLogs(level="WARNING") as logs:
            profiles["cpa-t"].fetch_models(api_key=API_KEY)
        self.assertTrue(any("management auth failed" in line for line in logs.output))
        self.assertFalse(any("wrong-mgmt" in line for line in logs.output))
        self.assertIn("cpa-t-claude", profiles)  # public grouping still works
        self.assertEqual(len(_mgmt(_Gateway.seen)), 1)
        _Gateway.seen = []
        _, again = self.load()
        with self.assertLogs(level="WARNING"):
            again["cpa-t"].fetch_models(api_key=API_KEY)
        self.assertEqual(_mgmt(_Gateway.seen), [])

    # ── Contract ──

    def test_profiles_have_no_create_client_override_or_ua(self):
        _, profiles = self.load()
        profiles["cpa-t"].fetch_models(api_key=API_KEY)
        from providers.base import ProviderProfile

        for profile in profiles.values():
            self.assertIs(type(profile).create_client, ProviderProfile.create_client)
            self.assertNotIn("User-Agent", profile.default_headers or {})

    def test_reads_config_without_pyyaml(self):
        # The Hermes interpreter ships ruamel.yaml, not PyYAML.
        saved = sys.modules.get("yaml")
        sys.modules["yaml"] = None  # type: ignore[assignment]
        try:
            _, profiles = self.load()
        finally:
            if saved is None:
                sys.modules.pop("yaml", None)
            else:
                sys.modules["yaml"] = saved
        self.assertIn("cpa-t", profiles)

    def test_late_registration_reaches_hermes_registry(self):
        import providers as hermes_providers

        _load_plugin()
        wide = hermes_providers.get_provider_profile("cpa-t")
        self.assertIsNotNone(wide)
        wide.fetch_models(api_key=API_KEY)
        self.assertIsNotNone(hermes_providers.get_provider_profile("cpa-t-claude"))

    def test_late_registration_lands_in_the_importing_home_layer(self):
        # Multi-profile gateways import each home's plugins with a per-home
        # registration target; discoveries after import must land there too.
        import providers as hermes_providers

        layer = hermes_providers._HomeLayer()
        token = hermes_providers._REGISTRATION_TARGET.set(layer)
        try:
            _load_plugin()
        finally:
            hermes_providers._REGISTRATION_TARGET.reset(token)
        self.assertIn("cpa-t", layer.registry)
        before = set(hermes_providers._REGISTRY)
        layer.registry["cpa-t"].fetch_models(api_key=API_KEY)
        self.assertIn("cpa-t-claude", layer.registry)
        self.assertEqual(set(hermes_providers._REGISTRY), before)

    def test_capabilities_are_swapped_not_mutated(self):
        _, profiles = self.load()
        wide = profiles["cpa-t"]
        old = wide.model_capabilities
        wide.fetch_models(api_key=API_KEY)
        self.assertIsNot(wide.model_capabilities, old)
        self.assertEqual(old, {})

    def test_cache_write_failure_after_rejected_key_warns(self):
        os.environ["T_MGMT"] = "wrong-mgmt-2"
        self.write_config(
            {"t": {"base_url": self.root, "key_env": "T_KEY", "mgmt_key_env": "T_MGMT"}}
        )
        module, profiles = self.load()
        real = module.cpa.save_cache

        def broken(*_a, **_k):
            raise PermissionError("read-only")

        module.cpa.save_cache = broken
        try:
            with self.assertLogs(level="WARNING") as logs:
                profiles["cpa-t"].fetch_models(api_key=API_KEY)
        finally:
            module.cpa.save_cache = real
        joined = "\n".join(logs.output)
        self.assertIn("tried again at next start", joined)
        self.assertIn("T_MGMT", joined)
        self.assertNotIn("wrong-mgmt-2", joined)


ROUTED = {"cpa-t", "cpa-t-origin"}


@unittest.skipUnless(HAVE_HERMES, "Hermes not importable")
class BillingRouteTests(unittest.TestCase):
    """Discovery from CPA, inference and the sold-model list through a billing gateway."""

    @classmethod
    def setUpClass(cls):
        cls.cpa = HTTPServer(("127.0.0.1", 0), _Gateway)
        cls.gw = HTTPServer(("127.0.0.1", 0), _BillingGateway)
        cls.cpa_root = f"http://127.0.0.1:{cls.cpa.server_address[1]}"
        cls.gw_root = f"http://127.0.0.1:{cls.gw.server_address[1]}"
        for server in (cls.cpa, cls.gw):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.cpa, cls.gw):
            server.shutdown()
            server.server_close()

    def setUp(self):
        _Gateway.seen, _BillingGateway.seen = [], []
        _Gateway.routes = dict(PUBLIC_ROUTES)
        _Gateway.mgmt_status = 200
        _BillingGateway.status = 200
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        names = ("HERMES_HOME", "T_KEY", "GW_A", "GW_B")
        self._env = {k: os.environ.get(k) for k in names}
        os.environ["HERMES_HOME"] = str(self.home)
        os.environ["T_KEY"] = API_KEY
        os.environ["GW_A"] = GW_KEY_A
        os.environ["GW_B"] = GW_KEY_B
        instance = {
            "base_url": self.cpa_root,
            "key_env": "T_KEY",
            "route_via": self.gw_root + "/v1",
            "route_key_env": "GW_A",
            "groups": {"xai": {"route_key_env": "GW_B", "api_mode": "chat_completions"}},
        }
        config = {"model": {"default": "x"}, "cliproxyapi": {"instances": {"t": instance}}}
        (self.home / "config.yaml").write_text(json.dumps(config))

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    load = PluginTests.load

    def test_profiles_point_at_the_gateway_with_its_key(self):
        _, profiles = self.load()
        wide = profiles["cpa-t"]
        # Hermes reads the profile's key from env_vars[0] and sends it to base_url.
        self.assertEqual(wide.base_url, self.gw_root + "/v1")
        self.assertEqual(wide.env_vars[0], "GW_A")
        self.assertNotIn("T_KEY", wide.env_vars)
        self.assertEqual(_Gateway.seen, [])
        self.assertEqual(_BillingGateway.seen, [])

    def test_discovery_on_cpa_and_only_sold_models_registered(self):
        _, profiles = self.load()
        ids = profiles["cpa-t"].fetch_models(api_key=GW_KEY_A)
        # Discovery used the CPA key at CPA only; the gateway saw only gateway keys.
        self.assertTrue(_Gateway.seen)
        self.assertTrue(all(r["auth"] == f"Bearer {API_KEY}" for r in _Gateway.seen))
        gw_auth = {r["auth"] for r in _BillingGateway.seen}
        self.assertEqual(gw_auth, {f"Bearer {GW_KEY_A}", f"Bearer {GW_KEY_B}"})
        self.assertEqual({r["path"] for r in _BillingGateway.seen}, {"/v1/models"})
        # Sold by key A: gpt-5.6, claude-opus-5-5. Sold by key B: grok-4.6.
        self.assertEqual(set(profiles), ROUTED | {"cpa-t-claude", "cpa-t-codex", "cpa-t-xai"})
        self.assertEqual(ids, ["claude-opus-5-5", "gpt-5.6"])
        claude = profiles["cpa-t-claude"]
        self.assertEqual(claude.api_mode, "anthropic_messages")
        self.assertEqual(claude.base_url, self.gw_root)
        self.assertEqual(claude.env_vars[0], "GW_A")
        # CPA metadata still applies through the gateway.
        self.assertEqual(claude.model_capabilities["claude-opus-5-5"]["context_window"], 1000000)
        xai = profiles["cpa-t-xai"]
        self.assertEqual(xai.api_mode, "chat_completions")
        self.assertEqual(xai.env_vars[0], "GW_B")
        self.assertEqual(xai.fallback_models, ("grok-4.6",))

    def test_restart_uses_cached_gateway_lists_without_network(self):
        _, first = self.load()
        first["cpa-t"].fetch_models(api_key=GW_KEY_A)
        _Gateway.seen, _BillingGateway.seen = [], []
        _, second = self.load()
        self.assertEqual(set(second), ROUTED | {"cpa-t-claude", "cpa-t-codex", "cpa-t-xai"})
        self.assertEqual(second["cpa-t-xai"].fallback_models, ("grok-4.6",))
        self.assertEqual((_Gateway.seen, _BillingGateway.seen), ([], []))
        text = cpa.cache_path(self.home, "t").read_text()
        for secret in (API_KEY, GW_KEY_A, GW_KEY_B):
            self.assertNotIn(secret, text)

    def test_gateway_list_failure_keeps_last_known_list(self):
        _, profiles = self.load()
        profiles["cpa-t"].fetch_models(api_key=GW_KEY_A)
        _BillingGateway.status = 503
        _, again = self.load()
        with self.assertLogs(level="WARNING") as logs:
            again["cpa-t"].fetch_models(api_key=GW_KEY_A)
        self.assertEqual(again["cpa-t-xai"].fallback_models, ("grok-4.6",))
        joined = "\n".join(logs.output)
        self.assertIn("GW_A", joined)
        self.assertNotIn(GW_KEY_A, joined)
        self.assertNotIn("127.0.0.1", joined)

    def test_no_gateway_list_ever_registers_no_group_providers(self):
        _BillingGateway.status = 503
        _, profiles = self.load()
        with self.assertLogs(level="WARNING"):
            ids = profiles["cpa-t"].fetch_models(api_key=GW_KEY_A)
        self.assertEqual(ids, [])
        self.assertEqual(set(profiles), ROUTED)

    def test_cpa_key_name_stays_registered_without_reaching_the_gateway(self):
        _, profiles = self.load()
        origin = profiles["cpa-t-origin"]
        # Hermes strips registered provider key names from agent subprocesses.
        self.assertEqual(origin.env_vars, ("T_KEY",))
        self.assertTrue(origin.base_url.startswith(self.cpa_root))
        self.assertTrue(origin.models_url.startswith(self.cpa_root))
        self.assertTrue(origin.hidden)
        self.assertFalse(origin.supports_health_check)
        # No gateway-facing profile carries the CPA key name.
        for name, profile in profiles.items():
            if name != "cpa-t-origin":
                self.assertNotIn("T_KEY", profile.env_vars, name)
                self.assertTrue(profile.base_url.startswith(self.gw_root), name)

    def test_unrouted_instance_has_no_origin_profile(self):
        config = json.loads((self.home / "config.yaml").read_text())
        inst = config["cliproxyapi"]["instances"]["t"]
        for key in ("route_via", "route_key_env"):
            inst.pop(key)
        inst["groups"]["xai"].pop("route_key_env")
        (self.home / "config.yaml").write_text(json.dumps(config))
        _, profiles = self.load()
        self.assertEqual(set(profiles), {"cpa-t"})

    def test_missing_cpa_key_cannot_fall_back_to_the_gateway_key(self):
        os.environ.pop("T_KEY")
        _, profiles = self.load()
        with self.assertLogs(level="WARNING") as logs:
            self.assertIsNone(profiles["cpa-t"].fetch_models(api_key=GW_KEY_A))
        self.assertTrue(any("T_KEY" in line for line in logs.output))
        self.assertEqual(_Gateway.seen, [])
        self.assertEqual(_BillingGateway.seen, [])


@unittest.skipUnless(HAVE_HERMES, "Hermes not importable")
class NoNetworkAtImportTests(unittest.TestCase):
    def test_import_opens_no_sockets(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("HERMES_HOME")
            os.environ["HERMES_HOME"] = tmp
            Path(tmp, "config.yaml").write_text(
                json.dumps(
                    {
                        "cliproxyapi": {
                            "instances": {
                                "t": {
                                    "base_url": "http://127.0.0.1:9",
                                    "key_env": "T_KEY",
                                    "mgmt_key_env": "T_MGMT",
                                }
                            }
                        }
                    }
                )
            )
            import providers as hermes_providers

            real = socket.socket.connect
            original = hermes_providers.register_provider

            def refuse(*_a, **_k):
                raise AssertionError("network at import")

            socket.socket.connect = refuse
            hermes_providers.register_provider = lambda p: None
            try:
                _load_plugin()
            finally:
                socket.socket.connect = real
                hermes_providers.register_provider = original
                if old is None:
                    os.environ.pop("HERMES_HOME", None)
                else:
                    os.environ["HERMES_HOME"] = old


if __name__ == "__main__":
    unittest.main()

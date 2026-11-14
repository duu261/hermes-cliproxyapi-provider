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

    # ── Wire per model on the instance-wide provider (Hermes 0.22.6 and later) ──

    def test_instance_wide_provider_names_each_models_wire(self):
        _, profiles = self.load()
        wide = profiles["cpa-t"]
        # Nothing listed yet: every model stays on the chat wire.
        self.assertEqual(wide.api_mode_for_model("claude-opus-5-5"), "chat_completions")
        wide.fetch_models(api_key=API_KEY)
        self.assertEqual(wide.api_mode, "chat_completions")
        self.assertEqual(wide.base_url, self.root + "/v1")
        for model, mode in (
            ("claude-opus-5-5", "anthropic_messages"),
            ("gpt-5.6", "codex_responses"),
            ("grok-4.6", "codex_responses"),
            ("gemini-3.8-flash-high", "chat_completions"),
            ("glm-5.3", "chat_completions"),
            # Never None: Hermes keeps a saved wire when a profile gives none.
            ("not-served", "chat_completions"),
            ("", "chat_completions"),
        ):
            self.assertEqual(wide.api_mode_for_model(model), mode, model)
            self.assertEqual(wide.api_mode_for_model(model, api_key=API_KEY), mode, model)

    def test_only_the_instance_wide_provider_picks_a_wire_per_model(self):
        from providers.base import ProviderProfile

        _, profiles = self.load()
        profiles["cpa-t"].fetch_models(api_key=API_KEY)
        # Hermes treats a profile that overrides the hook as one that serves
        # several wires; a provider profile has one wire and must not.
        base = getattr(ProviderProfile, "api_mode_for_model", None)
        for name, profile in profiles.items():
            own = getattr(type(profile), "api_mode_for_model", None)
            self.assertEqual(own is not base, name == "cpa-t", name)

    def test_wire_per_model_follows_overrides_and_shared_ids(self):
        self.write_config(
            {
                "t": {
                    "base_url": self.root,
                    "key_env": "T_KEY",
                    "groups": {
                        "xai": {"api_mode": "chat_completions"},
                        "picks": {"models": ["gpt-*", "glm-*"]},
                        "mine": {
                            "models": ["claude-*", "gemini-*"],
                            "api_mode": "anthropic_messages",
                        },
                    },
                }
            }
        )
        _, profiles = self.load()
        wide = profiles["cpa-t"]
        wide.fetch_models(api_key=API_KEY)
        self.assertEqual(wide.api_mode_for_model("grok-4.6"), "chat_completions")
        # picks is a named list on the default wire: gpt-5.6 is still a codex model.
        self.assertEqual(profiles["cpa-t-picks"].api_mode, "chat_completions")
        self.assertEqual(wide.api_mode_for_model("gpt-5.6"), "codex_responses")
        self.assertEqual(wide.api_mode_for_model("claude-opus-5-5"), "anthropic_messages")
        # gemini serves it on chat, mine on Messages: no single wire, so chat.
        self.assertEqual(wide.api_mode_for_model("gemini-3.8-flash-high"), "chat_completions")

    # ── Picker row, key hint and refresh (Hermes 0.22.6 and later) ──

    def test_profiles_of_one_instance_share_one_picker_group(self):
        self.write_config(
            {
                "t": {
                    "base_url": self.root,
                    "key_env": "T_KEY",
                    "groups": {"picks": {"models": ["gpt-*"], "display_name": "Picks"}},
                },
                "u": {"base_url": self.root, "key_env": "T_KEY", "display_name": "Second"},
            }
        )
        _, profiles = self.load()
        profiles["cpa-t"].fetch_models(api_key=API_KEY)
        profiles["cpa-u"].fetch_models(api_key=API_KEY)
        note = "every model, or one CPA provider"
        self.assertIn("cpa-t-picks", profiles)
        self.assertIn("cpa-u-claude", profiles)
        for name, profile in profiles.items():
            second = name == "cpa-u" or name.startswith("cpa-u-")
            want = ("cpa-u", "Second", note) if second else ("cpa-t", "CLIProxyAPI t", note)
            self.assertEqual(profile.picker_group, want, name)

    def test_missing_key_hint_names_the_variable_and_the_kind_of_key(self):
        _, profiles = self.load()
        profiles["cpa-t"].fetch_models(api_key=API_KEY)
        want = "T_KEY takes a CPA client key (one of CPA's api-keys), not the management key."
        for name in ("cpa-t", "cpa-t-claude"):
            self.assertEqual(profiles[name].missing_credentials_hint(), want, name)

    def test_model_refresh_asks_cpa_again_but_not_management(self):
        os.environ["T_MGMT"] = MGMT_KEY
        self.write_config(
            {"t": {"base_url": self.root, "key_env": "T_KEY", "mgmt_key_env": "T_MGMT"}}
        )
        _, profiles = self.load()
        wide = profiles["cpa-t"]
        wide.fetch_models(api_key=API_KEY)
        self.assertTrue(_mgmt(_Gateway.seen))
        _Gateway.seen = []
        wide.fetch_models(api_key=API_KEY)
        self.assertEqual(_Gateway.seen, [])  # one listing serves 30 seconds
        cache = cpa.cache_path(self.home, "t")
        before = cache.read_text()
        # Hermes calls the hook on every profile; any profile of the instance counts.
        self.assertIsNone(profiles["cpa-t-claude"].invalidate_catalog_cache())
        self.assertEqual(_Gateway.seen, [])  # the hook sends nothing
        self.assertEqual(cache.read_text(), before)  # startup still registers from it
        self.assertTrue(wide.fetch_models(api_key=API_KEY))
        self.assertIn("/v1/models", {r["path"] for r in _Gateway.seen})
        # The management pass keeps its own 15 minute pace.
        self.assertEqual(_mgmt(_Gateway.seen), [])

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

    # ── Reasoning effort projection ──

    def test_chat_wire_emits_and_clamps_reasoning_effort(self):
        _, profiles = self.load()
        wide = profiles["cpa-t"]
        wide.fetch_models(api_key=API_KEY)
        gemini = profiles["cpa-t-gemini"]
        claude = profiles["cpa-t-claude"]

        # Unset or empty reasoning config adds nothing (server owns default).
        self.assertEqual(gemini.build_api_kwargs_extras(model="gemini-3.8-flash-high"), ({}, {}))
        self.assertEqual(
            gemini.build_api_kwargs_extras(
                reasoning_config={"enabled": True}, model="gemini-3.8-flash-high"
            ),
            ({}, {}),
        )
        # Disabling thinking emits reasoning_effort="none" even when "none" is omitted from catalog.
        self.assertEqual(
            gemini.build_api_kwargs_extras(
                reasoning_config={"enabled": False, "effort": "none"},
                model="gemini-3.8-flash-high",
            ),
            ({}, {"reasoning_effort": "none"}),
        )
        self.assertEqual(
            gemini.build_api_kwargs_extras(
                reasoning_config={"enabled": False}, model="gemini-3.8-flash-high"
            ),
            ({}, {"reasoning_effort": "none"}),
        )
        # Catalog efforts ("low", "high", "max") clamp medium down to nearest weaker ("low").
        wide._cpa_instance.catalog.meta["gemini-3.8-flash-high"]["reasoning_efforts"] = [
            "minimal",
            "high",
        ]
        self.assertEqual(
            gemini.build_api_kwargs_extras(
                reasoning_config={"enabled": True, "effort": "medium"},
                model="gemini-3.8-flash-high",
            ),
            ({}, {"reasoning_effort": "minimal"}),
        )
        # Below-minimum clamps up to weakest supported.
        wide._cpa_instance.catalog.meta["gemini-3.8-flash-high"]["reasoning_efforts"] = [
            "low",
            "medium",
            "high",
        ]
        self.assertEqual(
            gemini.build_api_kwargs_extras(
                reasoning_config={"enabled": True, "effort": "minimal"},
                model="gemini-3.8-flash-high",
            ),
            ({}, {"reasoning_effort": "low"}),
        )
        # Unlisted model keeps OpenAI-compat levels while clamping Hermes-internal "ultra".
        self.assertEqual(
            wide.build_api_kwargs_extras(
                reasoning_config={"enabled": True, "effort": "xhigh"}, model="not-in-catalog"
            ),
            ({}, {"reasoning_effort": "xhigh"}),
        )
        self.assertEqual(
            wide.build_api_kwargs_extras(
                reasoning_config={"enabled": True, "effort": "ultra"}, model="not-in-catalog"
            ),
            ({}, {"reasoning_effort": "max"}),
        )
        # Models explicitly marked non-reasoning emit nothing.
        wide._cpa_instance.catalog.meta["plain-model"] = {"supports_reasoning": False}
        self.assertEqual(
            wide.build_api_kwargs_extras(
                reasoning_config={"enabled": True, "effort": "high"}, model="plain-model"
            ),
            ({}, {}),
        )
        # Extra Hermes context kwargs (present and future) are accepted without error.
        self.assertEqual(
            gemini.build_api_kwargs_extras(
                reasoning_config={"enabled": True, "effort": "high"},
                model="gemini-3.8-flash-high",
                cache_scope_id="scope-1",
                lmstudio_reasoning_options=["low", "high"],
            ),
            ({}, {"reasoning_effort": "high"}),
        )
        # Non-chat auxiliary calls pass extra_body.reasoning on both Hermes versions.
        self.assertEqual(
            claude.build_api_kwargs_extras(
                reasoning_config={"enabled": True, "effort": "medium"}, model="claude-opus-5-5"
            ),
            ({"reasoning": {"enabled": True, "effort": "low"}}, {}),
        )
        self.assertEqual(
            profiles["cpa-t-codex"].build_api_kwargs_extras(
                reasoning_config={"enabled": False}, model="gpt-5.6"
            ),
            ({"reasoning": {"enabled": False, "effort": "none"}}, {}),
        )

    def test_wide_profile_respects_hermes_per_model_wire_capability(self):
        from providers.base import ProviderProfile

        _, profiles = self.load()
        wide = profiles["cpa-t"]
        wide.fetch_models(api_key=API_KEY)
        has_hook = hasattr(ProviderProfile, "api_mode_for_model")
        got = wide.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "medium"}, model="claude-opus-5-5"
        )
        if has_hook:
            self.assertEqual(got, ({"reasoning": {"enabled": True, "effort": "low"}}, {}))
        else:
            self.assertEqual(got, ({}, {"reasoning_effort": "low"}))

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
        self.assertEqual(set(profiles), {"cpa-t", "cpa-t-claude", "cpa-t-codex", "cpa-t-xai"})
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

    def test_wide_wire_comes_from_the_provider_not_from_the_key_that_sells_it(self):
        config = json.loads((self.home / "config.yaml").read_text())
        config["cliproxyapi"]["instances"]["t"]["groups"]["codex"] = {"route_key_env": "GW_B"}
        (self.home / "config.yaml").write_text(json.dumps(config))
        _, profiles = self.load()
        wide = profiles["cpa-t"]
        ids = wide.fetch_models(api_key=GW_KEY_A)
        # Key B does not sell gpt-5.6, so there is no codex profile; the
        # instance key does, and the model is still a codex model.
        self.assertIn("gpt-5.6", ids)
        self.assertNotIn("cpa-t-codex", profiles)
        self.assertEqual(wide.api_mode_for_model("gpt-5.6"), "codex_responses")
        self.assertEqual(wide.api_mode_for_model("claude-opus-5-5"), "anthropic_messages")
        self.assertEqual(wide.api_mode_for_model("grok-4.6"), "chat_completions")
        self.assertEqual(wide.base_url, self.gw_root + "/v1")

    def test_missing_key_hint_names_the_gateway_key_and_the_cpa_key(self):
        _, profiles = self.load()
        profiles["cpa-t"].fetch_models(api_key=GW_KEY_A)
        for name, env_name in (("cpa-t", "GW_A"), ("cpa-t-claude", "GW_A"), ("cpa-t-xai", "GW_B")):
            self.assertEqual(
                profiles[name].missing_credentials_hint(),
                f"{env_name} takes the billing gateway's key; T_KEY takes the CPA client key.",
                name,
            )

    def test_model_refresh_reads_cpa_and_the_gateway_again(self):
        _, profiles = self.load()
        profiles["cpa-t"].fetch_models(api_key=GW_KEY_A)
        _Gateway.seen, _BillingGateway.seen = [], []
        profiles["cpa-t"].fetch_models(api_key=GW_KEY_A)
        self.assertEqual((_Gateway.seen, _BillingGateway.seen), ([], []))
        profiles["cpa-t-xai"].invalidate_catalog_cache()
        self.assertEqual((_Gateway.seen, _BillingGateway.seen), ([], []))
        profiles["cpa-t"].fetch_models(api_key=GW_KEY_A)
        # Each key still goes only to its own side.
        self.assertTrue(_Gateway.seen)
        self.assertTrue(all(r["auth"] == f"Bearer {API_KEY}" for r in _Gateway.seen))
        gw_auth = {r["auth"] for r in _BillingGateway.seen}
        self.assertEqual(gw_auth, {f"Bearer {GW_KEY_A}", f"Bearer {GW_KEY_B}"})

    def test_restart_uses_cached_gateway_lists_without_network(self):
        _, first = self.load()
        first["cpa-t"].fetch_models(api_key=GW_KEY_A)
        _Gateway.seen, _BillingGateway.seen = [], []
        _, second = self.load()
        self.assertEqual(set(second), {"cpa-t", "cpa-t-claude", "cpa-t-codex", "cpa-t-xai"})
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

    def test_import_registers_groups_from_cpa_cache_before_any_gateway_list(self):
        # The cache was written before route_via was set: CPA's groups, no gateway lists.
        catalog = cpa.Catalog(
            ids=["claude-opus-5-5", "grok-4.6"],
            owners={"claude-opus-5-5": "anthropic", "grok-4.6": "xai"},
        )
        cpa.save_cache(self.home, "t", catalog)
        _, profiles = self.load()
        self.assertEqual(set(profiles), {"cpa-t", "cpa-t-claude", "cpa-t-xai"})
        self.assertEqual(profiles["cpa-t-xai"].fallback_models, ("grok-4.6",))
        self.assertEqual(profiles["cpa-t-xai"].env_vars[0], "GW_B")
        self.assertEqual(profiles["cpa-t"].fallback_models, ("claude-opus-5-5", "grok-4.6"))
        self.assertEqual((_Gateway.seen, _BillingGateway.seen), ([], []))

    def test_import_with_empty_gateway_lists_registers_no_groups(self):
        # A known list is strict even when empty: nothing sold, nothing new registered.
        catalog = cpa.Catalog(
            ids=["claude-opus-5-5", "grok-4.6"],
            owners={"claude-opus-5-5": "anthropic", "grok-4.6": "xai"},
            route_ids={"GW_A": [], "GW_B": []},
        )
        cpa.save_cache(self.home, "t", catalog)
        _, profiles = self.load()
        self.assertEqual(set(profiles), {"cpa-t"})
        self.assertEqual(profiles["cpa-t"].fallback_models, ())

    def test_new_routed_instance_on_a_cached_root_registers_at_import(self):
        # "t" is already cached; "s" is added later on the same CPA, routed.
        instances = {
            "t": {"base_url": self.cpa_root, "key_env": "T_KEY"},
            "s": {
                "base_url": self.cpa_root + "/v1",
                "key_env": "T_KEY",
                "route_via": self.gw_root,
                "route_key_env": "GW_A",
            },
        }
        config = {"model": {"default": "x"}, "cliproxyapi": {"instances": instances}}
        (self.home / "config.yaml").write_text(json.dumps(config))
        catalog = cpa.Catalog(
            ids=["claude-opus-5-5", "grok-4.6"],
            meta={"claude-opus-5-5": {"context_window": 1000000}},
            owners={"claude-opus-5-5": "anthropic", "grok-4.6": "xai"},
            route_ids={"OTHER_GW": []},  # another instance's gateway state: not borrowed
        )
        cpa.save_cache(self.home, "t", catalog)
        _, profiles = self.load()
        self.assertIn("cpa-s-claude", profiles)
        claude = profiles["cpa-s-claude"]
        self.assertEqual(claude.base_url, self.gw_root)
        self.assertEqual(claude.env_vars[0], "GW_A")
        self.assertEqual(claude.fallback_models, ("claude-opus-5-5",))
        self.assertEqual(claude.model_capabilities["claude-opus-5-5"]["context_window"], 1000000)
        self.assertEqual((_Gateway.seen, _BillingGateway.seen), ([], []))
        # Borrowed, not copied: "s" gets its own cache only from a real listing.
        self.assertFalse(cpa.cache_path(self.home, "s").exists())
        profiles["cpa-s"].fetch_models(api_key=GW_KEY_A)
        own = cpa.load_cache(self.home, "s")
        self.assertEqual(set(own.route_ids), {"GW_A"})
        self.assertEqual(cpa.load_cache(self.home, "t").route_ids, {"OTHER_GW": []})

    def test_unknown_gateway_list_keeps_cpa_ids_until_it_is_read(self):
        _BillingGateway.status = 503
        _, profiles = self.load()
        with self.assertLogs(level="WARNING") as logs:
            ids = profiles["cpa-t"].fetch_models(api_key=GW_KEY_A)
        self.assertEqual(len(ids), 5)
        self.assertIn("cpa-t-gemini", profiles)
        self.assertIn("CPA's list", "\n".join(logs.output))
        # The first gateway list narrows every profile to what its key sells.
        _BillingGateway.status = 200
        _, again = self.load()
        ids = again["cpa-t"].fetch_models(api_key=GW_KEY_A)
        self.assertEqual(ids, ["claude-opus-5-5", "gpt-5.6"])
        self.assertEqual(again["cpa-t-xai"].fallback_models, ("grok-4.6",))
        self.assertEqual(again["cpa-t-gemini"].fallback_models, ())

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

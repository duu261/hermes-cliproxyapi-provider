"""View and argument tests for ``/cpa`` (no network: rejected or keyless pools)."""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
import tempfile
import unittest
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import status  # noqa: E402

import cpa  # noqa: E402

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)


def iso(minutes: float) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat()


LISTING = [
    {
        "provider": "codex",
        "name": "codex-alice@example.com.json",
        "status": "active",
        "email": "alice@example.com",
        "account": "alice@example.com",
        "recent_requests": [{"time": "x", "success": 3, "failed": 1}],
        "quota": {
            "observed_at": iso(-4),
            "signals": {"X-Codex-Primary-Used-Percent": "63"},
        },
    },
    {
        "provider": "codex",
        "name": "codex-bob@example.com.json",
        "status": "error",
        "unavailable": True,
        "cooldowns": [{"scope": "credential", "reason": "quota", "retry_at": iso(20)}],
    },
    {
        "provider": "codex",
        "name": "codex-carol@example.com.json",
        "status": "error",
        "status_message": "token for carol@example.com expired",
    },
    {"provider": "xai", "name": "xai-dan@example.com.json", "status": "active"},
]


class ViewTests(unittest.TestCase):
    def setUp(self):
        status.reset_state()
        self.home = Path(tempfile.mkdtemp())
        self.config = {
            "cliproxyapi": {
                "instances": {
                    "pre": {
                        "base_url": "http://127.0.0.1:1",
                        "key_env": "C",
                        "mgmt_key_env": "M",
                    },
                    "fast": {"base_url": "http://127.0.0.1:2", "key_env": "C"},
                }
            }
        }
        fetched = status.Fetched("ok", files=[dict(e) for e in LISTING])
        patcher = mock.patch.object(status, "_fetch", return_value=(fetched, False))
        self.fetch = patcher.start()
        self.addCleanup(patcher.stop)

    def run_cpa(self, args=""):
        return status.report(
            args, config=self.config, home=self.home, secret={"M": "k"}.get, now=NOW
        )

    def test_overview_lists_every_pool(self):
        out = self.run_cpa()
        self.assertIn("pre\n  codex 3: 1 active · 1 cooling (next ", out)
        self.assertIn("1 error", out)
        self.assertIn("  xai 1: 1 active", out)
        self.assertIn("fast: no management key (set mgmt_key_env)", out)
        self.assertNotIn("1h", out)

    def test_pool_view_adds_last_hour(self):
        out = self.run_cpa("pre")
        self.assertIn("· 1h 3 ok / 1 failed", out)
        self.assertNotIn("fast", out)

    def test_provider_view_one_line_per_account(self):
        out = self.run_cpa("pre codex")
        lines = out.splitlines()
        self.assertEqual(lines[0], "pre / codex · 3 accounts")
        self.assertTrue(lines[1].startswith("codex-ca…@…  error: token for ca…@… expired"))
        self.assertTrue(lines[2].startswith("codex-bo…@…  cooling until "))
        self.assertIn("(vendor limit reached)", lines[2])
        self.assertEqual(lines[3], "codex-al…@…  active · 1h 3/1 · 5h 63% (4m ago)")

    def provider_lines(self, *raw):
        # Through files_of, as a real listing goes: a field it drops is not shown.
        files = status.files_of({"files": [dict(entry) for entry in raw]})
        self.fetch.return_value = (status.Fetched("ok", files=files), False)
        return self.run_cpa("pre codex").splitlines()

    def test_provider_view_shows_totals_and_priority(self):
        counted = dict(LISTING[0], success=250, failed=8, priority=3)
        lines = self.provider_lines(counted, LISTING[1])
        self.assertEqual(lines[0], "pre / codex · 2 accounts · total = since CPA start")
        self.assertTrue(lines[1].startswith("codex-bo…@…  cooling until "))
        self.assertNotIn("total", lines[1])
        self.assertNotIn("prio", lines[1])
        self.assertEqual(
            lines[2], "codex-al…@…  active · prio 3 · 1h 3/1 · total 250/8 · 5h 63% (4m ago)"
        )

    def test_priority_an_operator_can_set(self):
        for value in (0, -5):
            lines = self.provider_lines(dict(LISTING[0], priority=value))
            self.assertEqual(lines[0], "pre / codex · 1 account")
            self.assertIn(f"active · prio {value} · 1h 3/1", lines[1])

    def test_totals_and_priority_ignore_junk(self):
        for junk in (
            {"success": True, "failed": "9", "priority": True},
            {"success": -3, "failed": None, "priority": "1"},
            {"success": 0, "failed": 0},
            {"success": 1.5, "priority": 1.5},
        ):
            lines = self.provider_lines(dict(LISTING[0], **junk))
            self.assertEqual(lines[0], "pre / codex · 1 account", junk)
            self.assertEqual(lines[1], "codex-al…@…  active · 1h 3/1 · 5h 63% (4m ago)", junk)

    def test_total_note_only_when_the_shown_provider_has_totals(self):
        other = dict(LISTING[3], success=7, failed=2)
        lines = self.provider_lines(LISTING[0], other)
        self.assertEqual(lines[0], "pre / codex · 1 account")
        self.assertNotIn("total", "\n".join(lines))

    def test_no_email_anywhere(self):
        for args in ("", "pre", "pre codex", "pre xai"):
            out = self.run_cpa(args)
            self.assertNotRegex(out, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", args)

    def test_provider_alias(self):
        self.assertIn("pre / xai · 1 account\n", self.run_cpa("pre grok"))

    def test_unknown_pool_lists_names(self):
        self.assertEqual(self.run_cpa("nope"), "Unknown pool 'nope'. Pools: pre, fast")
        self.fetch.assert_not_called()

    def test_unknown_provider_lists_providers(self):
        self.assertEqual(
            self.run_cpa("pre kiro"), "No provider 'kiro' in pre. Providers: codex, xai"
        )

    def test_forbidden_fields_dropped_on_ingest(self):
        files = status.files_of({"files": [dict(LISTING[0])]})
        self.assertNotIn("email", files[0])
        self.assertNotIn("account", files[0])
        self.assertEqual(files[0]["status"], "active")

    def test_unknown_provider_echo_masked(self):
        out = self.run_cpa("pre sk-" + "a" * 40)
        self.assertNotIn("a" * 24, out)

    def test_cooling_reason_in_plain_words(self):
        # Every reason code CPA can send (sdk/cliproxy/auth/cooldown_view.go).
        until = "codex-bo…@…  cooling until " + status.clock(NOW + timedelta(minutes=20), NOW)
        for code, words in (
            ("quota", " (vendor limit reached)"),
            ("credential_quota", " (vendor limit reached, all models)"),
            ("cloudflare_challenge", " (Cloudflare challenge)"),
            ("model_not_supported", " (model not supported)"),
            ("invalid_grant", " (refresh token rejected)"),
            ("payment_required", " (access refused or no credit)"),
            ("transient_rate_limit", " (vendor rate limited)"),
            ("unauthorized", " (token rejected by vendor)"),
            ("not_found", " (vendor HTTP 404)"),
            ("transient_error", " (temporary vendor error)"),
            ("unknown", ""),
        ):
            cooldown = {"scope": "credential", "reason": code, "retry_at": iso(20)}
            lines = self.provider_lines(dict(LISTING[1], cooldowns=[cooldown]))
            self.assertEqual(lines[1], until + words, code)

    def test_cooling_reason_that_is_not_a_code_is_shown_as_sent(self):
        # An older CPA, or a code added later: shown as it came, through the masks.
        for text in ("Quota", "quota exhausted today", "new_reason_code"):
            cooldown = {"scope": "credential", "reason": text, "retry_at": iso(20)}
            lines = self.provider_lines(dict(LISTING[1], cooldowns=[cooldown]))
            self.assertTrue(lines[1].endswith(f" ({text})"), lines[1])

    def test_cooling_reason_masked(self):
        entry = dict(LISTING[1])
        entry["cooldowns"] = [
            {"scope": "credential", "reason": "eve@example.com " + "z" * 40, "retry_at": iso(5)}
        ]
        self.fetch.return_value = (status.Fetched("ok", files=[entry]), False)
        out = self.run_cpa("pre codex")
        self.assertNotIn("eve@example.com", out)
        self.assertNotIn("z" * 24, out)

    def test_full_quota_is_limited_not_ready(self):
        reset = int((NOW + timedelta(hours=2)).timestamp())
        full = {
            "provider": "codex",
            "name": "codex-zara@example.com.json",
            "status": "active",
            "quota": {
                "signals": {
                    "X-Codex-Primary-Used-Percent": "100",
                    "X-Codex-Primary-Window-Minutes": "300",
                    "X-Codex-Primary-Reset-At": str(reset),
                }
            },
        }
        stale = {
            "provider": "codex",
            "name": "codex-old@example.com.json",
            "status": "active",
            "quota": {
                "signals": {
                    "X-Codex-Primary-Used-Percent": "100",
                    "X-Codex-Primary-Window-Minutes": "300",
                    "X-Codex-Primary-Reset-At": str(reset - 3 * 3600),
                }
            },
        }
        self.fetch.return_value = (
            status.Fetched("ok", files=[dict(LISTING[0]), full, stale]),
            False,
        )
        until = status.clock(datetime.fromtimestamp(reset, timezone.utc), NOW)
        self.assertIn(f"  codex 3: 2 active · 1 limited (next {until})", self.run_cpa())
        lines = self.run_cpa("pre codex").splitlines()
        self.assertTrue(lines[1].startswith("codex-za…@…  limited until "), lines)
        self.assertIn(f"5h 100% until {until}", lines[1])
        self.assertTrue(lines[2].startswith("codex-al…@…  active"), lines)
        self.assertTrue(lines[3].startswith("codex-ol…@…  active"), lines)

    def test_devin_last_seen_quota_in_the_views(self):
        reset = NOW + timedelta(hours=3)
        room = {
            "provider": "devin",
            "name": "devin-ivy@example.com.json",
            "status": "active",
            "quota": {
                "observed_at": iso(-4),
                "signals": {
                    "daily_quota_remaining_percent": "55%",
                    "daily_quota_reset_at": iso(180),
                    "weekly_quota_remaining_percent": "88%",
                },
            },
        }
        used_up = {
            "provider": "devin",
            "name": "devin-joe@example.com.json",
            "status": "active",
            "quota": {
                "observed_at": iso(-90),
                "signals": {
                    "daily_quota_remaining_percent": "0%",
                    "daily_quota_reset_at": iso(180),
                    "weekly_quota_remaining_percent": "40%",
                },
            },
        }
        self.fetch.return_value = (status.Fetched("ok", files=[room, used_up]), False)
        until = status.clock(reset, NOW)
        self.assertIn(f"  devin 2: 1 active · 1 limited (next {until})", self.run_cpa())
        lines = self.run_cpa("pre devin").splitlines()
        self.assertEqual(
            lines[1:3],
            [
                f"devin-jo…@…  limited until {until} · day 100% until {until} · wk 60% (1h ago)",
                "devin-iv…@…  active · day 45% · wk 12% (4m ago)",
            ],
        )

    def test_sign_in_note_on_pool_lines(self):
        serving = {
            "provider": "codex",
            "name": "codex-sam@example.com.json",
            "status": "active",
            "requires_sign_in": True,
            "renewal_state": "sign_in_required",
            "access_token_expires_at": iso(90),
        }
        self.fetch.return_value = (
            status.Fetched("ok", files=[dict(LISTING[0]), serving]),
            False,
        )
        self.assertIn("  codex 2: 2 active; 1 needs sign-in", self.run_cpa())
        lines = self.run_cpa("pre codex").splitlines()
        self.assertTrue(
            lines[2].startswith("codex-sa…@…  active · sign in again (serving until "), lines
        )

    def test_help_and_too_many_args(self):
        self.assertEqual(self.run_cpa("help"), status.USAGE)
        self.assertEqual(self.run_cpa("a b c"), status.USAGE)
        self.fetch.assert_not_called()

    def test_pool_without_key_in_provider_view(self):
        self.assertEqual(self.run_cpa("fast claude"), "fast: no management key (set mgmt_key_env)")

    def test_output_is_capped(self):
        many = [
            {"provider": "codex", "name": f"codex-{n:04d}-{'x' * 30}.json", "status": "active"}
            for n in range(400)
        ]
        self.fetch.return_value = (status.Fetched("ok", files=many), False)
        out = self.run_cpa("pre codex")
        self.assertLessEqual(len(out), status.OUTPUT_LIMIT)
        self.assertRegex(out.splitlines()[-1], r"^… \d+ more lines$")

    def test_cached_marker(self):
        fetched = status.Fetched("ok", files=[], at=0.0)
        self.fetch.return_value = (fetched, True)
        out = self.run_cpa()
        self.assertIn("pre (cached", out)
        self.assertIn("  no accounts", out)


class SameRootTests(unittest.TestCase):
    """A keyless instance on a CPA another instance manages is that CPA, not a pool."""

    def setUp(self):
        status.reset_state()
        self.home = Path(tempfile.mkdtemp())
        self.config = {
            "cliproxyapi": {
                "instances": {
                    "fast": {
                        "base_url": "http://127.0.0.1:2",
                        "key_env": "C",
                        "mgmt_key_env": "M",
                    },
                    "strict": {
                        "base_url": "http://127.0.0.1:2/v1",
                        "key_env": "C",
                        "route_via": "https://gateway.example",
                        "route_key_env": "G",
                    },
                    "mirror": {"base_url": "http://127.0.0.1:2", "key_env": "C"},
                    "lone": {"base_url": "http://127.0.0.1:3", "key_env": "C"},
                }
            }
        }
        fetched = status.Fetched("ok", files=[dict(e) for e in LISTING], version="v8.0.53")
        patcher = mock.patch.object(status, "_fetch", return_value=(fetched, False))
        self.fetch = patcher.start()
        self.addCleanup(patcher.stop)

    def run_cpa(self, args=""):
        return status.report(
            args, config=self.config, home=self.home, secret={"M": "k"}.get, now=NOW
        )

    def test_overview_folds_keyless_instances_under_their_cpa(self):
        with mock.patch.object(status, "latest_version", return_value=""):
            out = self.run_cpa()
        self.assertIn("fast · v8.0.53\n  same CPA: strict (routed), mirror\n  codex 3:", out)
        self.assertNotIn("strict:", out)
        self.assertNotIn("mirror:", out)
        self.assertIn("lone: no management key (set mgmt_key_env)", out)
        self.assertEqual(self.fetch.call_count, 1)

    def test_pool_view_points_to_the_managed_instance(self):
        self.assertEqual(self.run_cpa("strict"), "strict: same CPA as fast, see /cpa fast")
        self.assertEqual(
            self.run_cpa("strict codex"), "strict: same CPA as fast, see /cpa fast codex"
        )
        self.fetch.assert_not_called()

    def test_quota_points_to_the_managed_instance(self):
        self.assertEqual(
            self.run_cpa("quota strict"), "strict: same CPA as fast, see /cpa quota fast"
        )
        self.assertEqual(
            self.run_cpa("quota strict xai"),
            "strict: same CPA as fast, see /cpa quota fast xai",
        )
        self.fetch.assert_not_called()

    def test_first_keyed_instance_owns_the_cpa(self):
        instances = self.config["cliproxyapi"]["instances"]
        instances["other"] = {
            "base_url": "http://127.0.0.1:2",
            "key_env": "C",
            "mgmt_key_env": "M",
        }
        self.assertEqual(status.same_cpa_owner(cpa.parse_config(self.config)[0], "mirror"), "fast")
        self.assertEqual(status.same_cpa_owner(cpa.parse_config(self.config)[0], "other"), "")
        self.assertEqual(status.same_cpa_owner(cpa.parse_config(self.config)[0], "lone"), "")


class HandleTests(unittest.TestCase):
    def test_handle_never_raises(self):
        home = Path(tempfile.mkdtemp())
        with (
            mock.patch.object(status, "_hermes_home", return_value=home),
            mock.patch.object(status, "report", side_effect=ValueError("secret detail")),
        ):
            out = status.handle("")
        self.assertEqual(out, "cpa-status: internal error (ValueError)")

    def test_handle_takes_the_platform_hermes_passes(self):
        # Hermes 0.22.3+ passes `platform` to a handler that names it as a
        # keyword; older Hermes calls the handler with the text only.
        home = Path(tempfile.mkdtemp())
        with (
            mock.patch.object(status, "_hermes_home", return_value=home),
            mock.patch.object(status, "report", return_value="ok") as report,
        ):
            self.assertEqual(status.handle("quota pre", platform="telegram"), "ok")
            self.assertEqual(status.handle("quota pre"), "ok")
        self.assertEqual(
            [call.kwargs.get("platform") for call in report.call_args_list], ["telegram", None]
        )
        params = list(inspect.signature(status.handle).parameters.values())
        self.assertEqual([p.name for p in params], ["raw_args", "platform"])
        self.assertIs(params[1].kind, inspect.Parameter.KEYWORD_ONLY)

    @unittest.skipUnless(
        importlib.util.find_spec("ruamel") or importlib.util.find_spec("yaml"),
        "no YAML parser (runs in the Hermes CI job)",
    )
    def test_handle_reads_config_from_hermes_home(self):
        home = Path(tempfile.mkdtemp())
        (home / "config.yaml").write_text(
            "cliproxyapi:\n  instances:\n    solo:\n      base_url: http://127.0.0.1:1\n"
            "      key_env: C\n"
        )
        status.reset_state()
        with mock.patch.object(status, "_hermes_home", return_value=home):
            out = status.handle("")
        self.assertIn("solo: no management key", out)

    def test_register_adds_cpa_command(self):
        module = _load_plugin_module()
        calls = []

        class Ctx:
            def register_command(self, name, handler, description="", **kw):
                calls.append((name, handler, description, kw))

        module.register(Ctx())
        self.assertEqual(len(calls), 1)
        name, handler, description, kw = calls[0]
        self.assertEqual(name, "cpa")
        self.assertIs(handler, status.handle)
        self.assertNotIn("args_hint", kw)  # keeps /cpa in the Telegram menu
        self.assertTrue(description)


def _load_plugin_module():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    init = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("cpa_status_plugin", init)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Ctx:
    """Plugin context of Hermes 0.23.1 or later, with the rules its session-env hook enforces."""

    def __init__(self, settings=None, taken=()):
        self.settings = dict(settings or {})
        self.taken = set(taken)  # names another plugin already holds
        self.commands: list[str] = []
        self.asked: list[str] = []
        self.hidden: list[tuple[list[str], Callable[[str], dict]]] = []

    def register_command(self, name, handler, description="", **kw):
        self.commands.append(name)

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_session_env(self, names, resolver):
        names = [names] if isinstance(names, str) else list(names)
        self.asked.extend(names)
        if not names or any(n.upper().startswith("HERMES_") for n in names):
            raise ValueError("session env names must be valid names outside HERMES_*")
        if self.taken & set(names):
            raise ValueError("session env name is already registered by another plugin")
        self.hidden.append((names, resolver))

    def unset(self, session_id="session-1"):
        """What the registered resolvers tell Hermes to do for one session."""
        out: dict = {}
        for _names, resolver in self.hidden:
            out.update(resolver(session_id))
        return out


class ShellKeyTests(unittest.TestCase):
    """Keys no provider declares are hidden from a chat session's commands (Hermes 0.23.1+)."""

    INSTANCES = {
        "a": {"base_url": "http://127.0.0.1:9", "key_env": "A_KEY", "mgmt_key_env": "A_MGMT"},
        "b": {
            "base_url": "http://127.0.0.1:9",
            "key_env": "B_KEY",
            "mgmt_key_env": "B_MGMT",
            "route_via": "http://127.0.0.1:9/v1",
            "route_key_env": "B_GATEWAY",
        },
        "c": {"base_url": "http://127.0.0.1:9", "key_env": "C_KEY"},
        "d": {"base_url": "http://127.0.0.1:9", "key_env": "D_KEY", "mgmt_key_env": "A_MGMT"},
    }

    def register(self, ctx, instances=None, text=None):
        home = Path(tempfile.mkdtemp())
        if text is None:
            instances = self.INSTANCES if instances is None else instances
            text = json.dumps({"cliproxyapi": {"instances": instances}})  # JSON is valid YAML
        (home / "config.yaml").write_text(text)
        module = _load_plugin_module()
        with mock.patch.object(status, "_hermes_home", return_value=home):
            module.register(ctx)
        return ctx

    def test_management_keys_and_a_routed_cpa_key_are_hidden(self):
        ctx = self.register(_Ctx())
        # Not A_KEY, C_KEY, D_KEY or B_GATEWAY: a provider declares those, so Hermes strips them.
        self.assertEqual(sorted(ctx.asked), ["A_MGMT", "B_KEY", "B_MGMT"])
        self.assertEqual(ctx.unset(), {"A_MGMT": None, "B_KEY": None, "B_MGMT": None})
        self.assertEqual(ctx.unset(""), ctx.unset("another-session"))
        self.assertEqual(ctx.commands, ["cpa"])

    def test_the_setting_turns_hiding_off(self):
        for value in (False, "false", "off", "no", 0):
            ctx = self.register(_Ctx({"hide_keys_from_shells": value}))
            self.assertEqual((ctx.asked, ctx.commands), ([], ["cpa"]), repr(value))
        for value in (True, "true", None, "", "maybe"):
            ctx = self.register(_Ctx({"hide_keys_from_shells": value}))
            self.assertEqual(sorted(ctx.asked), ["A_MGMT", "B_KEY", "B_MGMT"], repr(value))

    def test_a_refused_name_does_not_stop_the_others(self):
        instances = dict(self.INSTANCES)
        instances["e"] = {
            "base_url": "http://127.0.0.1:9",
            "key_env": "E_KEY",
            "mgmt_key_env": "HERMES_E_MGMT",
        }
        with self.assertLogs(level="WARNING") as logs:
            ctx = self.register(_Ctx(taken={"B_MGMT"}), instances)
        self.assertEqual(ctx.unset(), {"A_MGMT": None, "B_KEY": None})
        self.assertEqual(ctx.commands, ["cpa"])
        # Hermes keeps HERMES_* for itself: never asked, and said so.
        self.assertNotIn("HERMES_E_MGMT", ctx.asked)
        joined = "\n".join(logs.output)
        self.assertEqual((joined.count("B_MGMT"), joined.count("HERMES_E_MGMT")), (1, 1))
        self.assertNotIn("A_MGMT", joined)

    def test_keys_are_read_from_config_without_a_lookup_or_a_valid_instance(self):
        instances = {
            # The provider skips both instances; their keys are in the environment all the same.
            "bad-url": {"base_url": "ftp://x", "key_env": "X_KEY", "mgmt_key_env": "X_MGMT"},
            "named": {
                "base_url": "http://pool.example",
                "key_env": "Y_KEY",
                "mgmt_key_env": "Y_MGMT",
            },
            "bad-name": {
                "base_url": "http://127.0.0.1:9",
                "key_env": "Z",
                "mgmt_key_env": "not a name",
            },
            "not-a-mapping": "x",
        }
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("address lookup")):
            ctx = self.register(_Ctx(), instances)
        self.assertEqual(sorted(ctx.asked), ["X_MGMT", "Y_MGMT"])

    def test_nothing_to_hide_registers_only_the_command(self):
        for text in (
            "model: {}\n",
            "cliproxyapi: 7\n",
            "cliproxyapi:\n  instances: []\n",
        ):
            with self.assertNoLogs(level="WARNING"):
                ctx = self.register(_Ctx(), text=text)
            self.assertEqual((ctx.asked, ctx.commands), ([], ["cpa"]), text)
        ctx = self.register(_Ctx(), text="cliproxyapi: [unclosed\n")  # not YAML
        self.assertEqual((ctx.asked, ctx.commands), ([], ["cpa"]))

    def test_older_hermes_registers_the_command_and_says_nothing(self):
        class Old:  # Hermes before 0.23.1: no register_session_env
            commands: list[str] = []

            def register_command(self, name, handler, description="", **kw):
                self.commands.append(name)

            def get_config(self, key, default=None):
                raise AssertionError("the setting is read only when Hermes can hide a key")

        with self.assertNoLogs(level="WARNING"):
            ctx = self.register(Old())
        self.assertEqual(ctx.commands, ["cpa"])

    def test_a_failing_hook_never_costs_the_command(self):
        class Broken(_Ctx):
            def get_config(self, key, default=None):
                raise RuntimeError("settings unavailable")

        try:
            with self.assertLogs(level="WARNING") as logs:
                ctx = self.register(Broken())
        except RuntimeError:
            self.fail("register() raised: the plugin would not load and /cpa would be gone")
        self.assertEqual((ctx.asked, ctx.commands), ([], ["cpa"]))
        self.assertIn("RuntimeError", "\n".join(logs.output))


class VersionTests(unittest.TestCase):
    def test_manifest_setting_is_declared(self):
        manifest = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
        self.assertIn(
            "config_schema:\n  hide_keys_from_shells:\n    type: bool\n    default: true\n",
            manifest,
        )

    def test_manifest_version_matches_code(self):
        manifest = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
        self.assertIn(f"version: {cpa.VERSION}\n", manifest)


if __name__ == "__main__":
    unittest.main()

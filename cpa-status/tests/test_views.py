"""View and argument tests for ``/cpa`` (no network: rejected or keyless pools)."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
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
        self.assertIn("pre\n  codex 3: 1 ready · 1 cooling (next ", out)
        self.assertIn("1 error", out)
        self.assertIn("  xai 1: 1 ready", out)
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
        self.assertIn("(quota)", lines[2])
        self.assertEqual(lines[3], "codex-al…@…  ready · 1h 3/1 · 5h 63% (4m ago)")

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

    def test_cooling_reason_masked(self):
        entry = dict(LISTING[1])
        entry["cooldowns"] = [
            {"scope": "credential", "reason": "eve@example.com " + "z" * 40, "retry_at": iso(5)}
        ]
        self.fetch.return_value = (status.Fetched("ok", files=[entry]), False)
        out = self.run_cpa("pre codex")
        self.assertNotIn("eve@example.com", out)
        self.assertNotIn("z" * 24, out)

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


class HandleTests(unittest.TestCase):
    def test_handle_never_raises(self):
        home = Path(tempfile.mkdtemp())
        with (
            mock.patch.object(status, "_hermes_home", return_value=home),
            mock.patch.object(status, "report", side_effect=ValueError("secret detail")),
        ):
            out = status.handle("")
        self.assertEqual(out, "cpa-status: internal error (ValueError)")

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
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

        init = Path(__file__).resolve().parents[1] / "__init__.py"
        spec = importlib.util.spec_from_file_location("cpa_status_plugin", init)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
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


class VersionTests(unittest.TestCase):
    def test_manifest_version_matches_code(self):
        manifest = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
        self.assertIn(f"version: {cpa.VERSION}\n", manifest)


if __name__ == "__main__":
    unittest.main()

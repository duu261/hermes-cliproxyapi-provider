"""``/cpa quota``: parsers, request shape, privacy and lockout run control."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import quota  # noqa: E402
import status  # noqa: E402

import cpa  # noqa: E402

NOW = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)
KEY = "mgmt-key-good"
BAD = "mgmt-key-bad"
CANARY_INDEX = "0123456789abcdef"
CANARY_ACCOUNT = "5f1c2d3e-aaaa-bbbb-cccc-0123456789ab"
TOKENISH = "eyJ" + "x" * 40


def epoch(minutes: float) -> int:
    return int((NOW + timedelta(minutes=minutes)).timestamp())


def iso(minutes: float) -> str:
    return (NOW + timedelta(minutes=minutes)).isoformat()


CLAUDE_BODY = {
    "five_hour": {"utilization": 55.0, "resets_at": iso(65)},
    "seven_day": {"utilization": 31, "resets_at": iso(3 * 1440)},
    "seven_day_opus": {"utilization": "12", "resets_at": None},
    "extra_usage": {"is_enabled": True, "monthly_limit": 999},
}
CODEX_PRO = {  # Management Center fixture shape: weekly primary, no secondary
    "plan_type": "pro",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {
            "used_percent": 40,
            "limit_window_seconds": 604800,
            "reset_after_seconds": 601888,
            "reset_at": epoch(5 * 1440),
        },
        "secondary_window": None,
    },
    "code_review_rate_limit": None,
}
CODEX_PLUS_OUT = {
    "plan_type": "plus",
    "rate_limit": {
        "allowed": False,
        "limit_reached": True,
        "primary_window": {
            "used_percent": 100,
            "limit_window_seconds": 18000,
            "reset_at": epoch(70),
        },
        "secondary_window": {
            "used_percent": 60,
            "limit_window_seconds": 604800,
            "reset_at": epoch(6 * 1440),
        },
    },
}


def account(provider, n, **extra):
    entry = {
        "provider": provider,
        "name": f"{provider}-user{n}@example.com.json",
        "status": "active",
        "auth_index": f"{n:016x}",
        "email": f"user{n}@example.com",
    }
    if provider == "codex":
        entry["id_token"] = {
            "chatgpt_account_id": f"acct-{n:04d}",
            "plan_type": "secret-plan-claim",
            "email": f"user{n}@example.com",
        }
    entry.update(extra)
    return entry


class _CPA(BaseHTTPRequestHandler):
    files: list = []
    seen: list = []
    posts: list = []
    inner: dict = {}  # auth_index -> (status, body)
    by_url: dict = {}  # (auth_index, url) -> (status, body); wins over inner
    plugin: dict = {}  # auth_index -> (outer status, body) for the plugin quota GET
    plugin_gets: list = []
    outer: int = 0  # force an outer status on api-call
    flip_after: int = -1  # api-call outer 401 after this many posts
    v8_api_call = True
    delay = 0.0
    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def log_message(self, format, *args):  # noqa: A002 - silence
        pass

    def _send(self, code, body=None):
        data = json.dumps(body if body is not None else {"error": "secret-body"}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _enter(self):
        cls = type(self)
        with cls.lock:
            cls.in_flight += 1
            cls.peak = max(cls.peak, cls.in_flight)

    def _leave(self):
        cls = type(self)
        with cls.lock:
            cls.in_flight -= 1

    def do_GET(self):  # noqa: N802
        cls = type(self)
        parts = urlsplit(self.path)
        path = parts.path
        cls.seen.append(("GET", path))
        if path.startswith("/v8/management/plugins/") and path.endswith("/quota"):
            self._enter()
            try:
                query = parse_qs(parts.query)
                with cls.lock:
                    cls.plugin_gets.append(
                        {"path": path, "query": query, "auth": self.headers.get("Authorization")}
                    )
                if cls.delay:
                    time.sleep(cls.delay)
                if self.headers.get("Authorization") != f"Bearer {KEY}":
                    return self._send(401)
                index = query.get("auth_index", [""])[0]
                code, body = cls.plugin.get(index, (404, {"error": "auth not found"}))
                return self._send(code, body)
            finally:
                self._leave()
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            return self._send(401)
        if path == "/v8/management/credentials":
            return self._send(200, {"files": cls.files})
        return self._send(404)

    def do_POST(self):  # noqa: N802
        cls = type(self)
        path = urlsplit(self.path).path
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with cls.lock:
            cls.seen.append(("POST", path))
            index = len(cls.posts)
        self._enter()
        try:
            if path == "/v8/management/requests/api-call" and not cls.v8_api_call:
                return self._send(404)
            if path not in ("/v8/management/requests/api-call", "/v0/management/api-call"):
                return self._send(404)
            body = json.loads(raw)
            with cls.lock:
                cls.posts.append(
                    {
                        "path": path,
                        "body": body,
                        "auth": self.headers.get("Authorization"),
                        "ua": self.headers.get("User-Agent"),
                    }
                )
            if cls.delay:
                time.sleep(cls.delay)
            if cls.outer:
                return self._send(cls.outer)
            if self.headers.get("Authorization") != f"Bearer {KEY}":
                return self._send(401)
            if 0 <= cls.flip_after <= index:
                return self._send(401)
            target = body.get("auth_index")
            code, inner = cls.by_url.get(
                (target, body.get("url")), cls.inner.get(target, (200, {}))
            )
            text = inner if isinstance(inner, str) else json.dumps(inner)
            return self._send(
                200, {"status_code": code, "header": {"Set-Cookie": ["leak"]}, "body": text}
            )
        finally:
            self._leave()


class QuotaTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _CPA)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.root = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _CPA.files = [account("claude", 1), account("codex", 2), account("codex", 3)]
        _CPA.seen, _CPA.posts = [], []
        _CPA.inner = {
            f"{1:016x}": (200, CLAUDE_BODY),
            f"{2:016x}": (200, CODEX_PRO),
            f"{3:016x}": (200, CODEX_PLUS_OUT),
        }
        _CPA.outer, _CPA.flip_after, _CPA.v8_api_call, _CPA.delay = 0, -1, True, 0.0
        _CPA.by_url, _CPA.plugin, _CPA.plugin_gets = {}, {}, []
        _CPA.in_flight = _CPA.peak = 0
        self.home = Path(tempfile.mkdtemp())
        self.env = {"PRE_MGMT": KEY}
        status.reset_state()

    def config(self):
        return {
            "cliproxyapi": {
                "instances": {
                    "pre": {"base_url": self.root, "key_env": "C", "mgmt_key_env": "PRE_MGMT"}
                }
            }
        }

    def run_quota(self, args="quota pre"):
        return status.report(
            args, config=self.config(), home=self.home, secret=self.env.get, now=NOW
        )

    def posts(self):
        return [s for s in _CPA.seen if s[0] == "POST"]


# ── Parsers (Task 6) ─────────────────────────────────────────────────────────


class ParserTests(unittest.TestCase):
    def test_claude_windows(self):
        usage = quota.parse_claude(CLAUDE_BODY)
        self.assertEqual(
            [(w.label, w.used) for w in usage.windows], [("5h", 55), ("wk", 31), ("opus", 12)]
        )
        self.assertEqual(usage.windows[0].resets_at, NOW + timedelta(minutes=65))
        self.assertIsNone(usage.windows[2].resets_at)

    def test_codex_pro_weekly_primary_only(self):
        usage = quota.parse_codex(CODEX_PRO, NOW)
        self.assertEqual(usage.plan, "pro")
        self.assertEqual([(w.label, w.used) for w in usage.windows], [("wk", 40)])
        self.assertFalse(usage.reached)

    def test_codex_five_hour_and_week_by_seconds(self):
        usage = quota.parse_codex(CODEX_PLUS_OUT, NOW)
        self.assertEqual([w.label for w in usage.windows], ["5h", "wk"])
        self.assertTrue(usage.reached)

    def test_codex_month_window(self):
        body = {
            "rate_limit": {
                "primary_window": {"used_percent": 3, "limit_window_seconds": 30 * 86400}
            }
        }
        self.assertEqual(quota.parse_codex(body, NOW).windows[0].label, "mo")

    def test_codex_position_fallback_without_seconds(self):
        body = {
            "rate_limit": {
                "primary_window": {"used_percent": 10},
                "secondary_window": {"used_percent": 20},
            }
        }
        self.assertEqual([w.label for w in quota.parse_codex(body, NOW).windows], ["5h", "wk"])

    def test_codex_zero_length_window_dropped(self):
        body = {
            "rate_limit": {
                "primary_window": {"used_percent": 10, "limit_window_seconds": 604800},
                "secondary_window": {"used_percent": 0, "limit_window_seconds": 0},
            }
        }
        self.assertEqual([w.label for w in quota.parse_codex(body, NOW).windows], ["wk"])

    def test_codex_reset_after_seconds(self):
        body = {
            "rate_limit": {
                "primary_window": {
                    "used_percent": 1,
                    "limit_window_seconds": 18000,
                    "reset_after_seconds": 600,
                }
            }
        }
        self.assertEqual(
            quota.parse_codex(body, NOW).windows[0].resets_at, NOW + timedelta(seconds=600)
        )

    def test_codex_reached_without_percent_counts_as_full(self):
        body = {
            "rate_limit": {
                "limit_reached": True,
                "primary_window": {"limit_window_seconds": 18000, "reset_at": epoch(30)},
            }
        }
        self.assertEqual(quota.parse_codex(body, NOW).windows[0].used, 100)

    def test_strings_clamps_and_junk(self):
        body = {
            "rate_limit": {
                "primary_window": {"used_percent": "250", "limit_window_seconds": "18000"}
            }
        }
        self.assertEqual(quota.parse_codex(body, NOW).windows[0].used, 100)
        self.assertEqual(quota.parse_codex([], NOW).windows, [])
        self.assertEqual(quota.parse_claude("x").windows, [])
        self.assertEqual(quota.parse_claude({"five_hour": {"utilization": True}}).windows, [])

    def test_plan_type_allowlist(self):
        for raw, expected in (
            ("PRO", "pro"),
            ("team_2", "team_2"),
            ("<b>x</b>", ""),
            ("a" * 30, ""),
            (5, ""),
        ):
            body = dict(CODEX_PRO, plan_type=raw)
            self.assertEqual(quota.parse_codex(body, NOW).plan, expected)


# ── Request shape (Task 7) ──────────────────────────────────────────────────


class RequestShapeTests(QuotaTestCase):
    def test_post_body_has_exactly_four_keys_and_table_values(self):
        self.run_quota()
        self.assertEqual(len(_CPA.posts), 3)
        for post in _CPA.posts:
            body = post["body"]
            self.assertEqual(set(body), {"auth_index", "method", "url", "header"})
            self.assertEqual(body["method"], "GET")
            self.assertEqual(post["path"], "/v8/management/requests/api-call")
            self.assertEqual(post["auth"], f"Bearer {KEY}")
            self.assertEqual(post["ua"], f"cpa-status/{cpa.VERSION}")
        by_url = {p["body"]["url"]: p["body"] for p in _CPA.posts}
        self.assertEqual(set(by_url), {quota.CLAUDE_USAGE_URL, quota.CODEX_USAGE_URL})
        claude = by_url[quota.CLAUDE_USAGE_URL]["header"]
        self.assertEqual(claude, quota.QUOTA_REQUESTS["claude"][0].header)
        self.assertEqual(claude["Authorization"], "Bearer $TOKEN$")
        codex = [
            p["body"]["header"] for p in _CPA.posts if p["body"]["url"] == quota.CODEX_USAGE_URL
        ]
        for header in codex:
            self.assertNotIn("Accept", header)
            self.assertIn(header["Chatgpt-Account-Id"], {"acct-0002", "acct-0003"})
            base = {k: v for k, v in header.items() if k != "Chatgpt-Account-Id"}
            self.assertEqual(base, quota.QUOTA_REQUESTS["codex"][0].header)

    def test_only_table_urls_and_token_only_where_allowed(self):
        urls = {p.url for probes in quota.QUOTA_REQUESTS.values() for p in probes}
        self.assertEqual(
            urls,
            {
                "https://api.anthropic.com/api/oauth/usage",
                "https://chatgpt.com/backend-api/wham/usage",
                "https://cli-chat-proxy.grok.com/v1/billing?format=credits",
                "https://cli-chat-proxy.grok.com/v1/billing",
                "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
                "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:retrieveUserQuotaSummary",
                "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
                "https://server.codeium.com/exa.seat_management_pb.SeatManagementService/GetUserStatus",
                "https://api.kimi.com/coding/v1/usages",
            },
        )
        for provider, probes in quota.QUOTA_REQUESTS.items():
            for probe in probes:
                self.assertNotIn("Host", probe.header)
                for name, value in probe.header.items():
                    self.assertEqual("$TOKEN$" in value, name == "Authorization")
                if "$TOKEN$" in probe.data:
                    self.assertEqual(provider, "devin")

    def test_codex_without_account_id_has_no_header(self):
        _CPA.files = [account("codex", 2, id_token=None)]
        self.run_quota()
        self.assertNotIn("Chatgpt-Account-Id", _CPA.posts[0]["body"]["header"])

    def test_v8_404_falls_back_to_v0(self):
        _CPA.v8_api_call = False
        out = self.run_quota()
        paths = [p for _, p in self.posts()]
        self.assertEqual(paths[0], "/v8/management/requests/api-call")
        self.assertEqual(paths[1], "/v0/management/api-call")
        self.assertTrue(all(p == "/v0/management/api-call" for p in paths[2:]))
        self.assertIn("claude · 1 account", out)

    def test_v8_401_never_tries_v0(self):
        _CPA.outer = 401
        out = self.run_quota()
        self.assertIn("key rejected", out)
        self.assertEqual([p for _, p in self.posts()], ["/v8/management/requests/api-call"])


# ── Privacy (Task 8) ────────────────────────────────────────────────────────


class PrivacyTests(QuotaTestCase):
    def test_canaries_never_shown(self):
        _CPA.files = [
            account(
                "codex", 9, auth_index=CANARY_INDEX, id_token={"chatgpt_account_id": CANARY_ACCOUNT}
            ),
            account("claude", 1),
        ]
        _CPA.inner = {
            CANARY_INDEX: (500, f"oops {TOKENISH} for victim@example.com"),
            f"{1:016x}": (200, {"five_hour": {"utilization": 5}, "note": TOKENISH}),
        }
        outputs = [
            self.run_quota(),
            self.run_quota("quota pre codex"),
            self.run_quota("quota pre claude"),
        ]
        status.reset_state()
        _CPA.outer = 502
        outputs.append(self.run_quota())
        for out in outputs:
            for canary in (CANARY_INDEX, CANARY_ACCOUNT, TOKENISH, "victim", "leak", "secret-body"):
                self.assertNotIn(canary, out)
        self.assertIn("vendor HTTP 500", outputs[0])

    def test_files_of_keeps_only_scalars_for_the_call(self):
        files = status.files_of({"files": [account("codex", 2), account("xai", 4)]})
        for entry in files:
            self.assertNotIn("id_token", entry)
            self.assertNotIn("auth_index", entry)
            self.assertNotIn("email", entry)
        self.assertEqual(files[0]["_target"], f"{2:016x}")
        self.assertEqual(files[0]["_acct"], "acct-0002")
        self.assertEqual(files[1]["_target"], f"{4:016x}")  # every built-in vendor now
        self.assertNotIn("_acct", files[1])  # the account id stays Codex-only

    def test_bad_shaped_identifiers_are_dropped(self):
        entry = account("codex", 2, auth_index="../../etc", id_token={"chatgpt_account_id": "a b"})
        files = status.files_of({"files": [entry]})
        self.assertNotIn("_target", files[0])
        self.assertNotIn("_acct", files[0])
        _CPA.files = [entry]
        self.assertIn("CPA lists no account index", self.run_quota())
        self.assertEqual(_CPA.posts, [])

    def test_cache_holds_no_identifiers_in_rendered_text(self):
        self.run_quota()
        for run in quota._CACHE.values():
            for item in run.accounts:
                self.assertNotIn(CANARY_INDEX, repr(item))
                self.assertFalse(hasattr(item, "_target"))


# ── Run control (Task 9) ────────────────────────────────────────────────────


class RunControlTests(QuotaTestCase):
    def test_latched_key_sends_nothing(self):
        for latch in ("shared", "old", "provider"):
            with self.subTest(latch=latch):
                self.setUp()
                if latch == "shared":
                    cpa.mark_rejected(self.home, self.root, KEY)
                    status.reset_state()
                elif latch == "old":
                    old = self.home / "cache" / "cpa-status" / "rejected.json"
                    old.parent.mkdir(parents=True)
                    old.write_text(json.dumps({self.root: [cpa.management_fingerprint(KEY)]}))
                else:
                    cpa.save_cache(
                        self.home,
                        "pre",
                        cpa.Catalog(mgmt_auth_failed=cpa.management_fingerprint(KEY)),
                    )
                self.assertIn("key rejected", self.run_quota())
                self.assertEqual(_CPA.seen, [])

    def test_bad_key_on_warm_listing_costs_one_post(self):
        _CPA.files = [account("codex", n) for n in range(2, 8)]
        status.report("pre", config=self.config(), home=self.home, secret=self.env.get, now=NOW)
        _CPA.seen = []
        self.env["PRE_MGMT"] = BAD  # listing for KEY is cached; BAD is a new job
        status._RESULTS[(self.root, status._memory_id(BAD))] = status._RESULTS[
            (self.root, status._memory_id(KEY))
        ]
        out = self.run_quota()
        self.assertIn("key rejected", out)
        self.assertEqual(len(self.posts()), 1)
        self.assertTrue(cpa.key_rejected(self.home, self.root, BAD))
        _CPA.seen = []
        quota.reset_state()
        self.assertIn("key rejected", self.run_quota())
        self.assertEqual(_CPA.seen, [])

    def test_latch_from_elsewhere_after_probe_stops_fan_out(self):
        _CPA.files = [account("codex", n) for n in range(2, 8)]
        real = quota.api_call
        calls = []

        def spy(*args, **kwargs):
            calls.append(1)
            result = real(*args, **kwargs)
            if len(calls) == 1:
                cpa.mark_rejected(self.home, self.root, KEY)  # another process latched it
            return result

        with mock.patch.object(quota, "api_call", spy):
            self.run_quota()
        self.assertEqual(len(self.posts()), 1)

    def test_revoked_after_good_probe_at_most_four_outer_401(self):
        _CPA.files = [account("codex", n) for n in range(2, 14)]
        _CPA.flip_after = 1
        _CPA.delay = 0.05
        out = self.run_quota()
        self.assertIn("key rejected", out)
        self.assertLessEqual(len(self.posts()) - 1, quota.MAX_IN_FLIGHT)
        self.assertTrue(cpa.key_rejected(self.home, self.root, KEY))

    def test_latched_before_guard_released(self):
        _CPA.files = [account("codex", 2)]
        self.env["PRE_MGMT"] = KEY
        _CPA.outer = 401
        _CPA.delay = 0.3
        seen = {}
        started = threading.Event()

        def watcher():
            started.wait(5)
            time.sleep(0.1)
            with cpa.mgmt_guard(self.home, self.root, 5) as ok:
                with cpa._REJECTED_LOCK:
                    cpa._REJECTED.clear()
                seen["latched"] = ok and cpa.key_rejected(self.home, self.root, KEY)

        thread = threading.Thread(target=watcher)
        thread.start()
        real = quota.api_call

        def spy(*args, **kwargs):
            started.set()
            return real(*args, **kwargs)

        status.report("pre", config=self.config(), home=self.home, secret=self.env.get, now=NOW)
        with mock.patch.object(quota, "api_call", spy):
            self.run_quota()
        thread.join(5)
        self.assertTrue(seen.get("latched"))

    def test_waits_for_provider_then_runs_or_busy(self):
        release, held = threading.Event(), threading.Event()

        def hold():
            with cpa.mgmt_guard(self.home, self.root, 0):
                held.set()
                release.wait(5)

        worker = threading.Thread(target=hold)
        worker.start()
        held.wait(5)
        try:
            with mock.patch.object(status, "GUARD_WAIT", 0.2):
                self.assertIn("busy", self.run_quota())
            self.assertEqual(_CPA.seen, [])
            threading.Timer(0.2, release.set).start()
            self.assertIn("codex · 2 accounts", self.run_quota())
        finally:
            release.set()
            worker.join(5)

    def test_cold_and_warm_listing_complete_without_deadlock(self):
        done = []

        def go():
            done.append(self.run_quota())

        for _ in range(2):
            quota.reset_state()
            thread = threading.Thread(target=go)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(done), 2)
        self.assertEqual(
            [p for p in _CPA.seen if p[0] == "GET"], [("GET", "/v8/management/credentials")]
        )

    def test_inner_401_does_not_latch(self):
        _CPA.inner[f"{1:016x}"] = (401, {"error": {"message": "secret-body"}})
        out = self.run_quota()
        self.assertIn("token rejected by vendor", out)
        self.assertEqual(len(_CPA.posts), 3)
        self.assertFalse(cpa.key_rejected(self.home, self.root, KEY))
        self.assertNotIn("secret-body", out)

    def test_twenty_five_accounts_twenty_posts_four_in_flight(self):
        _CPA.files = [account("codex", n) for n in range(2, 27)]
        _CPA.delay = 0.05
        out = self.run_quota()
        self.assertEqual(len(_CPA.posts), 20)
        self.assertIn("+5 more not checked", out)
        self.assertLessEqual(_CPA.peak, quota.MAX_IN_FLIGHT)
        self.assertGreater(_CPA.peak, 1)

    def test_disabled_and_uncovered_providers_not_called(self):
        _CPA.files = [
            account("codex", 2, disabled=True),
            account("codex", 3, status="disabled"),
            account("gemini", 4),
            account("meta", 5, supports_quota=True, quota_plugin_id="meta-plugin"),
            account("kiro", 6),  # no quota plugin listed
        ]
        out = self.run_quota()
        self.assertEqual(_CPA.seen, [("GET", "/v8/management/credentials")])
        self.assertIn("no accounts with live quota", out)

    def test_repeat_within_60s_is_cached_and_takes_no_guard(self):
        self.run_quota()
        _CPA.seen = []
        with cpa.mgmt_guard(self.home, self.root, 0) as ok:
            self.assertTrue(ok)
            out = self.run_quota()
        self.assertIn("cached", out)
        self.assertEqual(_CPA.seen, [])

    def test_injection_args_never_reach_a_request(self):
        allowed_urls = {p.url for probes in quota.QUOTA_REQUESTS.values() for p in probes}
        for args in ("quota pre https://evil.example", 'quota pre "codex', "quota pre codex extra"):
            _CPA.posts = []
            quota.reset_state()
            self.run_quota(args)
            for post in _CPA.posts:
                text = json.dumps(post["body"])
                self.assertNotIn("evil", text)
                self.assertNotIn('\\"codex', text)
                self.assertIn(post["body"]["url"], allowed_urls)
        self.assertEqual(self.run_quota("quota pre codex extra"), quota.USAGE)

    def test_outer_502_and_400_per_account(self):
        _CPA.outer = 502
        out = self.run_quota("quota pre claude")
        self.assertIn("CPA could not reach vendor", out)
        self.assertFalse(cpa.key_rejected(self.home, self.root, KEY))


# ── View and args (Task 10) ─────────────────────────────────────────────────


class ViewTests(QuotaTestCase):
    def test_count_lines_and_account_lines(self):
        out = self.run_quota()
        self.assertIn("pre quota · live", out)
        self.assertIn("claude · 1 account: 1 have room", out)
        self.assertIn("codex · 2 accounts: 1 have room · 1 out (next free", out)
        self.assertIn("5h 55%", out)
        self.assertIn("pro  wk 40%", out)
        lines = out.splitlines()
        codex = lines[lines.index(next(line for line in lines if line.startswith("codex ·"))) + 1 :]
        self.assertTrue(codex[0].endswith("OUT"))  # out first
        self.assertIn("plus  5h 100%", codex[0])

    def test_provider_filter_is_case_insensitive(self):
        out = self.run_quota("quota PRE Codex")
        self.assertNotIn("claude ·", out)
        self.assertIn("codex · 2 accounts", out)
        self.assertEqual(len(_CPA.posts), 2)

    def test_unsupported_provider_is_masked(self):
        out = self.run_quota("quota pre sk-" + "a" * 40)
        self.assertIn("Live quota covers claude, codex, xai", out)
        self.assertNotIn("a" * 24, out)
        self.assertEqual(_CPA.seen, [])

    def test_meta_is_explained_and_sends_nothing(self):
        self.assertIn("meta: no live quota", self.run_quota("quota pre meta"))
        self.assertEqual(_CPA.seen, [])

    def test_usage_and_unknown_pool(self):
        self.assertEqual(self.run_quota("quota"), quota.USAGE)
        self.assertIn("Unknown pool 'nope'", self.run_quota("quota nope"))
        self.assertEqual(_CPA.seen, [])

    def test_pool_named_quota(self):
        config = {
            "cliproxyapi": {
                "instances": {
                    "quota": {"base_url": self.root, "key_env": "C", "mgmt_key_env": "PRE_MGMT"}
                }
            }
        }
        out = status.report(
            "quota quota", config=config, home=self.home, secret=self.env.get, now=NOW
        )
        self.assertIn("quota quota · live", out)

    def test_no_key(self):
        self.env["PRE_MGMT"] = ""
        self.assertIn("key not set", self.run_quota())
        self.assertEqual(_CPA.seen, [])

    def test_failed_count_and_not_checked(self):
        _CPA.inner[f"{2:016x}"] = (429, {})
        out = self.run_quota()
        self.assertIn("1 failed", out)
        self.assertIn("vendor rate limited", out)

    def test_output_capped(self):
        _CPA.files = [account("codex", n, name=f"codex-{'x' * 150}{n}.json") for n in range(2, 22)]
        self.assertLessEqual(len(self.run_quota()), status.OUTPUT_LIMIT)


if __name__ == "__main__":
    os.environ.pop("HERMES_HOME", None)
    unittest.main()

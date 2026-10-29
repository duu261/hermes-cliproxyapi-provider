"""``/cpa quota`` 0.6.0: xai, antigravity, devin, kimi and CPA plugin quota.

Reuses the fake CPA from ``test_quota``. Vendor shapes follow the Management
Center (router-for-me/Cli-Proxy-API-Management-Center@64b4f5aed1).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import unittest.mock
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import quota  # noqa: E402
import status  # noqa: E402
from test_quota import (  # noqa: E402
    _CPA,
    BAD,
    KEY,
    NOW,
    TOKENISH,
    QuotaTestCase,
    account,
    epoch,
    iso,
)

import cpa  # noqa: E402

TOKEN = "$" + "TOKEN" + "$"
PROJECT = "proj-canary-123"
PLUGIN_ID = "kiro-quota"

XAI_WEEKLY = {
    "config": {
        "creditUsagePercent": 42.5,
        "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY",
            "start": iso(-1440),
            "end": iso(3 * 1440),
        },
    }
}
XAI_MONTHLY = {
    "config": {
        "monthlyLimit": {"val": 15000},
        "used": {"val": "3000"},
        "billingPeriodEnd": iso(20 * 1440),
    }
}
AG_BODY = {
    "groups": [
        {
            "displayName": "Gemini Pro",
            "buckets": [
                {"window": "weekly", "remainingFraction": 0.75, "resetTime": iso(2 * 1440)},
                {"window": "five_hour", "remainingFraction": "40%", "resetTime": iso(90)},
                {"window": "5h", "remainingFraction": None},
            ],
        },
        {"display_name": "Claude", "buckets": [{"window": "5h", "remaining_fraction": 0}]},
    ]
}
DEVIN_BODY = {
    "userStatus": {
        "planStatus": {
            "planInfo": {"planName": "Teams Pro"},
            "dailyQuotaRemainingPercent": "80",
            "weeklyQuotaRemainingPercent": 35,
            "dailyQuotaResetAtUnix": str(epoch(300)),
            "weeklyQuotaResetAtUnix": epoch(4 * 1440),
        }
    }
}
KIMI_BODY = {
    "limits": [
        {
            "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
            "detail": {"limit": 100, "used": 25},
        },
        {
            "window": {"duration": 5, "timeUnit": "TIME_UNIT_HOUR"},
            "detail": {"limit": 50, "remaining": 10},
        },
        {
            "window": {"duration": 1, "timeUnit": "TIME_UNIT_WEEK"},
            "detail": {"limit": 0, "used": 5},
        },
    ],
    "usage": {"limit": 1000, "used": 100, "reset_in": 3600},
    "usages": {"limit_month_total": {"used_ratio": 0.256, "reset_time": iso(10 * 1440)}},
}
KIRO_BODY = {  # kiro-cpa-plugin quotaFromUsage shape
    "subscription": {"plan": "Kiro Pro", "tierId": "PRO"},
    "summary": [{"key": "used_0", "label": "Credits used", "value": 40}],
    "groups": [
        {
            "displayName": "Credits",
            "buckets": [
                {
                    "window": "Plan",
                    "remainingFraction": 0.6,
                    "resetTime": iso(9 * 1440),
                    "description": "40 / 100",
                },
                {"window": "Free trial", "remainingFraction": 1},
            ],
        }
    ],
}


def ag(n, **extra):
    return account("antigravity", n, project_id=PROJECT, **extra)


def kiro(n, **extra):
    return account("kiro", n, supports_quota=True, quota_plugin_id=PLUGIN_ID, **extra)


def idx(n):
    return f"{n:016x}"


# ── Parsers ─────────────────────────────────────────────────────────────────


class ParserTests(unittest.TestCase):
    def test_xai_weekly_used_and_monthly_spend(self):
        usage = quota.parse_xai(XAI_WEEKLY, XAI_MONTHLY)
        self.assertEqual(
            [(w.label, round(w.used, 1)) for w in usage.windows], [("wk", 42.5), ("mo", 20.0)]
        )
        self.assertEqual(usage.windows[0].resets_at, NOW + timedelta(days=3))
        self.assertEqual(usage.plan, "supergrok")

    def test_xai_heavy_plan_and_one_half_missing(self):
        heavy = {"config": {"monthlyLimit": 150000, "used": 0}}
        self.assertEqual(quota.parse_xai(None, heavy).plan, "supergrok_heavy")
        self.assertEqual([w.label for w in quota.parse_xai(XAI_WEEKLY, None).windows], ["wk"])
        self.assertEqual([w.label for w in quota.parse_xai(None, XAI_MONTHLY).windows], ["mo"])

    def test_xai_missing_percent_is_not_zero(self):
        self.assertEqual(
            quota.parse_xai({"config": {"currentPeriod": {"type": "weekly"}}}, None).windows, []
        )
        self.assertEqual(quota.parse_xai("junk", []).windows, [])
        over = {"config": {"monthlyLimit": 100, "used": 250}}
        self.assertEqual(quota.parse_xai(None, over).windows[0].used, 100)

    def test_groups_remaining_fraction_becomes_used(self):
        windows = quota.parse_groups(AG_BODY)
        self.assertEqual(
            [(w.label, round(w.used)) for w in windows],
            [("Gemini Pro 5h", 60), ("Gemini Pro wk", 25), ("Claude 5h", 100)],
        )
        self.assertEqual(windows[0].resets_at, NOW + timedelta(minutes=90))

    def test_groups_clamp_junk_and_masking(self):
        body = {
            "groups": [
                {
                    "displayName": f"user@example.com {TOKENISH}",
                    "buckets": [{"window": "x", "remainingFraction": 7}],
                },
                {"buckets": "nope"},
                "junk",
            ]
        }
        windows = quota.parse_groups(body)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0].used, 0)
        self.assertNotIn("example.com", windows[0].label)
        self.assertNotIn(TOKENISH, windows[0].label)
        self.assertEqual(quota.parse_groups([]), [])

    def test_groups_keep_live_group_names_whole(self):
        body = {
            "groups": [
                {
                    "displayName": "Claude and GPT models",
                    "buckets": [{"window": "5h", "remainingFraction": 1}],
                }
            ]
        }
        self.assertEqual(quota.parse_groups(body)[0].label, "Claude and GPT models 5h")

    def test_plugin_kiro_shape(self):
        usage = quota.parse_plugin(KIRO_BODY)
        self.assertEqual(usage.plan, "kiro_pro")
        self.assertEqual(
            [(w.label, round(w.used)) for w in usage.windows],
            [("Credits Free trial", 0), ("Credits Plan", 40)],
        )

    def test_plugin_snake_aliases(self):
        body = {
            "subscription": {"tier_name": "Team"},
            "groups": [
                {
                    "display_name": "G",
                    "buckets": [
                        {"window": "week", "remaining_fraction": 0.5, "reset_time": iso(60)}
                    ],
                }
            ],
        }
        usage = quota.parse_plugin(body)
        self.assertEqual(usage.plan, "team")
        self.assertEqual([(w.label, w.used) for w in usage.windows], [("G wk", 50)])

    def test_devin_remaining_percent(self):
        usage = quota.parse_devin(DEVIN_BODY)
        self.assertEqual(usage.plan, "teams_pro")
        self.assertEqual([(w.label, w.used) for w in usage.windows], [("day", 20), ("wk", 65)])
        self.assertEqual(usage.windows[0].resets_at, NOW + timedelta(minutes=300))

    def test_devin_rejects_junk(self):
        body = {
            "userStatus": {
                "planStatus": {
                    "dailyQuotaRemainingPercent": "80%",
                    "weeklyQuotaRemainingPercent": 101,
                }
            }
        }
        self.assertEqual(quota.parse_devin(body).windows, [])
        self.assertEqual(quota.parse_devin(None).windows, [])

    def test_kimi_rows(self):
        usage = quota.parse_kimi(KIMI_BODY, NOW)
        self.assertEqual(
            [(w.label, round(w.used)) for w in usage.windows],
            [("5h", 25), ("5h", 80), ("wk", 10), ("mo", 26)],
        )
        self.assertEqual(usage.windows[2].resets_at, NOW + timedelta(hours=1))

    def test_kimi_time_units(self):
        for unit, duration, label in (
            ("TIME_UNIT_SECOND", 30, "30s"),
            ("TIME_UNIT_DAY", 1, "1d"),
            ("TIME_UNIT_WEEK", 1, "wk"),
            ("", 90, "90m"),
            (None, 120, "2h"),
        ):
            body = {
                "limits": [
                    {
                        "window": {"duration": duration, "timeUnit": unit},
                        "detail": {"limit": 10, "used": 1},
                    }
                ]
            }
            self.assertEqual(quota.parse_kimi(body, NOW).windows[0].label, label, unit)


# ── Requests ────────────────────────────────────────────────────────────────


class RequestTests(QuotaTestCase):
    def test_xai_two_gets_with_grok_cli_headers(self):
        _CPA.files = [account("xai", 4)]
        _CPA.by_url = {
            (idx(4), quota.XAI_WEEKLY_URL): (200, XAI_WEEKLY),
            (idx(4), quota.XAI_MONTHLY_URL): (200, XAI_MONTHLY),
        }
        out = self.run_quota("quota pre xai")
        self.assertEqual(
            [p["body"]["url"] for p in _CPA.posts], [quota.XAI_WEEKLY_URL, quota.XAI_MONTHLY_URL]
        )
        for post in _CPA.posts:
            body = post["body"]
            self.assertEqual(set(body), {"auth_index", "method", "url", "header"})
            self.assertEqual(body["method"], "GET")
            self.assertEqual(
                body["header"],
                {
                    "Authorization": "Bearer " + TOKEN,
                    "x-xai-token-auth": "xai-grok-cli",
                    "x-grok-client-version": "1.0.46",
                    "accept": "*/*",
                    "user-agent": "grok-pager/1.0.46 grok-shell/1.0.46 (macos; aarch64)",
                },
            )
        self.assertIn("supergrok  wk 42% → ", out)
        self.assertIn("mo 20%", out)

    def test_antigravity_post_with_project_and_fallback(self):
        _CPA.files = [ag(5)]
        first, second, third = quota.ANTIGRAVITY_URLS
        _CPA.by_url = {
            (idx(5), first): (500, {}),
            (idx(5), second): (200, {"groups": []}),
            (idx(5), third): (200, AG_BODY),
        }
        out = self.run_quota("quota pre antigravity")
        self.assertEqual([p["body"]["url"] for p in _CPA.posts], list(quota.ANTIGRAVITY_URLS))
        body = _CPA.posts[0]["body"]
        self.assertEqual(body["method"], "POST")
        self.assertEqual(body["data"], '{"project":"' + PROJECT + '"}')
        self.assertEqual(
            body["header"],
            {
                "Authorization": "Bearer " + TOKEN,
                "Content-Type": "application/json",
                "User-Agent": "antigravity/hub/2.23.0 darwin/arm64",
            },
        )
        self.assertIn("Gemini Pro 5h 60%", out)
        self.assertNotIn(PROJECT, out)

    def test_antigravity_stops_at_first_good_url(self):
        _CPA.files = [ag(5)]
        _CPA.inner = {idx(5): (200, AG_BODY)}
        self.run_quota("quota pre antigravity")
        self.assertEqual(len(_CPA.posts), 1)

    def test_antigravity_all_urls_fail_shows_last_inner_error(self):
        _CPA.files = [ag(5)]
        _CPA.inner = {idx(5): (403, {"error": "secret-body"})}
        out = self.run_quota("quota pre antigravity")
        self.assertEqual(len(_CPA.posts), 3)
        self.assertIn("vendor HTTP 403", out)
        self.assertNotIn("secret-body", out)

    def test_antigravity_outer_error_ends_the_account(self):
        _CPA.files = [ag(5)]
        _CPA.outer = 502
        out = self.run_quota("quota pre antigravity")
        self.assertEqual(len(_CPA.posts), 1)
        self.assertIn("CPA could not reach vendor", out)

    def test_antigravity_without_project_sends_nothing(self):
        _CPA.files = [
            account("antigravity", 5),
            account("antigravity", 6, project_id="bad project"),
        ]
        out = self.run_quota("quota pre antigravity")
        self.assertEqual(_CPA.posts, [])
        self.assertEqual(out.count("no project id"), 2)

    def test_devin_exact_body(self):
        _CPA.files = [account("devin", 7)]
        _CPA.inner = {idx(7): (200, DEVIN_BODY)}
        out = self.run_quota("quota pre devin")
        body = _CPA.posts[0]["body"]
        self.assertEqual(body["url"], quota.DEVIN_URL)
        self.assertEqual(body["method"], "POST")
        self.assertEqual(
            body["header"], {"Content-Type": "application/json", "Connect-Protocol-Version": "1"}
        )
        self.assertEqual(
            body["data"],
            '{"metadata":{"ideName":"chisel","ideVersion":"3000.10.21","apiKey":"'
            + TOKEN
            + '","locale":"en","os":"darwin","extensionVersion":"3000.10.21",'
            + '"clientName":"chisel"}}',
        )
        self.assertIn("teams_pro  day 20%", out)

    def test_kimi_domain_from_raw_listing_fields(self):
        _CPA.files = [
            account("kimi", 8),
            account("kimi-ai", 9),
            account("kimi", 10, name="kimi.ai-user10.json"),
        ]
        _CPA.inner = {idx(n): (200, KIMI_BODY) for n in (8, 9, 10)}
        self.run_quota("quota pre kimi")
        urls = {p["body"]["auth_index"]: p["body"]["url"] for p in _CPA.posts}
        self.assertEqual(urls[idx(8)], quota.KIMI_COM_URL)
        self.assertEqual(urls[idx(9)], quota.KIMI_AI_URL)
        self.assertEqual(urls[idx(10)], quota.KIMI_AI_URL)
        for post in _CPA.posts:
            self.assertEqual(post["body"]["header"], {"Authorization": "Bearer " + TOKEN})
            self.assertNotIn("data", post["body"])

    def test_plugin_get_for_kiro(self):
        _CPA.files = [kiro(11)]
        _CPA.plugin = {idx(11): (200, KIRO_BODY)}
        out = self.run_quota("quota pre kiro")
        self.assertEqual(_CPA.posts, [])
        self.assertEqual(len(_CPA.plugin_gets), 1)
        get = _CPA.plugin_gets[0]
        self.assertEqual(get["path"], f"/v8/management/plugins/{PLUGIN_ID}/quota")
        self.assertEqual(get["query"], {"auth_index": [idx(11)]})
        self.assertEqual(get["auth"], f"Bearer {KEY}")
        self.assertIn("kiro · 1 account: 1 have room", out)
        self.assertIn("kiro_pro  Credits Free trial 0%", out)
        self.assertNotIn(PLUGIN_ID, out)

    def test_plugin_404_and_502_are_per_account(self):
        _CPA.files = [kiro(11), kiro(12), kiro(13)]
        _CPA.plugin = {idx(12): (502, {"error": "secret-body"}), idx(13): (200, KIRO_BODY)}
        out = self.run_quota("quota pre plugin")
        self.assertIn("plugin quota not found", out)
        self.assertIn("plugin could not fetch", out)
        self.assertIn("1 have room", out)
        self.assertEqual(len(_CPA.plugin_gets), 3)
        self.assertNotIn("/v0/", json.dumps(_CPA.seen))
        self.assertNotIn("secret-body", out)

    def test_builtin_with_plugin_id_still_uses_api_call(self):
        _CPA.files = [
            account("claude", 1, supports_quota=True, quota_plugin_id=PLUGIN_ID),
            account("kimi", 8, supports_quota=True, quota_plugin_id=PLUGIN_ID),
        ]
        _CPA.inner[idx(8)] = (200, KIMI_BODY)
        self.run_quota()
        self.assertEqual(_CPA.plugin_gets, [])
        self.assertEqual(
            {p["body"]["url"] for p in _CPA.posts}, {quota.CLAUDE_USAGE_URL, quota.KIMI_COM_URL}
        )

    def test_bare_pool_includes_every_covered_provider(self):
        _CPA.files = [
            account("claude", 1),
            account("xai", 4),
            ag(5),
            kiro(11),
            account("gemini", 14),
        ]
        _CPA.inner[idx(5)] = (200, AG_BODY)
        _CPA.plugin = {idx(11): (200, KIRO_BODY)}
        out = self.run_quota()
        for name in ("claude ·", "xai ·", "antigravity ·", "kiro ·"):
            self.assertIn(name, out)
        self.assertNotIn("gemini", out)
        self.assertLess(out.index("claude ·"), out.index("kiro ·"))


# ── Lockout ─────────────────────────────────────────────────────────────────


class LockoutTests(QuotaTestCase):
    def bad_key(self):
        self.env["PRE_MGMT"] = BAD
        status._RESULTS[(self.root, status._memory_id(BAD))] = status._RESULTS[
            (self.root, status._memory_id(KEY))
        ]

    def warm(self):
        status.report("pre", config=self.config(), home=self.home, secret=self.env.get, now=NOW)
        _CPA.seen, _CPA.posts, _CPA.plugin_gets = [], [], []

    def requests(self):
        return len(_CPA.posts) + len(_CPA.plugin_gets)

    def test_xai_first_outer_401_sends_no_monthly(self):
        _CPA.files = [account("xai", n) for n in range(2, 8)]
        self.warm()
        self.bad_key()
        self.assertIn("key rejected", self.run_quota())
        self.assertEqual(self.requests(), 1)
        self.assertEqual(_CPA.posts[0]["body"]["url"], quota.XAI_WEEKLY_URL)

    def test_antigravity_first_outer_401_sends_no_fallback(self):
        _CPA.files = [ag(n) for n in range(2, 8)]
        self.warm()
        self.bad_key()
        self.assertIn("key rejected", self.run_quota())
        self.assertEqual(self.requests(), 1)

    def test_plugin_first_401_is_one_get(self):
        _CPA.files = [kiro(n) for n in range(2, 8)]
        self.warm()
        self.bad_key()
        self.assertIn("key rejected", self.run_quota())
        self.assertEqual((len(_CPA.plugin_gets), len(_CPA.posts)), (1, 0))
        self.assertTrue(cpa.key_rejected(self.home, self.root, BAD))

    def test_local_errors_do_not_count_as_the_probe(self):
        _CPA.files = [account("antigravity", 2), account("claude", 3, auth_index="bad index")]
        _CPA.files += [account("codex", n) for n in range(4, 10)]
        self.warm()
        self.bad_key()
        self.assertIn("key rejected", self.run_quota())
        self.assertEqual(self.requests(), 1)

    def test_plugin_404_does_not_prove_the_key(self):
        # A 404 can come from a CPA without the route before any key check, so
        # the next account is still probed alone: a bad key costs one 401.
        _CPA.files = [kiro(2)] + [account("codex", n) for n in range(3, 9)]
        self.warm()
        self.bad_key()

        def no_route(*args, **kwargs):  # an old CPA: 404 before any key check
            _CPA.plugin_gets.append({"path": "no-route"})
            raise cpa.GatewayError(404, "not_found")

        with unittest.mock.patch.object(quota, "plugin_quota", no_route):
            self.assertIn("key rejected", self.run_quota())
        self.assertEqual((len(_CPA.plugin_gets), len(_CPA.posts)), (1, 1))

    def test_in_flight_counts_requests_across_multi_call_vendors(self):
        _CPA.files = [account("xai", n) for n in range(2, 14)]
        _CPA.delay = 0.05
        self.run_quota()
        self.assertEqual(len(_CPA.posts), 24)
        self.assertLessEqual(_CPA.peak, quota.MAX_IN_FLIGHT)

    def test_revoked_mid_run_bounded(self):
        _CPA.files = [account("xai", n) for n in range(2, 14)]
        _CPA.flip_after = 2
        _CPA.delay = 0.05
        self.assertIn("key rejected", self.run_quota())
        self.assertLessEqual(len(_CPA.posts) - 2, quota.MAX_IN_FLIGHT)


# ── Privacy and closed table ────────────────────────────────────────────────


class PrivacyTests(QuotaTestCase):
    def test_new_call_fields_stay_in_memory_only(self):
        files = status.files_of(
            {
                "files": [
                    ag(5),
                    kiro(11),
                    account("kimi-ai", 9),
                    account("gemini", 14, project_id=PROJECT),
                ]
            }
        )
        self.assertEqual(files[0]["_project"], PROJECT)
        self.assertEqual(files[1]["_plugin"], PLUGIN_ID)
        self.assertIs(files[2]["_kimi_ai"], True)
        self.assertNotIn("_project", files[3])
        self.assertNotIn("_target", files[3])  # not covered: no call fields at all
        for entry in files:
            for field_name in ("project_id", "quota_plugin_id", "auth_index", "email"):
                self.assertNotIn(field_name, entry)

    def test_run_output_never_shows_identifiers(self):
        _CPA.files = [ag(5), kiro(11), account("xai", 4)]
        _CPA.inner[idx(5)] = (500, f"{PROJECT} {TOKENISH} victim@example.com")
        _CPA.plugin = {idx(11): (200, dict(KIRO_BODY, note=TOKENISH))}
        out = self.run_quota()
        for canary in (
            PROJECT,
            PLUGIN_ID,
            idx(5),
            idx(11),
            TOKENISH,
            "victim",
            "leak",
            "secret-body",
        ):
            self.assertNotIn(canary, out)
        for run in quota._CACHE.values():
            self.assertNotIn(PROJECT, repr(run))
            self.assertNotIn(PLUGIN_ID, repr(run))

    def test_banned_requests_absent_from_source(self):
        source = (Path(quota.__file__)).read_text(encoding="utf-8")
        for banned in (
            "api.x.ai",
            "chat/completions",
            "/v1/me",
            "loadCodeAssist",
            "onboardUser",
            "credentials/download",
            "/quota/reset",
            "cooldown/reset",
            "proxy_url",
            '"DELETE"',
        ):
            self.assertNotIn(banned, source)


if __name__ == "__main__":
    os.environ.pop("HERMES_HOME", None)
    unittest.main()

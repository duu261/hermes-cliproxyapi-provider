"""``/cpa quota`` 0.6.0: xai, antigravity, devin, kimi and CPA plugin quota.

Reuses the fake CPA from ``test_quota``. Vendor shapes follow the Management
Center (router-for-me/Cli-Proxy-API-Management-Center@64b4f5aed1).
"""

from __future__ import annotations

import json
import os
import sys
import time
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
    CLAUDE_BODY,
    CODEX_PRO,
    CODEX_SPARK,
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
# Field names as the Management Center reads them; the figures are made up.
XAI_PRODUCTS = {
    "config": {
        "creditUsagePercent": 36,
        "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "end": iso(3 * 1440)},
        "productUsage": [
            {"product": "GrokBuild", "usagePercent": 30.4},
            {"product": "Api", "usagePercent": 6},
        ],
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

    def test_xai_plan_is_never_guessed_from_the_monthly_limit(self):
        # xAI sends no plan name. The amounts once matched two plans; xAI
        # changed its plans, so an amount says nothing about the plan.
        self.assertEqual(quota.parse_xai(XAI_WEEKLY, XAI_MONTHLY).plan, "")
        for cents in (15000, 150000):
            monthly = {"config": {"monthlyLimit": cents, "used": 0}}
            self.assertEqual(quota.parse_xai(None, monthly).plan, "")

    def test_xai_one_half_missing(self):
        self.assertEqual([w.label for w in quota.parse_xai(XAI_WEEKLY, None).windows], ["wk"])
        self.assertEqual([w.label for w in quota.parse_xai(None, XAI_MONTHLY).windows], ["mo"])

    def test_xai_missing_percent_is_not_zero(self):
        self.assertEqual(
            quota.parse_xai({"config": {"currentPeriod": {"type": "weekly"}}}, None).windows, []
        )
        self.assertEqual(quota.parse_xai("junk", []).windows, [])
        over = {"config": {"monthlyLimit": 100, "used": 250}}
        self.assertEqual(quota.parse_xai(None, over).windows[0].used, 100)

    def test_xai_running_week_without_percent_is_pending_not_zero(self):
        # Live shape: xAI confirms the week but sends no percent until the
        # account has used enough; the monthly reply has monthlyLimit 0.
        weekly = {
            "config": {
                "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "end": iso(4 * 1440)},
                "onDemandCap": {"val": 0},
                "onDemandUsed": {"val": 0},
                "prepaidBalance": {"val": 0},
            }
        }
        monthly = {"config": {"monthlyLimit": {"val": 0}, "used": {"val": 1}}}
        usage = quota.parse_xai(weekly, monthly)
        self.assertEqual(usage.windows, [])
        self.assertEqual(
            [(w.label, w.resets_at) for w in usage.pending], [("wk", NOW + timedelta(days=4))]
        )
        result = quota.AccountResult("a", "xai", usage)
        self.assertEqual(result.out_until(), (False, None))
        out = "\n".join(quota._provider_block("xai", [result], NOW))
        self.assertIn("1 have room", out)
        self.assertIn("wk no figure yet → ", out)
        self.assertNotIn("0%", out)

    def test_xai_usage_by_product(self):
        self.assertEqual(quota.parse_xai(XAI_PRODUCTS, None).extra, "grokbuild 30%, api 6%")
        self.assertEqual(quota.parse_xai(XAI_WEEKLY, XAI_MONTHLY).extra, "")

        def extra(items, monthly=None, key="productUsage"):
            return quota.parse_xai({"config": {key: items}}, monthly).extra

        # The other spelling of both field names.
        self.assertEqual(
            extra([{"product": "Api", "usage_percent": "12.9"}], key="product_usage"), "api 12%"
        )
        # A product with no percent is left out, never shown as 0%.
        self.assertEqual(
            extra(
                [
                    {"product": "GrokBuild"},
                    {"product": "Api", "usagePercent": None},
                    {"product": "Voice", "usagePercent": True},
                    {"product": "Images", "usagePercent": -1},
                    {"product": "Chat", "usagePercent": 250},
                    {"usagePercent": 9},
                    {"product": 7, "usagePercent": 9},
                    "junk",
                    None,
                ]
            ),
            "chat 100%",
        )
        for junk in (None, "GrokBuild 30%", {"product": "Api", "usagePercent": 5}, [], 5):
            self.assertEqual(extra(junk), "", repr(junk))
        # The weekly reply's list is the one shown; the other reply fills in only
        # when the weekly one has nothing to show.
        other = {"config": {"productUsage": [{"product": "Api", "usagePercent": 3}]}}
        self.assertEqual(quota.parse_xai(XAI_PRODUCTS, other).extra, "grokbuild 30%, api 6%")
        self.assertEqual(extra([{"product": "GrokBuild"}], other), "api 3%")
        self.assertEqual(quota.parse_xai(None, other).extra, "api 3%")
        # A product name is vendor text: masked and bounded like any label.
        shown = extra(
            [
                {"product": "carol.canary@example.com", "usagePercent": 1},
                {"product": TOKENISH, "usagePercent": 2},
                {"product": "A very long product name indeed", "usagePercent": 3},
                {"product": "  Two\n words ", "usagePercent": 4},
            ]
        )
        self.assertEqual(shown, "ca…@… 1%, a very long prod 3%, two words 4%")
        many = [{"product": f"P{n}", "usagePercent": n} for n in range(1, 10)]
        self.assertEqual(extra(many), "p1 1%, p2 2%, p3 3%, p4 4%, p5 5%, p6 6%")

    def test_xai_pay_as_you_go(self):
        def extra(weekly=None, monthly=None):
            return quota.parse_xai(
                None if weekly is None else {"config": weekly},
                None if monthly is None else {"config": monthly},
            ).extra

        self.assertEqual(extra({"onDemandCap": {"val": 0}, "onDemandUsed": {"val": 0}}), "payg off")
        self.assertEqual(extra({"on_demand_cap": "0"}), "payg off")
        # No cap in either reply says nothing about pay as you go: no "off".
        self.assertEqual(extra({"onDemandUsed": {"val": 120}}), "")
        self.assertEqual(extra({}, {}), "")
        self.assertEqual(
            extra({"onDemandCap": {"val": 5000}, "onDemandUsed": {"val": "300"}}),
            "payg $3.00/$50.00",
        )
        self.assertEqual(extra({"on_demand_cap": 5000, "on_demand_used": 0}), "payg $0.00/$50.00")
        self.assertEqual(extra({"onDemandCap": 123456}), "payg cap $1,234.56")
        self.assertEqual(extra({"onDemandCap": 5000, "onDemandUsed": -1}), "payg cap $50.00")
        # The weekly reply's value is used; the other reply fills in what it lacks.
        self.assertEqual(extra({"onDemandCap": 0}, {"onDemandCap": 5000}), "payg off")
        self.assertEqual(
            extra({"onDemandUsed": 250}, {"onDemandCap": 5000, "onDemandUsed": 999}),
            "payg $2.50/$50.00",
        )
        self.assertEqual(extra(None, {"onDemandCap": {"val": 0}}), "payg off")
        for junk in (-5, True, "soon", {"val": None}, {"amount": 5000}, [5000], 10**13):
            self.assertEqual(extra({"onDemandCap": junk}), "", repr(junk))

    def test_xai_prepaid_balance(self):
        def extra(config):
            return quota.parse_xai({"config": config}, None).extra

        self.assertEqual(extra({"prepaidBalance": {"val": 2500}}), "prepaid $25.00")
        self.assertEqual(extra({"prepaid_balance": "1050"}), "prepaid $10.50")
        self.assertEqual(
            quota.parse_xai(None, {"config": {"prepaidBalance": 99}}).extra, "prepaid $0.99"
        )
        for none in ({"val": 0}, 0, -100, True, "x", None, 10**13):
            self.assertEqual(extra({"prepaidBalance": none}), "", repr(none))
        self.assertEqual(
            extra(
                {
                    "productUsage": [{"product": "Api", "usagePercent": 6}],
                    "onDemandCap": {"val": 0},
                    "prepaidBalance": {"val": 2500},
                }
            ),
            "api 6% · payg off · prepaid $25.00",
        )

    def test_xai_extras_are_on_the_account_line(self):
        result = quota.AccountResult("a", "xai", quota.parse_xai(XAI_PRODUCTS, XAI_MONTHLY))
        self.assertEqual(
            quota._provider_block("xai", [result], NOW),
            [
                "xai · 1 account: 1 have room",
                "a  wk 36% · mo 20% · grokbuild 30%, api 6%",
            ],
        )
        # Nothing but pay-as-you-go words is not quota data.
        bare = quota.parse_xai({"config": {"onDemandCap": {"val": 0}}}, None)
        self.assertEqual((bare.windows, bare.pending, bare.extra), ([], [], "payg off"))

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

    def test_antigravity_out_only_when_every_group_is_full(self):
        def body(gemini, claude):
            return {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "window": "weekly",
                                "remainingFraction": gemini,
                                "resetTime": iso(3000),
                            }
                        ],
                    },
                    {
                        "displayName": "Claude and GPT models",
                        "buckets": [
                            {
                                "window": "weekly",
                                "remainingFraction": claude,
                                "resetTime": iso(600),
                            },
                            {"window": "5h", "remainingFraction": 0.5, "resetTime": iso(60)},
                        ],
                    },
                ]
            }

        def account(payload, per_model=True):
            usage = quota.Usage(windows=quota.parse_groups(payload, per_model=per_model))
            return quota.AccountResult("a", "antigravity", usage)

        one = account(body(0.8, 0))
        self.assertEqual(one.out_until(), (False, None))
        self.assertEqual(list(one.full_groups() or {}), ["1:Claude and GPT models"])
        both = account(body(0, 0))
        self.assertEqual(both.out_until(), (True, NOW + timedelta(minutes=600)))
        self.assertEqual(account(body(0.8, 0.9)).out_until(), (False, None))
        # plugin quota (no per_model) keeps the any-window rule
        self.assertTrue(account(body(0.8, 0), per_model=False).out_until()[0])
        self.assertIsNone(account(body(0.8, 0), per_model=False).full_groups())

    def test_antigravity_one_line_per_model_group(self):
        def block(gemini, claude, per_model=True):
            payload = {
                "groups": [
                    {
                        "displayName": "Gemini Models",
                        "buckets": [
                            {
                                "window": "weekly",
                                "remainingFraction": gemini,
                                "resetTime": iso(3000),
                            },
                            {"window": "5h", "remainingFraction": 1, "resetTime": iso(60)},
                        ],
                    },
                    {
                        "displayName": "Claude and GPT models",
                        "buckets": [
                            {
                                "window": "weekly",
                                "remainingFraction": claude,
                                "resetTime": iso(600),
                            },
                            {"window": "5h", "remainingFraction": 0.75, "resetTime": iso(60)},
                        ],
                    },
                ]
            }
            usage = quota.Usage(windows=quota.parse_groups(payload, per_model=per_model))
            return quota._provider_block(
                "antigravity", [quota.AccountResult("ag-01", "antigravity", usage)], NOW
            )

        late = quota._when(NOW + timedelta(minutes=3000), NOW)
        soon = quota._when(NOW + timedelta(minutes=600), NOW)
        self.assertEqual(
            block(0.01, 0.5),
            [
                "antigravity · 1 account: 1 have room",
                "ag-01",
                f"  gemini          5h 0% · wk 99% → {late}",
                "  claude and gpt  5h 25% · wk 50%",
            ],
        )
        # One full group: the account still serves the other, and says which is out.
        self.assertEqual(
            block(0.01, 0)[1:],
            [
                "ag-01  out: claude and gpt",
                f"  gemini          5h 0% · wk 99% → {late}",
                f"  claude and gpt  5h 25% · wk 100% → {soon}",
            ],
        )
        # Every group full: OUT, with the group lines under the account name.
        self.assertEqual(
            block(0, 0)[1:],
            [
                "OUT ag-01",
                f"      gemini          5h 0% · wk 100% → {late}",
                f"      claude and gpt  5h 25% · wk 100% → {soon}",
            ],
        )
        # CPA plugin quota has no per-model rule: it stays on one line.
        flat = block(0.01, 0.5, per_model=False)
        self.assertEqual(len(flat), 2)
        self.assertIn("Gemini Models 5h 0% · Gemini Models wk 99%", flat[1])

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
    def test_extras_come_from_the_replies_already_fetched(self):
        _CPA.files = [account("xai", 4), account("codex", 2)]
        _CPA.by_url = {
            (idx(4), quota.XAI_WEEKLY_URL): (200, XAI_PRODUCTS),
            (idx(4), quota.XAI_MONTHLY_URL): (200, {"config": {"onDemandCap": {"val": 0}}}),
        }
        _CPA.inner[idx(2)] = (
            200,
            dict(
                CODEX_PRO,
                additional_rate_limits=[CODEX_SPARK],
                rate_limit_reset_credits={"available_count": 2},
            ),
        )
        out = self.run_quota()
        # The same three requests as before: nothing new is sent for the extras.
        self.assertEqual(
            sorted(p["body"]["url"] for p in _CPA.posts),
            sorted([quota.CODEX_USAGE_URL, quota.XAI_WEEKLY_URL, quota.XAI_MONTHLY_URL]),
        )
        self.assertIn("\nxai-us…@…  wk 36% · grokbuild 30%, api 6% · payg off\n", out + "\n")
        self.assertIn(
            "\ncodex-us…@… pro  wk 40% · gpt-5.3-codex-spark wk 12% · 2 resets banked\n",
            out + "\n",
        )

    def test_xai_extras_alone_are_not_quota_data(self):
        _CPA.files = [account("xai", 4)]
        only = {"config": {"onDemandCap": {"val": 0}, "prepaidBalance": {"val": 2500}}}
        _CPA.by_url = {
            (idx(4), quota.XAI_WEEKLY_URL): (200, only),
            (idx(4), quota.XAI_MONTHLY_URL): (200, only),
        }
        out = self.run_quota("quota pre xai")
        self.assertIn("xai · 1 account: 0 have room · 1 failed", out)
        self.assertIn("no quota data", out)
        self.assertNotIn("payg", out)
        self.assertNotIn("prepaid", out)

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
                    "x-grok-client-version": "1.0.70",
                    "accept": "*/*",
                    "user-agent": "grok-pager/1.0.70 grok-shell/1.0.70 (macos; aarch64)",
                },
            )
        self.assertIn("  wk 42% · mo 20%", out)  # no reset time below 80%
        self.assertNotIn("supergrok", out)
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
        self.assertEqual(
            [p["body"]["url"] for p in _CPA.posts],
            [*quota.ANTIGRAVITY_URLS, quota.ANTIGRAVITY_TIER_URL],
        )
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
        self.assertIn("\n  gemini pro  5h 60% · wk 25%\n", out)
        self.assertIn("\n  claude      5h 100%", out)
        self.assertNotIn(PROJECT, out)

    def test_antigravity_stops_at_first_good_url(self):
        _CPA.files = [ag(5)]
        _CPA.inner = {idx(5): (200, AG_BODY)}
        self.run_quota("quota pre antigravity")
        self.assertEqual(
            [p["body"]["url"] for p in _CPA.posts],
            [quota.ANTIGRAVITY_URLS[0], quota.ANTIGRAVITY_TIER_URL],
        )

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
            {p["body"]["url"] for p in _CPA.posts},
            {quota.CLAUDE_USAGE_URL, quota.CLAUDE_PROFILE_URL, quota.KIMI_COM_URL},
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
        self.assertNotIn("gemini ·", out)  # the gemini provider has no live quota
        self.assertNotIn("gemini-us", out)
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


# ── Subscription tier (one more request per Claude and Antigravity account) ─

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CODE_ASSIST_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
# The fields the Management Center reads for the plan. Everything else here is
# made up: it stands for what the real replies also carry and is never shown.
CLAUDE_PROFILE = {
    "account": {
        "uuid": "11111111-2222-3333-4444-555555555555",
        "full_name": "Carol Canary",
        "email": "carol.canary@example.com",
        "has_claude_max": True,
        "has_claude_pro": False,
    },
    "organization": {
        "uuid": "66666666-7777-8888-9999-000000000000",
        "name": "Canary Org Ltd",
        "organization_type": "claude_max",
        "billing_type": "stripe_subscription",
        "rate_limit_tier": "default_claude_max_20x",
        "subscription_status": "active",
    },
}
AG_TIER = {
    "currentTier": {"id": "free-tier", "name": "Free Canary"},
    "paidTier": {"id": "g1-pro-tier", "name": "Pro Canary"},
    "cloudaicompanionProject": PROJECT,
}
CLAUDE_TIERS = {"team", "max20", "max5", "max", "pro", "free", ""}
ANTIGRAVITY_TIERS = {"free", "pro", "ultra", "ultra-lite", ""}


class TierParserTests(unittest.TestCase):
    def test_claude_tier_is_a_word_from_the_closed_list(self):
        team = {"organization_type": "claude_team", "subscription_status": "active"}
        for org, acct, expected in (
            (team, {}, "team"),
            (dict(team, subscription_status=" Active "), {"has_claude_max": True}, "team"),
            # A team that is not active is not the team plan: the other fields decide.
            (dict(team, subscription_status="canceled"), {"has_claude_pro": True}, "pro"),
            ({"organization_type": "claude_team"}, {}, ""),
            ({"rate_limit_tier": "default_claude_max_20x"}, {"has_claude_max": True}, "max20"),
            ({"rate_limit_tier": " DEFAULT_CLAUDE_MAX_5X "}, {}, "max5"),
            # Only the rate-limit tier tells Max 5x from Max 20x; the flag says "max".
            ({"rate_limit_tier": "default_claude_ai"}, {"has_claude_max": True}, "max"),
            ({}, {"has_claude_max": True, "has_claude_pro": True}, "max"),
            ({}, {"has_claude_max": False, "has_claude_pro": True}, "pro"),
            ({}, {"has_claude_max": "false", "has_claude_pro": "TRUE"}, "pro"),
            ({}, {"has_claude_max": 0, "has_claude_pro": 1}, "pro"),
            ({}, {"has_claude_max": False, "has_claude_pro": False}, "free"),
            # Free only when both flags say no; a missing or odd flag is unknown.
            ({}, {"has_claude_max": False}, ""),
            ({}, {"has_claude_max": False, "has_claude_pro": "maybe"}, ""),
            ({}, {}, ""),
            ({"rate_limit_tier": "default_claude_max_40x"}, {}, ""),
            ({"rate_limit_tier": 20}, {}, ""),
            ({"rateLimitTier": "default_claude_max_20x"}, {}, ""),  # not an allowed field
        ):
            got = quota.parse_claude_tier({"organization": org, "account": acct})
            self.assertEqual(got, expected, (org, acct))
            self.assertIn(got, CLAUDE_TIERS)
        for junk in (None, [], "max", 5, {"organization": "claude_team", "account": [True]}):
            self.assertEqual(quota.parse_claude_tier(junk), "", repr(junk))
        self.assertEqual(quota.parse_claude_tier(CLAUDE_PROFILE), "max20")

    def test_antigravity_tier_is_a_word_from_the_closed_list(self):
        for payload, expected in (
            ({"paidTier": {"id": "g1-pro-tier"}}, "pro"),
            ({"paidTier": {"id": "g1-ultra-tier"}, "currentTier": {"id": "free-tier"}}, "ultra"),
            ({"paidTier": {"id": " g1-ultra-lite-tier "}}, "ultra-lite"),
            ({"currentTier": {"id": "free-tier"}}, "free"),
            # No paid tier id: the current tier decides.
            ({"paidTier": {"name": "no id"}, "currentTier": {"id": "g1-pro-tier"}}, "pro"),
            ({"paidTier": {"id": "  "}, "currentTier": {"id": "free-tier"}}, "free"),
            ({"paidTier": None, "currentTier": {"id": "free-tier"}}, "free"),
            # A paid tier the list does not know is not replaced by the current one.
            ({"paidTier": {"id": "g2-new-tier"}, "currentTier": {"id": "free-tier"}}, ""),
            ({"currentTier": {"id": "G1-PRO-TIER"}}, ""),  # ids are matched as sent
            ({"currentTier": {"id": "pro"}}, ""),
            ({"paid_tier": {"id": "g1-pro-tier"}}, ""),  # not an allowed field
            ({"paidTier": "g1-pro-tier"}, ""),
            ({"paidTier": {"id": 7}}, ""),
            ({}, ""),
            (None, ""),
            ([], ""),
            ("g1-pro-tier", ""),
        ):
            got = quota.parse_antigravity_tier(payload)
            self.assertEqual(got, expected, repr(payload))
            self.assertIn(got, ANTIGRAVITY_TIERS)
        self.assertEqual(quota.parse_antigravity_tier(AG_TIER), "pro")


class TierTests(QuotaTestCase):
    def claude_only(self, profile=(200, CLAUDE_PROFILE)):
        _CPA.files = [account("claude", 1)]
        _CPA.inner = {idx(1): (200, CLAUDE_BODY)}
        _CPA.by_url = {(idx(1), PROFILE_URL): profile}

    def antigravity_only(self, tier=(200, AG_TIER)):
        _CPA.files = [ag(5)]
        _CPA.inner = {idx(5): (200, AG_BODY)}
        _CPA.by_url = {(idx(5), CODE_ASSIST_URL): tier}

    def urls(self):
        return [p["body"]["url"] for p in _CPA.posts]

    def test_claude_profile_request_and_plan(self):
        self.claude_only()
        out = self.run_quota("quota pre claude")
        self.assertEqual(self.urls(), [USAGE_URL, PROFILE_URL])
        usage, profile = (p["body"] for p in _CPA.posts)
        self.assertEqual(set(profile), {"auth_index", "method", "url", "header"})
        self.assertEqual(profile["method"], "GET")
        self.assertEqual(profile["auth_index"], idx(1))
        # The usage request's headers, except Accept, which is not sent.
        self.assertEqual(
            profile["header"],
            {
                "Authorization": "Bearer " + TOKEN,
                "Content-Type": "application/json",
                "User-Agent": "claude-cli/2.1.280 (external, cli)",
                "anthropic-beta": "oauth-2025-04-20",
            },
        )
        self.assertEqual(usage["header"], dict(profile["header"], Accept="application/json"))
        self.assertIn("\nclaude-us…@… max20  5h 55% · wk 31%", out)

    def test_antigravity_tier_request_and_plan(self):
        self.antigravity_only()
        out = self.run_quota("quota pre antigravity")
        self.assertEqual(self.urls(), [quota.ANTIGRAVITY_URLS[0], CODE_ASSIST_URL])
        usage, tier = (p["body"] for p in _CPA.posts)
        self.assertEqual(set(tier), {"auth_index", "method", "url", "header", "data"})
        self.assertEqual(tier["method"], "POST")
        self.assertEqual(tier["auth_index"], idx(5))
        self.assertEqual(tier["data"], '{"metadata":{"ideType":"ANTIGRAVITY"}}')
        self.assertEqual(tier["header"], usage["header"])
        self.assertNotIn(PROJECT, json.dumps(tier))
        self.assertRegex(out, r"(?m)^antigravity-\S+ pro  out: claude$")

    def test_a_failed_tier_request_leaves_the_plan_out(self):
        big = "x" * (quota.MAX_INNER_BYTES + 1)
        replies = (
            (401, {"error": "secret-body"}),
            (403, "secret-body"),
            (429, {}),
            (500, {}),
            (200, "secret-body not json"),
            (200, []),
            (200, {"account": {"full_name": "secret-body"}}),
            (200, big),
        )
        for case in ("claude", "antigravity"):
            for reply in replies:
                with self.subTest(case=case, status=reply[0], body=str(reply[1])[:20]):
                    status.reset_state()  # the account listing is cached too
                    _CPA.posts = []
                    getattr(self, case + "_only")(reply)
                    out = self.run_quota(f"quota pre {case}")
                    # Sent once: no retry, and no fallback URL for Antigravity.
                    self.assertEqual(len(_CPA.posts), 2, self.urls())
                    self.assertIn(f"{case} · 1 account: 1 have room", out)
                    self.assertNotIn("failed", out)
                    self.assertNotIn("vendor", out)
                    self.assertNotIn("secret-body", out)
                    self.assertFalse(cpa.key_rejected(self.home, self.root, KEY))
        self.claude_only((500, {}))
        status.reset_state()
        self.assertIn("\nclaude-us…@…  5h 55% · wk 31%", self.run_quota("quota pre claude"))

    def test_a_tier_reply_counts_only_when_the_vendor_said_ok(self):
        # A reply that would give a plan, under a failed status or over the size cap.
        pad = "x" * quota.MAX_INNER_BYTES
        for case, good in (("claude", CLAUDE_PROFILE), ("antigravity", AG_TIER)):
            for reply in (
                (401, good),
                (403, good),
                (429, good),
                (500, good),
                (200, {**good, "pad": pad}),
            ):
                with self.subTest(case=case, status=reply[0]):
                    status.reset_state()
                    _CPA.posts = []
                    getattr(self, case + "_only")(reply)
                    out = self.run_quota(f"quota pre {case}")
                    self.assertEqual(len(_CPA.posts), 2, self.urls())
                    self.assertIn(f"{case} · 1 account: 1 have room", out)
                    self.assertNotIn("max20", out)
                    self.assertNotIn("@… pro", out)

    def test_no_tier_request_when_the_quota_part_failed(self):
        for reply, shown in (((500, {}), "vendor HTTP 500"), ((200, {}), "no quota data")):
            quota.reset_state()
            _CPA.posts = []
            self.claude_only()
            _CPA.inner = {idx(1): reply}
            self.assertIn(shown, self.run_quota("quota pre claude"))
            self.assertEqual(self.urls(), [USAGE_URL])
        status.reset_state()  # the account listing is cached too
        _CPA.posts = []
        self.antigravity_only()
        _CPA.inner = {idx(5): (200, {"groups": []})}
        self.assertIn("no quota data", self.run_quota("quota pre antigravity"))
        self.assertEqual(self.urls(), list(quota.ANTIGRAVITY_URLS))

    def test_a_cpa_side_tier_failure_keeps_the_usage_row(self):
        real = quota.api_call
        for error in (cpa.GatewayError(502, "upstream"), cpa.GatewayError(0, "timeout")):
            calls = []

            def flaky(root, key, path, body, _error=error, _calls=calls, **kwargs):
                _calls.append(body["url"])
                if body["url"] == PROFILE_URL:
                    raise _error
                return real(root, key, path, body, **kwargs)

            quota.reset_state()
            self.claude_only()
            with unittest.mock.patch.object(quota, "api_call", flaky):
                out = self.run_quota("quota pre claude")
            self.assertEqual(calls, [USAGE_URL, PROFILE_URL])
            self.assertIn("claude · 1 account: 1 have room", out)
            self.assertIn("\nclaude-us…@…  5h 55% · wk 31%", out)
            self.assertNotIn("CPA could not", out)
            self.assertNotIn("timeout", out)

    def test_tier_request_is_never_the_first_request(self):
        # A bad key must cost one usage request, as before the tier requests.
        _CPA.files = [account("claude", n) for n in range(2, 8)]
        _CPA.inner = {idx(n): (200, CLAUDE_BODY) for n in range(2, 8)}
        status.report("pre", config=self.config(), home=self.home, secret=self.env.get, now=NOW)
        _CPA.posts = []
        self.env["PRE_MGMT"] = BAD
        status._RESULTS[(self.root, status._memory_id(BAD))] = status._RESULTS[
            (self.root, status._memory_id(KEY))
        ]
        self.assertIn("key rejected", self.run_quota())
        self.assertEqual(self.urls(), [USAGE_URL])

    def test_outer_401_on_a_tier_request_latches_and_stops(self):
        _CPA.files = [account("claude", n) for n in range(2, 8)]
        _CPA.inner = {idx(n): (200, CLAUDE_BODY) for n in range(2, 8)}
        _CPA.flip_after = 1  # the second request, the first tier request, gets a 401
        self.assertIn("key rejected", self.run_quota())
        self.assertEqual(self.urls(), [USAGE_URL, PROFILE_URL])
        self.assertTrue(cpa.key_rejected(self.home, self.root, KEY))

    def test_tier_request_is_not_sent_after_the_run_time_limit(self):
        self.claude_only()
        real = quota.api_call

        def slow(root, key, path, body, **kwargs):
            reply = real(root, key, path, body, **kwargs)
            time.sleep(0.3)  # the usage reply arrives, then the run's time is up
            return reply

        with (
            unittest.mock.patch.object(quota, "api_call", slow),
            unittest.mock.patch.object(quota, "RUN_SECONDS", 0.2),
        ):
            out = self.run_quota("quota pre claude")
        self.assertEqual(self.urls(), [USAGE_URL])
        self.assertTrue(out.splitlines()[1].startswith("stopped after 0.2 s"), out)
        self.assertIn("\nclaude-us…@…  5h 55% · wk 31%", out)

    def test_other_providers_send_no_tier_request(self):
        _CPA.files = [account("codex", 2), account("xai", 4), account("kimi", 8), kiro(11)]
        _CPA.inner[idx(8)] = (200, KIMI_BODY)
        _CPA.plugin = {idx(11): (200, KIRO_BODY)}
        self.run_quota()
        self.assertEqual(len(_CPA.posts), 4)  # codex 1, xai 2, kimi 1
        self.assertEqual(len(_CPA.plugin_gets), 1)
        for url in self.urls():
            self.assertNotIn("profile", url)
            self.assertNotIn("loadCodeAssist", url)

    def test_tier_replies_show_only_the_fixed_word(self):
        _CPA.files = [account("claude", 1), ag(5)]
        _CPA.inner = {idx(1): (200, CLAUDE_BODY), idx(5): (200, AG_BODY)}
        _CPA.by_url = {
            (idx(1), PROFILE_URL): (200, CLAUDE_PROFILE),
            (idx(5), CODE_ASSIST_URL): (200, AG_TIER),
        }
        out = self.run_quota()
        self.assertIn("@… max20  5h 55%", out)
        self.assertRegex(out, r"(?m)^antigravity-\S+ pro  out: claude$")
        shown = out + "".join(repr(run) for run in quota._CACHE.values())
        self.assertEqual(len(quota._CACHE), 1)
        for canary in (
            "Carol",
            "carol",
            "Canary",
            "canary",  # also in the project id
            "11111111",
            "66666666",
            "stripe",
            "claude_max",  # the raw organization type and rate-limit tier
            "g1-pro-tier",
            "free-tier",
            idx(1),
            idx(5),
        ):
            self.assertNotIn(canary, shown)


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
            "onboardUser",
            "backend-api/subscriptions",
            "rate-limit-reset-credits",
            "v1/user",
            "v1/settings",
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

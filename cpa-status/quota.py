"""``/cpa quota``: live usage per account, read from the vendor through CPA.

Does what CLIProxyAPI's own Management Center (MC) does on its quota page.
For the built-in vendors, CPA's ``api-call`` route sends the fixed requests in
``QUOTA_REQUESTS`` with that account's token (CPA fills the token placeholder;
this plugin never sees a vendor token). Accounts of other providers whose CPA
quota plugin serves quota (e.g. Kiro) are read with CPA's plugin quota GET.
Rules (see AGENTS.md, "/cpa status"):

- Only on command. Requests are fixed in code; nothing from the user or config
  reaches one. No paid, download or reset calls. Besides usage, the only
  lookups are the two tier requests (the Claude profile and Antigravity
  ``loadCodeAssist``), sent after an account's usage was read and used for
  the plan word alone.
- The whole run holds the shared management guard for its CPA once. Accounts
  are tried one at a time, one request at a time, until a reply proves CPA
  accepted the key; an outer 401 latches the key and stops the run. After that
  at most ``MAX_IN_FLIGHT`` requests at a time, the latch checked before each.
- ``auth_index``, the Codex account id, the Antigravity project id and the CPA
  plugin id stay in memory to build requests; they are never shown, logged or
  cached. Vendor replies are read through allowlists; bodies and headers are
  never shown.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple, NoReturn
from urllib.parse import quote

try:  # loaded by Hermes as a package
    from . import cpa, status
except ImportError:  # pragma: no cover - direct import in tests
    import status  # type: ignore[no-redef]

    import cpa  # type: ignore[no-redef]

# ── Requests (closed table, copied from the Management Center) ──────────────
# router-for-me/Cli-Proxy-API-Management-Center@64b4f5aed1
# src/utils/quota/constants.ts, src/features/quota/providers/*/data.ts and
# devin/requests.ts (the xAI client version is from @38990aec51). CPA v8.0.46
# fills TOKEN in header values and in data. The two tier requests are from
# @953fb24443: providers/claude/data.ts and services/api/antigravitySubscription.ts.

TOKEN = "$" + "TOKEN" + "$"
BEARER = "Bearer " + TOKEN


class Probe(NamedTuple):
    method: str
    url: str
    header: dict[str, str]
    data: str = ""
    # A tier request: sent once, after the account's usage was read, to show
    # the plan. It never fails the account's row (see ``_Runner._tier``).
    tier: bool = False


CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
XAI_WEEKLY_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
XAI_MONTHLY_URL = "https://cli-chat-proxy.grok.com/v1/billing"
ANTIGRAVITY_URLS = (
    "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://daily-cloudcode-pa.sandbox.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
)
ANTIGRAVITY_TIER_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist"
ANTIGRAVITY_TIER_DATA = '{"metadata":{"ideType":"ANTIGRAVITY"}}'
DEVIN_URL = "https://server.codeium.com/exa.seat_management_pb.SeatManagementService/GetUserStatus"
KIMI_COM_URL = "https://api.kimi.com/coding/v1/usages"
KIMI_AI_URL = "https://api.kimi.ai/coding/v1/usages"

# The Management Center sends these on both Claude requests, plus ``Accept``
# on the usage request only.
_CLAUDE_HEADER = {
    "Authorization": BEARER,
    "Content-Type": "application/json",
    "User-Agent": "claude-cli/2.1.280 (external, cli)",
    "anthropic-beta": "oauth-2025-04-20",
}
_XAI_VERSION = "1.0.70"
_XAI_HEADER = {
    "Authorization": BEARER,
    "x-xai-token-auth": "xai-grok-cli",
    "x-grok-client-version": _XAI_VERSION,
    "accept": "*/*",
    "user-agent": f"grok-pager/{_XAI_VERSION} grok-shell/{_XAI_VERSION} (macos; aarch64)",
}
_ANTIGRAVITY_HEADER = {
    "Authorization": BEARER,
    "Content-Type": "application/json",
    "User-Agent": "antigravity/hub/2.23.0 darwin/arm64",
}
DEVIN_DATA = json.dumps(
    {
        "metadata": {
            "ideName": "chisel",
            "ideVersion": "3000.10.21",
            "apiKey": TOKEN,
            "locale": "en",
            "os": "darwin",
            "extensionVersion": "3000.10.21",
            "clientName": "chisel",
        }
    },
    separators=(",", ":"),
)
_PROJECT = "<project>"  # becomes {"project":"<project_id>"} for the account

QUOTA_REQUESTS: dict[str, tuple[Probe, ...]] = {
    "claude": (
        Probe("GET", CLAUDE_USAGE_URL, {**_CLAUDE_HEADER, "Accept": "application/json"}),
        Probe("GET", CLAUDE_PROFILE_URL, _CLAUDE_HEADER, tier=True),
    ),
    "codex": (
        Probe(
            "GET",
            CODEX_USAGE_URL,
            {
                "Authorization": BEARER,
                "Content-Type": "application/json",
                "User-Agent": (
                    "codex-tui/0.149.1 (Mac OS 26.5.2; arm64) iTerm.app/3.6.11 (codex-tui; 0.149.1)"
                ),
            },
        ),
    ),
    "xai": (
        Probe("GET", XAI_WEEKLY_URL, _XAI_HEADER),
        Probe("GET", XAI_MONTHLY_URL, _XAI_HEADER),
    ),
    "antigravity": (
        *(Probe("POST", url, _ANTIGRAVITY_HEADER, _PROJECT) for url in ANTIGRAVITY_URLS),
        Probe("POST", ANTIGRAVITY_TIER_URL, _ANTIGRAVITY_HEADER, ANTIGRAVITY_TIER_DATA, tier=True),
    ),
    "devin": (
        Probe(
            "POST",
            DEVIN_URL,
            {"Content-Type": "application/json", "Connect-Protocol-Version": "1"},
            DEVIN_DATA,
        ),
    ),
    "kimi": (Probe("GET", KIMI_COM_URL, {"Authorization": BEARER}),),
}
BUILTINS = status.QUOTA_BUILTINS
COVERED = "claude, codex, xai, antigravity, devin, kimi, or plugin (e.g. kiro)"

V8_API_CALL = "/v8/management/requests/api-call"
V0_API_CALL = "/v0/management/api-call"
MAX_ACCOUNTS = 20
MAX_IN_FLIGHT = 4  # management requests, not accounts
CALL_TIMEOUT = 15.0
# One run holds the CPA's management guard, so it has a time limit: requests
# that would start after it are not sent, and the timeout of each request is
# clipped to what is left. That timeout is per socket read, so a reply that
# trickles in can end a little after the limit.
RUN_SECONDS = 60.0
CACHE_SECONDS = 60.0
MAX_OUTER_BYTES = 1024 * 1024
MAX_INNER_BYTES = 256 * 1024

_PLAN_RE = re.compile(r"^[a-z0-9_]{1,32}$")
_SELECTOR_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")


def _body(probe: Probe, provider: str, entry: dict[str, Any], target: str) -> dict[str, Any]:
    url, header = probe.url, dict(probe.header)
    if provider == "codex" and entry.get("_acct"):
        header["Chatgpt-Account-Id"] = entry["_acct"]
    if provider == "kimi" and entry.get("_kimi_ai") is True:
        url = KIMI_AI_URL
    body: dict[str, Any] = {
        "auth_index": target,
        "method": probe.method,
        "url": url,
        "header": header,
    }
    if probe.data == _PROJECT:
        body["data"] = json.dumps({"project": entry["_project"]}, separators=(",", ":"))
    elif probe.data:
        body["data"] = probe.data
    return body


def request_bodies(provider: str, entry: dict[str, Any], target: str) -> list[dict[str, Any]]:
    """The exact JSON CPA receives for one account's usage, in send order."""
    return [
        _body(probe, provider, entry, target)
        for probe in QUOTA_REQUESTS[provider]
        if not probe.tier
    ]


def tier_body(provider: str, entry: dict[str, Any], target: str) -> dict[str, Any] | None:
    """The exact JSON of the provider's tier request, or None when it has none."""
    for probe in QUOTA_REQUESTS.get(provider, ()):
        if probe.tier:
            return _body(probe, provider, entry, target)
    return None


# ── Parsing vendor replies ──────────────────────────────────────────────────


@dataclass
class Window:
    label: str
    used: float  # percent used, 0..100
    resets_at: datetime | None = None
    group: str = ""  # model group (Antigravity, plugin quota); CPA cools per model
    # A limit on some models only (Claude Fable, Opus, Sonnet): when full, the
    # account still serves the rest, so it never makes the account OUT.
    scoped: bool = False


@dataclass
class Usage:
    plan: str = ""
    windows: list[Window] = field(default_factory=list)
    reached: bool = False  # the vendor says the account is blocked right now
    # The vendor confirmed a running window but sent no figure for it (xAI
    # leaves the percent out until an account has used enough). Never a 0.
    pending: list[Window] = field(default_factory=list)
    extra: str = ""  # Claude extra usage and credit pools, Codex credits; only when present


def _num(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    return status._number(raw)


def _used(raw: Any) -> float | None:
    value = _num(raw)
    if value is None or value < 0:
        return None
    return min(value, 100.0)


def _first(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if data.get(key) is not None:
            return data[key]
    return None


def _instant(raw: Any) -> datetime | None:
    """ISO time, or unix seconds (milliseconds when that large)."""
    return status.instant(raw)


def _plan(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    value = re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")
    return value if _PLAN_RE.match(value) else ""


def _label(raw: Any, limit: int) -> str:
    if not isinstance(raw, str):
        return ""
    return status.mask_text(" ".join(raw.split()))[:limit]


# Window keys in the Management Center's order (CLAUDE_USAGE_WINDOW_KEYS);
# scoped = a limit on some models or apps only. ``iguana_necktie`` is Fable
# unless it carries dollar fields (then it is a credit pool: _claude_balances).
_CLAUDE_WINDOWS = (
    ("five_hour", "5h", False),
    ("seven_day", "wk", False),
    ("seven_day_oauth_apps", "apps", True),
    ("seven_day_opus", "opus", True),
    ("seven_day_sonnet", "sonnet", True),
    ("seven_day_cowork", "cowork", True),
    ("iguana_necktie", "fable", True),
)
_DOLLAR_KEYS = ("limit_dollars", "used_dollars", "remaining_dollars")


def _claude_scoped(payload: dict[str, Any]) -> list[Window]:
    """``limits[]`` entries of kind ``weekly_scoped``: one per model (e.g. Fable)."""
    limits = payload.get("limits")
    found: dict[str, tuple[bool, Window]] = {}
    for item in limits if isinstance(limits, list) else []:
        if not isinstance(item, dict) or item.get("kind") != "weekly_scoped":
            continue
        scope = item.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        name = model.get("display_name") if isinstance(model, dict) else None
        label = _label(name, 24).lower().removesuffix(" 5").strip()
        used = _used(item.get("percent"))
        if not label or used is None:
            continue
        active = item.get("is_active") is True
        if label in found and (found[label][0] or not active):
            continue  # like the MC: the active entry, else the first
        window = Window(label, used, status.parse_time(item.get("resets_at")), scoped=True)
        found[label] = (active, window)
    return [window for _, window in found.values()]


def _money(minor: float, currency: str) -> str:
    amount = f"{minor / 100:,.2f}"
    return f"${amount}" if currency == "USD" else f"{amount} {currency}"


def _claude_extra(payload: dict[str, Any]) -> str:
    extra = payload.get("extra_usage")
    if not isinstance(extra, dict) or extra.get("is_enabled") is not True:
        return ""
    if extra.get("spend_limit_reached") is True:
        text = "extra limit reached"
    else:
        used = _used(extra.get("utilization"))
        text = f"extra {int(used)}%" if used is not None else "extra on"
    spent, limit = _num(extra.get("used_credits")), _num(extra.get("monthly_limit"))
    raw_currency = extra.get("currency")
    currency = raw_currency.strip().upper() if isinstance(raw_currency, str) else ""
    currency = currency or "USD"
    if spent is not None and limit is not None and spent >= 0 and limit > 0:
        if re.fullmatch(r"[A-Z]{3}", currency):  # amounts are minor units, as in the MC
            text += f" ({_money(spent, currency)}/{_money(limit, currency)})"
    return text


def _claude_balances(payload: dict[str, Any]) -> list[str]:
    """Dollar credit pools (Team/API): any window carrying dollar fields."""
    out = []
    for key, window in payload.items():
        if key in ("five_hour", "seven_day", "extra_usage") or not isinstance(window, dict):
            continue
        if not all(k in window for k in (*_DOLLAR_KEYS, "utilization", "resets_at")):
            continue  # the MC's rule for a credit pool
        left = _num(window.get("remaining_dollars"))
        # Key names are vendor field names, not secrets; spaces keep the
        # token mask from eating long ones.
        label = key.replace("_", " ") if re.fullmatch(r"[a-z0-9_]{1,40}", key) else ""
        if label and left is not None and left >= 0:
            out.append(f"{label} ${left:,.2f} left")
    return out


def parse_claude(payload: Any) -> Usage:
    usage = Usage()
    if not isinstance(payload, dict):
        return usage
    scoped = _claude_scoped(payload)
    labels = {w.label for w in scoped}
    for key, label, is_scoped in _CLAUDE_WINDOWS:
        window = payload.get(key)
        if not isinstance(window, dict) or label in labels:
            continue
        if any(window.get(k) is not None for k in _DOLLAR_KEYS):
            continue  # a dollar balance, not a usage window
        used = _used(window.get("utilization"))
        if used is None:
            continue
        resets = status.parse_time(window.get("resets_at"))
        usage.windows.append(Window(label, used, resets, scoped=is_scoped))
        labels.add(label)
    usage.windows += scoped
    usage.extra = " · ".join(filter(None, [_claude_extra(payload), *_claude_balances(payload)]))
    return usage


# ── Subscription tier (the two tier requests) ───────────────────────────────
# Only the fields named here are read; the replies also carry a name, an
# email, an organization name and the project id. The result is one of the
# fixed words below or "", never a value from the reply.


def _word(raw: Any) -> str:
    """A reply field as trimmed text, like the Management Center reads it."""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw):
        return str(raw)
    return ""


def _flag(raw: Any) -> bool | None:
    """A yes/no reply field; None when it is missing or not a yes or a no."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return raw != 0
    if isinstance(raw, str):
        word = raw.strip().lower()
        if word in ("true", "1", "yes", "y", "on"):
            return True
        if word in ("false", "0", "no", "n", "off"):
            return False
    return None


# Only the organization's rate-limit tier tells Max 5x from Max 20x.
_CLAUDE_MAX = (("claude_max_20x", "max20"), ("claude_max_5x", "max5"))


def parse_claude_tier(payload: Any) -> str:
    """``team``, ``max20``, ``max5``, ``max``, ``pro``, ``free`` or ``""``."""
    if not isinstance(payload, dict):
        return ""
    org, acct = payload.get("organization"), payload.get("account")
    org = org if isinstance(org, dict) else {}
    acct = acct if isinstance(acct, dict) else {}
    kind = _word(org.get("organization_type")).lower()
    if kind == "claude_team" and _word(org.get("subscription_status")).lower() == "active":
        return "team"
    rate = _word(org.get("rate_limit_tier")).lower()
    for ending, word in _CLAUDE_MAX:
        if rate.endswith(ending):
            return word
    has_max, has_pro = _flag(acct.get("has_claude_max")), _flag(acct.get("has_claude_pro"))
    if has_max:
        return "max"
    if has_pro:
        return "pro"
    if has_max is False and has_pro is False:
        return "free"
    return ""


_ANTIGRAVITY_TIERS = {
    "free-tier": "free",
    "g1-pro-tier": "pro",
    "g1-ultra-tier": "ultra",
    "g1-ultra-lite-tier": "ultra-lite",
}


def parse_antigravity_tier(payload: Any) -> str:
    """``free``, ``pro``, ``ultra``, ``ultra-lite`` or ``""``: the paid tier, else the current."""
    if not isinstance(payload, dict):
        return ""

    def tier_id(key: str) -> str:
        tier = payload.get(key)
        return _word(tier.get("id")) if isinstance(tier, dict) else ""

    return _ANTIGRAVITY_TIERS.get(tier_id("paidTier") or tier_id("currentTier"), "")


_TIER_PARSERS: dict[str, Callable[[Any], str]] = {
    "claude": parse_claude_tier,
    "antigravity": parse_antigravity_tier,
}


_FIVE_HOURS = 18000
_WEEK = 604800
_MONTH = (28 * 86400, 31 * 86400)


def _codex_label(seconds: float | None, position: str) -> str | None:
    """Label from the window's own length; position only when length is missing."""
    if seconds is None:
        return position
    if seconds <= 0:
        return None  # e.g. a Pro plan's empty secondary window
    if seconds == _FIVE_HOURS:
        return "5h"
    if seconds == _WEEK:
        return "wk"
    if _MONTH[0] <= seconds <= _MONTH[1]:
        return "mo"
    return status._minutes_label(int(seconds // 60))


def parse_codex(payload: Any, now: datetime) -> Usage:
    usage = Usage()
    if not isinstance(payload, dict):
        return usage
    plan = payload.get("plan_type")
    if isinstance(plan, str):
        # Codex also sends hyphenated plans ("pro-lite"); one spelling is shown.
        value = plan.strip().lower().replace("-", "_")
        if _PLAN_RE.match(value):
            usage.plan = value
    usage.extra = _codex_credits(payload)
    limit = payload.get("rate_limit")
    if limit is None:
        limit = payload.get("rateLimit")
    if not isinstance(limit, dict):
        return usage
    reached_flag = limit.get("limit_reached", limit.get("limitReached"))
    usage.reached = reached_flag is True or limit.get("allowed") is False
    taken: set[str] = set()
    for keys, position in (
        (("primary_window", "primaryWindow"), "5h"),
        (("secondary_window", "secondaryWindow"), "wk"),
    ):
        window = next((limit[k] for k in keys if isinstance(limit.get(k), dict)), None)
        if window is None:
            continue
        seconds = _num(window.get("limit_window_seconds", window.get("limitWindowSeconds")))
        label = _codex_label(seconds, position)
        if label is None or label in taken:
            continue
        resets = status._unix(window.get("reset_at", window.get("resetAt")))
        if resets is None:
            after = _num(window.get("reset_after_seconds", window.get("resetAfterSeconds")))
            if after is not None and after >= 0:
                resets = now + timedelta(seconds=after)
        used = _used(window.get("used_percent", window.get("usedPercent")))
        if used is None:
            if not (usage.reached and resets is not None):
                continue
            used = 100.0  # as the Management Center does
        taken.add(label)
        usage.windows.append(Window(label, used, resets))
    return usage


def _codex_credits(payload: dict[str, Any]) -> str:
    """Pay-as-you-go credits next to the plan windows, as the MC shows them."""
    credits = payload.get("credits")
    if not isinstance(credits, dict):
        return ""
    if credits.get("unlimited") is True:
        return "credits unlimited"
    raw = credits.get("balance")
    text = (
        str(raw).strip() if isinstance(raw, (int, float, str)) and not isinstance(raw, bool) else ""
    )
    if not re.fullmatch(r"\d{1,12}(\.\d{1,6})?", text):
        return ""
    value = float(text)
    return f"credits {value:,.2f}".removesuffix(".00") if value else ""


def _xai_config(payload: Any) -> dict[str, Any] | None:
    config = payload.get("config") if isinstance(payload, dict) else None
    return config if isinstance(config, dict) else None


def _cents(raw: Any) -> float | None:
    return _num(raw.get("val") if isinstance(raw, dict) else raw)


def parse_xai(weekly: Any, monthly: Any) -> Usage:
    """xAI weekly credits (``creditUsagePercent`` is used %) and monthly spend."""
    usage = Usage()
    config = _xai_config(weekly)
    if config is not None:
        period = _first(config, "currentPeriod", "current_period")
        period = period if isinstance(period, dict) else {}
        raw_kind = period.get("type")
        kind = raw_kind if isinstance(raw_kind, str) else ""
        used = _used(_first(config, "creditUsagePercent", "credit_usage_percent"))
        end = period.get("end") or _first(config, "billingPeriodEnd", "billing_period_end")
        label = "mo" if "monthly" in kind.lower() else "wk"
        period_end = _instant(period.get("end"))
        if used is not None:
            usage.windows.append(Window(label, used, _instant(end)))
        elif kind and period_end is not None:
            usage.pending.append(Window(label, 0.0, period_end))
    configs = [c for c in (_xai_config(weekly), _xai_config(monthly)) if c is not None]
    limits = [_cents(_first(c, "monthlyLimit", "monthly_limit")) for c in configs]
    limit = next((v for v in limits if v is not None), None)
    spent = next((v for c in configs if (v := _cents(c.get("used"))) is not None), None)
    end = next(
        (v for c in configs if (v := _first(c, "billingPeriodEnd", "billing_period_end"))),
        None,
    )
    if limit and limit > 0 and spent is not None and spent >= 0:
        if "mo" not in {w.label for w in usage.windows}:
            usage.windows.append(Window("mo", min(spent, limit) / limit * 100, _instant(end)))
    return usage


_WINDOW_LABELS = {"5h": "5h", "five-hour": "5h", "five_hour": "5h", "weekly": "wk", "week": "wk"}
_WINDOW_ORDER = {"5h": 0, "wk": 1}


def _fraction(raw: Any) -> float | None:
    """Remaining fraction: a number, or a string such as ``"12.5%"``."""
    if isinstance(raw, str) and raw.strip().endswith("%"):
        value = _num(raw.strip()[:-1])
        return None if value is None else value / 100
    return _num(raw)


def parse_groups(payload: Any, per_model: bool = False) -> list[Window]:
    """``groups[].buckets[]`` (Antigravity, CPA plugin quota). Fraction = remaining.

    ``per_model`` tags each window with its group: CPA cools Antigravity per
    model, so one full group does not stop the account (CPA v8.0.46).
    """
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list):
        return []
    windows: list[Window] = []
    for index, group in enumerate(groups):
        if not isinstance(group, dict) or not isinstance(group.get("buckets"), list):
            continue
        name = _label(_first(group, "displayName", "display_name"), 24) or f"g{index + 1}"
        found: list[tuple[int, str, Window]] = []
        for bucket in group["buckets"]:
            if not isinstance(bucket, dict):
                continue
            fraction = _fraction(_first(bucket, "remainingFraction", "remaining_fraction"))
            if fraction is None:
                continue
            fraction = min(max(fraction, 0.0), 1.0)
            raw_window = bucket.get("window")
            short = ""
            if isinstance(raw_window, str):
                short = _WINDOW_LABELS.get(raw_window.strip().lower(), "")
            label = short or _label(_first(bucket, "displayName", "display_name") or raw_window, 16)
            label = label or _label(bucket.get("description"), 16) or "?"
            resets = _instant(_first(bucket, "resetTime", "reset_time"))
            window = Window(f"{name} {label}", 100 - fraction * 100, resets)
            if per_model:
                window.group = f"{index}:{name}"
            found.append((_WINDOW_ORDER.get(label, 2), label, window))
        windows += [w for _, _, w in sorted(found, key=lambda item: (item[0], item[1]))]
    return windows


def parse_plugin(payload: Any) -> Usage:
    usage = Usage(windows=parse_groups(payload))
    subscription = payload.get("subscription") if isinstance(payload, dict) else None
    if isinstance(subscription, dict):
        usage.plan = _plan(
            _first(subscription, "tierName", "tier_name")
            or subscription.get("plan")
            or _first(subscription, "tierId", "tier_id")
        )
    return usage


def _remaining_percent(raw: Any) -> float | None:
    if isinstance(raw, str) and not re.fullmatch(r"\d+(\.\d+)?", raw.strip()):
        return None
    value = _num(raw)
    return value if value is not None and 0 <= value <= 100 else None


def _unix_seconds(raw: Any) -> datetime | None:
    if isinstance(raw, bool) or (isinstance(raw, str) and not raw.strip().isdigit()):
        return None
    return status._unix(raw)


def parse_devin(payload: Any) -> Usage:
    """Devin ``userStatus.planStatus``: remaining percent per day and week."""
    usage = Usage()
    user = payload.get("userStatus") if isinstance(payload, dict) else None
    plan_status = user.get("planStatus") if isinstance(user, dict) else None
    if not isinstance(plan_status, dict):
        return usage
    info = plan_status.get("planInfo")
    if isinstance(info, dict):
        usage.plan = _plan(info.get("planName"))
    for key, label in (("daily", "day"), ("weekly", "wk")):
        left = _remaining_percent(plan_status.get(f"{key}QuotaRemainingPercent"))
        if left is None:
            continue
        resets = _unix_seconds(plan_status.get(f"{key}QuotaResetAtUnix"))
        usage.windows.append(Window(label, 100 - left, resets))
    return usage


_KIMI_UNIT_MINUTES = {
    "SECOND": 1 / 60,
    "SECONDS": 1 / 60,
    "MINUTE": 1,
    "MINUTES": 1,
    "HOUR": 60,
    "HOURS": 60,
    "DAY": 1440,
    "DAYS": 1440,
    "WEEK": 10080,
    "WEEKS": 10080,
}


def _int(raw: Any) -> int | None:
    value = _num(raw)
    return None if value is None else math.floor(value)


def _kimi_window(duration: int | None, unit: Any) -> str:
    """Window label from ``duration`` + ``timeUnit``; no or unknown unit = minutes (MC)."""
    if duration is None or duration <= 0:
        return ""
    key = unit.strip().upper().removeprefix("TIME_UNIT_") if isinstance(unit, str) else ""
    minutes = duration * _KIMI_UNIT_MINUTES.get(key, 1)
    if minutes != int(minutes):
        return f"{duration}s"
    return status._minutes_label(int(minutes))


def _kimi_reset(data: dict[str, Any], now: datetime) -> datetime | None:
    for key in ("reset_at", "resetAt", "reset_time", "resetTime"):
        moment = _instant(data.get(key))
        if moment is not None:
            return moment
    for key in ("reset_in", "resetIn", "ttl"):
        seconds = _num(data.get(key))
        if seconds is not None and seconds > 0:
            return now + timedelta(seconds=seconds)
    return None


def _kimi_row(data: dict[str, Any], label: str, now: datetime) -> Window | None:
    limit, used = _int(data.get("limit")), _int(data.get("used"))
    if used is None:
        remaining = _int(data.get("remaining"))
        if remaining is not None and limit is not None:
            used = limit - remaining
    if used is None or limit is None or limit <= 0:
        return None
    label = _label(data.get("name") or data.get("title"), 16) or label
    return Window(label, min(max(used / limit * 100, 0.0), 100.0), _kimi_reset(data, now))


def parse_kimi(payload: Any, now: datetime) -> Usage:
    """Kimi ``limits[]`` rows, the ``usage`` summary (wk) and the monthly ratio."""
    usage = Usage()
    if not isinstance(payload, dict):
        return usage
    limits = payload.get("limits")
    for index, item in enumerate(limits if isinstance(limits, list) else []):
        if not isinstance(item, dict):
            continue
        detail = item["detail"] if isinstance(item.get("detail"), dict) else item
        window = item["window"] if isinstance(item.get("window"), dict) else {}
        label = ""
        for key in ("name", "title", "scope"):
            label = _label(item.get(key) or detail.get(key), 16)
            if label:
                break
        if not label:
            durations = (
                _int(window.get("duration")),
                _int(item.get("duration")),
                _int(detail.get("duration")),
            )
            duration = next((d for d in durations if d is not None), None)
            unit = window.get("timeUnit") or item.get("timeUnit") or detail.get("timeUnit")
            label = _kimi_window(duration, unit) or f"#{index + 1}"
        row = _kimi_row(detail, label, now)
        if row is not None:
            usage.windows.append(row)
    summary = payload.get("usage")
    if isinstance(summary, dict):
        row = _kimi_row(summary, "wk", now)
        if row is not None:
            usage.windows.append(row)
    usages = payload.get("usages")
    monthly = usages.get("limit_month_total") if isinstance(usages, dict) else None
    if isinstance(monthly, dict):
        ratio = _num(monthly.get("used_ratio"))  # 0..1
        if ratio is not None:
            data = {
                "used": math.floor(ratio * 100 + 0.5),
                "limit": 100,
                "reset_time": monthly.get("reset_time"),
            }
            row = _kimi_row(data, "mo", now)
            if row is not None:
                usage.windows.append(row)
    return usage


def parse_usage(provider: str, payload: Any, now: datetime) -> Usage:
    """Vendors answered by one reply."""
    if provider == "codex":
        return parse_codex(payload, now)
    if provider == "devin":
        return parse_devin(payload)
    if provider == "kimi":
        return parse_kimi(payload, now)
    return parse_claude(payload)


# ── CPA requests ────────────────────────────────────────────────────────────


@dataclass
class Inner:
    status: int
    body: str


def _request_json(url: str, key: str, body: dict[str, Any] | None, timeout: float) -> Any:
    """One management request: POST ``body``, or GET when it is None."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
    request.add_header("Authorization", f"Bearer {key}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", status.user_agent())
    try:
        with cpa._OPENER.open(request, timeout=timeout) as response:
            raw = response.read(MAX_OUTER_BYTES + 1)
    except cpa.GatewayError:
        raise
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise cpa.GatewayError(code, cpa._category(code)) from None
    except TimeoutError:
        raise cpa.GatewayError(0, "timeout") from None
    except (urllib.error.URLError, OSError):
        raise cpa.GatewayError(0, "network") from None
    if len(raw) > MAX_OUTER_BYTES:
        raise cpa.GatewayError(200, "response_too_large")
    try:
        return json.loads(raw)
    except ValueError:
        raise cpa.GatewayError(200, "bad_response") from None


def api_call(
    root: str, key: str, path: str, body: dict[str, Any], timeout: float = CALL_TIMEOUT
) -> Inner:
    """One CPA api-call. Raises GatewayError for CPA's own (outer) failures."""
    reply = _request_json(root + path, key, body, timeout)
    if not isinstance(reply, dict):
        raise cpa.GatewayError(200, "bad_response")
    code = reply.get("status_code")
    text = reply.get("body")
    if not isinstance(code, int) or isinstance(code, bool):
        raise cpa.GatewayError(200, "bad_response")
    return Inner(code, text if isinstance(text, str) else "")


def plugin_quota(
    root: str, key: str, plugin: str, target: str, timeout: float = CALL_TIMEOUT
) -> Any:
    """CPA's plugin quota GET: the reply is the plugin's own quota JSON."""
    url = (
        f"{root}/v8/management/plugins/{quote(plugin, safe='')}/quota"
        f"?auth_index={quote(target, safe='')}"
    )
    return _request_json(url, key, None, timeout)


# ── Run ─────────────────────────────────────────────────────────────────────


@dataclass
class AccountResult:
    name: str
    provider: str
    usage: Usage | None = None
    error: str = ""
    # When a Codex subscription ends, from CPA's listing; None when not listed.
    until: datetime | None = None

    def out_until(self) -> tuple[bool, datetime | None]:
        """Whether the account is out of quota now, and when it frees.

        With model groups, CPA cools only the models that ran out, so the
        account is out only when every group has a full window; it frees when
        the first of those groups does.
        """
        if self.usage is None:
            return False, None
        groups = self.full_groups()
        if groups is not None and not self.usage.reached:
            if not groups or len(groups) < len({w.group for w in self.usage.windows}):
                return False, None
            frees = [until for until in groups.values() if until]
            return True, min(frees) if frees else None
        full = [w for w in self.usage.windows if w.used >= 100 and not w.scoped]
        if not full and not self.usage.reached:
            return False, None
        resets = [w.resets_at for w in (full or self.usage.windows) if w.resets_at]
        return True, max(resets) if resets else None

    def scoped_full(self) -> list[str]:
        """Model-scoped limits that are used up (the account still serves the rest)."""
        if self.usage is None:
            return []
        return [w.label for w in self.usage.windows if w.scoped and w.used >= 100]

    def full_groups(self) -> dict[str, datetime | None] | None:
        """Model groups with a full window and when each frees; None if ungrouped."""
        if self.usage is None or not any(w.group for w in self.usage.windows):
            return None
        groups: dict[str, datetime | None] = {}
        for w in self.usage.windows:
            if w.used >= 100:
                resets = [
                    x.resets_at
                    for x in self.usage.windows
                    if x.group == w.group and x.used >= 100 and x.resets_at
                ]
                groups[w.group] = max(resets) if resets else None
        return groups


@dataclass
class QuotaRun:
    status: str  # ok, or a /cpa row status (rejected, blocked, busy, unavailable, ...)
    accounts: list[AccountResult] = field(default_factory=list)
    skipped: int = 0
    detail: str = ""
    at: float = 0.0
    when: datetime | None = None
    timed_out: bool = False  # stopped at RUN_SECONDS; never cached


_CACHE: dict[tuple[str, str, str], QuotaRun] = {}
_CACHE_LOCK = threading.Lock()


def reset_state() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def _outer_error(exc: cpa.GatewayError) -> str:
    if exc.status == 400:
        return "CPA could not use this account"
    if exc.status == 502:
        return "CPA could not reach vendor"
    if exc.status == 0:
        return exc.category  # timeout, network
    return f"CPA HTTP {exc.status}"


class _StopError(Exception):
    """The run is over: a fatal outer status, or the key latched meanwhile."""


class _AccountError(Exception):
    """This account failed; the message (status and category only) is shown."""


def _payload(inner: Inner) -> Any:
    """The vendor reply as JSON, else _AccountError (an inner failure never latches)."""
    if inner.status == 401:
        raise _AccountError("token rejected by vendor")
    if inner.status == 429:
        raise _AccountError("vendor rate limited")
    if not 200 <= inner.status < 300:
        raise _AccountError(f"vendor HTTP {inner.status}")
    if len(inner.body.encode("utf-8")) > MAX_INNER_BYTES:
        raise _AccountError("vendor reply too large")
    try:
        return json.loads(inner.body)
    except ValueError:
        raise _AccountError("bad vendor reply") from None


def route_of(entry: dict[str, Any]) -> str:
    """``api`` for built-in vendors (even with a plugin id), ``plugin``, or ``""``."""
    if status.provider_of(entry) in BUILTINS:
        return "api"
    return "plugin" if entry.get("_plugin") else ""


def _selected(entry: dict[str, Any], selector: str) -> bool:
    route = route_of(entry)
    if not route:
        return False
    if not selector:
        return True
    if selector == "plugin":
        return route == "plugin"
    return status.provider_of(entry) == selector


class _Runner:
    """Sends one run's requests; every request passes ``_gate`` first."""

    def __init__(
        self, home: Path, root: str, key: str, rejected: Callable[[], bool], deadline: float
    ):
        self.home, self.root, self.key, self.rejected = home, root, key, rejected
        self.deadline = deadline  # time.monotonic() value
        self.timed_out = False
        self.path = V8_API_CALL
        self.abort = threading.Event()
        self.fatal: list[str] = []
        self.proven = False  # a reply showed CPA accepted the key
        self.lock = threading.Lock()

    def _stop(self, why: str) -> NoReturn:
        with self.lock:
            self.fatal.append(why)
        self.abort.set()
        raise _StopError

    def _gate(self) -> None:
        if self.abort.is_set():
            raise _StopError
        if time.monotonic() >= self.deadline:  # not fatal: what was read is still shown
            self.timed_out = True
            self.abort.set()
            raise _StopError
        if self.rejected():  # e.g. another process latched it meanwhile
            self._stop("rejected")

    def _timeout(self) -> float:
        """This request's timeout, clipped to what is left of the run."""
        return max(0.05, min(CALL_TIMEOUT, self.deadline - time.monotonic()))

    def _outer(self, exc: cpa.GatewayError) -> None:
        if exc.status == 401:
            cpa.mark_rejected(self.home, self.root, self.key)  # inside the guard
            self._stop("rejected")
        if exc.status == 403:
            self._stop("blocked")
        if exc.status not in (0, 404):  # CPA answered past its key check
            self.proven = True

    def api(self, body: dict[str, Any]) -> Inner:
        self._gate()
        path = self.path  # this request's own path: workers share self.path
        try:
            try:
                inner = api_call(self.root, self.key, path, body, timeout=self._timeout())
            except cpa.GatewayError as exc:
                if exc.status != 404 or path == V0_API_CALL:
                    raise
                path = self.path = V0_API_CALL  # only after a v8 404, never after a 401
                self._gate()
                inner = api_call(self.root, self.key, path, body, timeout=self._timeout())
        except cpa.GatewayError as exc:
            self._outer(exc)
            if exc.status == 404:
                self._stop("no_route")
            raise _AccountError(_outer_error(exc)) from None
        self.proven = True
        return inner

    def plugin(self, plugin: str, target: str) -> Any:
        self._gate()
        try:
            reply = plugin_quota(self.root, self.key, plugin, target, timeout=self._timeout())
        except cpa.GatewayError as exc:
            self._outer(exc)
            if exc.status == 404:  # auth or plugin quota provider not found
                raise _AccountError("plugin quota not found") from None
            if exc.status == 502:
                raise _AccountError("plugin could not fetch") from None
            raise _AccountError(_outer_error(exc)) from None
        self.proven = True
        return reply

    def _usage(self, entry: dict[str, Any], provider: str, target: str, now: datetime) -> Usage:
        if route_of(entry) == "plugin":
            return parse_plugin(self.plugin(entry["_plugin"], target))
        if provider == "antigravity" and not entry.get("_project"):
            raise _AccountError("no project id")  # sends nothing
        bodies = request_bodies(provider, entry, target)
        if provider == "antigravity":
            failure = _AccountError("no quota data")
            for body in bodies:  # the next URL only after an inner failure
                inner = self.api(body)  # an outer failure ends the account
                try:
                    windows = parse_groups(_payload(inner), per_model=True)
                except _AccountError as exc:
                    failure = exc
                    continue
                if windows:
                    return Usage(windows=windows)
                failure = _AccountError("no quota data")
            raise failure
        if provider == "xai":
            payloads: list[Any] = []
            first: _AccountError | None = None
            for body in bodies:  # weekly, then monthly
                inner = self.api(body)  # an outer failure ends the account
                try:
                    payloads.append(_payload(inner))
                except _AccountError as exc:
                    first = first or exc
                    payloads.append(None)
            usage = parse_xai(*payloads)
            if not usage.windows and not usage.pending and first is not None:
                raise first
            return usage
        return parse_usage(provider, _payload(self.api(bodies[0])), now)

    def _tier(self, entry: dict[str, Any], provider: str, target: str) -> str:
        """The plan word from the provider's tier request, or ``""``.

        Sent once, and only after the account's usage was read, so it is never
        the run's first request. Any failure leaves the plan out: the account
        keeps its usage row. An outer 401 still latches the key and stops the
        run, like every other request.
        """
        body = tier_body(provider, entry, target)
        if body is None:
            return ""
        try:
            payload = _payload(self.api(body))
        except (_AccountError, _StopError):
            return ""
        return _TIER_PARSERS[provider](payload)

    def account(self, entry: dict[str, Any], now: datetime) -> AccountResult:
        name, provider = status.display_name(entry), status.provider_of(entry)
        target = entry.get("_target")
        if not isinstance(target, str) or not target:
            return AccountResult(name, provider, error="CPA lists no account index")
        if self.abort.is_set():
            return AccountResult(name, provider, error="not checked")
        try:
            usage = self._usage(entry, provider, target, now)
        except _StopError:
            return AccountResult(name, provider, error="not checked")
        except _AccountError as exc:
            return AccountResult(name, provider, error=str(exc))
        if not usage.windows and not usage.pending:
            return AccountResult(name, provider, error="no quota data")
        usage.plan = self._tier(entry, provider, target) or usage.plan
        return AccountResult(name, provider, usage=usage, until=entry.get("_until"))


def run(
    home: Path,
    specs: list[cpa.InstanceSpec],
    root: str,
    key: str,
    selector: str,
    now: datetime,
) -> tuple[QuotaRun, bool]:
    """One quota run for a CPA root and key; and whether it came from the cache."""
    job = (root, status._memory_id(key), selector)
    with _CACHE_LOCK:
        cached = _CACHE.get(job)
    if cached and 0 <= time.monotonic() - cached.at < CACHE_SECONDS:
        return cached, True  # never takes the guard
    with cpa.mgmt_guard(home, root, status.GUARD_WAIT) as held:
        if not held:
            return QuotaRun("busy"), False
        result = _run_locked(home, specs, root, key, selector, now)
    result.at, result.when = time.monotonic(), now
    if result.status == "ok" and not result.timed_out:
        with _CACHE_LOCK:
            _CACHE[job] = result
    return result, False


def _run_locked(
    home: Path,
    specs: list[cpa.InstanceSpec],
    root: str,
    key: str,
    selector: str,
    now: datetime,
) -> QuotaRun:
    def rejected() -> bool:
        return status.known_rejected(home, specs, root, key)

    if rejected():  # before the listing, even a cached one
        return QuotaRun("rejected")
    listing, _ = status._fetch_locked(home, specs, root, key)
    if listing.status != "ok":
        return QuotaRun(listing.status, detail=listing.detail)
    eligible = [
        entry
        for entry in listing.files
        if _selected(entry, selector) and status.classify(entry, now).kind != "disabled"
    ]
    checked, skipped = eligible[:MAX_ACCOUNTS], max(0, len(eligible) - MAX_ACCOUNTS)
    runner = _Runner(home, root, key, rejected, time.monotonic() + RUN_SECONDS)
    queue = list(checked)
    results: list[AccountResult] = []
    # The probe: one account and one request at a time until a reply proves the
    # key. Accounts that send nothing do not count. A bad key costs CPA one try.
    # A timeout proves nothing, so the run's deadline is what ends a probe that
    # never gets an answer.
    while queue and not runner.proven and not runner.abort.is_set():
        results.append(runner.account(queue.pop(0), now))
    if queue and not runner.abort.is_set():
        with ThreadPoolExecutor(max_workers=MAX_IN_FLIGHT) as pool:
            results += list(pool.map(lambda entry: runner.account(entry, now), queue))
    else:
        results += [runner.account(entry, now) for entry in queue]
    if runner.fatal:
        if runner.fatal[0] == "no_route":
            return QuotaRun("unavailable", detail="this CPA has no api-call route")
        return QuotaRun(runner.fatal[0])
    return QuotaRun("ok", accounts=results, skipped=skipped, timed_out=runner.timed_out)


# ── View ────────────────────────────────────────────────────────────────────

USAGE = (
    "/cpa quota <pool> - live usage of every account CPA's web UI shows quota for\n"
    "/cpa quota <pool> <provider> - claude, codex, xai, antigravity, devin, kimi,\n"
    "  or plugin (or a plugin's provider, e.g. kiro) for CPA plugin quota\n"
    "Asks each vendor through CPA, like CPA's web UI quota page: at most 20\n"
    "accounts and 4 requests per account, cached 60 s. Percent = used."
)


RESET_FROM = 80.0  # percent used from which a window shows its reset time
# The account and plan columns are as wide as the longest value in a provider
# block, up to these widths; a longer value is shown whole and breaks the column.
_NAME_PAD = 40
_PLAN_PAD = 28
# Chat platforms whose Hermes adapter renders a Markdown code block. The view
# is a table, so it is sent as one there. Anywhere else it stays plain text:
# the CLI and the TUI print a command's reply as it is.
_FENCE_PLATFORMS = frozenset({"telegram", "discord", "slack", "matrix", "mattermost"})


def _when(moment: datetime, now: datetime) -> str:
    local = moment.astimezone()
    if (moment - now).total_seconds() > 20 * 3600:
        return local.strftime("%b %d")
    return local.strftime("%H:%M")


def _window_text(window: Window, now: datetime, label: str = "") -> str:
    """``wk 85% → Nov 02``. The reset time is shown only on a nearly full window."""
    text = f"{label or window.label} {int(window.used)}%"
    if window.used >= RESET_FROM and window.resets_at is not None and window.resets_at > now:
        text += f" → {_when(window.resets_at, now)}"
    return text


def _pending_text(window: Window, now: datetime) -> str:
    text = f"{window.label} no figure yet"
    if window.resets_at is not None and window.resets_at > now:
        text += f" → {_when(window.resets_at, now)}"
    return text


def _until_text(until: datetime, now: datetime) -> str:
    """``until Nov 16``: the day a subscription ends, as CPA's listing has it.

    The date is a claim in the ID token CPA stores, fixed when that token was
    issued, so it can be older than the vendor's (the web UI prefers a live
    lookup this plugin does not send). One that has passed is marked, not hidden.
    """
    local, today = until.astimezone(), now.astimezone()
    text = "until " + local.strftime("%b %d")
    if local.year != today.year:
        text += f" {local.year}"
    return text + (" (past)" if until <= now else "")


def _sort_key(item: AccountResult) -> tuple[int, float, str]:
    out, _ = item.out_until()
    if out:
        rank = 0
    elif item.error:
        rank = 1
    else:
        rank = 2
    top = (
        max((w.used for w in item.usage.windows if not w.scoped), default=0.0)
        if item.usage
        else 0.0
    )
    return rank, -top, item.name


def _provider_block(provider: str, items: list[AccountResult], now: datetime) -> list[str]:
    room = out = failed = not_checked = 0
    next_free: datetime | None = None
    for item in items:
        is_out, until = item.out_until()
        if item.error == "not checked":
            not_checked += 1
        elif item.error:
            failed += 1
        elif is_out:
            out += 1
            if until and until > now and (next_free is None or until < next_free):
                next_free = until
        else:
            room += 1
    noun = "account" if len(items) == 1 else "accounts"
    parts = [f"{room} have room"]
    if out:
        parts.append(f"{out} out" + (f" (next free {_when(next_free, now)})" if next_free else ""))
    if failed:
        parts.append(f"{failed} failed")
    if not_checked:
        parts.append(f"{not_checked} not checked")
    lines = [f"{provider} · {len(items)} {noun}: " + " · ".join(parts)]
    rows = sorted(items, key=_sort_key)
    any_out = out > 0
    name_width = min(max((len(item.name) for item in rows), default=0), _NAME_PAD)
    plans = {
        id(item): _plan_words(item, now)
        for item in rows
        if not item.error and item.usage is not None
    }
    plan_width = min(max((len(text) for text in plans.values()), default=0), _PLAN_PAD)
    for item in rows:
        is_out, _ = item.out_until()
        # OUT leads the line; the other accounts of the block are indented to match.
        lead = "OUT " if is_out else "    " if any_out else ""
        if item.error or item.usage is None:
            lines.append(f"{lead}{item.name.ljust(name_width)}  {item.error}".rstrip())
            continue
        plan = plans[id(item)]
        groups = item.full_groups()
        suffix = ""
        if not is_out and groups:
            suffix = "  out: " + ", ".join(sorted(_short_group(key) for key in groups))
        elif not is_out and item.scoped_full():
            suffix = "  out: " + ", ".join(item.scoped_full())
        grouped = [w for w in item.usage.windows if w.group]
        rest = (
            [_window_text(w, now) for w in item.usage.windows if not w.group]
            + [_pending_text(w, now) for w in item.usage.pending]
            + ([item.usage.extra] if item.usage.extra else [])
        )
        if grouped:
            # One line per model group under the account: CPA cools per group.
            head = item.name + (f" {plan}" if plan else "")
            lines.append(lead + "  ".join(x for x in (head, " · ".join(rest)) if x) + suffix)
            keys = list(dict.fromkeys(w.group for w in grouped))
            width = max(len(_short_group(key)) for key in keys)
            for key in keys:
                name = key.split(":", 1)[-1]
                texts = [
                    _window_text(w, now, w.label.removeprefix(name).strip())
                    for w in grouped
                    if w.group == key
                ]
                label = _short_group(key).ljust(width)
                lines.append(f"{' ' * len(lead)}  {label}  {' · '.join(texts)}")
            continue
        head = item.name.ljust(name_width)
        if plan_width:
            head += " " + plan.ljust(plan_width)
        lines.append((lead + "  ".join(x for x in (head, " · ".join(rest)) if x) + suffix).rstrip())
    return lines


def _plan_words(item: AccountResult, now: datetime) -> str:
    """``pro until Nov 16``: the plan and, for Codex, the day the subscription ends."""
    words = [item.usage.plan] if item.usage is not None and item.usage.plan else []
    if isinstance(item.until, datetime):
        words.append(_until_text(item.until, now))
    return " ".join(words)


def _short_group(key: str) -> str:
    """``0:Gemini Models`` -> ``gemini``: a model group's label in the view."""
    name = key.split(":", 1)[-1]
    return name.lower().removesuffix(" models").strip() or name


def _fenced(text: str, platform: str | None) -> str:
    """The view as a Markdown code block where the chat renders one, so columns line up."""
    if not isinstance(platform, str) or platform.strip().lower() not in _FENCE_PLATFORMS:
        return text
    return "```\n" + text.replace("`", "'") + "\n```"


def render(
    pool: str,
    result: QuotaRun,
    cached: bool,
    selector: str,
    now: datetime,
    platform: str | None = None,
) -> str:
    when = result.when or now
    stamp = f"cached {int(time.monotonic() - result.at)}s" if cached else "live"
    lines = [f"{pool} quota · {stamp} {when.astimezone().strftime('%H:%M')}"]
    if result.timed_out:  # second line, so the size cap can never hide it
        lines.append(f"stopped after {RUN_SECONDS:g} s (slow replies); the rest is not checked")
    head = len(lines)
    seen = {a.provider for a in result.accounts}
    order = [p for p in BUILTINS if p in seen] + sorted(seen - set(BUILTINS))
    for provider in order:
        items = [a for a in result.accounts if a.provider == provider]
        lines.append("")  # a blank line in front of every provider block
        lines.extend(_provider_block(provider, items, now))
    if len(lines) == head:
        lines.append(f"no {selector + ' ' if selector else ''}accounts with live quota")
    if result.skipped:
        if len(lines) > head + 1:
            lines.append("")
        lines.append(f"+{result.skipped} more not checked (at most {MAX_ACCOUNTS} per run)")
    return _fenced(status._cap(lines), platform)


def report(
    args: list[str],
    *,
    config: Any,
    home: Path,
    secret: Callable[[str], str | None],
    now: datetime | None = None,
    platform: str | None = None,
) -> str:
    if not args or len(args) > 2 or args[0] in ("help", "-h", "--help"):
        return USAGE
    now = now or datetime.now(timezone.utc)
    specs, _errors = cpa.parse_config(config)
    spec = next((s for s in specs if s.name == args[0].lower()), None)
    if spec is None:
        names = ", ".join(s.name for s in specs) or "none"
        word = cpa.normalize_provider(args[0])
        if len(args) == 1 and (word in BUILTINS or word in ("plugin", "meta")):
            return f"'{word}' is a provider. Try /cpa quota <pool> {word} (pools: {names})."
        return f"Unknown pool '{status.mask_text(args[0])[:32]}'. Pools: {names}"
    selector = ""
    if len(args) == 2:
        selector = cpa.normalize_provider(args[1])
        if selector == "meta":
            return "meta: no live quota (CPA's web UI needs the raw credential for it)."
        if not _SELECTOR_RE.match(selector) or status.mask_text(selector) != selector:
            return f"Live quota covers {COVERED}; not '{status.mask_text(args[1])[:32]}'."
    if not spec.mgmt_key_env:
        hint = status.same_cpa_hint(
            specs, spec.name, " ".join(["/cpa quota {}", *status._shown(args[1:2])])
        )
        return hint or f"{spec.name}: {status._STATUS_TEXT['no_key']}"
    try:
        key = secret(spec.mgmt_key_env) or ""
    except Exception:  # noqa: BLE001 - e.g. unscoped read under multiplexing
        key = ""
    if not key:
        return f"{spec.name}: {status._STATUS_TEXT['key_not_set']}"
    result, cached = run(home, specs, spec.root, key, selector, now)
    if result.status != "ok":
        text = status._STATUS_TEXT.get(result.status) or "unavailable" + (
            f" ({result.detail})" if result.detail else ""
        )
        return f"{spec.name}: {text}"
    return render(spec.name, result, cached, selector, now, platform)

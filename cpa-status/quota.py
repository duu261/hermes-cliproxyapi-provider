"""``/cpa quota``: live usage per Claude and Codex account, read from the vendor.

Does what CLIProxyAPI's own Management Center does on its quota page: for each
account, CPA's ``api-call`` route sends one fixed GET to the vendor's usage
endpoint with that account's token (CPA substitutes ``$TOKEN$``). Rules (see
AGENTS.md, "/cpa status"):

- Only on command. Two vendor URLs, fixed in ``QUOTA_REQUESTS``; nothing from
  the user or config reaches a request. The POST body holds exactly
  ``auth_index``, ``method``, ``url`` and ``header``.
- The whole run holds the shared management guard for its CPA once. The first
  ``api-call`` goes alone; an outer 401 latches the key and stops the run.
  At most ``MAX_IN_FLIGHT`` calls after that, the latch checked before each.
- ``auth_index`` and the Codex account id stay in memory to build the call;
  they are never shown, logged or cached. Vendor replies are read through
  allowlists; bodies and headers are never shown.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:  # loaded by Hermes as a package
    from . import cpa, status
except ImportError:  # pragma: no cover - direct import in tests
    import status  # type: ignore[no-redef]

    import cpa  # type: ignore[no-redef]

# ── Requests (closed list, copied from the Management Center) ───────────────
# router-for-me/Cli-Proxy-API-Management-Center src/utils/quota/constants.ts
# (CLAUDE_REQUEST_HEADERS, CODEX_REQUEST_HEADERS); Claude adds Accept in
# src/features/quota/providers/claude/data.ts.

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
QUOTA_REQUESTS: dict[str, tuple[str, dict[str, str]]] = {
    "claude": (
        CLAUDE_USAGE_URL,
        {
            "Authorization": "Bearer $TOKEN$",
            "Content-Type": "application/json",
            "User-Agent": "claude-cli/2.1.280 (external, cli)",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
        },
    ),
    "codex": (
        CODEX_USAGE_URL,
        {
            "Authorization": "Bearer $TOKEN$",
            "Content-Type": "application/json",
            "User-Agent": (
                "codex-tui/0.149.1 (Mac OS 26.5.2; arm64) iTerm.app/3.6.11 (codex-tui; 0.149.1)"
            ),
        },
    ),
}
QUOTA_PROVIDERS = tuple(QUOTA_REQUESTS)

V8_API_CALL = "/v8/management/requests/api-call"
V0_API_CALL = "/v0/management/api-call"
MAX_ACCOUNTS = 20
MAX_IN_FLIGHT = 4
CALL_TIMEOUT = 15.0
CACHE_SECONDS = 60.0
MAX_OUTER_BYTES = 1024 * 1024
MAX_INNER_BYTES = 256 * 1024

_PLAN_RE = re.compile(r"^[a-z0-9_]{1,20}$")


# ── Parsing vendor replies ──────────────────────────────────────────────────


@dataclass
class Window:
    label: str
    used: float  # percent used, 0..100, as the vendor reports it
    resets_at: datetime | None = None


@dataclass
class Usage:
    plan: str = ""
    windows: list[Window] = field(default_factory=list)
    reached: bool = False  # the vendor says the account is blocked right now


def _num(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    return status._number(raw)


def _used(raw: Any) -> float | None:
    value = _num(raw)
    if value is None or value < 0:
        return None
    return min(value, 100.0)


_CLAUDE_WINDOWS = (
    ("five_hour", "5h"),
    ("seven_day", "wk"),
    ("seven_day_opus", "opus"),
    ("seven_day_sonnet", "sonnet"),
)


def parse_claude(payload: Any) -> Usage:
    usage = Usage()
    if not isinstance(payload, dict):
        return usage
    for key, label in _CLAUDE_WINDOWS:
        window = payload.get(key)
        if not isinstance(window, dict):
            continue
        used = _used(window.get("utilization"))
        if used is None:
            continue
        usage.windows.append(Window(label, used, status.parse_time(window.get("resets_at"))))
    return usage


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
    if isinstance(plan, str) and _PLAN_RE.match(plan.strip().lower()):
        usage.plan = plan.strip().lower()
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


def parse_usage(provider: str, payload: Any, now: datetime) -> Usage:
    return parse_codex(payload, now) if provider == "codex" else parse_claude(payload)


# ── CPA api-call ────────────────────────────────────────────────────────────


@dataclass
class Inner:
    status: int
    body: str


def _post_json(url: str, key: str, body: dict[str, Any], timeout: float) -> Any:
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST")
    request.add_header("Authorization", f"Bearer {key}")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", status.user_agent())
    try:
        with cpa._OPENER.open(request, timeout=timeout) as response:
            data = response.read(MAX_OUTER_BYTES + 1)
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
    if len(data) > MAX_OUTER_BYTES:
        raise cpa.GatewayError(200, "response_too_large")
    try:
        return json.loads(data)
    except ValueError:
        raise cpa.GatewayError(200, "bad_response") from None


def request_body(provider: str, target: str, account: str) -> dict[str, Any]:
    """The exact JSON CPA receives: four keys, values fixed in code."""
    url, headers = QUOTA_REQUESTS[provider]
    header = dict(headers)
    if provider == "codex" and account:
        header["Chatgpt-Account-Id"] = account
    return {"auth_index": target, "method": "GET", "url": url, "header": header}


def api_call(root: str, key: str, path: str, body: dict[str, Any]) -> Inner:
    """One CPA api-call. Raises GatewayError for CPA's own (outer) failures."""
    reply = _post_json(root + path, key, body, CALL_TIMEOUT)
    if not isinstance(reply, dict):
        raise cpa.GatewayError(200, "bad_response")
    code = reply.get("status_code")
    text = reply.get("body")
    if not isinstance(code, int) or isinstance(code, bool):
        raise cpa.GatewayError(200, "bad_response")
    return Inner(code, text if isinstance(text, str) else "")


# ── Run ─────────────────────────────────────────────────────────────────────


@dataclass
class AccountResult:
    name: str
    provider: str
    usage: Usage | None = None
    error: str = ""

    def out_until(self) -> tuple[bool, datetime | None]:
        """Whether the account is out of quota now, and when it frees."""
        if self.usage is None:
            return False, None
        full = [w for w in self.usage.windows if w.used >= 100]
        if not full and not self.usage.reached:
            return False, None
        resets = [w.resets_at for w in (full or self.usage.windows) if w.resets_at]
        return True, max(resets) if resets else None


@dataclass
class QuotaRun:
    status: str  # ok, or a /cpa row status (rejected, blocked, busy, unavailable, ...)
    accounts: list[AccountResult] = field(default_factory=list)
    skipped: int = 0
    detail: str = ""
    at: float = 0.0
    when: datetime | None = None


_CACHE: dict[tuple[str, str, tuple[str, ...]], QuotaRun] = {}
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


def _inner_result(name: str, provider: str, inner: Inner, now: datetime) -> AccountResult:
    if inner.status == 401:
        return AccountResult(name, provider, error="token rejected by vendor")
    if inner.status == 429:
        return AccountResult(name, provider, error="vendor rate limited")
    if not 200 <= inner.status < 300:
        return AccountResult(name, provider, error=f"vendor HTTP {inner.status}")
    if len(inner.body.encode("utf-8")) > MAX_INNER_BYTES:
        return AccountResult(name, provider, error="vendor reply too large")
    try:
        payload = json.loads(inner.body)
    except ValueError:
        return AccountResult(name, provider, error="bad vendor reply")
    usage = parse_usage(provider, payload, now)
    if not usage.windows:
        return AccountResult(name, provider, error="no quota data")
    return AccountResult(name, provider, usage=usage)


def run(
    home: Path,
    specs: list[cpa.InstanceSpec],
    root: str,
    key: str,
    providers: tuple[str, ...],
    now: datetime,
) -> tuple[QuotaRun, bool]:
    """One quota run for a CPA root and key; and whether it came from the cache."""
    job = (root, status._memory_id(key), providers)
    with _CACHE_LOCK:
        cached = _CACHE.get(job)
    if cached and 0 <= time.monotonic() - cached.at < CACHE_SECONDS:
        return cached, True  # never takes the guard
    with cpa.mgmt_guard(home, root, status.GUARD_WAIT) as held:
        if not held:
            return QuotaRun("busy"), False
        result = _run_locked(home, specs, root, key, providers, now)
    result.at, result.when = time.monotonic(), now
    if result.status == "ok":
        with _CACHE_LOCK:
            _CACHE[job] = result
    return result, False


def _run_locked(
    home: Path,
    specs: list[cpa.InstanceSpec],
    root: str,
    key: str,
    providers: tuple[str, ...],
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
        if status.provider_of(entry) in providers and status.classify(entry, now).kind != "disabled"
    ]
    checked, skipped = eligible[:MAX_ACCOUNTS], max(0, len(eligible) - MAX_ACCOUNTS)
    if not checked:
        return QuotaRun("ok", skipped=skipped)

    path = V8_API_CALL
    abort = threading.Event()
    fatal: list[str] = []

    def one(entry: dict[str, Any]) -> AccountResult:
        nonlocal path
        name, provider = status.display_name(entry), status.provider_of(entry)
        target = entry.get("_target")
        if not isinstance(target, str) or not target:
            return AccountResult(name, provider, error="CPA lists no account index")
        if abort.is_set():
            return AccountResult(name, provider, error="not checked")
        if rejected():  # e.g. another process latched it meanwhile
            fatal.append("rejected")
            abort.set()
            return AccountResult(name, provider, error="not checked")
        body = request_body(provider, target, entry.get("_acct") or "")
        try:
            try:
                inner = api_call(root, key, path, body)
            except cpa.GatewayError as exc:
                if exc.status != 404 or path == V0_API_CALL:
                    raise
                path = V0_API_CALL  # only after a v8 404, never after a 401
                inner = api_call(root, key, path, body)
        except cpa.GatewayError as exc:
            if exc.status == 401:
                cpa.mark_rejected(home, root, key)  # inside the guard
                fatal.append("rejected")
                abort.set()
                return AccountResult(name, provider, error="not checked")
            if exc.status == 403:
                fatal.append("blocked")
                abort.set()
                return AccountResult(name, provider, error="not checked")
            if exc.status == 404:
                fatal.append("no_route")
                abort.set()
                return AccountResult(name, provider, error="not checked")
            return AccountResult(name, provider, error=_outer_error(exc))
        return _inner_result(name, provider, inner, now)

    # Probe alone: a bad key costs CPA exactly one failure.
    results = [one(checked[0])]
    if not abort.is_set() and len(checked) > 1:
        with ThreadPoolExecutor(max_workers=MAX_IN_FLIGHT) as pool:
            results += list(pool.map(one, checked[1:]))
    if fatal:
        if fatal[0] == "no_route":
            return QuotaRun("unavailable", detail="this CPA has no api-call route")
        return QuotaRun(fatal[0])
    return QuotaRun("ok", accounts=results, skipped=skipped)


# ── View ────────────────────────────────────────────────────────────────────

USAGE = (
    "/cpa quota <pool> - live usage of every claude and codex account in a pool\n"
    "/cpa quota <pool> <provider> - claude or codex only\n"
    "Asks each vendor through CPA (like CPA's web UI quota page); one call per\n"
    "account, at most 20, cached 60 s. Percent = used."
)


def _when(moment: datetime, now: datetime) -> str:
    local = moment.astimezone()
    if (moment - now).total_seconds() > 20 * 3600:
        return local.strftime("%b %d")
    return local.strftime("%H:%M")


def _window_text(window: Window, now: datetime) -> str:
    text = f"{window.label} {int(window.used)}%"
    if window.resets_at is not None and window.resets_at > now:
        text += f" → {_when(window.resets_at, now)}"
    return text


def _sort_key(item: AccountResult) -> tuple[int, float, str]:
    out, _ = item.out_until()
    if out:
        rank = 0
    elif item.error:
        rank = 1
    else:
        rank = 2
    top = max((w.used for w in item.usage.windows), default=0.0) if item.usage else 0.0
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
    for item in sorted(items, key=_sort_key):
        if item.error or item.usage is None:
            lines.append(f"{item.name}  {item.error}")
            continue
        text = " · ".join(_window_text(w, now) for w in item.usage.windows)
        plan = f"  {item.usage.plan}" if item.usage.plan else ""
        is_out, _ = item.out_until()
        lines.append(f"{item.name}{plan}  {text}" + ("  OUT" if is_out else ""))
    return lines


def render(
    pool: str, result: QuotaRun, cached: bool, providers: tuple[str, ...], now: datetime
) -> str:
    when = result.when or now
    stamp = f"cached {int(time.monotonic() - result.at)}s" if cached else "live"
    lines = [f"{pool} quota · {stamp} {when.astimezone().strftime('%H:%M')}"]
    for provider in providers:
        items = [a for a in result.accounts if a.provider == provider]
        if items:
            lines.extend(_provider_block(provider, items, now))
    if len(lines) == 1:
        lines.append("no " + " or ".join(providers) + " accounts")
    if result.skipped:
        lines.append(f"+{result.skipped} more not checked (at most {MAX_ACCOUNTS} per run)")
    return status._cap(lines)


def report(
    args: list[str],
    *,
    config: Any,
    home: Path,
    secret: Callable[[str], str | None],
    now: datetime | None = None,
) -> str:
    if not args or len(args) > 2 or args[0] in ("help", "-h", "--help"):
        return USAGE
    now = now or datetime.now(timezone.utc)
    specs, _errors = cpa.parse_config(config)
    spec = next((s for s in specs if s.name == args[0].lower()), None)
    if spec is None:
        names = ", ".join(s.name for s in specs) or "none"
        return f"Unknown pool '{status.mask_text(args[0])[:32]}'. Pools: {names}"
    providers = QUOTA_PROVIDERS
    if len(args) == 2:
        provider = cpa.normalize_provider(args[1])
        if provider not in QUOTA_REQUESTS:
            shown = status.mask_text(args[1])[:32]
            return f"Live quota covers {' and '.join(QUOTA_PROVIDERS)}; not '{shown}'."
        providers = (provider,)
    if not spec.mgmt_key_env:
        return f"{spec.name}: {status._STATUS_TEXT['no_key']}"
    try:
        key = secret(spec.mgmt_key_env) or ""
    except Exception:  # noqa: BLE001 - e.g. unscoped read under multiplexing
        key = ""
    if not key:
        return f"{spec.name}: {status._STATUS_TEXT['key_not_set']}"
    result, cached = run(home, specs, spec.root, key, providers, now)
    if result.status != "ok":
        text = status._STATUS_TEXT.get(result.status) or "unavailable" + (
            f" ({result.detail})" if result.detail else ""
        )
        return f"{spec.name}: {text}"
    return render(spec.name, result, cached, providers, now)

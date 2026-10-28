"""``/cpa``: account health for every configured CLIProxyAPI (CPA) pool.

Rules (see AGENTS.md, "/cpa status"):
- Only when a user runs ``/cpa``. One ``GET credentials`` per distinct CPA root
  and management key; v0 ``auth-files`` only after a v8 404. No retries.
- Never send a key CPA already rejected: a fingerprint-only latch file plus the
  provider plugin's persisted latch of every instance on the same root.
- Never read ``email``, ``account``, ``id_token``, ``project_id``, ``path``,
  ``id`` or ``auth_index`` for display. ``/cpa quota`` keeps a few call fields
  in memory only (see ``files_of``). Names and messages are masked.
- Quota shown is CPA's last-seen observation from the same GET (claude, codex).
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

try:  # loaded by Hermes as a package
    from . import cpa
except ImportError:  # pragma: no cover - direct import in tests
    import cpa  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# ── Masking ─────────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"([A-Za-z0-9._%+-]+)@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{24,}")
MESSAGE_LIMIT = 60


def mask_text(text: str) -> str:
    """Hide email addresses (keep two characters) and long token-like runs."""
    text = _EMAIL_RE.sub(lambda m: m.group(1)[:2] + "…@…", text)
    return _TOKEN_RE.sub("…", text)


def display_name(entry: dict[str, Any]) -> str:
    """Account label for output: CPA ``label``, else the file name, masked.

    CPA names OAuth files ``<provider>-<email>.json``; the provider prefix is
    kept and only two characters of the rest survive masking.
    """
    raw = entry.get("label") or entry.get("name") or ""
    raw = raw.strip() if isinstance(raw, str) else ""
    if raw.endswith(".json"):
        raw = raw[: -len(".json")]
    prefix = provider_of(entry) + "-"
    if raw.lower().startswith(prefix) and len(raw) > len(prefix):
        return raw[: len(prefix)] + mask_text(raw[len(prefix) :])
    return mask_text(raw) or "?"


def short_message(text: Any) -> str:
    if not isinstance(text, str):
        return ""
    text = mask_text(" ".join(text.split()))
    return text if len(text) <= MESSAGE_LIMIT else text[: MESSAGE_LIMIT - 1] + "…"


# ── Classification ──────────────────────────────────────────────────────────


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    # Go's RFC3339Nano can carry 9 fractional digits; Python takes 6.
    raw = re.sub(r"(\.\d{6})\d+", r"\1", raw)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class AccountState:
    kind: str  # ready, limited, cooling, error, disabled, pending, refreshing, unknown
    until: datetime | None = None
    reason: str = ""
    models_cooling: int = 0

    @property
    def bucket(self) -> str:
        if self.kind in ("ready", "limited", "cooling", "error", "disabled"):
            return self.kind
        return "other"


def _cooldowns(entry: dict[str, Any]) -> list[dict[str, Any]]:
    value = entry.get("cooldowns")
    return [c for c in value if isinstance(c, dict)] if isinstance(value, list) else []


def classify(entry: dict[str, Any], now: datetime) -> AccountState:
    """One account's state; first match wins. Only ``active`` is ever ready."""
    raw_status = entry.get("status")
    status = raw_status.strip().lower() if isinstance(raw_status, str) else ""
    if entry.get("disabled") is True or status == "disabled":
        return AccountState("disabled")

    credential_until, reason, models = None, "", 0
    for item in _cooldowns(entry):
        retry = parse_time(item.get("retry_at"))
        if retry is None or retry <= now:
            continue
        if item.get("scope") == "credential":
            if credential_until is None or retry > credential_until:
                credential_until = retry
                raw_reason = item.get("reason")
                reason = raw_reason if isinstance(raw_reason, str) else ""
        elif item.get("scope") == "model":
            models += 1
    if credential_until is None:
        retry = parse_time(entry.get("next_retry_after"))
        if retry is not None and retry > now:
            credential_until = retry
    if credential_until is not None:
        return AccountState("cooling", until=credential_until, reason=reason)

    if status == "error" or entry.get("unavailable") is True:
        return AccountState("error")
    if status in ("pending", "refreshing"):
        return AccountState(status)
    if status == "active":
        # CPA has not cooled it, but its last-seen usage window is full.
        full_until = quota_full(entry, now)
        if full_until is not None:
            return AccountState("limited", until=full_until, models_cooling=models)
        return AccountState("ready", models_cooling=models)
    return AccountState("unknown")


def provider_of(entry: dict[str, Any]) -> str:
    raw = entry.get("provider") or entry.get("type") or ""
    return cpa.normalize_provider(raw) if isinstance(raw, str) and raw.strip() else "unknown"


@dataclass
class ProviderSummary:
    total: int = 0
    ready: int = 0
    limited: int = 0
    cooling: int = 0
    error: int = 0
    disabled: int = 0
    other: int = 0
    next_retry: datetime | None = None
    next_free: datetime | None = None
    success: int = 0
    failed: int = 0

    def add(self, state: AccountState, recent: tuple[int, int]) -> None:
        self.total += 1
        setattr(self, state.bucket, getattr(self, state.bucket) + 1)
        if state.kind == "limited":
            if state.until and (self.next_free is None or state.until < self.next_free):
                self.next_free = state.until
        elif state.until and (self.next_retry is None or state.until < self.next_retry):
            self.next_retry = state.until
        self.success += recent[0]
        self.failed += recent[1]


# Fields /cpa reads. Everything else in a CPA entry (email, account, id_token,
# project_id, path, id, auth_index, ...) is dropped on ingest, so it is never
# kept in the short result cache either.
_KEPT_FIELDS = frozenset(
    {
        "provider",
        "type",
        "label",
        "name",
        "status",
        "status_message",
        "disabled",
        "unavailable",
        "next_retry_after",
        "cooldowns",
        "recent_requests",
        "quota",
    }
)
# Providers ``/cpa quota`` reads through CPA's api-call (see quota.py). Others
# are read only when CPA lists a quota plugin for them.
QUOTA_BUILTINS = ("claude", "codex", "xai", "antigravity", "devin", "kimi")
_KIMI_AI = ("kimi-ai", "kimi.ai")


def files_of(listing: Any) -> list[dict[str, Any]]:
    """Listing entries reduced to ``_KEPT_FIELDS``, plus in-memory call fields.

    For entries ``/cpa quota`` can read: ``_target`` (CPA ``auth_index``),
    ``_acct`` (Codex ``id_token.chatgpt_account_id``), ``_project``
    (Antigravity ``project_id``), ``_plugin`` (CPA ``quota_plugin_id``, for
    non-built-in providers) and ``_kimi_ai`` (from the raw provider fields,
    before ``kimi-ai`` is normalized to ``kimi``). They stay in memory only and
    are never rendered; everything else is dropped.
    """
    files = listing.get("files") if isinstance(listing, dict) else None
    if not isinstance(files, list):
        return []
    out = []
    for raw in files:
        if not isinstance(raw, dict):
            continue
        entry = {k: v for k, v in raw.items() if k in _KEPT_FIELDS}
        provider = provider_of(entry)
        plugin = _shaped(raw.get("quota_plugin_id"), _PLUGIN_RE)
        builtin = provider in QUOTA_BUILTINS
        if builtin or (plugin and provider != "meta" and _true(raw.get("supports_quota"))):
            target, account = call_target(raw)
            if target:
                entry["_target"] = target
            if account and provider == "codex":
                entry["_acct"] = account
            if provider == "antigravity" and _shaped(raw.get("project_id"), _TARGET_RE):
                entry["_project"] = raw["project_id"]
            if provider == "kimi":
                entry["_kimi_ai"] = _is_kimi_ai(raw)
            if not builtin:
                entry["_plugin"] = plugin
        out.append(entry)
    return out


_TARGET_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PLUGIN_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _shaped(value: Any, pattern: re.Pattern[str]) -> str:
    return value if isinstance(value, str) and pattern.match(value) else ""


def _true(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in ("true", "1"))


def _is_kimi_ai(raw: dict[str, Any]) -> bool:
    """Kimi's .ai service, from listing fields only (MC also reads the file)."""
    for name in ("provider", "type"):
        value = raw.get(name)
        if isinstance(value, str) and value.strip().lower() in _KIMI_AI:
            return True
    label = raw.get("name")
    return isinstance(label, str) and any(hint in label.lower() for hint in _KIMI_AI)


def call_target(entry: dict[str, Any]) -> tuple[str, str]:
    """``(auth_index, chatgpt_account_id)`` from a raw CPA listing entry, shape-checked."""
    raw = entry.get("auth_index")
    target = raw if isinstance(raw, str) and _TARGET_RE.match(raw) else ""
    account = ""
    claims = entry.get("id_token")
    if isinstance(claims, dict):
        value = claims.get("chatgpt_account_id")
        if isinstance(value, str) and _ACCOUNT_RE.match(value):
            account = value
    return target, account


def summarize(files: Iterable[dict[str, Any]], now: datetime) -> dict[str, ProviderSummary]:
    out: dict[str, ProviderSummary] = {}
    for entry in files:
        out.setdefault(provider_of(entry), ProviderSummary()).add(
            classify(entry, now), recent_hour(entry)
        )
    return dict(sorted(out.items()))


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def recent_hour(entry: dict[str, Any]) -> tuple[int, int]:
    """Successes and failures over the newest six 10-minute buckets (oldest first in CPA)."""
    buckets = entry.get("recent_requests")
    if not isinstance(buckets, list):
        return 0, 0
    newest = [b for b in buckets if isinstance(b, dict)][-6:]
    return sum(_count(b.get("success")) for b in newest), sum(
        _count(b.get("failed")) for b in newest
    )


# ── Last-seen quota ─────────────────────────────────────────────────────────
# Header names from CPA v8.0.44: sdk/cliproxy/auth/quota_signals.go
# (isQuotaSignalHeaderForProvider: claude and codex),
# internal/runtime/executor/helps/codex_quota.go (per window: Used-Percent,
# Window-Minutes, Reset-At or Reset-After-Seconds) and
# internal/runtime/executor/helps/claude_ratelimit.go (utilization is 0..1,
# reset is unix seconds).
#
# Codex windows are labelled from their own Window-Minutes, never from their
# position: on Pro plans the primary window is the 7-day one and the
# secondary has no length. Position is only a fallback for signals stored
# without Window-Minutes.
_CODEX_WINDOWS = (("Primary", "5h"), ("Secondary", "wk"))
_CLAUDE_WINDOWS = (("5h", "5h"), ("7d", "wk"))
_MINUTE_LABELS = {300: "5h", 10080: "wk"}


@dataclass
class Window:
    label: str
    used: int
    reset: datetime | None = None


def _percent(raw: Any, scale: float) -> int | None:
    try:
        value = float(raw) * scale
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value) or value < 0:
        return None
    return int(value)


def _number(raw: Any) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) or math.isinf(value) else value


def _unix(raw: Any) -> datetime | None:
    value = _number(raw)
    if value is None or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _minutes_label(minutes: int) -> str:
    if minutes in _MINUTE_LABELS:
        return _MINUTE_LABELS[minutes]
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def quota_windows(entry: dict[str, Any]) -> list[Window]:
    """Usage windows CPA last saw for this account (claude and codex only)."""
    quota = entry.get("quota")
    if not isinstance(quota, dict):
        return []
    signals = quota.get("signals")
    if not isinstance(signals, dict):
        return []
    lower = {k.lower(): v for k, v in signals.items() if isinstance(k, str)}
    provider = provider_of(entry)
    out: list[Window] = []
    if provider == "codex":
        seen = parse_time(quota.get("observed_at"))
        for name, fallback in _CODEX_WINDOWS:
            prefix = f"x-codex-{name.lower()}-"
            used = _percent(lower.get(prefix + "used-percent"), 1.0)
            if used is None:
                continue
            label = fallback
            if prefix + "window-minutes" in lower:
                minutes = _number(lower[prefix + "window-minutes"])
                if minutes is None or minutes <= 0:
                    continue
                label = _minutes_label(int(minutes))
            reset = _unix(lower.get(prefix + "reset-at"))
            after = _number(lower.get(prefix + "reset-after-seconds"))
            if reset is None and after is not None and after >= 0 and seen is not None:
                reset = seen + timedelta(seconds=after)
            out.append(Window(label, used, reset))
    elif provider == "claude":
        for name, label in _CLAUDE_WINDOWS:
            prefix = f"anthropic-ratelimit-unified-{name}-"
            used = _percent(lower.get(prefix + "utilization"), 100.0)
            if used is not None:
                out.append(Window(label, used, _unix(lower.get(prefix + "reset"))))
    return out


def quota_full(entry: dict[str, Any], now: datetime) -> datetime | None:
    """When a used-up window frees again, if one is still used up now."""
    until = None
    for window in quota_windows(entry):
        if window.used >= 100 and window.reset is not None and window.reset > now:
            if until is None or window.reset > until:
                until = window.reset
    return until


def age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def quota_text(entry: dict[str, Any], now: datetime) -> str:
    parts = []
    for window in quota_windows(entry):
        part = f"{window.label} {window.used}%"
        if window.used >= 100 and window.reset is not None and window.reset > now:
            part += f" until {clock(window.reset, now)}"
        parts.append(part)
    if not parts:
        return ""
    text = " · ".join(parts)
    quota = entry.get("quota")
    seen = parse_time(quota.get("observed_at")) if isinstance(quota, dict) else None
    if seen is not None:
        text += f" ({age_text((now - seen).total_seconds())} ago)"
    return text


def handle(raw_args: str = "") -> str:
    """Hermes command handler (sync: Hermes runs it off the event loop)."""
    try:
        home = _hermes_home()
        return report(raw_args, config=_read_config(home), home=home, secret=_read_secret)
    except Exception as exc:  # noqa: BLE001 - never raise into the gateway, never echo details
        logger.warning("cpa-status: /cpa failed (%s)", type(exc).__name__)
        return f"cpa-status: internal error ({type(exc).__name__})"


# ── Config and secrets (same approach as the provider's __init__.py) ────────


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - older Hermes or unusual embedding
        configured = os.environ.get("HERMES_HOME", "").strip()
        return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def _read_config(home: Path) -> dict[str, Any]:
    try:
        text = (home / "config.yaml").read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = _load_yaml(text)
    except Exception as exc:  # noqa: BLE001 - any parser error means "no usable config"
        logger.warning("cpa-status: config.yaml unreadable (%s)", type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _load_yaml(text: str) -> Any:
    """Read YAML as Hermes does: ruamel.yaml (PyYAML is not guaranteed), YAML 1.1."""
    try:
        from ruamel.yaml import YAML
    except ImportError:  # pragma: no cover - not a Hermes interpreter
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_load(text)
    reader = YAML(typ="safe", pure=True)
    reader.version = (1, 1)
    return reader.load(text)


def _read_secret(name: str) -> str:
    try:
        from agent.secret_scope import get_secret

        return get_secret(name) or ""
    except ImportError:  # pragma: no cover - older Hermes
        return os.environ.get(name, "")


# ── Rejected-key latch and guard ────────────────────────────────────────────
# CPA bans a client IP for 30 minutes after five failed management keys in a
# row, localhost included. The latch and the per-root guard live in the
# shared cpa.py so the provider plugin, this plugin and every process on one
# HERMES_HOME coordinate (see cpa.mgmt_guard).

GUARD_WAIT = 25.0  # seconds /cpa waits for another management pass on the same CPA
_PROCESS_SALT = os.urandom(16).hex()
_STATE_LOCK = threading.Lock()
_RESULTS: dict[tuple[str, str], Fetched] = {}


def reset_state() -> None:
    """Forget in-memory results and rejections. The latch files are never touched."""
    with _STATE_LOCK:
        _RESULTS.clear()
    with cpa._REJECTED_LOCK:
        cpa._REJECTED.clear()
    _quota().reset_state()


def _quota():
    """The ``/cpa quota`` module (imported late: it imports this one)."""
    try:
        from . import quota
    except ImportError:  # pragma: no cover - direct import in tests
        import quota  # type: ignore[no-redef]
    return quota


def _memory_id(key: str) -> str:
    return cpa.management_fingerprint(key, _PROCESS_SALT)


def _names_on_root(specs: Iterable[cpa.InstanceSpec], root: str) -> tuple[str, ...]:
    """The provider latches per instance cache and caches carry no root."""
    return tuple(spec.name for spec in specs if spec.root == root)


def known_rejected(home: Path, specs: list[cpa.InstanceSpec], root: str, key: str) -> bool:
    return cpa.key_rejected(home, root, key, _names_on_root(specs, root))


# ── Fetch ───────────────────────────────────────────────────────────────────

CACHE_SECONDS = 20.0
TIMEOUT = 8.0
MAX_WORKERS = 4


@dataclass
class Fetched:
    status: str  # ok, rejected, blocked, busy, unavailable
    files: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    at: float = 0.0


def user_agent() -> str:
    return f"cpa-status/{cpa.VERSION}"


def _get_listing(root: str, key: str) -> Any:
    """v8 credentials; v0 auth-files only after a v8 404. Raises GatewayError."""
    options = {
        "timeout": TIMEOUT,
        "user_agent": user_agent(),
        "max_bytes": cpa.MAX_MANAGEMENT_LIST_BYTES,
    }
    try:
        return cpa.get_json(root + cpa._V8.credentials, key, **options)
    except cpa.GatewayError as exc:
        if exc.status != 404:
            raise
    return cpa.get_json(root + cpa._V0.credentials, key, **options)


def _cached_listing(job: tuple[str, str]) -> Fetched | None:
    with _STATE_LOCK:
        cached = _RESULTS.get(job)
    if cached and 0 <= time.monotonic() - cached.at < CACHE_SECONDS:
        return cached
    return None


def _fetch(home: Path, specs: list[cpa.InstanceSpec], root: str, key: str) -> tuple[Fetched, bool]:
    """Result for one root and key, and whether it came from the short cache.

    A cache hit never takes the guard; otherwise the GET runs under the shared
    per-root guard, so it never overlaps a provider pass or a quota run.
    """
    cached = _cached_listing((root, _memory_id(key)))
    if cached:
        return cached, True
    with cpa.mgmt_guard(home, root, GUARD_WAIT) as held:
        if not held:
            return Fetched("busy"), False
        return _fetch_locked(home, specs, root, key)


def _fetch_locked(
    home: Path, specs: list[cpa.InstanceSpec], root: str, key: str
) -> tuple[Fetched, bool]:
    """``_fetch`` for a caller that already holds the guard (never takes it)."""
    job = (root, _memory_id(key))
    cached = _cached_listing(job)
    if cached:
        return cached, True
    result = _fetch_once(home, specs, root, key)
    result.at = time.monotonic()
    with _STATE_LOCK:
        _RESULTS[job] = result
    return result, False


def _fetch_once(home: Path, specs: list[cpa.InstanceSpec], root: str, key: str) -> Fetched:
    if known_rejected(home, specs, root, key):
        return Fetched("rejected")
    if not cpa.transport_allowed(root):
        return Fetched("unavailable", detail="transport not allowed")
    try:
        listing = _get_listing(root, key)
    except cpa.GatewayError as exc:
        if exc.status == 401:
            cpa.mark_rejected(home, root, key)  # inside the guard, before release
            return Fetched("rejected")
        if exc.status == 403:
            return Fetched("blocked")
        detail = exc.category + (f" HTTP {exc.status}" if exc.status else "")
        return Fetched("unavailable", detail=detail)
    return Fetched("ok", files=files_of(listing))


# ── Report ──────────────────────────────────────────────────────────────────

OUTPUT_LIMIT = 3500
USAGE = (
    "/cpa - account health for every CLIProxyAPI pool\n"
    "/cpa <pool> - one pool, with the last hour of requests\n"
    "/cpa <pool> <provider> - one line per account\n"
    "/cpa quota <pool> [claude|codex] - live usage from each vendor\n"
    "Needs mgmt_key_env on the pool. Read-only; one request per pool\n"
    "(quota: one more per account)."
)
_STATUS_TEXT = {
    "rejected": "key rejected (fix the key; it will not be sent again)",
    "blocked": "blocked or remote management off",
    "busy": "busy (another check of this CPA is running), try again",
    "no_key": "no management key (set mgmt_key_env)",
    "key_not_set": "key not set",
}
_BUCKET_ORDER = {"error": 0, "cooling": 1, "limited": 2, "other": 3, "disabled": 4, "ready": 5}


@dataclass
class _Row:
    spec: cpa.InstanceSpec
    status: str
    result: Fetched | None = None
    cached: bool = False


def clock(when: datetime, now: datetime) -> str:
    local = when.astimezone()
    if (when - now).total_seconds() > 20 * 3600:
        return local.strftime("%b %d %H:%M")
    return local.strftime("%H:%M")


def _provider_line(name: str, summary: ProviderSummary, now: datetime, detail: bool) -> str:
    parts = []
    if summary.ready:
        parts.append(f"{summary.ready} ready")
    if summary.limited:
        nxt = f" (next {clock(summary.next_free, now)})" if summary.next_free else ""
        parts.append(f"{summary.limited} limited{nxt}")
    if summary.cooling:
        nxt = f" (next {clock(summary.next_retry, now)})" if summary.next_retry else ""
        parts.append(f"{summary.cooling} cooling{nxt}")
    for label in ("error", "disabled", "other"):
        count = getattr(summary, label)
        if count:
            parts.append(f"{count} {label}")
    line = f"  {name} {summary.total}: " + (" · ".join(parts) or "none")
    if detail and (summary.success or summary.failed):
        line += f" · 1h {summary.success} ok / {summary.failed} failed"
    return line


def _state_text(entry: dict[str, Any], state: AccountState, now: datetime) -> str:
    if state.kind == "cooling":
        text = f"cooling until {clock(state.until, now)}" if state.until else "cooling"
        reason = short_message(state.reason)
        return text + (f" ({reason})" if reason else "")
    if state.kind == "error":
        message = short_message(entry.get("status_message"))
        return "error" + (f": {message}" if message else "")
    if state.kind == "limited":
        text = f"limited until {clock(state.until, now)}" if state.until else "limited"
        return text + (f" · {state.models_cooling} models cooling" if state.models_cooling else "")
    if state.kind == "ready" and state.models_cooling:
        return f"ready · {state.models_cooling} models cooling"
    return state.kind


def _account_lines(files: list[dict[str, Any]], provider: str, now: datetime) -> list[str]:
    rows = []
    for entry in files:
        if provider_of(entry) != provider:
            continue
        state = classify(entry, now)
        line = f"{display_name(entry)}  {_state_text(entry, state, now)}"
        ok, failed = recent_hour(entry)
        if ok or failed:
            line += f" · 1h {ok}/{failed}"
        quota = quota_text(entry, now)
        if quota:
            line += f" · {quota}"
        rows.append((_BUCKET_ORDER[state.bucket], display_name(entry), line))
    return [line for _, _, line in sorted(rows)]


def _cap(lines: list[str]) -> str:
    out: list[str] = []
    size = 0
    for index, line in enumerate(lines):
        if size + len(line) + 1 > OUTPUT_LIMIT - 40:
            out.append(f"… {len(lines) - index} more lines")
            break
        out.append(line)
        size += len(line) + 1
    return "\n".join(out)


def report(
    raw_args: str,
    *,
    config: Any,
    home: Path,
    secret: Callable[[str], str | None],
    now: datetime | None = None,
) -> str:
    args = (raw_args or "").split()
    if args[:1] == ["quota"]:
        # Reserved word: a pool literally named "quota" is "/cpa quota quota".
        return _quota().report(args[1:], config=config, home=home, secret=secret, now=now)
    if args[:1] in (["help"], ["-h"], ["--help"]) or len(args) > 2:
        return USAGE
    now = now or datetime.now(timezone.utc)
    specs, errors = cpa.parse_config(config)
    wanted = specs
    if args:
        wanted = [s for s in specs if s.name == args[0].lower()]
        if not wanted:
            names = ", ".join(s.name for s in specs) or "none"
            return f"Unknown pool '{mask_text(args[0])[:32]}'. Pools: {names}"

    rows = [_Row(spec, "") for spec in wanted]
    jobs: dict[tuple[str, str], tuple[str, str]] = {}
    keys: dict[int, tuple[str, str]] = {}
    for index, row in enumerate(rows):
        if not row.spec.mgmt_key_env:
            row.status = "no_key"
            continue
        try:
            key = secret(row.spec.mgmt_key_env) or ""
        except Exception:  # noqa: BLE001 - e.g. unscoped read under multiplexing
            key = ""
        if not key:
            row.status = "key_not_set"
            continue
        job = (row.spec.root, _memory_id(key))
        jobs.setdefault(job, (row.spec.root, key))
        keys[index] = job

    results: dict[tuple[str, str], tuple[Fetched, bool]] = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(jobs))) as pool:
            futures = {
                job: pool.submit(_fetch, home, specs, root, key)
                for job, (root, key) in jobs.items()
            }
            results = {job: future.result() for job, future in futures.items()}
    for index, job in keys.items():
        fetched, cached = results[job]
        rows[index].result, rows[index].cached, rows[index].status = (
            fetched,
            cached,
            fetched.status,
        )

    if len(args) == 2:
        return _provider_view(rows[0], cpa.normalize_provider(args[1]), now)

    lines = [f"CPA pools · {now.astimezone().strftime('%H:%M')}"]
    for row in rows:
        result = row.result
        if row.status != "ok" or result is None:
            text = _STATUS_TEXT.get(row.status) or "unavailable" + (
                f" ({result.detail})" if result and result.detail else ""
            )
            lines.append(f"{row.spec.name}: {text}")
            continue
        age = int(time.monotonic() - result.at)
        lines.append(row.spec.name + (f" (cached {age}s)" if row.cached else ""))
        summary = summarize(result.files, now)
        if not summary:
            lines.append("  no accounts")
        for provider, item in summary.items():
            lines.append(_provider_line(provider, item, now, detail=bool(args)))
    lines.extend(f"config: {error}" for error in errors)
    return _cap(lines)


def _provider_view(row: _Row, provider: str, now: datetime) -> str:
    result = row.result
    if row.status != "ok" or result is None:
        text = _STATUS_TEXT.get(row.status) or "unavailable"
        return f"{row.spec.name}: {text}"
    lines = _account_lines(result.files, provider, now)
    if not lines:
        names = ", ".join(summarize(result.files, now)) or "none"
        shown = mask_text(provider)[:32]
        return f"No provider '{shown}' in {row.spec.name}. Providers: {names}"
    count = len(lines)
    noun = "account" if count == 1 else "accounts"
    return _cap([f"{row.spec.name} / {provider} · {count} {noun}", *lines])

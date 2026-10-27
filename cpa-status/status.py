"""``/cpa``: account health for every configured CLIProxyAPI (CPA) pool.

Rules (see AGENTS.md, "/cpa status"):
- Only when a user runs ``/cpa``. One ``GET credentials`` per distinct CPA root
  and management key; v0 ``auth-files`` only after a v8 404. No retries.
- Never send a key CPA already rejected: a fingerprint-only latch file plus the
  provider plugin's persisted latch of every instance on the same root.
- Never read ``email``, ``account``, ``id_token``, ``project_id``, ``path``,
  ``id`` or ``auth_index``. Names and messages are masked.
- Quota shown is CPA's last-seen observation from the same GET (claude, codex).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

try:  # loaded by Hermes as a package
    from . import cpa
except ImportError:  # pragma: no cover - direct import in tests
    import cpa  # type: ignore[no-redef]

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
    kind: str  # ready, cooling, error, disabled, pending, refreshing, unknown
    until: datetime | None = None
    reason: str = ""
    models_cooling: int = 0

    @property
    def bucket(self) -> str:
        if self.kind in ("ready", "cooling", "error", "disabled"):
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
        return AccountState("ready", models_cooling=models)
    return AccountState("unknown")


def provider_of(entry: dict[str, Any]) -> str:
    raw = entry.get("provider") or entry.get("type") or ""
    return cpa.normalize_provider(raw) if isinstance(raw, str) and raw.strip() else "unknown"


@dataclass
class ProviderSummary:
    total: int = 0
    ready: int = 0
    cooling: int = 0
    error: int = 0
    disabled: int = 0
    other: int = 0
    next_retry: datetime | None = None
    success: int = 0
    failed: int = 0

    def add(self, state: AccountState, recent: tuple[int, int]) -> None:
        self.total += 1
        setattr(self, state.bucket, getattr(self, state.bucket) + 1)
        if state.until and (self.next_retry is None or state.until < self.next_retry):
            self.next_retry = state.until
        self.success += recent[0]
        self.failed += recent[1]


def files_of(listing: Any) -> list[dict[str, Any]]:
    files = listing.get("files") if isinstance(listing, dict) else None
    return [f for f in files if isinstance(f, dict)] if isinstance(files, list) else []


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
# (isQuotaSignalHeaderForProvider: claude and codex) and
# internal/runtime/executor/helps/claude_ratelimit.go (utilization is 0..1).
_QUOTA_WINDOWS = {
    "codex": (
        ("5h", "X-Codex-Primary-Used-Percent", 1.0),
        ("wk", "X-Codex-Secondary-Used-Percent", 1.0),
    ),
    "claude": (
        ("5h", "Anthropic-Ratelimit-Unified-5h-Utilization", 100.0),
        ("wk", "Anthropic-Ratelimit-Unified-7d-Utilization", 100.0),
    ),
}


def _percent(raw: Any, scale: float) -> int | None:
    try:
        value = float(raw) * scale
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value) or value < 0:
        return None
    return int(value)


def age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def quota_text(entry: dict[str, Any], now: datetime) -> str:
    windows = _QUOTA_WINDOWS.get(provider_of(entry))
    quota = entry.get("quota")
    if not windows or not isinstance(quota, dict):
        return ""
    signals = quota.get("signals")
    if not isinstance(signals, dict):
        return ""
    lower = {k.lower(): v for k, v in signals.items() if isinstance(k, str)}
    parts = []
    for label, header, scale in windows:
        value = _percent(lower.get(header.lower()), scale)
        if value is not None:
            parts.append(f"{label} {value}%")
    if not parts:
        return ""
    text = " · ".join(parts)
    seen = parse_time(quota.get("observed_at"))
    if seen is not None:
        text += f" ({age_text((now - seen).total_seconds())} ago)"
    return text


def handle(raw_args: str = "") -> str:
    return "cpa-status: not implemented yet"

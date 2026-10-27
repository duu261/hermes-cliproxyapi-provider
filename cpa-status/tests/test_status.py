"""Tests for the ``/cpa`` status companion. Run from the repo root:

python -m unittest discover -s cpa-status/tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import status  # noqa: E402

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=timezone.utc)


def iso(delta_minutes: float) -> str:
    return (NOW + timedelta(minutes=delta_minutes)).isoformat().replace("+00:00", "Z")


class _Forbidden(dict):
    """An entry that fails the test if a forbidden field is read."""

    FORBIDDEN = {"email", "account", "id_token", "project_id", "path", "id", "auth_index"}

    def __getitem__(self, key):
        if key in self.FORBIDDEN:
            raise AssertionError(f"read forbidden field {key}")
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in self.FORBIDDEN:
            raise AssertionError(f"read forbidden field {key}")
        return super().get(key, default)


class MaskTests(unittest.TestCase):
    def test_email_in_file_name_is_masked(self):
        self.assertEqual(
            status.display_name({"provider": "codex", "name": "codex-alice@example.com.json"}),
            "codex-al…@…",
        )

    def test_hyphenated_local_part_keeps_two_chars(self):
        entry = {"provider": "codex", "name": "john-smith@example.com.json"}
        self.assertEqual(status.display_name(entry), "jo…@…")

    def test_label_wins_over_name(self):
        self.assertEqual(status.display_name({"label": "team-03", "name": "x.json"}), "team-03")

    def test_short_local_part(self):
        self.assertEqual(status.mask_text("a@b.co"), "a…@…")

    def test_token_like_run_is_masked(self):
        text = "key sk-" + "A" * 40 + " end"
        self.assertEqual(status.mask_text(text), "key … end")

    def test_never_reads_forbidden_fields(self):
        entry = _Forbidden(
            name="claude-bob@example.org.json",
            email="bob@example.org",
            account="sk-secret",
            status="active",
            provider="claude",
        )
        self.assertEqual(status.display_name(entry), "claude-bo…@…")
        status.classify(entry, NOW)

    def test_message_masked_and_cut(self):
        out = status.short_message("refresh failed for carol@example.com " + "x" * 200)
        self.assertNotIn("carol@example.com", out)
        self.assertLessEqual(len(out), 60)

    def test_empty_name(self):
        self.assertEqual(status.display_name({}), "?")


class ClassifyTests(unittest.TestCase):
    def state(self, **entry):
        return status.classify(entry, NOW)

    def test_active_is_ready(self):
        self.assertEqual(self.state(status="active").kind, "ready")

    def test_disabled_wins(self):
        st = self.state(status="active", disabled=True, unavailable=True)
        self.assertEqual(st.kind, "disabled")

    def test_status_disabled(self):
        self.assertEqual(self.state(status="disabled").kind, "disabled")

    def test_credential_cooldown_future(self):
        st = self.state(
            status="error",
            unavailable=True,
            cooldowns=[{"scope": "credential", "reason": "quota", "retry_at": iso(20)}],
        )
        self.assertEqual(st.kind, "cooling")
        self.assertEqual(st.until, NOW + timedelta(minutes=20))
        self.assertEqual(st.reason, "quota")

    def test_credential_cooldown_past_is_not_cooling(self):
        st = self.state(
            status="active",
            cooldowns=[{"scope": "credential", "reason": "quota", "retry_at": iso(-5)}],
        )
        self.assertEqual(st.kind, "ready")

    def test_next_retry_after_fallback(self):
        st = self.state(status="error", unavailable=True, next_retry_after=iso(45))
        self.assertEqual(st.kind, "cooling")
        self.assertEqual(st.until, NOW + timedelta(minutes=45))

    def test_error(self):
        self.assertEqual(self.state(status="error").kind, "error")

    def test_unavailable_without_cooldown_is_error(self):
        self.assertEqual(self.state(status="active", unavailable=True).kind, "error")

    def test_never_ready_unless_active(self):
        for value in ("pending", "refreshing", "unknown", "new-state", ""):
            st = self.state(status=value)
            self.assertNotEqual(st.kind, "ready", value)
            self.assertEqual(st.bucket, "other", value)
        self.assertEqual(self.state(status="pending").kind, "pending")
        self.assertEqual(self.state(status="refreshing").kind, "refreshing")

    def test_missing_status_disk_fallback(self):
        self.assertEqual(self.state(name="a.json", cooldowns=None).kind, "unknown")

    def test_model_cooldowns_counted(self):
        st = self.state(
            status="active",
            cooldowns=[
                {"scope": "model", "model_key": "m1", "retry_at": iso(10)},
                {"scope": "model", "model_key": "m2", "retry_at": iso(10)},
                {"scope": "model", "model_key": "m3", "retry_at": iso(-1)},
            ],
        )
        self.assertEqual((st.kind, st.models_cooling), ("ready", 2))

    def test_cooldowns_null(self):
        self.assertEqual(self.state(status="active", cooldowns=None).kind, "ready")

    def test_bad_time_ignored(self):
        st = self.state(status="active", next_retry_after="not-a-time")
        self.assertEqual(st.kind, "ready")


def _account(provider, state, n=0, **extra):
    entry = {"provider": provider, "name": f"{provider}-{n}.json", "status": "active"}
    if state == "cooling":
        entry.update(
            status="error",
            unavailable=True,
            cooldowns=[{"scope": "credential", "reason": "quota", "retry_at": iso(30 + n)}],
        )
    elif state in ("error", "pending", "refreshing"):
        entry["status"] = state
    elif state == "disabled":
        entry["disabled"] = True
    entry.update(extra)
    return entry


class SummaryTests(unittest.TestCase):
    def fixture(self):
        states = (
            ["ready"] * 20
            + ["cooling"] * 3
            + ["error"] * 2
            + ["disabled"] * 2
            + ["pending", "refreshing", "unknown"]
        )
        files = []
        for n, state in enumerate(states):
            if state == "unknown":
                files.append({"provider": "codex", "name": f"codex-{n}.json"})
            else:
                files.append(_account("codex", state, n))
        files.append(_account("xai", "ready", 99))
        return files

    def test_counts_sum_to_total(self):
        summary = status.summarize(self.fixture(), NOW)
        codex = summary["codex"]
        self.assertEqual(codex.total, 30)
        self.assertEqual(
            (codex.ready, codex.cooling, codex.error, codex.disabled, codex.other),
            (20, 3, 2, 2, 3),
        )
        self.assertEqual(
            codex.ready + codex.cooling + codex.error + codex.disabled + codex.other,
            codex.total,
        )
        self.assertEqual(codex.next_retry, NOW + timedelta(minutes=30 + 20))
        self.assertEqual(summary["xai"].total, 1)

    def test_provider_falls_back_to_type(self):
        summary = status.summarize([{"type": "Claude", "status": "active"}], NOW)
        self.assertIn("claude", summary)

    def test_recent_hour_sums_newest_six(self):
        buckets = [{"time": f"{i}", "success": 1, "failed": i % 2} for i in range(20)]
        self.assertEqual(status.recent_hour({"recent_requests": buckets}), (6, 3))

    def test_recent_hour_missing(self):
        self.assertEqual(status.recent_hour({}), (0, 0))
        self.assertEqual(status.recent_hour({"recent_requests": "junk"}), (0, 0))


class QuotaTests(unittest.TestCase):
    def test_codex_windows(self):
        entry = {
            "provider": "codex",
            "quota": {
                "observed_at": iso(-4),
                "signals": {
                    "X-Codex-Primary-Used-Percent": "63",
                    "X-Codex-Primary-Window-Minutes": "300",
                    "X-Codex-Secondary-Used-Percent": "22.5",
                    "X-Codex-Secondary-Window-Minutes": "10080",
                    "X-Codex-Plan-Type": "pro",
                    "X-Unknown": "ignored",
                },
            },
        }
        self.assertEqual(status.quota_text(entry, NOW), "5h 63% · wk 22% (4m ago)")

    def test_claude_utilization(self):
        entry = {
            "provider": "claude",
            "quota": {
                "observed_at": iso(-90),
                "signals": {
                    "Anthropic-Ratelimit-Unified-5h-Utilization": "0.41",
                    "Anthropic-Ratelimit-Unified-7d-Utilization": "0.07",
                },
            },
        }
        self.assertEqual(status.quota_text(entry, NOW), "5h 41% · wk 7% (1h ago)")

    def test_other_providers_show_nothing(self):
        entry = {
            "provider": "xai",
            "quota": {"signals": {"X-Codex-Primary-Used-Percent": "10"}},
        }
        self.assertEqual(status.quota_text(entry, NOW), "")

    def test_bad_values_ignored(self):
        entry = {
            "provider": "codex",
            "quota": {"signals": {"X-Codex-Primary-Used-Percent": "nan"}},
        }
        self.assertEqual(status.quota_text(entry, NOW), "")

    def test_no_quota(self):
        self.assertEqual(status.quota_text({"provider": "codex"}, NOW), "")


if __name__ == "__main__":
    unittest.main()

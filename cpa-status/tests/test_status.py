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

    def test_email_cut_off_after_the_at_is_masked(self):
        # CPA can cut a vendor error in the middle of an address and add no
        # "...". Whatever is left of the domain, the name in front is masked.
        address = "carol@example.com"
        for end in range(len("carol@"), len(address) + 1):
            part = address[:end]
            for text in (
                f"http 403: denied for {part}",
                f"http 403: denied for {part} and more text",
                f'http 403: {{"email":"{part}',
            ):
                out = status.short_message(text)
                self.assertNotIn("carol", out, text)
                self.assertNotIn("exa", out, text)
                self.assertIn("ca…@…", out, text)
        self.assertEqual(status.short_message("denied for carol@"), "denied for ca…@…")
        self.assertEqual(status.short_message("ssh root@buildhost failed"), "ssh ro…@… failed")
        self.assertEqual(
            status.display_name({"provider": "codex", "name": "codex-alice@exam"}), "codex-al…@…"
        )
        # A whole address is masked as before: what follows its domain stays.
        entry = {"provider": "xai", "name": "xai-carol@example.com-0123456789abcdef.json"}
        self.assertEqual(status.display_name(entry), "xai-ca…@…-0123456789abcdef")
        # An "@" with no name in front is not an address.
        for text in ("limit @ 5 per minute", "@here try again"):
            self.assertEqual(status.short_message(text), text)

    def test_empty_name(self):
        self.assertEqual(status.display_name({}), "?")

    def test_project_id_is_cut_from_label_and_name(self):
        # CPA labels Vertex accounts "<project_id> (<email>)" and names the
        # file "vertex-<project_id>.json"; a label can also be the bare id.
        project = "acme-prod-482113"
        listing = {
            "files": [
                {
                    "provider": "vertex",
                    "name": f"vertex-{project}.json",
                    "label": f"{project} (alice@example.com)",
                    "project_id": project,
                },
                {"provider": "vertex", "name": f"vertex-{project}.json", "project_id": project},
                {
                    "provider": "gemini",
                    "name": "gemini-x.json",
                    "label": project,
                    "project_id": project,
                },
                {
                    "provider": "vertex",
                    "name": "vertex-example.com_my-proj.json",
                    "project_id": "example.com:my-proj",
                },
            ]
        }
        names = [status.display_name(entry) for entry in status.files_of(listing)]
        self.assertEqual(names, ["… (al…@…)", "vertex-…", "…", "vertex-…"])
        for entry in status.files_of(listing):
            self.assertNotIn("project_id", entry)
            self.assertNotIn(project, str(entry))
            self.assertNotIn("acme", str(entry))  # no part of the id survives

    def test_short_project_id_does_not_eat_names(self):
        listing = {"files": [{"provider": "codex", "label": "team-a1", "project_id": "a1"}]}
        self.assertEqual(status.display_name(status.files_of(listing)[0]), "team-a1")

    def test_urls_hosts_and_paths_are_masked(self):
        out = status.short_message(
            'refresh failed: Post "https://auth.internal.example:8443/oauth/token": '
            "dial tcp 10.1.2.3:443: i/o timeout"
        )
        for leak in ("internal", "8443", "oauth", "10.1.2.3"):
            self.assertNotIn(leak, out)
        self.assertIn("refresh failed", out)
        out = status.mask_text("open /CLIProxyAPI/config.yaml: read-only file system")
        self.assertEqual(out, "open …: read-only file system")
        out = status.mask_text("dial tcp relay.example.net:9443: connection refused")
        self.assertEqual(out, "dial tcp …: connection refused")

    def test_bare_hosts_and_addresses_are_masked_in_messages_only(self):
        out = status.short_message("lookup auth.internal.example on 10.1.2.3: no such host")
        self.assertEqual(out, "lookup … on …: no such host")
        # Model ids, versions and Go's own error words are not host names.
        for text in (
            "gpt-5.2-codex is cooling",
            "needs CPA v8.0.64",
            "claude-3.5-sonnet",
            "Client.Timeout exceeded while awaiting headers",
        ):
            self.assertEqual(status.short_message(text), text)
        entry = {"provider": "kiro", "name": "kiro-first.last.json"}
        self.assertEqual(status.display_name(entry), "kiro-first.last")

    def test_project_id_is_cut_from_messages(self):
        # CPA stores the vendor's error text, and Google names the project in it.
        project = "acme-prod-482113"
        raw = {
            "provider": "vertex",
            "name": "vertex-x.json",
            "status": "error",
            "unavailable": True,
            "project_id": project,
            "status_message": f"Permission denied on resource project {project}.",
            "cooldowns": [
                {"scope": "credential", "retry_at": iso(30), "reason": f"quota for {project}"}
            ],
        }
        entry = status.files_of({"files": [raw]})[0]
        self.assertNotIn("acme", str(entry))
        state = status.classify(entry, NOW)
        self.assertEqual(state.kind, "cooling")
        self.assertTrue(status._state_text(entry, state, NOW).endswith("(quota for …)"))
        del entry["cooldowns"]
        state = status.classify(entry, NOW)
        self.assertEqual(
            status._state_text(entry, state, NOW),
            "error: Permission denied on resource project ….",
        )
        self.assertEqual(raw["cooldowns"][0]["reason"], f"quota for {project}")  # input kept

    def test_project_id_cut_leaves_the_cooldown_scope_alone(self):
        # "credential" is a valid project id; the scope must still be read.
        raw = {
            "provider": "vertex",
            "name": "vertex-x.json",
            "status": "active",
            "project_id": "credential",
            "cooldowns": [
                {"scope": "credential", "retry_at": iso(30), "reason": "credential cooling"}
            ],
        }
        entry = status.files_of({"files": [raw]})[0]
        state = status.classify(entry, NOW)
        self.assertEqual(state.kind, "cooling")
        self.assertEqual(state.reason, "… cooling")

    def test_ipv6_and_dial_addresses_are_masked_in_messages(self):
        for text, want in (
            ("dial tcp [2001:db8::1]:443: connect refused", "dial tcp …: connect refused"),
            ("read tcp [fd00::5%eth0]:5120->[fd00::9]:443: reset", "read tcp …->…: reset"),
            ("no route to fd00::5 today", "no route to … today"),
            ("route 2001:db8:0:1:2:3:4:5 down", "route … down"),
            # Go's "<addr>: <error>" shape, and an address after a colon.
            ("peer fd00::5: reset", "peer …: reset"),
            ("address 2001:db8::1: too many colons", "address …: too many colons"),
            ("addr 2001:db8:0:1:2:3:4:5: down", "addr …: down"),
            ("fe80::1%eth0: down", "…: down"),
            ("ip:2001:db8::1 blocked", "ip:… blocked"),
            ("dial tcp myhost:8080: refused", "dial tcp …: refused"),
            ("dial tcp localhost:8317: refused", "dial tcp …: refused"),
            ("localhost:8317 refused", "… refused"),
            ("lookup myhost: no such host", "lookup …: no such host"),
            ("lookup myhost on 10.1.2.3:53: no such host", "lookup … on …: no such host"),
            # A message cut short (by CPA, or here) can end inside the error.
            ("dial tcp: lookup myhost on", "dial tcp: lookup … on"),
            ("dial tcp: lookup myhost on 10.1.2", "dial tcp: lookup … on …"),
            ("dial tcp: lookup myhost", "dial tcp: lookup …"),
            (r"open C:\Users\x\auth\file.json: denied", "open …: denied"),
        ):
            self.assertEqual(status.short_message(text), want)
        # Times, status codes and C++ names are not addresses.
        for text in (
            "retry at 17:20:05",
            "window [17:20:05] closed",
            "status code:429 from vendor",
            "std::bad_alloc in worker",
            "de::Error at line 3",
            "face::book is not an address",
            "failed to read tcp stream early",
            "dns lookup took 3s",
            "lookup myhost on fire now",
            "5h 100% until 17:20",
        ):
            self.assertEqual(status.short_message(text), text)

    def test_long_text_is_cut_at_a_space_before_masking(self):
        # The masks cost quadratic time on a long run, and CPA can store a
        # whole vendor error page as the message.
        self.assertEqual(status.short_message("gateway error " + "a." * 1500), "gateway error")
        # A word cut in half could be half a token: it is dropped, not shown.
        text = "x" * 240 + " sk-" + "A" * 40
        self.assertEqual(status.mask_text(text), "…")
        self.assertEqual(status.mask_text("y" * 300), "…")
        self.assertEqual(status.display_name({"label": "team " + "b" * 300}), "team")

    def test_lookup_host_stays_masked_wherever_the_cut_lands(self):
        # The cut can land in the name, in "on" or in the server; none may show.
        tail = '": dial tcp: lookup myhost on 10.1.2.3:53: no such host'
        for pad in range(180, 232):
            out = status.short_message('Post "https://example.com/' + "p" * pad + tail)
            self.assertNotIn("myhost", out, pad)
            self.assertNotIn("10.1", out, pad)
            self.assertNotIn("example", out, pad)
            self.assertTrue(out.startswith("Post"), (pad, out))

    def test_message_cut_short_by_cpa_shows_no_fragment(self):
        # CPA ends a message it cut with "...", after "<word> ... access token
        # expired" when the cut removed that phrase. The word at the cut can be
        # half a host, an address or an email, at any length of the message.
        cases = (
            ("dial tcp: lookup zqhost on 10.77.88.99:53: no such host", ("zqh", "10.7", "88.9")),
            ("dial tcp zqhost:8080: connect: connection refused", ("zqh", "808")),
            ("peer fd00::5: reset by peer", ("fd00",)),
            ("refresh failed for carol@example.com: invalid_grant", ("car", "exa")),
            ("open /home/bob/auths/codex-bob.json: denied", ("/h", "bob")),
        )
        for text, hidden in cases:
            for end in range(1, len(text) + 1):
                head = " ".join(text[:end].split())
                for cut in (head + "...", head + " ... Access Token Expired..."):
                    out = status.short_message(cut)
                    for fragment in hidden:
                        self.assertNotIn(fragment, out, cut)
        # A cut message longer than the 256 characters that are masked: CPA's
        # tail must be found before that cut, or the half word in front stays.
        text = "dial tcp: lookup zqhost on 10.77.88.99:53: no such host"
        for pad in range(190, 262, 6):
            for end in range(1, len(text) + 1):
                head = "id " + "p" * pad + " " + " ".join(text[:end].split())
                for cut in (head + "...", head + " ... access token expired..."):
                    out = status.short_message(cut)
                    for fragment in ("zqh", "10.7", "88.9"):
                        self.assertNotIn(fragment, out, (pad, end))
        for text, want in (
            ("dial tcp: lookup zqhost on...", "dial tcp: lookup …"),
            ("upstream: dial tcp zqhost:...", "upstream: dial tcp …"),
            ("refresh failed for carol@examp...", "refresh failed for …"),
            ("read tcp 10.77.88....", "read tcp …"),
            (
                "dial tcp: lookup zqhost on 10.7 ... Access Token Expired...",
                "dial tcp: lookup … on … Access Token Expired",
            ),
            ("please try again later...", "please try again …"),
            ("retrying...", "…"),
        ):
            self.assertEqual(status.short_message(text), want)
        # Not cut: nothing is dropped.
        for text in ("unauthorized: access token expired", "wait... then retry", "a ... b"):
            self.assertEqual(status.short_message(text), text)

    def test_plain_text_with_slashes_is_kept(self):
        for text in ("1h 3/1", "3 ok / 1 failed", "5h and/or wk", "HTTP 429 at 12:30"):
            self.assertEqual(status.mask_text(text), text)
            self.assertEqual(status.short_message(text), text)


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

    def test_requires_sign_in_is_error_even_while_cooling(self):
        st = self.state(
            status="active",
            requires_sign_in=True,
            cooldowns=[{"scope": "credential", "reason": "quota", "retry_at": iso(30)}],
        )
        self.assertEqual((st.kind, st.reason), ("error", "sign in again"))
        entry = {"status": "active", "requires_sign_in": True, "status_message": "x@y.com expired"}
        text = status._state_text(entry, status.classify(entry, NOW), NOW)
        self.assertEqual(text, "error: sign in again")
        self.assertEqual(self.state(status="active", requires_sign_in=False).kind, "ready")
        kept = status.files_of({"files": [dict(entry, provider="codex")]})[0]
        self.assertIs(kept["requires_sign_in"], True)

    def test_sign_in_required_but_still_serving(self):
        # CPA keeps serving on the access token it has (renewal_state
        # "sign_in_required") until that token expires.
        entry = {
            "provider": "codex",
            "status": "active",
            "requires_sign_in": True,
            "renewal_state": "sign_in_required",
            "access_token_expires_at": iso(90),
        }
        st = status.classify(entry, NOW)
        self.assertEqual((st.kind, st.sign_in), ("ready", True))
        until = status.clock(NOW + timedelta(minutes=90), NOW)
        self.assertEqual(
            status._state_text(entry, st, NOW), f"active · sign in again (serving until {until})"
        )
        no_expiry = dict(entry)
        del no_expiry["access_token_expires_at"]
        st = status.classify(no_expiry, NOW)
        self.assertEqual(
            status._state_text(no_expiry, st, NOW),
            "active · sign in again (serving, no expiry known)",
        )
        cooling = dict(entry, cooldowns=[{"scope": "credential", "retry_at": iso(30)}])
        st = status.classify(cooling, NOW)
        self.assertEqual((st.kind, st.sign_in), ("cooling", True))
        self.assertIn("sign in again (token valid until ", status._state_text(cooling, st, NOW))
        kept = status.files_of({"files": [entry]})[0]
        self.assertEqual(kept["renewal_state"], "sign_in_required")
        self.assertEqual(kept["access_token_expires_at"], iso(90))

    def test_sign_in_required_and_expired_is_error(self):
        expired = self.state(
            status="active", requires_sign_in=True, renewal_state="expired_sign_in_required"
        )
        self.assertEqual((expired.kind, expired.reason), ("error", "sign in again (expired)"))
        late = self.state(
            status="active",
            requires_sign_in=True,
            renewal_state="sign_in_required",
            access_token_expires_at=iso(-5),
        )
        self.assertEqual((late.kind, late.reason), ("error", "sign in again (expired)"))

    def test_sign_in_required_and_unavailable_is_error(self):
        st = self.state(
            status="error",
            unavailable=True,
            requires_sign_in=True,
            renewal_state="sign_in_required",
            access_token_expires_at=iso(90),
        )
        self.assertEqual((st.kind, st.reason, st.sign_in), ("error", "sign in again", False))

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

    def test_codex_pro_weekly_primary(self):
        # Codex Pro: the primary window is the 7-day one and the secondary has
        # no length. Labels come from each window's own minutes, never position.
        entry = {
            "provider": "codex",
            "quota": {
                "observed_at": iso(-4),
                "signals": {
                    "X-Codex-Primary-Used-Percent": "29",
                    "X-Codex-Primary-Window-Minutes": "10080",
                    "X-Codex-Secondary-Used-Percent": "0",
                    "X-Codex-Secondary-Window-Minutes": "0",
                },
            },
        }
        self.assertEqual(status.quota_text(entry, NOW), "wk 29% (4m ago)")

    def test_codex_unknown_length_uses_minutes(self):
        entry = {
            "provider": "codex",
            "quota": {
                "signals": {
                    "X-Codex-Primary-Used-Percent": "12",
                    "X-Codex-Primary-Window-Minutes": "1440",
                }
            },
        }
        self.assertEqual(status.quota_text(entry, NOW), "1d 12%")

    def test_codex_without_minutes_keeps_position(self):
        # Older CPA builds stored no Window-Minutes; keep the old labels then.
        entry = {
            "provider": "codex",
            "quota": {"signals": {"X-Codex-Primary-Used-Percent": "63"}},
        }
        self.assertEqual(status.quota_text(entry, NOW), "5h 63%")

    def test_full_window_shows_reset(self):
        entry = {
            "provider": "codex",
            "quota": {
                "signals": {
                    "X-Codex-Primary-Used-Percent": "100",
                    "X-Codex-Primary-Window-Minutes": "300",
                    "X-Codex-Primary-Reset-At": str(int((NOW + timedelta(hours=2)).timestamp())),
                    "X-Codex-Secondary-Used-Percent": "96",
                    "X-Codex-Secondary-Window-Minutes": "10080",
                }
            },
        }
        self.assertEqual(
            status.quota_text(entry, NOW),
            f"5h 100% until {status.clock(NOW + timedelta(hours=2), NOW)} · wk 96%",
        )
        self.assertTrue(status.quota_full(entry, NOW))

    def test_claude_full_window_reset(self):
        reset = int((NOW + timedelta(hours=26)).timestamp())
        entry = {
            "provider": "claude",
            "quota": {
                "signals": {
                    "Anthropic-Ratelimit-Unified-5h-Utilization": "0.2",
                    "Anthropic-Ratelimit-Unified-7d-Utilization": "1.0",
                    "Anthropic-Ratelimit-Unified-7d-Reset": str(reset),
                }
            },
        }
        until = status.clock(datetime.fromtimestamp(reset, timezone.utc), NOW)
        self.assertEqual(status.quota_text(entry, NOW), f"5h 20% · wk 100% until {until}")
        self.assertTrue(status.quota_full(entry, NOW))

    def test_not_full(self):
        entry = {
            "provider": "claude",
            "quota": {"signals": {"Anthropic-Ratelimit-Unified-5h-Utilization": "0.99"}},
        }
        self.assertFalse(status.quota_full(entry, NOW))
        self.assertFalse(status.quota_full({"provider": "xai"}, NOW))

    def test_full_window_without_reset_is_limited(self):
        entry = {
            "provider": "claude",
            "status": "active",
            "quota": {"signals": {"Anthropic-Ratelimit-Unified-5h-Utilization": "1.0"}},
        }
        st = status.classify(entry, NOW)
        self.assertEqual((st.kind, st.until), ("limited", None))
        self.assertEqual(status._state_text(entry, st, NOW), "limited")
        self.assertIsNone(status.quota_full(entry, NOW))

    def test_window_past_its_reset_is_not_full(self):
        entry = {
            "provider": "codex",
            "status": "active",
            "quota": {
                "observed_at": iso(-200),
                "signals": {
                    "X-Codex-Primary-Used-Percent": "100",
                    "X-Codex-Primary-Window-Minutes": "300",
                    "X-Codex-Primary-Reset-At": str(int((NOW - timedelta(hours=1)).timestamp())),
                    "X-Codex-Secondary-Used-Percent": "40",
                    "X-Codex-Secondary-Window-Minutes": "10080",
                },
            },
        }
        self.assertEqual(status.classify(entry, NOW).kind, "ready")

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

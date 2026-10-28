"""Fetch and lockout tests for ``/cpa`` against a local fake CPA."""

from __future__ import annotations

import importlib
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import status  # noqa: E402

import cpa  # noqa: E402

KEY = "mgmt-key-good"
BAD = "mgmt-key-bad"
LISTING = {
    "observed_at": "2026-10-27T09:00:00Z",
    "files": [
        {"provider": "codex", "name": "codex-a@example.com.json", "status": "active"},
        {"provider": "codex", "name": "codex-b@example.com.json", "status": "error"},
    ],
}


class _CPA(BaseHTTPRequestHandler):
    seen: list = []
    v8 = True
    force: int = 0
    redirect = False

    def log_message(self, *args):
        pass

    def _send(self, code, body=None):
        data = json.dumps(body if body is not None else {"error": "secret-body"}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        cls = type(self)
        path = urlsplit(self.path).path
        cls.seen.append(
            {
                "path": path,
                "auth": self.headers.get("Authorization", ""),
                "ua": self.headers.get("User-Agent", ""),
                "headers": sorted(h.lower() for h in self.headers.keys()),
            }
        )
        if cls.force:
            return self._send(cls.force)
        if cls.redirect:
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:1/x")
            self.end_headers()
            return
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            return self._send(401)
        if path == "/v8/management/credentials" and cls.v8:
            return self._send(200, LISTING)
        if path == "/v0/management/auth-files" and not cls.v8:
            return self._send(200, LISTING)
        return self._send(404)


class FetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _CPA)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.root = f"http://127.0.0.1:{cls.server.server_port}"
        cls.other_server = HTTPServer(("127.0.0.1", 0), _CPA)
        threading.Thread(target=cls.other_server.serve_forever, daemon=True).start()
        cls.other_root = f"http://127.0.0.1:{cls.other_server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.other_server.shutdown()

    def setUp(self):
        _CPA.seen = []
        _CPA.v8 = True
        _CPA.force = 0
        _CPA.redirect = False
        self.home = Path(tempfile.mkdtemp())
        self.env = {"CPA_A_MGMT": KEY, "CPA_B_MGMT": KEY}
        status.reset_state()

    def config(self, **instances):
        body = {}
        for name, (root, mgmt_env) in instances.items():
            entry = {"base_url": root, "key_env": "CPA_CLIENT"}
            if mgmt_env:
                entry["mgmt_key_env"] = mgmt_env
            body[name] = entry
        return {"cliproxyapi": {"instances": body}}

    def run_cpa(self, args="", config=None):
        config = config or self.config(a=(self.root, "CPA_A_MGMT"))
        return status.report(args, config=config, home=self.home, secret=self.env.get)

    def test_one_get_per_instance_v8(self):
        out = self.run_cpa()
        self.assertEqual([s["path"] for s in _CPA.seen], ["/v8/management/credentials"])
        self.assertIn("codex 2", out)

    def test_v0_only_after_v8_404(self):
        _CPA.v8 = False
        self.run_cpa()
        self.assertEqual(
            [s["path"] for s in _CPA.seen],
            ["/v8/management/credentials", "/v0/management/auth-files"],
        )

    def test_headers_are_bearer_and_own_user_agent(self):
        self.run_cpa()
        seen = _CPA.seen[0]
        self.assertEqual(seen["auth"], f"Bearer {KEY}")
        self.assertEqual(seen["ua"], f"cpa-status/{cpa.VERSION}")
        self.assertNotIn("cookie", seen["headers"])

    def test_401_latches_to_file_and_never_resends(self):
        self.env["CPA_A_MGMT"] = BAD
        out = self.run_cpa()
        self.assertIn("key rejected", out)
        # Exactly one request: a 401 on v8 must never fall back to v0.
        self.assertEqual([s["path"] for s in _CPA.seen], ["/v8/management/credentials"])
        latch = cpa.latch_path(self.home)
        self.assertEqual(latch, self.home / "cache" / "cliproxyapi" / "locks" / "rejected.json")
        self.assertTrue(latch.exists())
        self.assertEqual(stat.S_IMODE(latch.stat().st_mode), 0o600)
        self.assertNotIn(BAD, latch.read_text())
        self.assertFalse((self.home / "cache" / "cpa-status").exists())  # 0.4 file never written
        _CPA.seen = []
        self.assertIn("key rejected", self.run_cpa())
        self.assertEqual(_CPA.seen, [])

    def test_latch_of_0_4_still_blocks(self):
        old = self.home / "cache" / "cpa-status" / "rejected.json"
        old.parent.mkdir(parents=True)
        old.write_text(json.dumps({self.root: [cpa.management_fingerprint(KEY)]}))
        self.assertIn("key rejected", self.run_cpa())
        self.assertEqual(_CPA.seen, [])

    def test_provider_latch_in_shared_file_blocks(self):
        cpa.mark_rejected(self.home, self.root, KEY)
        status.reset_state()  # memory gone: the file must do it
        self.assertIn("key rejected", self.run_cpa())
        self.assertEqual(_CPA.seen, [])

    def test_busy_when_provider_holds_the_guard(self):
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
                out = self.run_cpa()
        finally:
            release.set()
            worker.join(5)
        self.assertIn("busy", out)
        self.assertEqual(_CPA.seen, [])
        self.assertIn("codex 2", self.run_cpa())  # busy is never cached

    def test_waits_for_a_short_provider_pass(self):
        release, held = threading.Event(), threading.Event()

        def hold():
            with cpa.mgmt_guard(self.home, self.root, 0):
                held.set()
                release.wait(5)

        worker = threading.Thread(target=hold)
        worker.start()
        held.wait(5)
        threading.Timer(0.2, release.set).start()
        out = self.run_cpa()
        worker.join(5)
        self.assertIn("codex 2", out)

    def test_cache_hit_does_not_take_the_guard(self):
        self.run_cpa()
        with cpa.mgmt_guard(self.home, self.root, 0) as ok:
            self.assertTrue(ok)
            out = self.run_cpa()
        self.assertIn("cached", out)

    def test_latch_survives_a_fresh_process(self):
        self.env["CPA_A_MGMT"] = BAD
        self.run_cpa()
        status.reset_state()  # also clears cpa's in-memory latch
        importlib.reload(status)
        _CPA.seen = []
        self.assertIn("key rejected", self.run_cpa())
        self.assertEqual(_CPA.seen, [])

    def test_changed_key_is_tried_again(self):
        self.env["CPA_A_MGMT"] = BAD
        self.run_cpa()
        self.env["CPA_A_MGMT"] = KEY
        _CPA.seen = []
        out = self.run_cpa()
        self.assertEqual(len(_CPA.seen), 1)
        self.assertIn("codex 2", out)

    def _provider_latch(self, instance, key):
        catalog = cpa.Catalog(mgmt_auth_failed=cpa.management_fingerprint(key))
        cpa.save_cache(self.home, instance, catalog)

    def test_provider_latch_on_same_instance(self):
        self._provider_latch("a", KEY)
        self.assertIn("key rejected", self.run_cpa())
        self.assertEqual(_CPA.seen, [])

    def test_provider_latch_on_sibling_instance_same_root(self):
        self._provider_latch("a", KEY)
        config = self.config(a=(self.root, "CPA_A_MGMT"), b=(self.root, "CPA_B_MGMT"))
        out_b = self.run_cpa("b", config)
        self.assertEqual(_CPA.seen, [])
        self.assertIn("key rejected", out_b)
        status.reset_state()
        out_all = self.run_cpa("", config)
        self.assertEqual(_CPA.seen, [])
        self.assertEqual(out_all.count("key rejected"), 2)

    def test_provider_latch_on_other_root_does_not_block(self):
        self._provider_latch("a", KEY)
        config = self.config(a=(self.other_root, "CPA_A_MGMT"), b=(self.root, "CPA_B_MGMT"))
        out = self.run_cpa("b", config)
        self.assertEqual(len(_CPA.seen), 1)
        self.assertIn("codex 2", out)

    def test_corrupt_latch_file_is_empty(self):
        latch = cpa.latch_path(self.home)
        latch.parent.mkdir(parents=True)
        latch.write_text("{not json")
        self.assertIn("codex 2", self.run_cpa())

    def test_403_shows_blocked_and_does_not_latch(self):
        _CPA.force = 403
        out = self.run_cpa()
        self.assertIn("blocked or remote management off", out)
        self.assertFalse(cpa.latch_path(self.home).exists())
        self.assertNotIn("secret-body", out)

    def test_server_error_is_category_only(self):
        _CPA.force = 500
        out = self.run_cpa()
        self.assertIn("unavailable", out)
        self.assertNotIn("secret-body", out)
        self.assertNotIn(self.root, out)

    def test_redirect_not_followed(self):
        _CPA.redirect = True
        self.run_cpa()
        self.assertEqual(len(_CPA.seen), 1)

    def test_shared_root_and_key_read_once(self):
        self.env["CPA_B_MGMT"] = KEY
        config = self.config(a=(self.root, "CPA_A_MGMT"), b=(self.root, "CPA_A_MGMT"))
        out = self.run_cpa("", config)
        self.assertEqual(len(_CPA.seen), 1)
        self.assertEqual(out.count("codex 2"), 2)

    def test_short_cache(self):
        self.run_cpa()
        out = self.run_cpa()
        self.assertEqual(len(_CPA.seen), 1)
        self.assertIn("cached", out)

    def test_no_management_key_configured(self):
        out = self.run_cpa("", self.config(a=(self.root, "")))
        self.assertIn("no management key", out)
        self.assertEqual(_CPA.seen, [])

    def test_key_env_empty(self):
        self.env["CPA_A_MGMT"] = ""
        self.assertIn("key not set", self.run_cpa())
        self.assertEqual(_CPA.seen, [])

    def test_secret_lookup_raising(self):
        def boom(name):
            raise RuntimeError("unscoped")

        out = status.report(
            "", config=self.config(a=(self.root, "CPA_A_MGMT")), home=self.home, secret=boom
        )
        self.assertIn("key not set", out)
        self.assertEqual(_CPA.seen, [])

    def test_bad_config_is_reported_without_values(self):
        config = {"cliproxyapi": {"instances": {"a": {"base_url": "http://user:pw@x"}}}}
        out = self.run_cpa("", config)
        self.assertIn("instance 'a'", out)
        self.assertNotIn("pw", out)


if __name__ == "__main__":
    os.environ.pop("HERMES_HOME", None)
    unittest.main()

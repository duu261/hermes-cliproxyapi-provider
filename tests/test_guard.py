"""Shared management guard and rejected-key latch (both plugins use these)."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import cpa  # noqa: E402

R = "http://127.0.0.1:8317"
OTHER = "http://127.0.0.1:9999"


def _copy_module():
    """The companion's copy of cpa.py, loaded as its own module, as Hermes does."""
    spec = importlib.util.spec_from_file_location(
        "cpa_status_copy", ROOT_DIR / "cpa-status" / "cpa.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look the module up
    spec.loader.exec_module(module)
    return module


class LatchTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        cpa._REJECTED.clear()

    def test_mark_then_rejected_for_that_key_and_root_only(self):
        cpa.mark_rejected(self.home, R, "bad")
        cpa._REJECTED.clear()  # prove the file, not memory
        self.assertTrue(cpa.key_rejected(self.home, R, "bad"))
        self.assertFalse(cpa.key_rejected(self.home, R, "other"))
        self.assertFalse(cpa.key_rejected(self.home, OTHER, "bad"))

    def test_file_lives_in_locks_dir_0600_without_key_text(self):
        cpa.mark_rejected(self.home, R, "bad-key-text")
        path = self.home / "cache" / "cliproxyapi" / "locks" / "rejected.json"
        self.assertEqual(cpa.latch_path(self.home), path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertNotIn("bad-key-text", path.read_text())

    def test_keeps_at_most_eight_per_root(self):
        for n in range(12):
            cpa.mark_rejected(self.home, R, f"key-{n}")
        cpa._REJECTED.clear()
        data = json.loads(cpa.latch_path(self.home).read_text())
        self.assertEqual(len(data[R]), 8)
        self.assertTrue(cpa.key_rejected(self.home, R, "key-11"))
        self.assertFalse(cpa.key_rejected(self.home, R, "key-0"))

    def test_instance_named_rejected_does_not_collide(self):
        cpa.mark_rejected(self.home, R, "bad")
        cpa.save_cache(self.home, "rejected", cpa.Catalog(ids=["m"]))
        cpa._REJECTED.clear()
        self.assertTrue(cpa.key_rejected(self.home, R, "bad"))
        self.assertEqual(cpa.load_cache(self.home, "rejected").ids, ["m"])

    def test_old_companion_latch_still_honored(self):
        old = self.home / "cache" / "cpa-status" / "rejected.json"
        old.parent.mkdir(parents=True)
        old.write_text(json.dumps({R: [cpa.management_fingerprint("bad")]}))
        self.assertTrue(cpa.key_rejected(self.home, R, "bad"))
        self.assertFalse(cpa.key_rejected(self.home, OTHER, "bad"))

    def test_provider_instance_latch_honored_for_named_instances(self):
        cpa.save_cache(
            self.home, "pre", cpa.Catalog(mgmt_auth_failed=cpa.management_fingerprint("bad"))
        )
        self.assertTrue(cpa.key_rejected(self.home, R, "bad", ("pre",)))
        self.assertFalse(cpa.key_rejected(self.home, R, "bad", ("other",)))

    def test_corrupt_or_missing_file_is_not_rejected(self):
        self.assertFalse(cpa.key_rejected(self.home, R, "bad"))
        path = cpa.latch_path(self.home)
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        self.assertFalse(cpa.key_rejected(self.home, R, "bad"))
        cpa.mark_rejected(self.home, R, "bad")  # rewrites a usable file
        cpa._REJECTED.clear()
        self.assertTrue(cpa.key_rejected(self.home, R, "bad"))

    def test_unwritable_home_still_remembers_in_memory(self):
        (self.home / "cache").write_text("a file, not a dir")
        cpa.mark_rejected(self.home, R, "bad")
        self.assertTrue(cpa.key_rejected(self.home, R, "bad"))

    def test_no_home_is_memory_only(self):
        cpa.mark_rejected(None, R, "bad")
        self.assertTrue(cpa.key_rejected(None, R, "bad"))


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())

    def hold(self, release: threading.Event, held: threading.Event, root=R, home=None):
        with cpa.mgmt_guard(home or self.home, root, 0) as ok:
            self.assertTrue(ok)
            held.set()
            release.wait(5)

    def test_second_thread_same_root_is_busy(self):
        release, held = threading.Event(), threading.Event()
        worker = threading.Thread(target=self.hold, args=(release, held))
        worker.start()
        held.wait(5)
        with cpa.mgmt_guard(self.home, R, 0) as ok:
            self.assertFalse(ok)
        with cpa.mgmt_guard(self.home, OTHER, 0) as ok:
            self.assertTrue(ok)  # other roots never wait
        release.set()
        worker.join(5)
        with cpa.mgmt_guard(self.home, R, 0) as ok:
            self.assertTrue(ok)

    def test_nested_same_thread_returns_busy_without_hanging(self):
        with cpa.mgmt_guard(self.home, R, 0) as outer:
            self.assertTrue(outer)
            started = time.monotonic()
            with cpa.mgmt_guard(self.home, R, 0) as inner:
                self.assertFalse(inner)
            self.assertLess(time.monotonic() - started, 1)

    def test_wait_times_out_cleanly(self):
        release, held = threading.Event(), threading.Event()
        worker = threading.Thread(target=self.hold, args=(release, held))
        worker.start()
        held.wait(5)
        started = time.monotonic()
        with cpa.mgmt_guard(self.home, R, 0.2) as ok:
            self.assertFalse(ok)
        self.assertGreaterEqual(time.monotonic() - started, 0.15)
        release.set()
        worker.join(5)

    def test_wait_gets_it_when_released(self):
        release, held = threading.Event(), threading.Event()
        worker = threading.Thread(target=self.hold, args=(release, held))
        worker.start()
        held.wait(5)
        threading.Timer(0.2, release.set).start()
        with cpa.mgmt_guard(self.home, R, 3) as ok:
            self.assertTrue(ok)
        worker.join(5)

    def test_released_on_exception(self):
        with self.assertRaises(RuntimeError):
            with cpa.mgmt_guard(self.home, R, 0):
                raise RuntimeError("boom")
        with cpa.mgmt_guard(self.home, R, 0) as ok:
            self.assertTrue(ok)

    @unittest.skipIf(cpa.fcntl is None, "no fcntl on this platform")
    def test_lock_file_is_empty_and_0600(self):
        with cpa.mgmt_guard(self.home, R, 0):
            pass
        files = list((self.home / "cache" / "cliproxyapi" / "locks").glob("*.lock"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), b"")
        self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)

    @unittest.skipIf(cpa.fcntl is None, "no fcntl on this platform")
    def test_companion_copy_in_same_process_is_coordinated(self):
        # Both plugins load their own cpa module: separate thread locks, one lock file.
        copy = _copy_module()
        with cpa.mgmt_guard(self.home, R, 0) as ok:
            self.assertTrue(ok)
            with copy.mgmt_guard(self.home, R, 0) as other:
                self.assertFalse(other)
        with copy.mgmt_guard(self.home, R, 0) as other:
            self.assertTrue(other)

    @unittest.skipIf(cpa.fcntl is None, "no fcntl on this platform")
    def test_other_process_is_coordinated_and_death_releases(self):
        script = (
            "import sys, time; sys.path.insert(0, sys.argv[1]); import cpa; "
            "from pathlib import Path\n"
            "with cpa.mgmt_guard(Path(sys.argv[2]), sys.argv[3], 0) as ok:\n"
            "    print('held' if ok else 'busy', flush=True); time.sleep(60)\n"
        )
        child = subprocess.Popen(
            [sys.executable, "-c", script, str(ROOT_DIR), str(self.home), R],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "held")
            with cpa.mgmt_guard(self.home, R, 0) as ok:
                self.assertFalse(ok)
        finally:
            child.kill()
            child.wait(5)
            child.stdout.close()
        with cpa.mgmt_guard(self.home, R, 1) as ok:
            self.assertTrue(ok)


if __name__ == "__main__":
    os.environ.pop("HERMES_HOME", None)
    unittest.main()

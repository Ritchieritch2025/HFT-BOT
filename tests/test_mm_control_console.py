"""Offline tests for the localhost crypto-MM control plane."""
from __future__ import annotations

import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request


REPO = Path(__file__).resolve().parents[1]
MM_DIR = REPO / "tools" / "research" / "crypto_mm"
sys.path.insert(0, str(MM_DIR))

import control_console as cc  # noqa: E402
import mm_control as mc  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.control = root / "control.json"
        self.status = root / "status.json"
        self.store = cc.ControlStore(
            self.control,
            self.status,
            hard_max_cost=200.0,
            hard_max_clip="20.00",
            hard_max_net=100,
        )

    def tearDown(self):
        self.temp.cleanup()

    def digest(self):
        return hashlib.sha256(self.control.read_bytes()).hexdigest()

    def publish_status(self, control, *, mode="shadow", **overrides):
        status = {
            "schema_version": mc.STATUS_SCHEMA,
            "observed_at_ns": time.time_ns(),
            "mode": mode,
            "revision": control["revision"],
            "effective": {
                "max_open_cost": control["max_open_cost"],
                "clip": control["clip"],
                "max_net": control["max_net"],
                "paused": control["paused"],
                "kill": control["kill"],
            },
            "halted": False,
            "limit_breached": False,
            "open_orders": 0,
            "exposure": 0.0,
            "realized": 0.0,
            "control_error": "",
        }
        status.update(overrides)
        mc.atomic_write_json(self.status, status)

    def test_initial_document_is_fail_closed_and_owner_only(self):
        current = self.store.read()
        self.assertEqual(current["revision"], 0)
        self.assertTrue(current["paused"])
        self.assertFalse(current["kill"])
        self.assertEqual(current["max_open_cost"], 0.0)
        self.assertEqual(
            stat.S_IMODE(self.control.stat().st_mode),
            0o600,
        )

    def test_apply_updates_all_limits_atomically(self):
        current = self.store.read()
        updated = self.store.apply(
            {
                "max_open_cost": 8.0,
                "clip": "2.00",
                "max_net": 6,
                "note": "canary",
            },
            expected_revision=current["revision"],
            confirmation="APPLY",
        )
        self.assertEqual(updated["revision"], 1)
        self.assertEqual(self.store.read(), updated)

    def test_invalid_update_preserves_file_bytes_and_revision(self):
        before = self.digest()
        revision = self.store.read()["revision"]
        with self.assertRaises(mc.ControlError):
            self.store.apply(
                {"max_open_cost": 200.01},
                expected_revision=revision,
                confirmation="APPLY",
            )
        self.assertEqual(self.digest(), before)
        self.assertEqual(self.store.read()["revision"], revision)

    def test_stale_revision_is_rejected(self):
        revision = self.store.read()["revision"]
        self.store.apply(
            {"max_open_cost": 8.0},
            expected_revision=revision,
            confirmation="APPLY",
        )
        with self.assertRaises(cc.StaleRevision):
            self.store.apply(
                {"max_open_cost": 9.0},
                expected_revision=revision,
                confirmation="APPLY",
            )

    def test_pause_transition_requires_pause_and_succeeds(self):
        current = self.store.read()
        self.publish_status(current)
        resumed = self.store.apply(
            {"paused": False, "note": "shadow resume"},
            expected_revision=current["revision"],
            confirmation="APPLY",
        )
        with self.assertRaises(mc.ControlError):
            self.store.apply(
                {"paused": True},
                expected_revision=resumed["revision"],
                confirmation="APPLY",
            )
        paused = self.store.apply(
            {"paused": True, "note": "operator pause"},
            expected_revision=resumed["revision"],
            confirmation="PAUSE",
        )
        self.assertTrue(paused["paused"])
        self.assertEqual(paused["revision"], resumed["revision"] + 1)

    def test_resume_requires_fresh_matching_shadow_status(self):
        current = self.store.read()
        with self.assertRaisesRegex(mc.ControlError, "status is unavailable"):
            self.store.apply(
                {"paused": False},
                expected_revision=current["revision"],
                confirmation="APPLY",
            )
        self.publish_status(current, mode="live")
        with self.assertRaisesRegex(mc.ControlError, "live resume is disabled"):
            self.store.apply(
                {"paused": False},
                expected_revision=current["revision"],
                confirmation="APPLY",
            )
        self.publish_status(current)
        resumed = self.store.apply(
            {"paused": False},
            expected_revision=current["revision"],
            confirmation="APPLY",
        )
        self.assertFalse(resumed["paused"])

    def test_kill_and_reset_require_exact_confirmations(self):
        revision = self.store.read()["revision"]
        with self.assertRaises(mc.ControlError):
            self.store.apply(
                {"paused": True, "kill": True},
                expected_revision=revision,
                confirmation="APPLY",
            )
        killed = self.store.apply(
            {"paused": True, "kill": True, "note": "test"},
            expected_revision=revision,
            confirmation="KILL",
        )
        self.assertTrue(killed["kill"])
        with self.assertRaises(mc.ControlError):
            self.store.apply(
                {"kill": False},
                expected_revision=killed["revision"],
                confirmation="APPLY",
            )
        reset = self.store.apply(
            {"kill": False},
            expected_revision=killed["revision"],
            confirmation="RESET KILL",
        )
        self.assertFalse(reset["kill"])
        self.assertTrue(reset["paused"])

    def test_unknown_field_and_non_finite_number_rejected(self):
        revision = self.store.read()["revision"]
        for patch in (
            {"unknown": 1},
            {"max_open_cost": float("nan")},
            {"max_open_cost": -1},
        ):
            with self.subTest(patch=patch):
                with self.assertRaises(mc.ControlError):
                    self.store.apply(
                        patch,
                        expected_revision=revision,
                        confirmation="APPLY",
                    )


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = cc.ControlStore(
            root / "control.json",
            root / "status.json",
            hard_max_cost=200.0,
            hard_max_clip="20.00",
            hard_max_net=100,
        )
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            cc.make_handler(self.store, "0123456789abcdef"),
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def post(self, body, *, token=None, origin=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-MM-Control-Token"] = token
        if origin is not None:
            headers["Origin"] = origin
        request = urllib.request.Request(
            self.base + "/api/control",
            method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
        )
        return urllib.request.urlopen(request, timeout=2)

    def test_state_is_read_only_and_control_requires_token(self):
        state = json.load(urllib.request.urlopen(self.base + "/api/state", timeout=2))
        body = {
            "expected_revision": state["control"]["revision"],
            "confirm": "APPLY",
            "patch": {"max_open_cost": 8.0},
        }
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.post(body)
        self.assertEqual(denied.exception.code, 403)
        with self.post(body, token="0123456789abcdef") as response:
            self.assertEqual(response.status, 200)

    def test_page_requires_effective_match_before_applied_label(self):
        page = urllib.request.urlopen(self.base + "/", timeout=2).read().decode()
        self.assertIn("effectiveMatches", page)
        self.assertIn("!s.control_error", page)
        self.assertIn("REQUESTED / WAITING", page)
        self.assertIn("exchange reconciliation unavailable", page)
        self.assertIn("LOCAL ONLY", page)

    def test_cross_origin_and_stale_revision_return_403_and_409(self):
        body = {
            "expected_revision": 0,
            "confirm": "APPLY",
            "patch": {"max_open_cost": 8.0},
        }
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.post(
                body,
                token="0123456789abcdef",
                origin="https://evil.example",
            )
        self.assertEqual(denied.exception.code, 403)
        with self.post(body, token="0123456789abcdef"):
            pass
        with self.assertRaises(urllib.error.HTTPError) as stale:
            self.post(body, token="0123456789abcdef")
        self.assertEqual(stale.exception.code, 409)


if __name__ == "__main__":
    unittest.main(verbosity=2)

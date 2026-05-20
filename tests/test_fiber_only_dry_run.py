# -*- coding: utf-8 -*-

"""Fixture-driven dry-run harness for the fiber-only detection path.

This suite proves the four wired callers in ``bot.py`` can be exercised
end-to-end in ``DETECTION_MODE="fiber_only"`` mode WITHOUT:

- joining a real Zoom meeting,
- sending chat,
- starting the BotManager,
- binding the dashboard port,
- requiring Selenium to actually open a browser,
- contacting Telegram / Discord / Railway.

Fake driver classes stand in for Selenium. Any call to a legacy DOM
selector method (``find_element*``, etc.) raises ``AssertionError`` so
a regression that leaks DOM scraping in fiber-only mode fails the
suite loudly.

Run:

    python -m unittest discover -s tests -p "test_*fiber*.py" -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_FIXTURES = os.path.join(_HERE, "fixtures")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ── Conditional bot import (selenium may be absent in CI) ────────────────


try:
    import bot  # noqa: F401  — transitively triggers selenium import
    import bot_fiber
    import config
    _BOT_IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    bot = None
    bot_fiber = None
    config = None
    _BOT_IMPORT_ERROR = exc


# ── Fixture loader ───────────────────────────────────────────────────────


def load_fixture(name):
    """Load a JSON fixture from ``tests/fixtures/``."""
    path = os.path.join(_FIXTURES, name)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ── Fake driver ──────────────────────────────────────────────────────────


class LegacyDomLeak(AssertionError):
    """Raised when a legacy DOM selector method is hit in fiber-only mode."""


class FakeFiberDriver:
    """Selenium-driver lookalike that serves canned ``execute_script``
    payloads from a fixture map, and raises ``LegacyDomLeak`` for any
    legacy DOM selector call.

    ``payloads_by_marker`` maps a substring of the JS payload to the
    canned dict to return. The first marker matching the JS body wins.
    A default may be supplied via the ``"*"`` key.
    """

    def __init__(self, payloads_by_marker=None, current_url="https://zoom.us/wc/123/start"):
        self.payloads_by_marker = payloads_by_marker or {}
        self.current_url = current_url
        self.execute_script_calls = 0
        self.last_js_marker = None

    # --- Allowed by fiber path -------------------------------------

    def execute_script(self, js, *args, **kwargs):
        self.execute_script_calls += 1
        for marker, payload in self.payloads_by_marker.items():
            if marker == "*":
                continue
            if marker in js:
                self.last_js_marker = marker
                return payload
        if "*" in self.payloads_by_marker:
            self.last_js_marker = "*"
            return self.payloads_by_marker["*"]
        # No canned response — pretend the page returned nothing.
        self.last_js_marker = None
        return None

    # --- All of these MUST NOT be called in fiber-only mode --------

    def find_element(self, *args, **kwargs):
        raise LegacyDomLeak("driver.find_element called in fiber-only mode")

    def find_elements(self, *args, **kwargs):
        raise LegacyDomLeak("driver.find_elements called in fiber-only mode")

    def __getattr__(self, name):
        # Catch any legacy `find_element_by_*` / `find_elements_by_*`
        # attribute access by raising loudly.
        if name.startswith("find_element_by_") or name.startswith("find_elements_by_"):
            raise LegacyDomLeak("driver." + name + " called in fiber-only mode")
        raise AttributeError(name)


# ── Skip base ────────────────────────────────────────────────────────────


@unittest.skipIf(_BOT_IMPORT_ERROR is not None,
                 f"bot.py not importable: {_BOT_IMPORT_ERROR!r}")
class _DryRunBase(unittest.TestCase):
    """Base test case that flips DETECTION_MODE to ``fiber_only`` for the
    duration of a single test and restores it on teardown."""

    def setUp(self):
        self._prev_mode = config.DETECTION_MODE
        config.DETECTION_MODE = "fiber_only"

    def tearDown(self):
        config.DETECTION_MODE = self._prev_mode


# ── Fixture sanity ───────────────────────────────────────────────────────


class TestFixturesLoad(_DryRunBase):

    def test_participants_fixture_has_expected_shape(self):
        fx = load_fixture("fiber_participants_ok.json")
        self.assertEqual(fx["outcome"], "OK")
        self.assertGreaterEqual(len(fx["data"]), 3)
        for p in fx["data"]:
            self.assertIn("displayName", p)
            self.assertIn("role", p)

    def test_chat_fixture_has_expected_shape(self):
        fx = load_fixture("fiber_chat_ok.json")
        self.assertEqual(fx["outcome"], "OK")
        for m in fx["data"]:
            self.assertIn("sender", m)
            self.assertIn("text", m)

    def test_meeting_state_ok_fixture(self):
        fx = load_fixture("fiber_meeting_state_ok.json")
        self.assertEqual(fx["outcome"], "OK")
        self.assertTrue(fx["data"]["inMeeting"])

    def test_meeting_state_ended_fixture(self):
        fx = load_fixture("fiber_meeting_state_ended.json")
        self.assertTrue(fx["data"]["meetingEnded"])
        self.assertFalse(fx["data"]["inMeeting"])


# ── read_chat_messages ───────────────────────────────────────────────────


class TestReadChatMessagesDryRun(_DryRunBase):

    def test_fixture_ok_maps_to_legacy_shape_no_dom(self):
        fx = load_fixture("fiber_chat_ok.json")
        driver = FakeFiberDriver(payloads_by_marker={
            # The chat JS payload mentions chat-virtualized-list — that's
            # our reliable substring marker.
            "chat-virtualized-list": fx,
        })
        out = bot.read_chat_messages(driver, bot_id=0)
        # 4 fixture rows, 1 has empty text → 3 legacy entries.
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0], {"sender": "Alice Host", "text": "Welcome everyone"})
        self.assertEqual(out[-1]["text"], "Question about the agenda?")
        # No DOM selector leak.
        self.assertEqual(driver.execute_script_calls, 1)

    def test_unsupported_returns_empty_no_dom(self):
        canned = {"outcome": "UNSUPPORTED", "data": [], "elapsedMs": 30,
                  "error": "no chat collection in fiber"}
        driver = FakeFiberDriver(payloads_by_marker={"*": canned})
        out = bot.read_chat_messages(driver, bot_id=0)
        self.assertEqual(out, [])
        self.assertEqual(driver.execute_script_calls, 1)

    def test_empty_returns_empty(self):
        canned = {"outcome": "EMPTY", "data": [], "elapsedMs": 5}
        driver = FakeFiberDriver(payloads_by_marker={"*": canned})
        self.assertEqual(bot.read_chat_messages(driver, bot_id=0), [])


# ── get_participant_count ────────────────────────────────────────────────


class TestGetParticipantCountDryRun(_DryRunBase):

    def test_fixture_ok_returns_count(self):
        fx = load_fixture("fiber_participants_ok.json")
        driver = FakeFiberDriver(payloads_by_marker={"participants-ul": fx})
        self.assertEqual(bot.get_participant_count(driver), 4)
        self.assertEqual(driver.execute_script_calls, 1)

    def test_empty_returns_zero(self):
        canned = {"outcome": "EMPTY", "data": [], "elapsedMs": 5}
        driver = FakeFiberDriver(payloads_by_marker={"*": canned})
        self.assertEqual(bot.get_participant_count(driver), 0)

    def test_driver_error_returns_zero(self):
        # Wrapper catches exceptions inside bot_fiber._execute_fiber_js,
        # so even a driver that raises stays inside the fiber path.
        class _RaisingDriver(FakeFiberDriver):
            def execute_script(self, *a, **k):
                self.execute_script_calls += 1
                raise RuntimeError("driver dead")
        d = _RaisingDriver(payloads_by_marker={})
        self.assertEqual(bot.get_participant_count(d), 0)


# ── get_participants ─────────────────────────────────────────────────────


class TestGetParticipantsDryRun(_DryRunBase):

    def test_fixture_ok_maps_to_legacy_shape_no_dom(self):
        fx = load_fixture("fiber_participants_ok.json")
        driver = FakeFiberDriver(payloads_by_marker={"participants-ul": fx})
        out = bot.get_participants(driver, bot_id=0)
        self.assertEqual(len(out), 4)
        # Field-by-field for the first row (host, video on, unmuted).
        self.assertEqual(out[0], {
            "name": "Alice Host", "role": "host", "isSelf": False,
            "videoOff": False, "audioMuted": False, "handRaised": False,
            "_source": "fiber",
        })
        # Self with unknown video state → videoOff defaults to False.
        bob = next(p for p in out if p["name"] == "Bob Self")
        self.assertTrue(bob["isSelf"])
        self.assertTrue(bob["audioMuted"])
        self.assertTrue(bob["handRaised"])
        self.assertFalse(bob["videoOff"])
        # Cohost with video off.
        carol = next(p for p in out if p["name"] == "Carol")
        self.assertEqual(carol["role"], "cohost")
        self.assertTrue(carol["videoOff"])
        self.assertEqual(driver.execute_script_calls, 1)

    def test_empty_returns_empty_list(self):
        driver = FakeFiberDriver(payloads_by_marker={
            "*": {"outcome": "EMPTY", "data": [], "elapsedMs": 4},
        })
        self.assertEqual(bot.get_participants(driver, bot_id=0), [])

    def test_unsupported_returns_empty_list(self):
        driver = FakeFiberDriver(payloads_by_marker={
            "*": {"outcome": "UNSUPPORTED", "data": [], "elapsedMs": 50,
                  "error": "no fiber participants"},
        })
        self.assertEqual(bot.get_participants(driver, bot_id=0), [])


# ── check_bot_alive ──────────────────────────────────────────────────────


class TestCheckBotAliveDryRun(_DryRunBase):

    def test_fixture_in_meeting_returns_true(self):
        fx = load_fixture("fiber_meeting_state_ok.json")
        driver = FakeFiberDriver(payloads_by_marker={"meetingNumber": fx})
        self.assertTrue(bot.check_bot_alive(driver))

    def test_fixture_meeting_ended_returns_false(self):
        fx = load_fixture("fiber_meeting_state_ended.json")
        driver = FakeFiberDriver(payloads_by_marker={"meetingNumber": fx})
        self.assertFalse(bot.check_bot_alive(driver))

    def test_off_zoom_returns_false_without_fiber_call(self):
        driver = FakeFiberDriver(
            payloads_by_marker={"*": {"outcome": "OK", "data": {"inMeeting": True}}},
            current_url="https://example.com/",
        )
        self.assertFalse(bot.check_bot_alive(driver))
        self.assertEqual(driver.execute_script_calls, 0)

    def test_unsupported_on_zoom_returns_true_no_dom(self):
        # URL still on zoom.us + UNSUPPORTED → safe alive (re-probe next
        # tick). Crucially, no DOM XPATH fallback.
        driver = FakeFiberDriver(payloads_by_marker={
            "*": {"outcome": "UNSUPPORTED", "data": {}, "elapsedMs": 80,
                  "error": "no fiber state"},
        })
        self.assertTrue(bot.check_bot_alive(driver))

    def test_deadline_on_zoom_returns_true(self):
        driver = FakeFiberDriver(payloads_by_marker={
            "*": {"outcome": "DEADLINE_EXCEEDED", "data": {}, "elapsedMs": 100},
        })
        self.assertTrue(bot.check_bot_alive(driver))


# ── Import smoke (fiber-only mode does not bind anything) ────────────────


class TestImportSmokeFiberOnly(_DryRunBase):
    """Verifies the standalone modules import cleanly in fiber-only mode
    without binding the dashboard port or starting any background thread.

    This is the "code-level smoke check" alternative to a fixture-driven
    integration test described in Phase 4.
    """

    def test_core_modules_import_under_fiber_only(self):
        # All four are already imported by the test header. Re-importing
        # via __import__ is a no-op but confirms the modules still
        # resolve when DETECTION_MODE has been flipped.
        for name in ("bot", "bot_fiber", "config", "bot_manager"):
            mod = __import__(name)
            self.assertIsNotNone(mod)

    def test_predicate_returns_true_in_fiber_only(self):
        self.assertTrue(bot._is_fiber_only_mode())

    def test_default_is_fiber_only(self):
        # Phase 7 flipped the on-disk default from "hybrid" to
        # "fiber_only". The dry-run base class explicitly forces
        # fiber_only for each test, so we verify the predicate via
        # both branches: forced hybrid → False, forced fiber_only → True.
        config.DETECTION_MODE = "hybrid"
        self.assertFalse(bot._is_fiber_only_mode())
        config.DETECTION_MODE = "fiber_only"
        self.assertTrue(bot._is_fiber_only_mode())

    def test_web_app_imports_without_binding(self):
        """Import ``web_app`` and verify it exposes the Flask app without
        having started ``socketio.run()``.

        ``socketio.run()`` is gated behind ``if __name__ == "__main__":``
        in ``web_app.py``, so plain import never binds a port. This test
        proves that contract holds in fiber-only mode.
        """
        import importlib
        try:
            webmod = importlib.import_module("web_app")
        except Exception as exc:  # noqa: BLE001
            self.fail(f"web_app import failed in fiber-only mode: {exc}")
        self.assertTrue(hasattr(webmod, "app"))
        self.assertTrue(hasattr(webmod, "socketio"))
        # Read the source and assert the run call is in fact gated.
        with open(os.path.join(_ROOT, "web_app.py"), "r", encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('if __name__ == "__main__":', src)
        self.assertIn("socketio.run(app", src)


# ── Phase-4 invariant ────────────────────────────────────────────────────


class TestPhase4Invariants(_DryRunBase):
    """Phase 4 must not regress the prior invariants."""

    def test_legacy_hybrid_path_still_present(self):
        """Phase 3-4 must NOT have deleted the hybrid path — the legacy
        XPATH and DOM strategies must still exist in bot.py to keep
        DETECTION_MODE='hybrid' as the safe default."""
        with open(os.path.join(_ROOT, "bot.py"), "r", encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("'meeting has ended'", src)
        self.assertIn("participants-li", src)
        self.assertIn("[class*=\"chat-message\"]", src)

    def test_default_detection_mode_is_fiber_only(self):
        # Phase 7: on-disk default flipped to "fiber_only". Read the
        # config source straight from disk rather than the live
        # `config.DETECTION_MODE` (which the base class flips).
        with open(os.path.join(_ROOT, "config.py"), "r", encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('"DETECTION_MODE", "fiber_only"', src)
        # "hybrid" must remain a permitted choice — the env-var override
        # is the documented rollback path.
        self.assertIn('"hybrid"', src)
        self.assertIn('"fiber_only"', src)

    def test_subprocess_default_with_no_env_is_fiber_only(self):
        """Confirm the on-disk default is honored at module import time
        when no DETECTION_MODE env var is set. Uses a subprocess with
        the env scrubbed so the parent shell can't leak into the check."""
        import subprocess
        env = {k: v for k, v in os.environ.items() if k != "DETECTION_MODE"}
        result = subprocess.run(
            [sys.executable, "-c", "import config; print(config.DETECTION_MODE)"],
            cwd=_ROOT, env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "fiber_only")

    def test_subprocess_env_override_to_hybrid_works(self):
        """Rollback path: setting DETECTION_MODE=hybrid in the env must
        force the legacy hybrid path even after the Phase 7 default flip."""
        import subprocess
        env = dict(os.environ)
        env["DETECTION_MODE"] = "hybrid"
        result = subprocess.run(
            [sys.executable, "-c", "import config; print(config.DETECTION_MODE)"],
            cwd=_ROOT, env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "hybrid")

    def test_subprocess_invalid_env_falls_back_to_fiber_only(self):
        """Unrecognized DETECTION_MODE values must fall back to the new
        default ('fiber_only'), per the validator block in config.py."""
        import subprocess
        env = dict(os.environ)
        env["DETECTION_MODE"] = "lolnope"
        result = subprocess.run(
            [sys.executable, "-c", "import config; print(config.DETECTION_MODE)"],
            cwd=_ROOT, env=env, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(result.stdout.strip(), "fiber_only")


if __name__ == "__main__":
    unittest.main()

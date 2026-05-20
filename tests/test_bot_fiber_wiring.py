# -*- coding: utf-8 -*-

"""Phase 3 wiring tests.

Verifies that the four detection callers in ``bot.py`` route through
``bot_fiber`` when ``config.DETECTION_MODE == "fiber_only"`` and never
fall back to DOM scraping in that mode.

Tests do not import Selenium. ``bot.py`` is imported (which transitively
imports selenium), so this test file gates its import behind a Selenium
availability check — when Selenium is absent, the suite skips with a
clear reason rather than failing on import.

Run:
    python -m unittest discover -s tests -p "test_bot_fiber_wiring.py" -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


try:
    import bot  # noqa: F401  — also triggers selenium import side-effects
    import bot_fiber
    import config
    _BOT_IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    bot = None
    bot_fiber = None
    config = None
    _BOT_IMPORT_ERROR = exc


# ── Fake driver used as a sentinel — never executed ──────────────────────


class _SentinelDriver:
    """Marker object passed through wrappers. Tests patch bot_fiber.* so
    no method on this driver is ever called.

    Provides ``current_url`` because ``check_bot_alive`` reads it directly
    in fiber_only mode before delegating to ``bot_fiber.capture_meeting_state``.
    """

    def __init__(self, current_url="https://zoom.us/wc/123/start"):
        self.current_url = current_url
        self.execute_script_calls = 0

    # If bot.py ever tries to scrape DOM in fiber_only mode, these will
    # blow up the test loudly instead of silently succeeding.
    def execute_script(self, *args, **kwargs):
        self.execute_script_calls += 1
        raise AssertionError(
            "execute_script called in fiber_only mode — caller leaked a "
            "DOM fallback path."
        )

    def find_element(self, *args, **kwargs):
        raise AssertionError(
            "find_element called in fiber_only mode — caller leaked DOM."
        )

    def find_elements(self, *args, **kwargs):
        raise AssertionError(
            "find_elements called in fiber_only mode — caller leaked DOM."
        )


# ── Skip-if-import-failed base ───────────────────────────────────────────


@unittest.skipIf(_BOT_IMPORT_ERROR is not None,
                 f"bot.py not importable in this environment: "
                 f"{_BOT_IMPORT_ERROR!r}")
class _FiberWiringBase(unittest.TestCase):
    """Base that flips DETECTION_MODE for the duration of a test."""

    def _set_mode(self, mode):
        self._prev_mode = config.DETECTION_MODE
        config.DETECTION_MODE = mode

    def tearDown(self):
        if hasattr(self, "_prev_mode"):
            config.DETECTION_MODE = self._prev_mode


# ── Hybrid mode is now a compatibility shim ─────────────────────────────
#
# Phase 10 removed the legacy DOM fallbacks. `DETECTION_MODE=hybrid`
# is still accepted by config.py (so the env-var rollback command
# doesn't error out for operators with that muscle memory) but the
# detection callers always route through bot_fiber regardless of
# mode. Source rollback (`git revert`) is required to restore DOM
# behaviour.


class TestHybridStillCallsFiberAfterPhase10(_FiberWiringBase):
    """After Phase 10, hybrid is a no-op compat shim — fiber is the
    sole detection authority."""

    def test_get_participant_count_in_hybrid_still_calls_bot_fiber(self):
        """Setting DETECTION_MODE=hybrid must still drive the call
        through bot_fiber.capture_participants; no DOM fallback runs.

        We patch the capture wrapper so the sentinel driver's
        DOM-leak guard is never reached."""
        self._set_mode("hybrid")
        canned = bot_fiber.FiberResult.ok_result([
            {"displayName": "Solo", "isMe": True},
        ])
        with patch.object(bot_fiber, "capture_participants", return_value=canned) as cp:
            count = bot.get_participant_count(_SentinelDriver())
        cp.assert_called_once()
        self.assertEqual(count, 1)

    def test_read_chat_messages_in_hybrid_calls_bot_fiber(self):
        self._set_mode("hybrid")
        canned = bot_fiber.FiberResult.ok_result([
            {"sender": "A", "text": "hi"},
        ])
        with patch.object(bot_fiber, "capture_chat_messages", return_value=canned) as cc:
            out = bot.read_chat_messages(_SentinelDriver(), bot_id=0)
        cc.assert_called_once()
        self.assertEqual(out, [{"sender": "A", "text": "hi"}])

    def test_check_bot_alive_in_hybrid_calls_bot_fiber(self):
        self._set_mode("hybrid")
        canned = bot_fiber.FiberResult.ok_result({
            "inMeeting": True, "inWaitingRoom": False, "meetingEnded": False,
        })
        with patch.object(bot_fiber, "capture_meeting_state", return_value=canned) as cm:
            alive = bot.check_bot_alive(_SentinelDriver())
        cm.assert_called_once()
        self.assertTrue(alive)


# ── Fiber-only: read_chat_messages ───────────────────────────────────────


class TestReadChatMessagesFiberOnly(_FiberWiringBase):

    def test_ok_maps_to_legacy_shape(self):
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.ok_result([
            {"sender": "Alice", "text": "hi", "timestamp": 1, "messageId": "a", "rawKeys": []},
            {"sender": "Bob",   "text": "yo", "timestamp": 2, "messageId": "b", "rawKeys": []},
        ], elapsed_ms=10)
        with patch.object(bot_fiber, "capture_chat_messages", return_value=canned):
            out = bot.read_chat_messages(_SentinelDriver(), bot_id=0)
        self.assertEqual(out, [
            {"sender": "Alice", "text": "hi"},
            {"sender": "Bob",   "text": "yo"},
        ])

    def test_ok_drops_empty_text(self):
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.ok_result([
            {"sender": "Alice", "text": "",    "messageId": "a"},
            {"sender": "Bob",   "text": "ok",  "messageId": "b"},
        ])
        with patch.object(bot_fiber, "capture_chat_messages", return_value=canned):
            out = bot.read_chat_messages(_SentinelDriver(), bot_id=0)
        self.assertEqual(out, [{"sender": "Bob", "text": "ok"}])

    def test_unsupported_returns_empty(self):
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.unsupported("not surfaced", [])
        with patch.object(bot_fiber, "capture_chat_messages", return_value=canned):
            out = bot.read_chat_messages(_SentinelDriver(), bot_id=0)
        self.assertEqual(out, [])

    def test_empty_returns_empty(self):
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.empty([])
        with patch.object(bot_fiber, "capture_chat_messages", return_value=canned):
            out = bot.read_chat_messages(_SentinelDriver(), bot_id=0)
        self.assertEqual(out, [])

    def test_driver_error_returns_empty(self):
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.driver_error("boom", [])
        with patch.object(bot_fiber, "capture_chat_messages", return_value=canned):
            out = bot.read_chat_messages(_SentinelDriver(), bot_id=0)
        self.assertEqual(out, [])

    def test_no_dom_fallback_on_unsupported(self):
        """SentinelDriver.execute_script raises AssertionError; if the
        fiber_only path leaks a DOM fallback, the test fails loudly."""
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.unsupported("nope", [])
        with patch.object(bot_fiber, "capture_chat_messages", return_value=canned):
            # Should not raise.
            bot.read_chat_messages(_SentinelDriver(), bot_id=0)


# ── Fiber-only: get_participant_count ────────────────────────────────────


class TestGetParticipantCountFiberOnly(_FiberWiringBase):

    def test_ok_returns_len(self):
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.ok_result(
            [{"displayName": "a"}, {"displayName": "b"}, {"displayName": "c"}]
        )
        with patch.object(bot_fiber, "capture_participants", return_value=canned):
            self.assertEqual(bot.get_participant_count(_SentinelDriver()), 3)

    def test_empty_returns_zero(self):
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.empty([])
        with patch.object(bot_fiber, "capture_participants", return_value=canned):
            self.assertEqual(bot.get_participant_count(_SentinelDriver()), 0)

    def test_driver_error_returns_zero(self):
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.driver_error("driver died", [])
        with patch.object(bot_fiber, "capture_participants", return_value=canned):
            self.assertEqual(bot.get_participant_count(_SentinelDriver()), 0)


# ── Fiber-only: get_participants ─────────────────────────────────────────


class TestGetParticipantsFiberOnly(_FiberWiringBase):

    def test_ok_maps_to_legacy_shape(self):
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.ok_result([
            {
                "displayName": "Alice",
                "userId": "1",
                "persistentId": "p1",
                "role": "host",
                "isHost": True,
                "isCoHost": False,
                "isMe": False,
                "inWaitingRoom": False,
                "isMuted": True,
                "isVideoOn": True,
                "isSharing": False,
                "isHandRaised": False,
                "rawKeys": [],
            },
            {
                "displayName": "Bob",
                "role": "participant",
                "isMe": True,
                "isMuted": False,
                "isVideoOn": None,   # unknown video state
                "isHandRaised": True,
            },
        ])
        with patch.object(bot_fiber, "capture_participants", return_value=canned):
            out = bot.get_participants(_SentinelDriver(), bot_id=0)
        self.assertEqual(out, [
            {"name": "Alice", "role": "host", "isSelf": False,
             "videoOff": False, "audioMuted": True, "handRaised": False,
             "_source": "fiber"},
            {"name": "Bob", "role": "participant", "isSelf": True,
             "videoOff": False, "audioMuted": False, "handRaised": True,
             "_source": "fiber"},
        ])

    def test_drops_nameless_entries(self):
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.ok_result([
            {"displayName": "", "role": "participant"},
            {"displayName": "Alice", "role": "participant"},
        ])
        with patch.object(bot_fiber, "capture_participants", return_value=canned):
            out = bot.get_participants(_SentinelDriver(), bot_id=0)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "Alice")

    def test_unsupported_returns_empty_list(self):
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.unsupported("nope", [])
        with patch.object(bot_fiber, "capture_participants", return_value=canned):
            self.assertEqual(bot.get_participants(_SentinelDriver(), bot_id=0), [])

    def test_no_dom_fallback_on_empty(self):
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.empty([])
        with patch.object(bot_fiber, "capture_participants", return_value=canned):
            # SentinelDriver.execute_script would raise — must not be called.
            bot.get_participants(_SentinelDriver(), bot_id=0)


# ── Fiber-only: check_bot_alive ──────────────────────────────────────────


class TestCheckBotAliveFiberOnly(_FiberWiringBase):

    def _driver(self, url="https://zoom.us/wc/123/start"):
        return _SentinelDriver(current_url=url)

    def test_meeting_ended_returns_false(self):
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.ok_result({
            "inMeeting": False,
            "inWaitingRoom": False,
            "meetingEnded": True,
            "leaveButtonVisible": None,
            "errorCode": None,
            "rawKeys": [],
        })
        with patch.object(bot_fiber, "capture_meeting_state", return_value=canned):
            self.assertFalse(bot.check_bot_alive(self._driver()))

    def test_in_meeting_returns_true(self):
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.ok_result({
            "inMeeting": True,
            "inWaitingRoom": False,
            "meetingEnded": False,
        })
        with patch.object(bot_fiber, "capture_meeting_state", return_value=canned):
            self.assertTrue(bot.check_bot_alive(self._driver()))

    def test_in_waiting_room_returns_true(self):
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.ok_result({
            "inMeeting": False,
            "inWaitingRoom": True,
            "meetingEnded": False,
        })
        with patch.object(bot_fiber, "capture_meeting_state", return_value=canned):
            self.assertTrue(bot.check_bot_alive(self._driver()))

    def test_url_off_zoom_returns_false_without_calling_fiber(self):
        self._set_mode("fiber_only")
        with patch.object(bot_fiber, "capture_meeting_state") as cm:
            self.assertFalse(bot.check_bot_alive(self._driver(url="https://example.com/")))
            cm.assert_not_called()

    def test_unsupported_keeps_alive_when_url_still_zoom(self):
        """UNSUPPORTED + URL still on zoom.us → treat as alive (callers
        re-probe next tick). No DOM scrape allowed."""
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.unsupported("no fiber state", {})
        with patch.object(bot_fiber, "capture_meeting_state", return_value=canned):
            self.assertTrue(bot.check_bot_alive(self._driver()))

    def test_deadline_keeps_alive_when_url_still_zoom(self):
        self._set_mode("fiber_only")
        canned = bot_fiber.FiberResult.deadline({})
        with patch.object(bot_fiber, "capture_meeting_state", return_value=canned):
            self.assertTrue(bot.check_bot_alive(self._driver()))


# ── Mode predicate ───────────────────────────────────────────────────────


class TestModePredicate(_FiberWiringBase):

    def test_hybrid_default_predicate(self):
        self._set_mode("hybrid")
        self.assertFalse(bot._is_fiber_only_mode())

    def test_fiber_only_predicate(self):
        self._set_mode("fiber_only")
        self.assertTrue(bot._is_fiber_only_mode())


if __name__ == "__main__":
    unittest.main()

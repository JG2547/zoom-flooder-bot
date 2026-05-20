# -*- coding: utf-8 -*-

"""Unit tests for the fiber detector adapter.

Stdlib-only. No Selenium, no Chrome, no live Zoom required — the tests
drive ``bot_fiber`` with a ``FakeDriver`` whose ``execute_script``
returns canned payloads. Run with:

    python -m unittest discover -s tests -p "test_bot_fiber.py"
"""

from __future__ import annotations

import os
import sys
import unittest

# Make the project root importable without installing the package.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from bot_fiber import (  # noqa: E402
    FiberOutcome,
    FiberResult,
    capture_participants,
    capture_chat_messages,
    capture_meeting_state,
    _FIBER_PARTICIPANTS_JS,
    _FIBER_CHAT_JS,
    _FIBER_MEETING_STATE_JS,
)


# ── Fake driver ──────────────────────────────────────────────────────────


class _FakeDriver:
    """Minimal Selenium-driver lookalike for unit tests.

    The ``execute_script`` contract is exactly what real Selenium gives
    us back: whatever the IIFE in ``js`` returns. We don't run the JS —
    we inject a canned return value instead.
    """

    def __init__(self, *, returns=None, raises=None):
        self.returns = returns
        self.raises = raises
        self.last_js = None
        self.last_args = None
        self.call_count = 0

    def execute_script(self, js, *args):
        self.last_js = js
        self.last_args = args
        self.call_count += 1
        if self.raises is not None:
            raise self.raises
        return self.returns


# ── FiberOutcome / FiberResult basics ────────────────────────────────────


class TestFiberOutcomeValues(unittest.TestCase):
    def test_six_outcomes_present(self):
        self.assertEqual(
            {o.value for o in FiberOutcome},
            {"OK", "EMPTY", "DEADLINE_EXCEEDED", "PARSE_ERROR",
             "DRIVER_ERROR", "UNSUPPORTED"},
        )

    def test_str_compatibility(self):
        # Inheriting from str lets the enum value travel as plain text.
        self.assertEqual(str(FiberOutcome.OK.value), "OK")


class TestFiberResultSemantics(unittest.TestCase):
    def test_ok_result_is_ok(self):
        r = FiberResult.ok_result([1, 2, 3], elapsed_ms=42)
        self.assertTrue(r.is_ok)
        self.assertTrue(r.ok)
        self.assertEqual(r.outcome, FiberOutcome.OK)
        self.assertEqual(r.data, [1, 2, 3])
        self.assertEqual(r.elapsed_ms, 42)
        self.assertFalse(r.is_terminal_error)

    def test_empty_result_not_ok(self):
        r = FiberResult.empty([])
        self.assertFalse(r.is_ok)
        self.assertFalse(r.ok)
        self.assertEqual(r.outcome, FiberOutcome.EMPTY)
        self.assertEqual(r.data, [])

    def test_deadline_keeps_default_shape(self):
        r = FiberResult.deadline([])
        self.assertEqual(r.outcome, FiberOutcome.DEADLINE_EXCEEDED)
        self.assertEqual(r.data, [])

    def test_driver_error_truncates(self):
        long = "x" * 500
        r = FiberResult.driver_error(long, [])
        self.assertEqual(r.outcome, FiberOutcome.DRIVER_ERROR)
        self.assertTrue(r.is_terminal_error)
        self.assertLessEqual(len(r.error or ""), 200)

    def test_parse_error_truncates(self):
        r = FiberResult.parse_error("y" * 500, {})
        self.assertEqual(r.outcome, FiberOutcome.PARSE_ERROR)
        self.assertLessEqual(len(r.error or ""), 200)

    def test_unsupported_carries_reason(self):
        r = FiberResult.unsupported("no fiber state object found", {})
        self.assertEqual(r.outcome, FiberOutcome.UNSUPPORTED)
        self.assertIn("no fiber", (r.error or ""))


# ── JS payloads — string sanity ──────────────────────────────────────────


class TestJsPayloads(unittest.TestCase):
    """Payloads aren't run here — just sanity-checked as strings."""

    def test_participants_payload_uses_arguments(self):
        self.assertIn("arguments[0]", _FIBER_PARTICIPANTS_JS)
        self.assertIn("__reactFiber$", _FIBER_PARTICIPANTS_JS)
        self.assertIn("outcome: \"OK\"", _FIBER_PARTICIPANTS_JS)
        self.assertIn("DEADLINE_EXCEEDED", _FIBER_PARTICIPANTS_JS)

    def test_chat_payload_has_no_dom_scrape_fallback(self):
        self.assertIn("arguments[0]", _FIBER_CHAT_JS)
        # No use of innerText / textContent walks here — fiber only.
        self.assertNotIn(".innerText", _FIBER_CHAT_JS)
        self.assertNotIn(".textContent", _FIBER_CHAT_JS)

    def test_meeting_state_payload_supports_unsupported(self):
        self.assertIn("UNSUPPORTED", _FIBER_MEETING_STATE_JS)
        self.assertIn("arguments[0]", _FIBER_MEETING_STATE_JS)


# ── Wrapper: participants ────────────────────────────────────────────────


class TestCaptureParticipants(unittest.TestCase):
    def test_ok_passes_through_data(self):
        canned = {
            "outcome": "OK",
            "data": [
                {"displayName": "Alice", "userId": "1"},
                {"displayName": "Bob",   "userId": "2"},
            ],
            "elapsedMs": 17,
        }
        d = _FakeDriver(returns=canned)
        r = capture_participants(d, timeout_ms=100)
        self.assertEqual(r.outcome, FiberOutcome.OK)
        self.assertTrue(r.is_ok)
        self.assertEqual(len(r.data), 2)
        self.assertEqual(r.data[0]["displayName"], "Alice")
        self.assertEqual(r.elapsed_ms, 17)
        # Wrapper must have passed the clamped timeout into the JS.
        self.assertEqual(d.last_args, (100,))

    def test_timeout_clamped_low(self):
        d = _FakeDriver(returns={"outcome": "EMPTY", "data": [], "elapsedMs": 0})
        capture_participants(d, timeout_ms=1)
        self.assertEqual(d.last_args, (20,))  # clamped to floor

    def test_timeout_clamped_high(self):
        d = _FakeDriver(returns={"outcome": "EMPTY", "data": [], "elapsedMs": 0})
        capture_participants(d, timeout_ms=10_000)
        self.assertEqual(d.last_args, (2000,))  # clamped to ceiling

    def test_empty_returns_empty_list_data(self):
        d = _FakeDriver(returns={"outcome": "EMPTY", "data": [], "elapsedMs": 5})
        r = capture_participants(d)
        self.assertEqual(r.outcome, FiberOutcome.EMPTY)
        self.assertFalse(r.ok)
        self.assertEqual(r.data, [])

    def test_deadline_maps_through(self):
        d = _FakeDriver(returns={"outcome": "DEADLINE_EXCEEDED", "data": [], "elapsedMs": 100})
        r = capture_participants(d)
        self.assertEqual(r.outcome, FiberOutcome.DEADLINE_EXCEEDED)
        self.assertEqual(r.data, [])

    def test_driver_exception_maps_to_driver_error(self):
        d = _FakeDriver(raises=RuntimeError("WebDriverException simulated"))
        r = capture_participants(d)
        self.assertEqual(r.outcome, FiberOutcome.DRIVER_ERROR)
        self.assertTrue(r.is_terminal_error)
        self.assertIn("WebDriverException", r.error or "")

    def test_malformed_top_level_maps_to_parse_error(self):
        d = _FakeDriver(returns=["not", "a", "dict"])
        r = capture_participants(d)
        self.assertEqual(r.outcome, FiberOutcome.PARSE_ERROR)

    def test_unknown_outcome_maps_to_parse_error(self):
        d = _FakeDriver(returns={"outcome": "WAT", "data": []})
        r = capture_participants(d)
        self.assertEqual(r.outcome, FiberOutcome.PARSE_ERROR)

    def test_ok_with_non_list_data_maps_to_parse_error(self):
        d = _FakeDriver(returns={"outcome": "OK", "data": "not-a-list"})
        r = capture_participants(d)
        self.assertEqual(r.outcome, FiberOutcome.PARSE_ERROR)

    def test_none_return_maps_to_parse_error(self):
        d = _FakeDriver(returns=None)
        r = capture_participants(d)
        self.assertEqual(r.outcome, FiberOutcome.PARSE_ERROR)


# ── Wrapper: chat ────────────────────────────────────────────────────────


class TestCaptureChatMessages(unittest.TestCase):
    def test_ok_passes_through(self):
        canned = {
            "outcome": "OK",
            "data": [{"sender": "Alice", "text": "hello", "timestamp": 0, "messageId": "1"}],
            "elapsedMs": 9,
        }
        d = _FakeDriver(returns=canned)
        r = capture_chat_messages(d)
        self.assertTrue(r.is_ok)
        self.assertEqual(r.data[0]["text"], "hello")

    def test_empty_default_shape_is_list(self):
        d = _FakeDriver(returns={"outcome": "EMPTY", "data": None, "elapsedMs": 3})
        r = capture_chat_messages(d)
        # JS handed back None for data; wrapper must substitute the default shape.
        self.assertEqual(r.data, [])


# ── Wrapper: meeting state ───────────────────────────────────────────────


class TestCaptureMeetingState(unittest.TestCase):
    def test_ok_returns_dict_payload(self):
        canned = {
            "outcome": "OK",
            "data": {
                "inMeeting": True,
                "inWaitingRoom": False,
                "meetingEnded": False,
                "leaveButtonVisible": None,
                "errorCode": None,
                "rawKeys": ["meetingNumber", "meetingStatus"],
            },
            "elapsedMs": 12,
        }
        d = _FakeDriver(returns=canned)
        r = capture_meeting_state(d)
        self.assertEqual(r.outcome, FiberOutcome.OK)
        self.assertTrue(r.data["inMeeting"])
        self.assertIn("meetingNumber", r.data["rawKeys"])

    def test_unsupported_passes_through_with_default_dict(self):
        d = _FakeDriver(returns={"outcome": "UNSUPPORTED", "data": None,
                                 "elapsedMs": 80, "error": "no fiber state"})
        r = capture_meeting_state(d)
        self.assertEqual(r.outcome, FiberOutcome.UNSUPPORTED)
        self.assertEqual(r.data, {})
        self.assertIn("no fiber state", r.error or "")

    def test_ok_with_non_dict_data_maps_to_parse_error(self):
        d = _FakeDriver(returns={"outcome": "OK", "data": ["wrong", "shape"]})
        r = capture_meeting_state(d)
        self.assertEqual(r.outcome, FiberOutcome.PARSE_ERROR)


# ── Caller-flip status ───────────────────────────────────────────────────


class TestCallerWiringPresent(unittest.TestCase):
    """Phase 3 wires bot.py callers through bot_fiber behind DETECTION_MODE.

    Phase 2 asserted the opposite invariant (no import yet). After Phase 3
    the wiring import MUST be present so the four detection callers can
    route through the fiber adapter.
    """

    def test_bot_py_imports_bot_fiber(self):
        with open(os.path.join(_ROOT, "bot.py"), "r", encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("import bot_fiber", src)


if __name__ == "__main__":
    unittest.main()

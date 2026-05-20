# -*- coding: utf-8 -*-

"""Phase 5 waiting-room fiber conversion tests.

Verifies that ``bot._in_waiting_room(driver)`` reads only from
``bot_fiber.capture_meeting_state`` and never touches DOM selectors.
Stdlib-only. Uses a ``_SentinelDriver`` whose ``find_element*`` /
``execute_script`` raise ``AssertionError`` if a DOM probe leaks back
into the fiberized waiting-room path.
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


try:
    import bot
    import bot_fiber
    _IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    bot = None
    bot_fiber = None
    _IMPORT_ERROR = exc


def _load_fixture(name):
    with open(os.path.join(_FIXTURES, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def _fiber_result_from_fixture(fixture):
    outcome = bot_fiber.FiberOutcome(fixture["outcome"])
    return bot_fiber.FiberResult(
        outcome=outcome,
        ok=(outcome is bot_fiber.FiberOutcome.OK),
        data=fixture.get("data"),
        error=fixture.get("error"),
        elapsed_ms=int(fixture.get("elapsedMs") or 0),
        source="fiber",
    )


class _SentinelDriver:
    """No-op driver that fails loud on any DOM/legacy access."""

    def __init__(self, current_url="https://zoom.us/wc/123/start"):
        self.current_url = current_url

    def execute_script(self, *a, **k):
        raise AssertionError("execute_script called — fiber-only WR path leaked DOM")

    def find_element(self, *a, **k):
        raise AssertionError("find_element called — WR DOM probe leaked")

    def find_elements(self, *a, **k):
        raise AssertionError("find_elements called — WR DOM probe leaked")

    def __getattr__(self, name):
        if name.startswith("find_element_by_") or name.startswith("find_elements_by_"):
            raise AssertionError("driver." + name + " called — WR DOM probe leaked")
        raise AttributeError(name)


@unittest.skipIf(_IMPORT_ERROR is not None,
                 f"bot not importable: {_IMPORT_ERROR!r}")
class TestInWaitingRoomHelper(unittest.TestCase):

    def test_returns_true_when_fiber_says_in_wr(self):
        canned = _fiber_result_from_fixture(_load_fixture("fiber_meeting_state_waiting_room.json"))
        with patch.object(bot_fiber, "capture_meeting_state", return_value=canned) as cm:
            out = bot._in_waiting_room(_SentinelDriver())
        cm.assert_called_once()
        self.assertIs(out, True)

    def test_returns_false_when_fiber_says_admitted(self):
        canned = _fiber_result_from_fixture(_load_fixture("fiber_meeting_state_ok.json"))
        with patch.object(bot_fiber, "capture_meeting_state", return_value=canned):
            out = bot._in_waiting_room(_SentinelDriver())
        self.assertIs(out, False)

    def test_returns_false_when_fiber_says_ended(self):
        canned = _fiber_result_from_fixture(_load_fixture("fiber_meeting_state_ended.json"))
        with patch.object(bot_fiber, "capture_meeting_state", return_value=canned):
            out = bot._in_waiting_room(_SentinelDriver())
        self.assertIs(out, False)

    def test_returns_none_on_unsupported(self):
        canned = bot_fiber.FiberResult.unsupported("no fiber state", {})
        with patch.object(bot_fiber, "capture_meeting_state", return_value=canned):
            out = bot._in_waiting_room(_SentinelDriver())
        self.assertIsNone(out)

    def test_returns_none_on_empty(self):
        canned = bot_fiber.FiberResult.empty({})
        with patch.object(bot_fiber, "capture_meeting_state", return_value=canned):
            out = bot._in_waiting_room(_SentinelDriver())
        self.assertIsNone(out)

    def test_returns_none_on_deadline_exceeded(self):
        canned = bot_fiber.FiberResult.deadline({})
        with patch.object(bot_fiber, "capture_meeting_state", return_value=canned):
            out = bot._in_waiting_room(_SentinelDriver())
        self.assertIsNone(out)

    def test_returns_none_on_driver_error(self):
        canned = bot_fiber.FiberResult.driver_error("WebDriverException", {})
        with patch.object(bot_fiber, "capture_meeting_state", return_value=canned):
            out = bot._in_waiting_room(_SentinelDriver())
        self.assertIsNone(out)

    def test_returns_false_when_data_missing_wr_field(self):
        """OK fiber result with no ``inWaitingRoom`` key should be
        treated as 'not in WR' rather than unknown. The fiber walker
        always emits the key; a missing key means the meeting-state
        object surfaced but did not flag WR."""
        canned = bot_fiber.FiberResult.ok_result({"inMeeting": True})
        with patch.object(bot_fiber, "capture_meeting_state", return_value=canned):
            out = bot._in_waiting_room(_SentinelDriver())
        self.assertIs(out, False)

    def test_never_calls_dom_selectors(self):
        """The sentinel driver raises AssertionError on any DOM access.
        If this test passes, no DOM probe was attempted."""
        canned = _fiber_result_from_fixture(_load_fixture("fiber_meeting_state_waiting_room.json"))
        with patch.object(bot_fiber, "capture_meeting_state", return_value=canned):
            # Multiple successive calls — would expose any caching/
            # fallback that touched DOM.
            for _ in range(3):
                bot._in_waiting_room(_SentinelDriver())


@unittest.skipIf(_IMPORT_ERROR is not None,
                 f"bot not importable: {_IMPORT_ERROR!r}")
class TestWaitingRoomLegacySourceAbsent(unittest.TestCase):
    """Phase 5 removed the legacy XPATH list. Re-introducing it would
    silently re-enable DOM detection."""

    def test_wr_selectors_list_absent(self):
        with open(os.path.join(_ROOT, "bot.py"), "r", encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("_WAITING_ROOM_SELECTORS = [", src)
        self.assertNotIn("'host will admit you'", src)
        self.assertNotIn("'Waiting for the host'", src)
        self.assertNotIn("'will let you in soon'", src)
        self.assertNotIn("Please wait, the meeting host", src)

    def test_in_waiting_room_helper_present(self):
        with open(os.path.join(_ROOT, "bot.py"), "r", encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("def _in_waiting_room(driver)", src)

    def test_join_flow_uses_helper_not_legacy_list(self):
        """Both join-flow call sites should call the helper, not
        `_find_element_multi(driver, _WAITING_ROOM_SELECTORS)`."""
        with open(os.path.join(_ROOT, "bot.py"), "r", encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("_find_element_multi(driver, _WAITING_ROOM_SELECTORS)", src)
        # Helper is called at least twice (initial probe + poll loop body).
        self.assertGreaterEqual(src.count("_in_waiting_room(driver)"), 2)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-

"""Tests for ``tools/fiber_only_self_test.py``.

Stdlib-only. Drives the self-test programmatically, including failure
injection so the exit-nonzero path is exercised. Verifies the
sanitized report never embeds a meeting-ID-shaped numeric run.

Run:

    python -m unittest discover -s tests -p "test_*self_test*.py" -v

The shipping fixtures intentionally avoid containing any 9-12 digit
number to keep the regex-based redactor from triggering on benign
data; this test asserts that property still holds.
"""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


try:
    from tools import fiber_only_self_test as st
    import bot_fiber
    import config
    _IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    st = None
    bot_fiber = None
    config = None
    _IMPORT_ERROR = exc


_NUMERIC_RUN = re.compile(r"\b\d{9,12}\b")
_SECRET_PATTERNS = re.compile(
    r"(BOT_TOKEN=|TELEGRAM_TOKEN=|DISCORD_TOKEN=|ZOOM_SECRET=|"
    r"ZOOM_SDK_SECRET=|postgres://|redis://|Bearer [A-Za-z0-9._-]{20,}|"
    r"ghp_|ghs_|\d{9}:AA[A-Za-z0-9_-]{20,})"
)


@unittest.skipIf(_IMPORT_ERROR is not None,
                 f"tools.fiber_only_self_test not importable: {_IMPORT_ERROR!r}")
class TestSelfTestRunner(unittest.TestCase):

    def _orig_mode(self):
        # Snapshot to verify the runner restores it.
        return config.DETECTION_MODE

    def test_main_exit_zero_on_full_pass(self):
        prev = self._orig_mode()
        rc = st.main(["--quiet"])
        self.assertEqual(rc, 0)
        # Runner must restore DETECTION_MODE on the way out.
        self.assertEqual(config.DETECTION_MODE, prev)

    def test_default_remains_hybrid_after_run(self):
        # The on-disk default is hybrid; after the runner finishes the
        # in-memory module-level value should match that default.
        st.main(["--quiet"])
        self.assertEqual(config.DETECTION_MODE, "hybrid")

    def test_summary_prints_pass_line(self):
        buf = io.StringIO()
        checks, all_passed = st.run_self_test()
        st.print_summary(checks, all_passed, stream=buf)
        text = buf.getvalue()
        self.assertIn("PASS", text)
        self.assertIn("checks passed", text)

    def test_write_report_to_temp_dir(self):
        # Monkeypatch the self-test module's _ROOT so reports land in a
        # tempdir for the test. This avoids polluting the repo.
        with tempfile.TemporaryDirectory() as td:
            with patch.object(st, "_ROOT", td):
                checks, all_passed = st.run_self_test()
                path = st.write_report(checks, all_passed)
            self.assertTrue(os.path.isfile(path))
            self.assertTrue(path.startswith(td))
            with open(path, "r", encoding="utf-8") as fh:
                report = fh.read()
            self.assertIn("Fiber-only self-test report", report)
            self.assertIn("PASS", report)
            # No meeting-ID-shaped numerics, no token patterns.
            self.assertIsNone(_NUMERIC_RUN.search(report),
                              f"report leaked a numeric run: {report!r}")
            self.assertIsNone(_SECRET_PATTERNS.search(report),
                              "report leaked a secret-shaped string")

    def test_failure_injection_exits_nonzero(self):
        """Inject a fixture-load failure so the participants/chat checks fail.

        Each check patches capture_* at its own scope, so we can't fail
        the runner from outside the checks via patching capture_*.
        Instead we break ``load_fixture`` for the fixtures the checks
        depend on; the wrappers then catch the exception and the check
        records a FAIL.
        """
        original_load = st.load_fixture

        def _broken_load(name):
            if "participants" in name or "chat" in name:
                raise RuntimeError("simulated fixture load failure")
            return original_load(name)

        with patch.object(st, "load_fixture", _broken_load):
            checks, all_passed = st.run_self_test()
        self.assertFalse(all_passed)
        failed_names = {c.name for c in checks if not c.passed}
        self.assertIn("get_participant_count.OK", failed_names)
        self.assertIn("get_participants.OK", failed_names)
        self.assertIn("read_chat_messages.OK", failed_names)

    def test_failure_injection_main_returns_nonzero(self):
        """Same idea but via the ``main()`` entrypoint — must exit 1."""
        original_load = st.load_fixture

        def _broken_load(name):
            if "participants" in name:
                raise RuntimeError("simulated load failure")
            return original_load(name)

        with patch.object(st, "load_fixture", _broken_load):
            rc = st.main(["--quiet"])
        self.assertEqual(rc, 1)

    def test_sanitize_strips_long_digit_runs(self):
        sanitized = st._sanitize("meeting 1234567890 ended")
        self.assertNotIn("1234567890", sanitized)
        self.assertIn("[REDACTED_NUMERIC]", sanitized)

    def test_sanitize_strips_bearer_token(self):
        sample = "Authorization: Bearer abcdef0123456789ABCDEFghijklmnop"
        sanitized = st._sanitize(sample)
        self.assertNotIn("abcdef0123456789ABCDEFghijklmnop", sanitized)
        self.assertIn("[REDACTED_TOKEN]", sanitized)

    def test_sanitize_preserves_safe_text(self):
        text = "all good — no secrets here, just 42 things"
        self.assertEqual(st._sanitize(text), text)

    def test_fake_driver_raises_legacy_dom_leak(self):
        d = st.FakeFiberDriver()
        with self.assertRaises(st.LegacyDomLeak):
            d.execute_script("noop")
        with self.assertRaises(st.LegacyDomLeak):
            d.find_element("by", "x")
        with self.assertRaises(st.LegacyDomLeak):
            d.find_elements("by", "x")
        with self.assertRaises(st.LegacyDomLeak):
            d.find_element_by_id("foo")

    def test_fixture_load_helpers(self):
        fx = st.load_fixture("fiber_chat_ok.json")
        self.assertEqual(fx["outcome"], "OK")
        fr = st.fixture_to_fiber_result(bot_fiber, fx)
        self.assertEqual(fr.outcome, bot_fiber.FiberOutcome.OK)
        self.assertTrue(fr.is_ok)


if __name__ == "__main__":
    unittest.main()

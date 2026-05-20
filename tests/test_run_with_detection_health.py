# -*- coding: utf-8 -*-

"""Tests for ``tools/run_with_detection_health.py``.

Stdlib-only. The dashboard subprocess is never actually started — a
fake ``runner`` is injected so tests verify the launcher's wiring
without binding ports or starting `web_app.py`.
"""

from __future__ import annotations

import io
import os
import sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


try:
    from tools import run_with_detection_health as launcher
    from tools import fiber_only_self_test as st
    _IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    launcher = None
    st = None
    _IMPORT_ERROR = exc


@unittest.skipIf(_IMPORT_ERROR is not None,
                 f"launcher not importable: {_IMPORT_ERROR!r}")
class TestRunHealthCheck(unittest.TestCase):

    def test_health_check_passes_on_clean_tree(self):
        buf = io.StringIO()
        ok = launcher.run_health_check(stream=buf)
        self.assertTrue(ok)
        self.assertIn("PASS", buf.getvalue())

    def test_health_check_reports_fail_when_self_test_fails(self):
        # Inject a failing fixture-load so run_self_test reports
        # failing checks. The launcher should print FAIL and return
        # False without crashing.
        original_load = st.load_fixture

        def _broken_load(name):
            if "participants" in name:
                raise RuntimeError("simulated load failure")
            return original_load(name)

        buf = io.StringIO()
        with patch.object(st, "load_fixture", _broken_load):
            ok = launcher.run_health_check(stream=buf)
        self.assertFalse(ok)
        text = buf.getvalue()
        self.assertIn("FAIL", text)
        self.assertIn("get_participants.OK", text)


@unittest.skipIf(_IMPORT_ERROR is not None,
                 f"launcher not importable: {_IMPORT_ERROR!r}")
class TestChildEnvBuilder(unittest.TestCase):

    def test_default_mode_scrubs_detection_mode(self):
        with patch.dict(os.environ, {"DETECTION_MODE": "hybrid", "OTHER": "x"}, clear=False):
            env = launcher._build_child_env("default")
        self.assertNotIn("DETECTION_MODE", env)
        self.assertEqual(env.get("OTHER"), "x")

    def test_default_mode_when_no_env_var_set(self):
        # The launcher tolerates the absence of DETECTION_MODE.
        env_clean = {k: v for k, v in os.environ.items() if k != "DETECTION_MODE"}
        with patch.dict(os.environ, env_clean, clear=True):
            env = launcher._build_child_env("default")
        self.assertNotIn("DETECTION_MODE", env)

    def test_hybrid_mode_sets_env_in_child_only(self):
        original_outer = os.environ.get("DETECTION_MODE")
        with patch.dict(os.environ, {"DETECTION_MODE": "fiber_only"}, clear=False):
            env = launcher._build_child_env("hybrid")
            self.assertEqual(env["DETECTION_MODE"], "hybrid")
            # Outer env was NOT mutated.
            self.assertEqual(os.environ["DETECTION_MODE"], "fiber_only")
        # And after the patch exits, the outer var is whatever it was.
        self.assertEqual(os.environ.get("DETECTION_MODE"), original_outer)

    def test_unsupported_mode_raises(self):
        with self.assertRaises(ValueError):
            launcher._build_child_env("wat")


@unittest.skipIf(_IMPORT_ERROR is not None,
                 f"launcher not importable: {_IMPORT_ERROR!r}")
class TestMainCli(unittest.TestCase):

    class _FakeRunResult:
        def __init__(self, returncode=0):
            self.returncode = returncode

    def _runner(self, *, captured, returncode=0):
        """Build a fake runner that records the call args + env."""
        def _impl(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = kwargs.get("cwd")
            captured["env"] = kwargs.get("env")
            return self._FakeRunResult(returncode=returncode)
        return _impl

    def test_no_start_returns_zero_on_pass_without_spawning_dashboard(self):
        captured = {}
        buf = io.StringIO()
        runner = self._runner(captured=captured)
        rc = launcher.main(["--no-start"], runner=runner, stream=buf)
        self.assertEqual(rc, 0)
        self.assertNotIn("cmd", captured)  # dashboard NOT spawned
        self.assertIn("PASS", buf.getvalue())

    def test_no_start_returns_one_when_health_fails(self):
        captured = {}
        buf = io.StringIO()
        original_load = st.load_fixture

        def _broken_load(name):
            if "participants" in name:
                raise RuntimeError("simulated")
            return original_load(name)

        runner = self._runner(captured=captured)
        with patch.object(st, "load_fixture", _broken_load):
            rc = launcher.main(["--no-start"], runner=runner, stream=buf)
        self.assertEqual(rc, 1)
        self.assertNotIn("cmd", captured)

    def test_default_mode_spawns_dashboard_with_detection_mode_scrubbed(self):
        captured = {}
        buf = io.StringIO()
        runner = self._runner(captured=captured)
        with patch.dict(os.environ, {"DETECTION_MODE": "hybrid"}, clear=False):
            rc = launcher.main(["--mode", "default"], runner=runner, stream=buf)
        self.assertEqual(rc, 0)
        self.assertIn("cmd", captured)
        self.assertEqual(captured["cmd"][1], "web_app.py")
        self.assertNotIn("DETECTION_MODE", captured["env"])
        self.assertIn("on-disk default", buf.getvalue())

    def test_hybrid_mode_spawns_dashboard_with_env_override(self):
        captured = {}
        buf = io.StringIO()
        runner = self._runner(captured=captured)
        rc = launcher.main(["--mode", "hybrid"], runner=runner, stream=buf)
        self.assertEqual(rc, 0)
        self.assertEqual(captured["env"]["DETECTION_MODE"], "hybrid")
        self.assertIn("hybrid", buf.getvalue())
        self.assertIn("rollback", buf.getvalue())

    def test_force_starts_even_when_health_fails(self):
        captured = {}
        buf = io.StringIO()
        original_load = st.load_fixture

        def _broken_load(name):
            if "participants" in name:
                raise RuntimeError("simulated")
            return original_load(name)

        runner = self._runner(captured=captured)
        with patch.object(st, "load_fixture", _broken_load):
            rc = launcher.main(
                ["--mode", "default", "--force"],
                runner=runner, stream=buf,
            )
        self.assertEqual(rc, 0)
        self.assertIn("cmd", captured)
        self.assertIn("--force overriding failed health gate", buf.getvalue())

    def test_no_health_failure_aborts_without_force(self):
        captured = {}
        buf = io.StringIO()
        original_load = st.load_fixture

        def _broken_load(name):
            if "participants" in name:
                raise RuntimeError("simulated")
            return original_load(name)

        runner = self._runner(captured=captured)
        with patch.object(st, "load_fixture", _broken_load):
            rc = launcher.main(
                ["--mode", "default"],
                runner=runner, stream=buf,
            )
        self.assertEqual(rc, 1)
        self.assertNotIn("cmd", captured)
        self.assertIn("health gate failed", buf.getvalue())

    def test_subprocess_returncode_propagates(self):
        """If the dashboard subprocess exits nonzero, the launcher
        returns that exit code (not 0)."""
        captured = {}
        buf = io.StringIO()
        runner = self._runner(captured=captured, returncode=42)
        rc = launcher.main(["--mode", "default"], runner=runner, stream=buf)
        self.assertEqual(rc, 42)

    def test_no_env_value_in_output(self):
        """The launcher must never echo back env values from the
        operator's shell, even when they appear in the child env."""
        captured = {}
        buf = io.StringIO()
        runner = self._runner(captured=captured)
        # Use distinct sentinels that the launcher's stock messages
        # would never contain. ``127.0.0.1`` and similar literals
        # appear in the static help text (documented default bind),
        # so we don't assert against those — only against sentinels
        # that *only* exist inside the env.
        with patch.dict(os.environ, {
            "DASHBOARD_HOST": "SENTINEL_HOST_VALUE_XYZ",
            "DASHBOARD_CORS_ORIGINS": "SENTINEL_CORS_VALUE_QWE",
        }, clear=False):
            launcher.main(["--mode", "default"], runner=runner, stream=buf)
        text = buf.getvalue()
        self.assertNotIn("SENTINEL_HOST_VALUE_XYZ", text)
        self.assertNotIn("SENTINEL_CORS_VALUE_QWE", text)
        # The env values were passed through to the child runner, not
        # printed.
        self.assertEqual(captured["env"]["DASHBOARD_HOST"],
                         "SENTINEL_HOST_VALUE_XYZ")
        self.assertEqual(captured["env"]["DASHBOARD_CORS_ORIGINS"],
                         "SENTINEL_CORS_VALUE_QWE")


@unittest.skipIf(_IMPORT_ERROR is not None,
                 f"launcher not importable: {_IMPORT_ERROR!r}")
class TestNoLiveActions(unittest.TestCase):
    """Module-level safety invariants — never join Zoom, never bind ports."""

    def test_module_does_not_import_selenium_at_module_level(self):
        with open(os.path.join(_ROOT, "tools", "run_with_detection_health.py"),
                  "r", encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("import selenium", src)
        self.assertNotIn("from selenium", src)

    def test_module_does_not_call_socketio_run(self):
        with open(os.path.join(_ROOT, "tools", "run_with_detection_health.py"),
                  "r", encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("socketio.run", src)

    def test_module_does_not_invoke_bot_manager_threads(self):
        with open(os.path.join(_ROOT, "tools", "run_with_detection_health.py"),
                  "r", encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("BotManager(", src)
        self.assertNotIn(".start(", src)


if __name__ == "__main__":
    unittest.main()

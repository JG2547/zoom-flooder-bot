# -*- coding: utf-8 -*-

"""Health-gated launcher for the standalone bot dashboard.

Runs the non-live fiber-only self-test first; if it passes, optionally
starts the local dashboard (``web_app.py``) under a chosen
``DETECTION_MODE``. The dashboard binds 127.0.0.1:5000 by default
(per the security hardening in `web_app.py`). No live Zoom action is
ever taken by this script — starting the dashboard only mounts the
Flask app; bots only join Zoom when explicitly triggered through the
dashboard UI / API.

Usage:

    # Health check only — no dashboard started
    python tools/run_with_detection_health.py --no-start

    # Health-gated start in the on-disk default (currently fiber_only)
    python tools/run_with_detection_health.py --mode default

    # Health-gated start with hybrid rollback override (one process only)
    python tools/run_with_detection_health.py --mode hybrid

    # Skip the health gate (NOT recommended; use with --force)
    python tools/run_with_detection_health.py --mode default --force

Exit codes:

- ``0`` — health check passed and (if requested) dashboard started
  cleanly (or, in test/CI, the dashboard subprocess was substituted).
- ``1`` — health check failed; nothing started.
- ``2`` — invalid arguments.

Never prints env values. Never modifies the operator's persistent
shell env. The only env mutation is on the child subprocess.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import List, Optional, Sequence

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ── Health gate ──────────────────────────────────────────────────────────


def run_health_check(stream=sys.stdout) -> bool:
    """Run the non-live self-test in-process. Returns ``True`` on full pass.

    Imports ``tools.fiber_only_self_test`` lazily so an import failure
    surfaces as a clean ``False`` rather than a script-level traceback.
    """
    try:
        from tools import fiber_only_self_test as st
    except Exception as exc:  # noqa: BLE001
        stream.write(f"health: import failed ({exc.__class__.__name__})\n")
        return False
    try:
        checks, all_passed = st.run_self_test()
    except Exception as exc:  # noqa: BLE001
        stream.write(f"health: self-test errored ({exc.__class__.__name__})\n")
        return False
    passed = sum(1 for c in checks if c.passed)
    total = len(checks)
    stream.write(f"health: {passed}/{total} self-test checks "
                 f"{'PASS' if all_passed else 'FAIL'}\n")
    if not all_passed:
        for c in checks:
            if not c.passed:
                detail = (c.error or "").splitlines()[0][:200] if c.error else ""
                suffix = f" — {detail}" if detail else ""
                stream.write(f"  FAIL: {c.name}{suffix}\n")
    return all_passed


# ── Dashboard launch ─────────────────────────────────────────────────────


def _build_child_env(mode: str) -> dict:
    """Construct the env dict passed to the dashboard subprocess.

    ``mode`` controls how ``DETECTION_MODE`` is set in the child:

    - ``"default"``: scrub ``DETECTION_MODE`` from the child env so the
      on-disk default in ``config.py`` is honored (currently
      ``fiber_only``). This is the recommended normal-operation path.
    - ``"hybrid"``: explicitly set ``DETECTION_MODE=hybrid`` in the
      child env only. The operator's persistent shell env is untouched.
    """
    env = {k: v for k, v in os.environ.items()}
    if mode == "default":
        env.pop("DETECTION_MODE", None)
    elif mode == "hybrid":
        env["DETECTION_MODE"] = "hybrid"
    else:
        raise ValueError(f"unsupported mode: {mode!r}")
    return env


def _spawn_dashboard(env: dict, *, runner=None) -> int:
    """Start ``python web_app.py`` with the given env. Blocks until exit.

    ``runner`` is dependency-injected for tests; defaults to
    ``subprocess.run``. Returns the dashboard's exit code.
    """
    runner = runner or subprocess.run
    cmd = [sys.executable, "web_app.py"]
    result = runner(cmd, cwd=_ROOT, env=env)
    return getattr(result, "returncode", 0)


# ── CLI ──────────────────────────────────────────────────────────────────


def main(argv: Optional[Sequence[str]] = None, *,
         runner=None, stream=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Health-gated dashboard launcher. Runs the fiber-only "
            "self-test, then optionally starts python web_app.py "
            "under a chosen DETECTION_MODE."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=("default", "hybrid"),
        default="default",
        help=(
            "default = honor the on-disk DETECTION_MODE default "
            "(currently fiber_only); hybrid = force "
            "DETECTION_MODE=hybrid in the child process only."
        ),
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Run the health check only; do not start the dashboard.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Start the dashboard even if the health gate fails. "
            "NOT recommended — use only when self-test is known broken "
            "for unrelated reasons."
        ),
    )
    args = parser.parse_args(argv)
    out = stream or sys.stdout

    health_ok = run_health_check(stream=out)

    if args.no_start:
        return 0 if health_ok else 1

    if not health_ok and not args.force:
        out.write("aborting: health gate failed (use --force to override)\n")
        return 1

    if not health_ok and args.force:
        out.write("warning: --force overriding failed health gate\n")

    try:
        env = _build_child_env(args.mode)
    except ValueError as exc:
        out.write(f"argument error: {exc}\n")
        return 2

    if args.mode == "default":
        out.write("starting dashboard with on-disk default DETECTION_MODE\n")
    else:
        out.write("starting dashboard with DETECTION_MODE=hybrid (rollback)\n")
    out.write("note: dashboard binds 127.0.0.1:5000 unless DASHBOARD_HOST is set\n")
    out.write("note: bots only join Zoom when triggered through the dashboard\n")

    rc = _spawn_dashboard(env, runner=runner)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

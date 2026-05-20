# -*- coding: utf-8 -*-

"""Non-live fiber-only self-test CLI.

A runnable diagnostic that exercises the four wired detection callers
in ``bot.py`` through ``bot_fiber`` using fake drivers + JSON fixtures.
Stands in for the owned-meeting smoke test when no live Zoom meeting
is available.

Usage:

    python tools/fiber_only_self_test.py
    python tools/fiber_only_self_test.py --write-report

With ``--write-report``, a sanitized Markdown report is written to::

    reports/self_tests/fiber_only_self_test_<YYYYMMDD_HHMMSS>.md

The script:

- Sets ``config.DETECTION_MODE = "fiber_only"`` for the in-process
  test only. The on-disk default in ``config.py`` is NOT touched.
- Restores the prior value on exit.
- Patches ``bot_fiber.capture_*`` to return canned :class:`FiberResult`
  objects derived from the JSON fixtures under ``tests/fixtures/``.
- Calls the actual wired callers ``bot.read_chat_messages``,
  ``bot.get_participant_count``, ``bot.get_participants``,
  ``bot.check_bot_alive``.
- Asserts each return matches the legacy shape and that the fake
  driver's legacy DOM methods are never called.
- Prints a concise sanitized PASS/FAIL summary.
- Exits ``0`` on full pass, ``1`` on any failure.

Stdlib-only. No Selenium. No network. No Zoom. No env values printed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime
from typing import Any, List, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_FIXTURES = os.path.join(_ROOT, "tests", "fixtures")

if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


# ── Late imports gated behind the path setup above ──────────────────────


def _load_modules():
    """Import the bot stack lazily so import errors surface as a check
    failure, not a script-level traceback."""
    import bot as _bot
    import bot_fiber as _bot_fiber
    import config as _config
    return _bot, _bot_fiber, _config


# ── Fake driver ──────────────────────────────────────────────────────────


class LegacyDomLeak(AssertionError):
    """Raised if a legacy DOM selector method is called in fiber-only mode."""


class FakeFiberDriver:
    """Selenium-driver lookalike. ``execute_script`` is never reached
    here — ``capture_*`` are patched at the ``bot_fiber`` boundary so
    the wrappers don't touch the driver. Any DOM selector call raises
    :class:`LegacyDomLeak`.
    """

    def __init__(self, current_url="https://zoom.us/wc/123/start"):
        self.current_url = current_url

    def execute_script(self, *args, **kwargs):
        raise LegacyDomLeak("execute_script called — fiber-only path leaked DOM")

    def find_element(self, *args, **kwargs):
        raise LegacyDomLeak("find_element called — fiber-only path leaked DOM")

    def find_elements(self, *args, **kwargs):
        raise LegacyDomLeak("find_elements called — fiber-only path leaked DOM")

    def __getattr__(self, name):
        if name.startswith("find_element_by_") or name.startswith("find_elements_by_"):
            raise LegacyDomLeak("driver." + name + " called — fiber-only DOM leak")
        raise AttributeError(name)


# ── Fixture loader ───────────────────────────────────────────────────────


def load_fixture(name):
    path = os.path.join(_FIXTURES, name)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def fixture_to_fiber_result(fiber_module, fixture):
    """Wrap a fixture dict into a :class:`FiberResult` of the right outcome."""
    outcome = fixture.get("outcome")
    data = fixture.get("data")
    elapsed = fixture.get("elapsedMs", 0)
    error = fixture.get("error")
    outcome_enum = fiber_module.FiberOutcome(outcome)
    return fiber_module.FiberResult(
        outcome=outcome_enum,
        ok=(outcome_enum is fiber_module.FiberOutcome.OK),
        data=data,
        error=error,
        elapsed_ms=int(elapsed) if isinstance(elapsed, (int, float)) else None,
        source="fiber",
    )


# ── Fixture builders that don't live on disk ─────────────────────────────


def _empty_result(fiber_module, shape):
    return fiber_module.FiberResult.empty(shape)


def _unsupported_result(fiber_module, shape, reason="no fiber state"):
    return fiber_module.FiberResult.unsupported(reason, shape)


# ── Check infrastructure ─────────────────────────────────────────────────


class Check:
    """A single self-test check. Captures name, expected, observed, pass."""

    def __init__(self, name, description=""):
        self.name = name
        self.description = description
        self.expected = None
        self.observed = None
        self.passed = False
        self.error = None

    def record(self, expected, observed, passed, error=None):
        self.expected = expected
        self.observed = observed
        self.passed = passed
        self.error = error
        return self

    def to_row(self):
        status = "PASS" if self.passed else "FAIL"
        return (self.name, self.expected, self.observed, status, self.error)


@contextmanager
def _patched_capture(bot_fiber_mod, *,
                     participants=None, chat=None, meeting_state=None):
    """Temporarily replace ``bot_fiber.capture_*`` with constant returns."""
    originals = {
        "capture_participants": bot_fiber_mod.capture_participants,
        "capture_chat_messages": bot_fiber_mod.capture_chat_messages,
        "capture_meeting_state": bot_fiber_mod.capture_meeting_state,
    }
    if participants is not None:
        bot_fiber_mod.capture_participants = lambda *a, **k: participants
    if chat is not None:
        bot_fiber_mod.capture_chat_messages = lambda *a, **k: chat
    if meeting_state is not None:
        bot_fiber_mod.capture_meeting_state = lambda *a, **k: meeting_state
    try:
        yield
    finally:
        for name, fn in originals.items():
            setattr(bot_fiber_mod, name, fn)


@contextmanager
def _fiber_only_mode(config_mod):
    prev = config_mod.DETECTION_MODE
    config_mod.DETECTION_MODE = "fiber_only"
    try:
        yield
    finally:
        config_mod.DETECTION_MODE = prev


# ── Individual checks ────────────────────────────────────────────────────


def _check_mode_predicate(checks, bot_mod):
    chk = Check("mode_predicate", "fiber_only mode predicate returns True")
    try:
        observed = bot_mod._is_fiber_only_mode()
        chk.record(True, observed, observed is True)
    except Exception as exc:
        chk.record(True, repr(exc), False, error=str(exc))
    checks.append(chk)


def _check_read_chat_ok(checks, bot_mod, bot_fiber_mod):
    chk = Check("read_chat_messages.OK",
                "read_chat_messages maps fiber OK to legacy [{sender, text}]")
    try:
        fx = load_fixture("fiber_chat_ok.json")
        fr = fixture_to_fiber_result(bot_fiber_mod, fx)
        with _patched_capture(bot_fiber_mod, chat=fr):
            out = bot_mod.read_chat_messages(FakeFiberDriver(), bot_id=0)
        # Fixture has 4 messages but one has empty text → expect 3.
        ok = (isinstance(out, list)
              and len(out) == 3
              and all(isinstance(m, dict) and set(m) == {"sender", "text"} for m in out)
              and all(m["text"] != "" for m in out))
        chk.record(
            "3 entries, each {sender, text}, no empty-text rows",
            f"{len(out)} entries",
            ok,
        )
    except LegacyDomLeak as exc:
        chk.record("no DOM access", "DOM leak: " + str(exc), False, error=str(exc))
    except Exception as exc:
        chk.record("legacy shape", repr(exc), False, error=traceback.format_exc())
    checks.append(chk)


def _check_read_chat_unsupported(checks, bot_mod, bot_fiber_mod):
    chk = Check("read_chat_messages.UNSUPPORTED",
                "chat UNSUPPORTED returns [] and never touches DOM")
    try:
        fr = _unsupported_result(bot_fiber_mod, [], "no chat collection")
        with _patched_capture(bot_fiber_mod, chat=fr):
            out = bot_mod.read_chat_messages(FakeFiberDriver(), bot_id=0)
        chk.record("[]", repr(out), out == [])
    except LegacyDomLeak as exc:
        chk.record("[] without DOM", "DOM leak: " + str(exc), False, error=str(exc))
    except Exception as exc:
        chk.record("[] without DOM", repr(exc), False, error=str(exc))
    checks.append(chk)


def _check_participant_count_ok(checks, bot_mod, bot_fiber_mod):
    chk = Check("get_participant_count.OK",
                "participant count returns len(data) from fiber OK fixture")
    try:
        fx = load_fixture("fiber_participants_ok.json")
        fr = fixture_to_fiber_result(bot_fiber_mod, fx)
        with _patched_capture(bot_fiber_mod, participants=fr):
            count = bot_mod.get_participant_count(FakeFiberDriver())
        chk.record(len(fx["data"]), count, count == len(fx["data"]))
    except LegacyDomLeak as exc:
        chk.record("count without DOM", "DOM leak: " + str(exc), False, error=str(exc))
    except Exception as exc:
        chk.record("count", repr(exc), False, error=str(exc))
    checks.append(chk)


def _check_participant_count_empty(checks, bot_mod, bot_fiber_mod):
    chk = Check("get_participant_count.EMPTY",
                "EMPTY outcome returns 0")
    try:
        fr = _empty_result(bot_fiber_mod, [])
        with _patched_capture(bot_fiber_mod, participants=fr):
            count = bot_mod.get_participant_count(FakeFiberDriver())
        chk.record(0, count, count == 0)
    except Exception as exc:
        chk.record(0, repr(exc), False, error=str(exc))
    checks.append(chk)


def _check_get_participants_ok(checks, bot_mod, bot_fiber_mod):
    chk = Check("get_participants.OK",
                "get_participants maps OK to legacy participant dicts with _source=fiber")
    try:
        fx = load_fixture("fiber_participants_ok.json")
        fr = fixture_to_fiber_result(bot_fiber_mod, fx)
        with _patched_capture(bot_fiber_mod, participants=fr):
            out = bot_mod.get_participants(FakeFiberDriver(), bot_id=0)
        required_keys = {"name", "role", "isSelf", "videoOff",
                         "audioMuted", "handRaised", "_source"}
        ok = (isinstance(out, list)
              and len(out) == len(fx["data"])
              and all(isinstance(p, dict) and required_keys.issubset(p)
                      and p["_source"] == "fiber" for p in out))
        chk.record(
            f"{len(fx['data'])} entries, legacy keys, _source=fiber",
            f"{len(out)} entries",
            ok,
        )
    except LegacyDomLeak as exc:
        chk.record("legacy + fiber", "DOM leak: " + str(exc), False, error=str(exc))
    except Exception as exc:
        chk.record("legacy shape", repr(exc), False, error=str(exc))
    checks.append(chk)


def _check_get_participants_unsupported(checks, bot_mod, bot_fiber_mod):
    chk = Check("get_participants.UNSUPPORTED",
                "UNSUPPORTED returns [] and never touches DOM")
    try:
        fr = _unsupported_result(bot_fiber_mod, [])
        with _patched_capture(bot_fiber_mod, participants=fr):
            out = bot_mod.get_participants(FakeFiberDriver(), bot_id=0)
        chk.record("[]", repr(out), out == [])
    except LegacyDomLeak as exc:
        chk.record("[] without DOM", "DOM leak: " + str(exc), False, error=str(exc))
    except Exception as exc:
        chk.record("[] without DOM", repr(exc), False, error=str(exc))
    checks.append(chk)


def _check_alive_in_meeting(checks, bot_mod, bot_fiber_mod):
    chk = Check("check_bot_alive.in_meeting",
                "in-meeting fixture returns True")
    try:
        fx = load_fixture("fiber_meeting_state_ok.json")
        fr = fixture_to_fiber_result(bot_fiber_mod, fx)
        with _patched_capture(bot_fiber_mod, meeting_state=fr):
            alive = bot_mod.check_bot_alive(FakeFiberDriver())
        chk.record(True, alive, alive is True)
    except Exception as exc:
        chk.record(True, repr(exc), False, error=str(exc))
    checks.append(chk)


def _check_alive_meeting_ended(checks, bot_mod, bot_fiber_mod):
    chk = Check("check_bot_alive.meeting_ended",
                "meetingEnded=true fixture returns False")
    try:
        fx = load_fixture("fiber_meeting_state_ended.json")
        fr = fixture_to_fiber_result(bot_fiber_mod, fx)
        with _patched_capture(bot_fiber_mod, meeting_state=fr):
            alive = bot_mod.check_bot_alive(FakeFiberDriver())
        chk.record(False, alive, alive is False)
    except Exception as exc:
        chk.record(False, repr(exc), False, error=str(exc))
    checks.append(chk)


def _check_alive_off_zoom(checks, bot_mod, bot_fiber_mod):
    chk = Check("check_bot_alive.off_zoom_url",
                "URL not on zoom.us returns False without calling fiber")
    try:
        called = {"fiber": False}

        def _shouldnt_be_called(*a, **k):
            called["fiber"] = True
            return None

        with _patched_capture(bot_fiber_mod, meeting_state=None):
            bot_fiber_mod.capture_meeting_state = _shouldnt_be_called
            try:
                alive = bot_mod.check_bot_alive(
                    FakeFiberDriver(current_url="https://example.com/"),
                )
            finally:
                pass
        ok = alive is False and called["fiber"] is False
        chk.record("False, fiber NOT called", f"{alive}, fiber called={called['fiber']}", ok)
    except Exception as exc:
        chk.record("False without fiber call", repr(exc), False, error=str(exc))
    checks.append(chk)


def _check_alive_unsupported_safe_reprobe(checks, bot_mod, bot_fiber_mod):
    chk = Check("check_bot_alive.unsupported_safe_reprobe",
                "URL on zoom.us + UNSUPPORTED returns True (safe re-probe)")
    try:
        fr = _unsupported_result(bot_fiber_mod, {})
        with _patched_capture(bot_fiber_mod, meeting_state=fr):
            alive = bot_mod.check_bot_alive(FakeFiberDriver())
        chk.record(True, alive, alive is True)
    except Exception as exc:
        chk.record(True, repr(exc), False, error=str(exc))
    checks.append(chk)


def _check_default_restored(checks, config_mod, expected):
    """After all the checks above ran inside _fiber_only_mode, the
    context manager restores the prior value. Verify that restored value
    matches the on-disk default (Phase 7+: ``fiber_only``).

    ``expected`` is captured before the in-process flip and re-asserted
    after restoration, so this stays correct whether the operator runs
    with no env override or with ``DETECTION_MODE=hybrid`` for rollback.
    """
    chk = Check("default_restored",
                "config.DETECTION_MODE restored to its pre-test value")
    try:
        observed = config_mod.DETECTION_MODE
        chk.record(expected, observed, observed == expected)
    except Exception as exc:
        chk.record(expected, repr(exc), False, error=str(exc))
    checks.append(chk)


# ── Runner ───────────────────────────────────────────────────────────────


def run_self_test() -> Tuple[List[Check], bool]:
    """Run every check. Returns (checks, all_passed)."""
    checks: List[Check] = []
    try:
        bot_mod, bot_fiber_mod, config_mod = _load_modules()
    except Exception as exc:
        c = Check("import", "import bot, bot_fiber, config")
        c.record("import ok", repr(exc), False, error=traceback.format_exc())
        return [c], False

    # Snapshot the at-rest default BEFORE the in-process flip so we can
    # assert the context manager restores it cleanly.
    pre_test_mode = config_mod.DETECTION_MODE

    with _fiber_only_mode(config_mod):
        _check_mode_predicate(checks, bot_mod)
        _check_read_chat_ok(checks, bot_mod, bot_fiber_mod)
        _check_read_chat_unsupported(checks, bot_mod, bot_fiber_mod)
        _check_participant_count_ok(checks, bot_mod, bot_fiber_mod)
        _check_participant_count_empty(checks, bot_mod, bot_fiber_mod)
        _check_get_participants_ok(checks, bot_mod, bot_fiber_mod)
        _check_get_participants_unsupported(checks, bot_mod, bot_fiber_mod)
        _check_alive_in_meeting(checks, bot_mod, bot_fiber_mod)
        _check_alive_meeting_ended(checks, bot_mod, bot_fiber_mod)
        _check_alive_off_zoom(checks, bot_mod, bot_fiber_mod)
        _check_alive_unsupported_safe_reprobe(checks, bot_mod, bot_fiber_mod)

    _check_default_restored(checks, config_mod, pre_test_mode)
    all_passed = all(c.passed for c in checks)
    return checks, all_passed


# ── Output ───────────────────────────────────────────────────────────────


def print_summary(checks, all_passed, stream=sys.stdout):
    width = 38
    stream.write("Fiber-only self-test (non-live)\n")
    stream.write("=" * 56 + "\n")
    for c in checks:
        name = c.name.ljust(width)
        status = "PASS" if c.passed else "FAIL"
        stream.write(f"{name} {status}\n")
        if not c.passed and c.error:
            # Truncate so stack-trace noise doesn't drown the summary.
            stream.write("    " + c.error.splitlines()[0][:200] + "\n")
    stream.write("=" * 56 + "\n")
    total = len(checks)
    passed = sum(1 for c in checks if c.passed)
    stream.write(f"{passed} / {total} checks passed — "
                 f"{'PASS' if all_passed else 'FAIL'}\n")


def _git_head(short=True):
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short" if short else "HEAD", "HEAD"],
            cwd=_ROOT, stderr=subprocess.DEVNULL,
        )
        return out.decode("utf-8", "replace").strip()
    except Exception:
        return "unknown"


def write_report(checks, all_passed) -> str:
    """Write a sanitized Markdown report. Returns the path."""
    out_dir = os.path.join(_ROOT, "reports", "self_tests")
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"fiber_only_self_test_{ts}.md")
    head = _git_head()
    total = len(checks)
    passed = sum(1 for c in checks if c.passed)
    overall = "PASS" if all_passed else "FAIL"

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"# Fiber-only self-test report\n\n")
        fh.write(f"- Generated: `{ts}`\n")
        fh.write(f"- Commit: `{head}`\n")
        fh.write(f"- Mode: `DETECTION_MODE=fiber_only` (in-process only)\n")
        fh.write(f"- Live actions: none — no Zoom join, no chat send, no driver started\n")
        fh.write(f"- Sanitization: participant/chat data substituted by fixtures with redacted placeholders\n")
        fh.write(f"\n")
        fh.write(f"## Result: {overall} ({passed}/{total} checks passed)\n\n")
        fh.write(f"## Checks\n\n")
        fh.write(f"| Check | Expected | Observed | Result |\n")
        fh.write(f"| ----- | -------- | -------- | ------ |\n")
        for c in checks:
            name, expected, observed, status, _ = c.to_row()
            # Sanitize: strip anything that looks like a meeting ID or a
            # token. We use the same regex the operator-side scan uses.
            exp = _sanitize(str(expected))
            obs = _sanitize(str(observed))
            fh.write(f"| `{name}` | {exp} | {obs} | **{status}** |\n")
        fh.write(f"\n")
        if not all_passed:
            fh.write(f"## Failure detail (truncated)\n\n")
            for c in checks:
                if c.passed:
                    continue
                err = (c.error or "").splitlines()
                first = err[0] if err else ""
                fh.write(f"- `{c.name}`: {_sanitize(first)[:200]}\n")
            fh.write(f"\n")
        fh.write(f"## What this report does NOT include\n\n")
        fh.write(f"- Meeting IDs\n")
        fh.write(f"- Passcodes\n")
        fh.write(f"- Real participant names\n")
        fh.write(f"- Real chat content\n")
        fh.write(f"- Env values\n")
        fh.write(f"- Tokens / signatures\n")
        fh.write(f"\n")
        fh.write(f"## Run command\n\n")
        fh.write(f"```bash\n")
        fh.write(f"python tools/fiber_only_self_test.py --write-report\n")
        fh.write(f"```\n")
    return path


_SANITIZE_PATTERNS = [
    # Long digit runs that could be a meeting ID or phone number.
    (r"\b\d{9,12}\b", "[REDACTED_NUMERIC]"),
    # Bearer-ish tokens.
    (r"Bearer [A-Za-z0-9._-]{20,}", "Bearer [REDACTED_TOKEN]"),
    # Telegram bot tokens.
    (r"\b\d{9}:AA[A-Za-z0-9_-]{20,}\b", "[REDACTED_TELEGRAM_TOKEN]"),
    # GitHub PAT prefixes.
    (r"\bghp_[A-Za-z0-9]{20,}\b", "[REDACTED_GH_TOKEN]"),
    (r"\bghs_[A-Za-z0-9]{20,}\b", "[REDACTED_GH_TOKEN]"),
    # postgres/redis URIs.
    (r"postgres://\S+", "[REDACTED_PG_URI]"),
    (r"redis://\S+", "[REDACTED_REDIS_URI]"),
]


def _sanitize(text: str) -> str:
    """Conservatively redact anything that looks like a secret or
    meeting ID. The self-test never emits real values into ``text`` —
    this is belt-and-suspenders so future check additions can't leak."""
    import re
    out = text
    for pat, repl in _SANITIZE_PATTERNS:
        out = re.sub(pat, repl, out)
    return out


# ── Entrypoint ───────────────────────────────────────────────────────────


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Non-live fiber-only self-test (no Zoom join, no driver, no network).",
    )
    parser.add_argument(
        "--write-report",
        action="store_true",
        help="Write a sanitized Markdown report to reports/self_tests/",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-check output; print final summary line only.",
    )
    args = parser.parse_args(argv)

    checks, all_passed = run_self_test()

    if not args.quiet:
        print_summary(checks, all_passed)
    else:
        total = len(checks)
        passed = sum(1 for c in checks if c.passed)
        sys.stdout.write(
            f"{passed}/{total} {'PASS' if all_passed else 'FAIL'}\n"
        )

    if args.write_report:
        path = write_report(checks, all_passed)
        rel = os.path.relpath(path, _ROOT)
        sys.stdout.write(f"Report: {rel}\n")

    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

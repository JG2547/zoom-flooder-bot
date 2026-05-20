# Fiber-Only Default-Flip Plan (Phase 7 — No-Execution Plan)

Plan-only document describing the future change from
`DETECTION_MODE = "hybrid"` to `DETECTION_MODE = "fiber_only"` as the
on-disk default in [`config.py`](../config.py).

**This commit DOES NOT flip the default.** The default remains
`"hybrid"`. Authoring this plan does not authorize the flip — the
flip requires a separate approval phrase (§10).

Companion documents:

- [`docs/SETUP.md`](SETUP.md) — install + run instructions.
- [`docs/DETECTION_ARCHITECTURE.md`](DETECTION_ARCHITECTURE.md) —
  detection architecture, migration plan, validation tracks.
- [`docs/FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md`](FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md) —
  the stricter owned-meeting smoke-test checklist.

---

## 1. Purpose

This document specifies the exact future change to the on-disk default
for `config.DETECTION_MODE` (from `"hybrid"` to `"fiber_only"`),
together with the validation gate, the rollback, the monitoring, and
the no-go conditions that must hold around it.

This is a **plan**. It is committed for review and for explicit
operator approval. The runtime change is **not** part of this commit
and is not authorized until the operator issues the phrase in §10.

The plan is written assuming the only validation evidence in hand is
the non-live self-test (see §3). Where stricter evidence is available
(owned-meeting smoke test results), it should be substituted at the
gate point.

---

## 2. Current state

| Item                                                                  | Value                                                                                  |
| --------------------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `config.DETECTION_MODE` on-disk default                                | `"hybrid"`                                                                              |
| Override path                                                          | `DETECTION_MODE=fiber_only` env var, per-shell                                          |
| Wired callers in `bot.py`                                              | `read_chat_messages`, `get_participant_count`, `get_participants`, `check_bot_alive`   |
| Fiber adapter                                                          | [`bot_fiber.py`](../bot_fiber.py) — `FiberOutcome`, `FiberResult`, three JS payloads, three wrappers |
| Non-live self-test CLI                                                 | [`tools/fiber_only_self_test.py`](../tools/fiber_only_self_test.py)                    |
| Unit test suite                                                        | `tests/test_bot_fiber.py`, `tests/test_bot_fiber_wiring.py`, `tests/test_fiber_only_dry_run.py`, `tests/test_fiber_only_self_test.py` |
| Test count at latest commit                                            | 84/84 passing (`Ran 84 tests in 0.433s ... OK`)                                         |
| Latest classification                                                  | `STANDALONE_BOT_FIBER_ONLY_NON_LIVE_SELF_TEST_PASSED` (commit `3266a45`)                |
| Owned-meeting smoke test                                               | Plan committed (`docs/FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md`); execution **NOT yet** performed |
| Legacy hybrid DOM paths in `bot.py`                                    | Still present and live under `DETECTION_MODE=hybrid`                                    |

How to confirm the above today, in order:

```bash
python tools/fiber_only_self_test.py
python tools/fiber_only_self_test.py --write-report
python -m unittest discover -s tests -p "test_*fiber*.py" -v
```

---

## 3. Risk statement

The non-live fixtures and the self-test CLI prove:

- The Python-side wiring routes the four detection callers through
  `bot_fiber.capture_*()` when `DETECTION_MODE=fiber_only`.
- The legacy return shapes are preserved (`{sender, text}` for chat;
  `{name, role, isSelf, videoOff, audioMuted, handRaised, _source}`
  for participants; `True/False` for `check_bot_alive`).
- Non-OK outcomes (`EMPTY`, `UNSUPPORTED`, `DEADLINE_EXCEEDED`,
  `PARSE_ERROR`, `DRIVER_ERROR`) fail closed — no DOM fallback in
  `fiber_only` mode, asserted by `LegacyDomLeak`.
- Default `DETECTION_MODE` is restored to `"hybrid"` after every
  in-process scenario.

The non-live fixtures **do not** prove:

- That Zoom's current Web SDK React fiber tree still exposes the
  fields the three walkers in `bot_fiber.py` heuristically match
  (`displayName`, `isVideoOn`, `isMuted`, `bMeetingNumber`, etc.).
  Zoom can and does rename internal React state shapes between
  releases.
- That the `check_bot_alive` fiber probe surfaces a meeting-state
  object at all for the current Zoom build. If it does not, the
  function returns `True` (safe re-probe), which is correct on
  zoom.us but useless as a positive liveness signal.
- That `read_chat_messages` actually finds a chat collection in the
  live React tree. If not, fiber-only returns `[]` every poll while
  the legacy DOM walker would have succeeded.
- Latency / overhead profile against a real Zoom client under load.

**Flipping the default to `fiber_only` without owned-meeting
validation is acceptable only if the operator explicitly accepts this
risk.** The mitigating controls are:

- `DETECTION_MODE=hybrid` env var override remains a one-line
  rollback.
- All hybrid DOM paths remain in the source — Phase 5 cleanup has not
  been authorized.
- The chat monitor in `bot_manager.py` already disables itself
  cleanly when `read_chat_messages` returns `[]`, so the worst
  observable failure mode is "spam monitor stops firing" rather than
  "bot crashes".

The conservative alternative is to run the owned-meeting smoke test
first (`APPROVE_STANDALONE_BOT_FIBER_ONLY_OWNED_MEETING_SMOKE_TEST_EXECUTION`)
and only flip the default after that test passes (§11).

---

## 4. Pre-flip gate

Before authoring the runtime PR that flips the default, **all** of
the following must hold. Re-run them in order on a clean checkout.

```bash
cd zoom-flooder-bot-main

# 1. Working tree clean
git status --short

# 2. Syntax clean
python -m py_compile bot.py bot_fiber.py config.py

# 3. Full fiber-test suite passes
python -m unittest discover -s tests -p "test_*fiber*.py" -v

# 4. Non-live self-test passes 12/12
python tools/fiber_only_self_test.py

# 5. Sanitized report writes cleanly + secret-scan passes
python tools/fiber_only_self_test.py --write-report
grep -RInE "([0-9]{9}:AA[A-Za-z0-9_-]{20,}|BOT_TOKEN=|TELEGRAM_TOKEN=|DISCORD_TOKEN=|ZOOM_SECRET=|ZOOM_SDK_SECRET=|postgres://|redis://|Bearer [A-Za-z0-9._-]{20,}|ghp_|ghs_|[0-9]{9,12})" \
    reports/self_tests/*.md || echo "secret scan clean"
```

Pass criteria:

- [ ] `git status --short` empty.
- [ ] `py_compile` clean.
- [ ] All `tests/test_*fiber*.py` pass (currently 84 — count must
      grow, never shrink).
- [ ] `python tools/fiber_only_self_test.py` reports `12/12 PASS`
      and exits `0`.
- [ ] The generated report under `reports/self_tests/` is
      secret-scan clean.
- [ ] No runtime file is dirty other than the planned single-line
      `config.py` change in §5.

A single failure on any of these aborts the flip. Do not proceed.

---

## 5. Flip implementation

The flip is a **single-line change** to [`config.py`](../config.py):

```diff
- DETECTION_MODE = os.environ.get("DETECTION_MODE", "hybrid").strip().lower()
+ DETECTION_MODE = os.environ.get("DETECTION_MODE", "fiber_only").strip().lower()
```

The surrounding `_DETECTION_MODES = ("hybrid", "fiber_only")` tuple
and the fallback-to-hybrid validator block are NOT touched — both
values remain valid and the env var override path stays intact.

The implementation commit message will be:

> `Flip standalone bot detection default to fiber_only`

with the body explaining the gate evidence (test count, self-test
result hash, optional owned-meeting test result).

**Must NOT be removed** in the flip commit (preserves rollback
safety):

- The `"hybrid"` value as a permitted choice in `_DETECTION_MODES`.
- The legacy DOM paths inside `bot.py` for `read_chat_messages`,
  `get_participant_count`, `get_participants`, `check_bot_alive`
  (these only execute when `DETECTION_MODE == "hybrid"`).
- The non-live self-test harness or its tests.
- The owned-meeting smoke-test checklist doc.
- Any of the fiber-related unit-test files or fixtures.

Phase 5 of the migration plan (delete the hybrid paths) is **out of
scope here** — it requires its own approval after the default flip
has soaked.

---

## 6. Post-flip validation

Immediately after applying the flip locally on a backup branch (or
the master branch on a clean checkout, depending on the operator's
preference), run:

```bash
# Syntax & test suite still green
python -m py_compile bot.py bot_fiber.py config.py
python -m unittest discover -s tests -p "test_*fiber*.py" -v

# Self-test still passes — its TestPhase4Invariants checks now invert
# (since the default is no longer hybrid). Update the corresponding
# assertion in tests/test_fiber_only_dry_run.py:
#   assertIn('"DETECTION_MODE", "fiber_only"', src)
# in the same flip commit so the invariant test stays green.
python tools/fiber_only_self_test.py
```

Confirm the default has actually changed by importing config in a
sub-process (avoids any in-process env override leaking from the
operator's shell):

```bash
python - <<'PY'
import os
os.environ.pop("DETECTION_MODE", None)
import config
print(config.DETECTION_MODE)
PY
```

Expected output:

```
fiber_only
```

Then verify the env-var override path still works in both
directions — first force hybrid:

```bash
# Unix / bash / zsh
DETECTION_MODE=hybrid python - <<'PY'
import config
print(config.DETECTION_MODE)
PY
```

```cmd
REM Windows cmd.exe
set DETECTION_MODE=hybrid
python -c "import config; print(config.DETECTION_MODE)"
set DETECTION_MODE=
```

```powershell
# PowerShell
$env:DETECTION_MODE = "hybrid"
python -c "import config; print(config.DETECTION_MODE)"
Remove-Item env:DETECTION_MODE
```

Expected output in all three: `hybrid`. Then drop the env var and
re-import — should print `fiber_only` again.

Finally, on the operator's own owned-meeting test rig (separate
authorization), run the live smoke checklist if not already run.

---

## 7. Rollback

Three rollback levels, ordered from cheapest to heaviest:

1. **Per-shell env override** (no code change). Set
   `DETECTION_MODE=hybrid` in the shell that runs the bot:

   ```cmd
   set DETECTION_MODE=hybrid
   ```

   ```powershell
   $env:DETECTION_MODE = "hybrid"
   ```

   ```bash
   export DETECTION_MODE=hybrid
   ```

   This is instant and reversible. Use this first.

2. **Per-host env override** (no code change). Persist the env var
   in the operator's shell profile or process-supervisor unit file
   so every future invocation picks up `hybrid`.

3. **Source revert**. If the flip commit itself is the suspected
   cause, revert it cleanly:

   ```bash
   git revert <flip-commit-sha>
   git push
   ```

   Do **not** force-push. Do **not** rewrite `master` history.

The legacy DOM paths inside `bot.py` must remain in source for at
least one full release cycle of stable fiber-only operation before
they can be deleted. Phase 5 cleanup (delete legacy paths) has its
own approval gate and is not implied by this flip.

---

## 8. Monitoring after flip

For at least one cycle of normal operation, collect the following
sanitized signals:

| Signal                                                    | Source                                                    | What "good" looks like                                                                              |
| --------------------------------------------------------- | --------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Distribution of `FiberOutcome` values                      | `bot.log` (`log.debug` lines from `bot.py` wrappers)      | Majority `OK`; small steady-state `EMPTY` during panel mount; rare `DEADLINE_EXCEEDED`              |
| `UNSUPPORTED` rate                                         | `bot.log`                                                  | Should be ~0 for participants; possibly steady-state >0 for chat/meeting-state if those collections aren't surfaced in the current Zoom build |
| `elapsed_ms` for each fiber call type                      | Add a debug-log line in a future fixup if needed           | Typical < 200 ms; sustained > 500 ms is a regression                                                |
| Participant count drift versus toolbar badge               | Periodic diff between fiber count and the (still-legacy) badge if the operator runs hybrid bots side-by-side | Within ±1 across a poll cycle                                                                       |
| Chat mapping failure rate                                  | `bot.log` — chat monitor / spam monitor signals             | No drop in detection events vs the pre-flip hybrid baseline                                          |
| `check_bot_alive` false positives                          | Bots reported alive that have actually been kicked          | None — false positives keep the bot in a dead session and break re-join                              |
| `check_bot_alive` false negatives                          | Bots reported dead that are actually still in the meeting   | Tolerable as long as the rejoin path triggers                                                       |
| Self-test report archive                                   | `reports/self_tests/*.md` (gitignored — collect locally)    | All recent runs still 12/12 PASS                                                                    |

Do **not** publish raw logs. Sanitize all artifacts (`[REDACTED_*]`
placeholders for names, meeting IDs, chat content) before sharing.

If any signal degrades, escalate per §9.

---

## 9. No-go conditions

Do not flip the default if any of the following hold at the gate:

- [ ] Pre-flip gate (§4) fails on any criterion.
- [ ] `python tools/fiber_only_self_test.py` does not exit `0`.
- [ ] Any `test_*fiber*.py` test fails on a clean checkout.
- [ ] The working tree carries unrelated dirty changes that would
      ride along with the flip commit.
- [ ] The most recent self-test report contains any secret-scan hit
      that is a real value (not a synthetic test fixture).
- [ ] The operator cannot accept the no-live-validation risk
      described in §3.
- [ ] Past sanitized reports show `UNSUPPORTED` outcomes dominating
      participant extraction — fiber-only would degrade visibility.
- [ ] An owned-meeting validation window is plausibly available
      within the same week — wait for it and use the stricter gate
      (§11).

If any of these trip, stop, file a follow-up under
`APPROVE_STANDALONE_BOT_FIBER_ONLY_FIXUP_SLICE`, and leave the
default at `hybrid`.

---

## 10. Approval phrase for actual default flip

The exact phrase that authorizes the runtime change in §5 is:

```
APPROVE_STANDALONE_BOT_FIBER_ONLY_DEFAULT_FLIP_IMPLEMENTATION
```

That phrase must arrive in a turn from the operator. Authoring this
plan does NOT carry the implementation authorization forward — the
operator must explicitly approve at the implementation gate.

When the phrase is issued, the implementing turn must also include:

- Confirmation that §4 was just re-run on a clean checkout (paste
  the test summary line and the self-test summary line — env values
  redacted).
- Confirmation that the operator accepts the §3 risk statement.
- Optional pointer to a sanitized owned-meeting smoke-test report,
  if one exists.

---

## 11. Conservative alternative

If the owned-meeting smoke test has not been run, the recommended
sequence is:

1. Run the owned-meeting checklist under approval
   `APPROVE_STANDALONE_BOT_FIBER_ONLY_OWNED_MEETING_SMOKE_TEST_EXECUTION`
   (see [`docs/FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md`](FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md)).
2. Confirm pass classification.
3. **Then** issue
   `APPROVE_STANDALONE_BOT_FIBER_ONLY_DEFAULT_FLIP_IMPLEMENTATION`.

This sequence is the stricter gate. It is the recommended path. The
default-flip-without-live-meeting path (this plan) is acceptable but
explicitly carries the §3 residual risk.

---

## Appendix A — Source-of-truth references

| Item                                       | Path                                                                                                                          |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------- |
| Detection mode constant                    | [`config.py`](../config.py) — `DETECTION_MODE = os.environ.get(...)` block                                                    |
| Wired callers                              | [`bot.py`](../bot.py): `read_chat_messages` (~1456), `get_participant_count` (~2163), `get_participants` (~2199), `check_bot_alive` (~2352) |
| Fiber adapter                              | [`bot_fiber.py`](../bot_fiber.py)                                                                                              |
| Non-live self-test CLI                     | [`tools/fiber_only_self_test.py`](../tools/fiber_only_self_test.py)                                                            |
| Self-test tests                            | [`tests/test_fiber_only_self_test.py`](../tests/test_fiber_only_self_test.py)                                                  |
| Dry-run fixture harness                    | [`tests/test_fiber_only_dry_run.py`](../tests/test_fiber_only_dry_run.py), [`tests/fixtures/`](../tests/fixtures)              |
| Unit tests for adapter + wiring             | [`tests/test_bot_fiber.py`](../tests/test_bot_fiber.py), [`tests/test_bot_fiber_wiring.py`](../tests/test_bot_fiber_wiring.py) |
| Architecture & migration plan              | [`docs/DETECTION_ARCHITECTURE.md`](DETECTION_ARCHITECTURE.md)                                                                  |
| Owned-meeting smoke-test checklist         | [`docs/FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md`](FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md)                                        |
| ZMT reference (read-only)                  | `Botify-Network/services/zmt-electron-client/enforcer/fiber_result.py`, `enforcer/zoom_auto_kick.py`, `agent/src/enforcement/adapters/PlaywrightAdapter.js` |

# Phase 10 — Remove Legacy DOM Paths (Plan-Only)

Plan-only document specifying the future removal of the hybrid /
DOM-fallback paths in [`bot.py`](../bot.py) after the fiber-only
default has soaked for at least one stable cycle (or an owned-meeting
smoke test has passed). The runtime change is **not** in this commit
and requires a separate operator approval (§9).

Companion documents:

- [`docs/SETUP.md`](SETUP.md) — install + run instructions
- [`docs/DETECTION_ARCHITECTURE.md`](DETECTION_ARCHITECTURE.md) — detection design + migration plan
- [`docs/FIBER_ONLY_DEFAULT_FLIP_PLAN.md`](FIBER_ONLY_DEFAULT_FLIP_PLAN.md) — Phase 7 (implemented in `280cc44`)
- [`docs/FIBER_ONLY_POST_FLIP_MONITORING.md`](FIBER_ONLY_POST_FLIP_MONITORING.md) — post-flip runbook
- [`docs/FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md`](FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md) — stricter empirical gate

---

## 1. Purpose

Phase 7 flipped the on-disk default for `config.DETECTION_MODE` from
`"hybrid"` to `"fiber_only"`. Phases 8-9 added the post-flip
monitoring runbook and the health-gated launcher. The legacy DOM
fallback paths in `bot.py` are still present in source, reachable
only when an operator sets `DETECTION_MODE=hybrid`.

This Phase 10 plan specifies the future deletion of those legacy
paths. After Phase 10:

- The fiber-only path becomes the *only* detection implementation.
- `DETECTION_MODE=hybrid` either becomes a no-op compatibility shim
  (preferred) or is removed entirely (requires its own approval
  phase).
- The `bot.py` line count drops materially; `bot_fiber.py` stays.

**This commit does NOT delete anything.** The legacy DOM paths
remain reachable under `DETECTION_MODE=hybrid` until the operator
explicitly approves the implementation phase per §9.

---

## 2. Current status

| Item                                                                  | Value                                                                                                       |
| --------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Phase 7 default-flip commit                                           | `280cc44  Flip standalone bot detection default to fiber-only`                                              |
| Phase 8 monitoring runbook commit                                     | `cd7dd87  Document fiber-only post-flip monitoring`                                                          |
| Phase 9 health-gated launcher commit                                  | `ffb37b3  Add health-gated launcher and rollback wrappers`                                                  |
| Current on-disk default                                                | `"fiber_only"`                                                                                              |
| Rollback                                                               | `DETECTION_MODE=hybrid` env override (per-shell) or `scripts\start_dashboard_hybrid_rollback.bat`            |
| Non-live self-test                                                    | `12/12 PASS`, exit 0                                                                                         |
| Unit test count                                                       | `104/104` passing                                                                                            |
| Owned-meeting live validation                                          | **NOT yet run** — see §3 for waiver path                                                                    |
| Legacy DOM paths in `bot.py`                                          | Present and reachable only when `DETECTION_MODE=hybrid`                                                     |
| Operator workflow                                                     | `scripts\fiber_health_check.bat` → `scripts\start_dashboard_fiber_default.bat` → monitor per runbook §4    |

---

## 3. Removal prerequisites

At least **one** of the two paths below must be satisfied before the
implementation approval phrase can be issued.

### A. Preferred — empirical evidence

- [ ] The owned-meeting smoke test
  ([`docs/FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md`](FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md))
  has been run against a meeting the operator owns and **passed**
  every check in §6 of that document.
- [ ] A sanitized report with no real meeting IDs / names / chat
  content was archived (locally or committed only if rigorously
  sanitized — generated reports under `reports/` are gitignored on
  purpose).
- [ ] The post-flip monitoring runbook §8 exit criteria all hold.

### B. Waiver path — first stable cycle

Acceptable only if a controlled owned-meeting validation is not
feasible:

- [ ] Operator explicitly accepts the no-live-test residual risk
  (already accepted at the Phase 7 flip in `280cc44`, restated here).
- [ ] At least one full stable cycle of normal operation on the
  fiber-only default with no monitoring-runbook §4 regressions.
- [ ] `python tools/fiber_only_self_test.py` continues to report
  `12/12 PASS` across the cycle (no drift in fiber walker shape).
- [ ] The runbook §6 rollback procedure was rehearsed at least once
  (i.e. the operator has actually toggled `DETECTION_MODE=hybrid`
  via env override and confirmed the legacy path still works).
- [ ] Rollback strategy after legacy removal is documented (§7).

If neither path can be satisfied, **stop**. Keep the legacy paths
in source. File any monitoring concerns under
`APPROVE_STANDALONE_BOT_FIBER_ONLY_FIXUP_SLICE` instead.

---

## 4. Inventory of legacy DOM paths to remove

Exact targets in `bot.py` (line numbers are at the time of this
plan; they may shift before the implementation commit):

### 4.1 `read_chat_messages` DOM walker
- **Location:** `bot.py:1615-1680` (function body excluding the
  fiber-only branch wired by Phase 3).
- **Current purpose:** Two-strategy DOM walk over `[class*="chat-message"]` /
  `[role="listitem"]` / `[class*="message-item"]` containers, with
  inline sender/content selector chains, to harvest chat messages
  when fiber is unavailable.
- **Current risk:** Class-name churn between Zoom releases. Already
  silently misses virtualised messages outside the viewport.
- **Replacement:** `bot_fiber.capture_chat_messages` (active path
  when `DETECTION_MODE=fiber_only`, the new default).
- **Removal notes:** Delete the `else:` branch inside
  `read_chat_messages`. Drop the inline JS payload. Keep the
  function signature and docstring so callers in
  `bot_manager.py` and the chat-monitor / spam-monitor are
  unaffected.

### 4.2 `get_participant_count` toolbar-badge scrape
- **Location:** `bot.py:2203-2260` legacy branch.
- **Current purpose:** Read the Zoom toolbar badge / section header
  via `button[aria-label*="participant"]` + `[class*="badge"]` /
  `[class*="number"]` / `.footer-button-base__number` to derive the
  count when fiber misses.
- **Current risk:** Drifts with each Zoom UI rework. The fiber list
  is authoritative.
- **Replacement:** `len(bot_fiber.capture_participants(...).data)`
  via the existing fiber-only branch.
- **Removal notes:** Delete the DOM scrape branch. Make the
  function simply call `len(get_participants(...))` or call
  `bot_fiber.capture_participants(...)` directly.

### 4.3 `get_participants` DOM aria-label fallback
- **Location:** `bot.py:2275-2440` legacy branch (includes
  `.participants-li` and `[class*="participants-li"]` parsing at
  `bot.py:2396`).
- **Current purpose:** Parse `aria-label` strings of
  `.participants-li` rows to recover `{name, role, isSelf, videoOff,
  audioMuted, handRaised}` when fiber misses. Requires the
  participants panel to be open and visible.
- **Current risk:** Misses virtualised participants; depends on
  panel state; locale-sensitive (English-only labels like `"(Host)"`,
  `"(Me)"`).
- **Replacement:** `bot_fiber.capture_participants` + the existing
  `_fiber_participants_to_legacy` mapper.
- **Removal notes:** Delete the DOM branch. Keep
  `_fiber_participants_to_legacy` — the shape callers rely on.

### 4.4 `check_bot_alive` XPATH + leave-button + meeting-UI DOM
- **Location:** `bot.py:2443-2510` legacy branch. Includes XPATH at
  `:2480` matching `"meeting has ended"` / `"Meeting has been
  ended"` / `"The host has ended"` / `"You have been removed"`
  (also referenced from waiting-room helpers at `bot.py:268`).
- **Current purpose:** Detect end-of-meeting and kicked-out states
  via locale-sensitive English text matching, plus
  `_find_element_with_cache("leave_btn", _LEAVE_BTN_SELECTORS)` and
  `document.querySelector('[class*="meeting"], [class*="footer"]')`.
- **Current risk:** Breaks on non-English Zoom clients; brittle
  against UI reworks; the leave-button activation
  (`_activate_toolbar`) sends real mouse events.
- **Replacement:** `bot_fiber.capture_meeting_state` (covers
  `meetingEnded` / `inMeeting` / `inWaitingRoom`) plus the
  URL-on-zoom.us sanity check that already runs first in
  `check_bot_alive`.
- **Removal notes:** Delete the XPATH chain, the
  `_activate_toolbar` call, the `_find_element_with_cache` leave-
  button check, and the bottom-of-function meeting-UI
  `querySelector` fallback. Keep the `url = driver.current_url` /
  `"zoom.us" not in url` check at the top — it is locale-
  independent and does not scrape DOM.

### 4.5 TODO markers
- **Location:** `bot.py:~1610, ~2199, ~2271, ~2440`
  (`TODO(fiber-only, Phase 3): ...` / `Phase 2/3: ...`).
- **Removal notes:** Remove the markers in the same commit that
  removes the code they reference. They become factually wrong as
  soon as the legacy code is gone.

### 4.6 Waiting-room helper at `bot.py:268`
- **Location:** `_WAITING_ROOM_SELECTORS` XPATH list, line 261-266
  area; the `'meeting has ended'` entry at `:268` is part of an
  ended-state selector list used by `check_bot_alive`.
- **Decision:** Out of scope for Phase 10. Waiting-room detection
  is currently a separate concern; conversion to a fiber `inWR`
  probe is tracked under Phase 5 cleanup in
  [`docs/DETECTION_ARCHITECTURE.md`](DETECTION_ARCHITECTURE.md) §6.
  Phase 10 removes only the four legacy detection-caller fallbacks.

---

## 5. What remains after removal

| Stays                                                                          | Why                                                                                                                                              |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| [`bot_fiber.py`](../bot_fiber.py)                                              | The fiber-only adapter is now the only detection implementation.                                                                                  |
| [`tools/fiber_only_self_test.py`](../tools/fiber_only_self_test.py)            | Continuous non-live diagnostic. Still relevant post-removal — it asserts the wired callers return the expected legacy-shape dicts.                |
| [`tools/run_with_detection_health.py`](../tools/run_with_detection_health.py)  | Health-gated launcher. Keep both `--mode default` and `--mode hybrid` flags; hybrid becomes a no-op compatibility shim unless removed in §6.     |
| Windows `.bat` wrappers under `scripts/`                                       | Operator entry points. Keep all three; the hybrid wrapper stays for the compatibility-shim period (or also gets a deprecation note if removed).  |
| The four wired callers in `bot.py`                                              | `read_chat_messages`, `get_participant_count`, `get_participants`, `check_bot_alive` — keep the *function names + signatures* unchanged so the rest of `bot_manager.py` / `web_app.py` does not need a coordinated rewrite. |
| URL sanity check inside `check_bot_alive`                                       | `"zoom.us" not in driver.current_url` is locale-independent and does not scrape DOM. Stays as the cheap pre-filter before `bot_fiber.capture_meeting_state`. |
| All Phase 1-9 docs                                                              | Historical record + ongoing operator runbook.                                                                                                     |
| `DETECTION_MODE` constant (compatibility-shim variant — preferred)              | Setting `DETECTION_MODE=hybrid` after removal becomes a no-op (the validator still accepts it, but the callers always run fiber). This avoids breaking the muscle-memory rollback command. A separate later approval can remove the env var entirely. |

| Goes                                                                           | Why                                                                                                                                                          |
| ------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `read_chat_messages` DOM `else:` branch                                         | Replaced by fiber.                                                                                                                                            |
| `get_participant_count` toolbar-badge `execute_script` body                     | Replaced by `len(...)` over fiber result.                                                                                                                     |
| `get_participants` `.participants-li` aria-label parsing block                  | Replaced by fiber + `_fiber_participants_to_legacy`.                                                                                                           |
| `check_bot_alive` XPATH "meeting has ended" / leave-button / meeting-UI fallback | Replaced by `bot_fiber.capture_meeting_state`.                                                                                                                |
| `TODO(fiber-only, Phase N)` comment blocks above the four wired callers        | Become factually wrong once their body is fiber-only.                                                                                                          |

---

## 6. Proposed implementation slice

Single commit, scoped to detection-reader removal only.

1. **Delete the legacy DOM branches** in the four wired callers
   (§4.1-4.4). Keep function signatures + docstrings.
2. **Collapse the `DETECTION_MODE` branching** OR leave a
   compatibility shim:
   - Preferred (compatibility shim): keep
     `if _is_fiber_only_mode(): ...` as the body and remove the
     `else:` clause. Setting `DETECTION_MODE=hybrid` then becomes a
     no-op because the predicate returns `False` but the same
     fiber-only path still runs (rewrite the predicate-gated branch
     to always run). Document this in the same commit.
   - Aggressive (later separate approval — not Phase 10): remove
     `_is_fiber_only_mode` entirely and inline the fiber path.
3. **Update tests:**
   - Invert
     `TestPhase4Invariants.test_legacy_hybrid_path_still_present` →
     `test_legacy_dom_selectors_absent`. Assert `bot.py` no longer
     contains `'meeting has ended'`, `participants-li`,
     `[class*="chat-message"]`.
   - Update `tests/test_bot_fiber_wiring.py` hybrid tests if any
     still expect the hybrid branch to call DOM methods on the fake
     driver.
   - Add a new test asserting `DETECTION_MODE=hybrid` no longer
     surfaces DOM selector calls (proves the compatibility shim is
     a true no-op).
   - Keep the rollback documentation in
     `docs/FIBER_ONLY_POST_FLIP_MONITORING.md` §6, with a clear
     note that source rollback (`git revert <removal-commit>`) is
     now the only path to legacy DOM behaviour.
4. **Validation chain** to run in the implementing turn:

   ```bash
   python -m py_compile bot.py bot_fiber.py config.py
   python -m unittest discover -s tests -p "test_*fiber*.py" -v
   python -m unittest discover -s tests -p "test_*.py" -v
   python tools/fiber_only_self_test.py --quiet
   python tools/run_with_detection_health.py --no-start
   ```

   Plus subprocess env round-trip (proves the compatibility shim
   path still loads cleanly with `DETECTION_MODE=hybrid` set):

   ```bash
   DETECTION_MODE=hybrid python -c "import config; print(config.DETECTION_MODE)"
   ```

5. **Commit message:**

   > `Remove legacy DOM detection fallbacks`

   Body must include:
   - Confirmation of either §3.A (smoke test passed, sanitized
     report archived) or §3.B (waiver accepted + stable cycle
     evidence).
   - Line counts removed.
   - Test count before/after.
   - Pointer to this plan.

---

## 7. Rollback after removal

After the legacy DOM paths are deleted, the cheap rollback options
get fewer. Plan accordingly.

| Tier                | Before Phase 10                                                                 | After Phase 10                                                                                                          |
| ------------------- | ------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| 1. Per-shell env    | `DETECTION_MODE=hybrid` restores full legacy DOM behaviour.                     | Compatibility-shim variant: no-op (still runs fiber). Aggressive variant: env var has been removed entirely.            |
| 2. Per-host env     | Same as Tier 1 but persistent.                                                  | Same as Tier 1 above.                                                                                                   |
| 3. Source revert    | `git revert <flip-commit>` (Phase 7) restores hybrid as default.                | `git revert <removal-commit>` restores all legacy DOM code. Cleanest option in the aggressive variant.                  |

Operator hygiene at implementation time:

- **Tag a known-good pre-removal commit before merging the removal.**
  Recommended: `git tag pre-legacy-removal <removal-commit>^`.
  The tag gives a one-command return point even if the revert
  history later becomes messy.
- Do **not** force-push the removal commit.
- Do **not** rewrite `master` history.
- Keep `bot_fiber.py`, the self-test, the health-gated launcher,
  and all docs intact through the removal.

---

## 8. No-go conditions

Do not approve Phase 10 implementation if **any** of the following
hold at the gate:

- [ ] Owned-meeting smoke test has not passed AND no §3.B waiver
      is granted.
- [ ] `python tools/fiber_only_self_test.py` fails on the current
      `master`.
- [ ] Any `test_*fiber*.py` test fails on a clean checkout.
- [ ] Most recent monitoring runbook §4 signal sample shows
      sustained `UNSUPPORTED` or `DRIVER_ERROR` on the participant
      reader.
- [ ] Rollback procedure has not been rehearsed (operator has not
      actually flipped to `DETECTION_MODE=hybrid` and back at least
      once).
- [ ] Working tree carries unrelated dirty changes that would
      ride along with the removal commit.
- [ ] Operator still genuinely depends on `DETECTION_MODE=hybrid`
      for production usage — e.g. fiber walker is known to miss
      participants on the operator's current Zoom build.
- [ ] Phase 5 / waiting-room-fiber conversion is out of scope but
      a *prerequisite* for the operator's use case (e.g. heavy
      waiting-room flows that the fiber `inWR` probe doesn't yet
      cover reliably).

If any of these trip, file the concern under
`APPROVE_STANDALONE_BOT_FIBER_ONLY_FIXUP_SLICE` and leave the
legacy paths in source.

---

## 9. Required approval phrase for actual removal

The exact phrase that authorizes deletion of the legacy DOM paths is:

```
APPROVE_STANDALONE_BOT_FIBER_ONLY_REMOVE_LEGACY_DOM_PATHS_IMPLEMENTATION
```

Issuing this phrase implies §3.A has been satisfied — the owned-
meeting smoke test passed and the sanitized report exists.

If the operator chooses to remove legacy paths without empirical
validation (waiver path §3.B), the alternative phrase is:

```
APPROVE_STANDALONE_BOT_FIBER_ONLY_REMOVE_LEGACY_DOM_PATHS_IMPLEMENTATION_WITH_NO_LIVE_TEST_WAIVER
```

The waiver phrase requires the implementing turn to also include:

- Explicit acceptance of the §3.B no-live-test residual risk.
- Pointer to the stable-cycle evidence (date range + sanitized
  monitoring summary).
- Confirmation that the rollback procedure was rehearsed at least
  once during the stable cycle.

Either way, the implementing turn must also:

- Confirm §3 prerequisites are met.
- Paste the latest self-test summary and the latest test-suite
  summary line.
- Reference this plan by path.

---

## 10. Conservative alternative

Strongly recommended before either §9 phrase:

```
APPROVE_STANDALONE_BOT_FIBER_ONLY_OWNED_MEETING_SMOKE_TEST_EXECUTION
```

Runs the owned-meeting smoke checklist
([`docs/FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md`](FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md))
against a meeting the operator owns. Pass classification on that
checklist satisfies §3.A and unblocks the non-waiver removal phrase
in §9. The Phase 9 health-gated launcher slots in as the smoke
test's step 5.3, so the actual execution is a small number of
operator-driven actions.

---

## Appendix A — Source-of-truth references

| Item                                       | Path                                                                                                                                                              |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Detection mode constant                    | [`config.py`](../config.py) — `DETECTION_MODE = os.environ.get("DETECTION_MODE", "fiber_only")...`                                                                |
| Wired callers (legacy DOM still present)   | [`bot.py`](../bot.py): `read_chat_messages` (~1615), `get_participant_count` (~2203), `get_participants` (~2275), `check_bot_alive` (~2443)                       |
| Fiber adapter                              | [`bot_fiber.py`](../bot_fiber.py)                                                                                                                                  |
| Self-test CLI                              | [`tools/fiber_only_self_test.py`](../tools/fiber_only_self_test.py)                                                                                                |
| Health-gated launcher                      | [`tools/run_with_detection_health.py`](../tools/run_with_detection_health.py)                                                                                      |
| Windows wrappers                           | [`scripts/fiber_health_check.bat`](../scripts/fiber_health_check.bat), [`scripts/start_dashboard_fiber_default.bat`](../scripts/start_dashboard_fiber_default.bat), [`scripts/start_dashboard_hybrid_rollback.bat`](../scripts/start_dashboard_hybrid_rollback.bat) |
| Architecture + migration                   | [`docs/DETECTION_ARCHITECTURE.md`](DETECTION_ARCHITECTURE.md)                                                                                                       |
| Default-flip plan                          | [`docs/FIBER_ONLY_DEFAULT_FLIP_PLAN.md`](FIBER_ONLY_DEFAULT_FLIP_PLAN.md) (implemented `280cc44`)                                                                   |
| Post-flip monitoring runbook               | [`docs/FIBER_ONLY_POST_FLIP_MONITORING.md`](FIBER_ONLY_POST_FLIP_MONITORING.md)                                                                                     |
| Owned-meeting checklist                    | [`docs/FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md`](FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md)                                                                             |
| Last known-good pre-Phase-10 commit        | `ffb37b3` at plan-authoring time (will shift as monitoring or fixup commits land before the implementation)                                                       |

# Phase 5 — Waiting-Room Fiber Conversion (Plan-Only)

Plan-only document specifying the future conversion of the
waiting-room detection helper in `bot.py` from
locale-dependent XPATH text scrape to a fiber `inWR` probe via
`bot_fiber.capture_meeting_state`.

**This commit does NOT change runtime code.** Actual conversion
requires a separate operator approval phrase (§9).

Companion documents:

- [`docs/DETECTION_ARCHITECTURE.md`](DETECTION_ARCHITECTURE.md)
- [`docs/FIBER_ONLY_REMOVE_LEGACY_DOM_PATHS_PLAN.md`](FIBER_ONLY_REMOVE_LEGACY_DOM_PATHS_PLAN.md) — Phase 10 (implemented in `710eeb4`)
- [`docs/FIBER_ONLY_POST_FLIP_MONITORING.md`](FIBER_ONLY_POST_FLIP_MONITORING.md)

---

## 1. Purpose

Phase 10 removed legacy DOM detection from the four post-join
callers (`read_chat_messages`, `get_participant_count`,
`get_participants`, `check_bot_alive`). The join-flow
waiting-room helper (`_WAITING_ROOM_SELECTORS` at `bot.py:293-299`
and its callers at `bot.py:1012-1028`) was explicitly out of
Phase 10 scope per the Phase 10 plan §4.6.

Phase 5 closes that gap. The fiber adapter already surfaces an
`inWaitingRoom` field per participant (`bot_fiber.capture_participants`)
and an `inWaitingRoom` boolean on the meeting-state probe
(`bot_fiber.capture_meeting_state`). The waiting-room helper can
read that signal instead of scraping en-US text from the DOM.

After Phase 5:

- Waiting-room presence is detected via the fiber tree (locale-
  independent, no XPATH).
- Hand-off into the meeting (admission) is detected by the
  `inWaitingRoom` signal flipping false in the same fiber probe.
- The legacy XPATH list `_WAITING_ROOM_SELECTORS` is removed.
- The `bot.py` join flow continues to support the existing
  `waiting_room_timeout` parameter and `status_callback`.

---

## 2. Current status

| Item                                            | Value                                                                                            |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| Phase 10 implementation commit                   | `710eeb4  Remove legacy DOM detection fallbacks`                                                  |
| Current `bot.py` HEAD                            | `710eeb4` (master == upstream)                                                                   |
| `DETECTION_MODE` default                         | `fiber_only`                                                                                     |
| Non-live test count                              | 108/108 passing                                                                                  |
| Self-test                                        | 12/12 PASS                                                                                       |
| Waiting-room helper                              | Still uses XPATH list (`_WAITING_ROOM_SELECTORS`); reachable from join flow only                  |
| Fiber `inWR` adapter                             | Already present in `bot_fiber.capture_participants` (per-participant) and `capture_meeting_state` (meeting-level) |
| Live owned-meeting smoke test                    | Skipped by operator directive (deferred indefinitely)                                            |

---

## 3. Inventory of waiting-room paths

### 3.1 `_WAITING_ROOM_SELECTORS` (bot.py:293-299)

```python
_WAITING_ROOM_SELECTORS = [
    (By.XPATH, "//*[contains(text(), 'host will admit you')]"),
    (By.XPATH, "//*[contains(text(), 'Waiting for the host')]"),
    (By.XPATH, "//*[contains(text(), 'Host has joined')]"),
    (By.XPATH, "//*[contains(text(), 'will let you in soon')]"),
    (By.XPATH, "//*[contains(text(), \"Please wait, the meeting host\")]"),
]
```

- **Current purpose:** detect whether the bot is currently held in
  Zoom's waiting room.
- **Current risk:** locale-sensitive (English only). Breaks against
  Zoom UI string drift. Requires DOM mount of the waiting-room
  copy, which may differ across Web SDK versions.
- **Replacement:** `bot_fiber.capture_meeting_state(driver)` →
  `.data.get("inWaitingRoom")`.
- **Removal notes:** delete the list. No callers outside the join
  flow rely on it after Phase 5.

### 3.2 Join-flow call sites (bot.py:1011-1028)

```python
in_waiting_room = _find_element_multi(driver, _WAITING_ROOM_SELECTORS)
if in_waiting_room:
    log.info("Bot %d: In waiting room, waiting for host admission…", bot_id + 1)
    if status_callback:
        status_callback(bot_id, "waiting_room")
    _take_screenshot(driver, bot_id, "waiting_room")
    wr_polls = max(1, waiting_room_timeout // 2)
    for _ in range(wr_polls):
        if _stopped():
            quit_driver(driver); driver = None
            return (None, time.monotonic() - t_start)
        time.sleep(2)
        if not _find_element_multi(driver, _WAITING_ROOM_SELECTORS):
            log.info("Bot %d: Admitted from waiting room.", bot_id + 1)
            break
    else:
        log.warning("Bot %d: Timed out in waiting room after %ds.", bot_id + 1, waiting_room_timeout)
```

- **Current purpose:** poll the waiting-room state every 2 seconds
  for up to `waiting_room_timeout` seconds. Break out when the
  bot is admitted (XPATH returns no match).
- **Risk:** same XPATH locale issue + sleep loop driving a noisy
  DOM probe every 2s.
- **Replacement structure:**
  ```python
  def _in_waiting_room(driver):
      """Fiber-only waiting-room probe."""
      result = bot_fiber.capture_meeting_state(driver)
      if not result.is_ok:
          return None  # unknown — caller decides
      return bool(result.data.get("inWaitingRoom"))
  ```
- **Call-site behaviour change:**
  - Initial check (`in_waiting_room`): unchanged — call `_in_waiting_room(driver)` once.
  - Poll loop: replace `_find_element_multi(driver, _WAITING_ROOM_SELECTORS)` with `_in_waiting_room(driver)`.
  - **None handling:** if fiber returns `None` (i.e. `UNSUPPORTED`/`EMPTY`/`DEADLINE_EXCEEDED`), the loop should treat it as "still in waiting room" and keep polling, then timeout normally. This matches the conservative behaviour of the legacy XPATH miss (when the XPATH didn't match, the previous code assumed the bot was admitted — Phase 5 inverts this to fail closed because fiber miss is more often a transient probe failure than a real admission). The bias-toward-staying-in-WR is the safer default for a bot that should not declare itself in-meeting without proof.

### 3.3 `check_bot_alive` waiting-room reuse (already fiber)

```python
if data.get("inMeeting") is True or data.get("inWaitingRoom") is True:
    return True
```

- Already fiber-only after Phase 10. No change required.

---

## 4. What remains after conversion

| Stays                                                                          | Why                                                                                            |
| ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| `waiting_room_timeout` config parameter                                        | Operators may still want to bound how long a bot waits before giving up.                       |
| `status_callback(bot_id, "waiting_room")` emission                              | Dashboard state machine consumes this; unchanged.                                              |
| `_take_screenshot(driver, bot_id, "waiting_room")`                              | Diagnostic only; unchanged.                                                                    |
| Existing fiber `inWR` extractor in `bot_fiber.py` (`_FIBER_MEETING_STATE_JS`)   | Already implemented; no detector change.                                                       |

| Goes                                                                           | Why                                                                                            |
| ------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| `_WAITING_ROOM_SELECTORS` list                                                  | Replaced by fiber probe.                                                                       |
| Both `_find_element_multi(driver, _WAITING_ROOM_SELECTORS)` call sites          | Replaced by `_in_waiting_room(driver)`.                                                        |

---

## 5. Implementation slice (single commit)

1. Add `_in_waiting_room(driver)` helper near `_is_fiber_only_mode()`.
2. Replace the two `_find_element_multi(driver, _WAITING_ROOM_SELECTORS)` call sites at `bot.py:1012` and `bot.py:1024`.
3. Delete `_WAITING_ROOM_SELECTORS` list (bot.py:293-299).
4. Update tests:
   - Add `tests/fixtures/fiber_meeting_state_waiting_room.json` (inWaitingRoom=true).
   - Add `tests/test_fiber_waiting_room.py` covering:
     - fiber `inWaitingRoom=true` → helper returns `True`.
     - fiber `inWaitingRoom=false` → helper returns `False`.
     - fiber `UNSUPPORTED` / `EMPTY` / `DEADLINE` → helper returns `None`.
     - join-flow loop calls helper twice (initial + at least one poll iteration) with patched driver.
   - Update `TestPhase10Invariants.test_legacy_detection_dom_selectors_absent` to also assert `_WAITING_ROOM_SELECTORS` absent.
   - Add self-test check `waiting_room.fiber_only` to `tools/fiber_only_self_test.py` (13th check).
5. Validation chain:
   - `python -m py_compile bot.py bot_fiber.py config.py`
   - `python -m unittest discover -s tests -p "test_*.py" -v`
   - `python tools/fiber_only_self_test.py --quiet`
   - `python tools/run_with_detection_health.py --no-start`
   - `node --check static/app.js + signature.mjs`
6. Commit message:
   > `Convert waiting-room detection to fiber-only`

---

## 6. Rollback after conversion

| Tier | Procedure | Effect |
|---|---|---|
| 1. None at env level | `DETECTION_MODE` env vars no longer affect waiting-room helper. | Source revert required to restore XPATH path. |
| 2. Source revert | `git revert <conversion-commit>` | Restores `_WAITING_ROOM_SELECTORS` + the two call sites. |

Tag a known-good pre-Phase-5 commit before merging:
`git tag pre-waiting-room-fiber-conversion <conversion-commit>^`.

No force-push. No master rewrite.

---

## 7. No-go conditions

Do not implement if:

- [ ] `python tools/fiber_only_self_test.py` fails on master.
- [ ] `python -m unittest discover -s tests -p "test_*.py"` fails on master.
- [ ] Working tree carries unrelated dirty changes.
- [ ] Operator has not run the test suite at least once during the post-Phase-10 stable cycle.
- [ ] Operator wants to keep legacy waiting-room XPATH for a known non-en Zoom client edge case (would defer until fiber `inWR` is verified on that client).

---

## 8. Monitoring after conversion

| Signal | Normal | Warning |
|---|---|---|
| `_in_waiting_room` returns `None` rate | Brief at startup, then 0 | Sustained — fiber meeting-state probe is missing on this Zoom build |
| `Bot N: In waiting room, waiting for host admission…` log frequency | Matches `waiting_room_timeout` semantics | Bot never enters WR even on WR-enabled meeting → false negative |
| `Bot N: Admitted from waiting room.` | Single log per admission | Repeated within one join → flapping fiber `inWR` |
| `Bot N: Timed out in waiting room after Ns.` | Only on real host-no-admit | Frequent — fiber says still in WR even after admission |

If any "warning" condition surfaces, env-rollback no longer works for this helper. Source revert is the only rollback. File under `APPROVE_STANDALONE_BOT_FIBER_ONLY_FIXUP_SLICE`.

---

## 9. Required approval phrase for actual implementation

```
APPROVE_STANDALONE_BOT_WAITING_ROOM_FIBER_CONVERSION_IMPLEMENTATION
```

Issuing this phrase implies §7 no-go conditions all pass. The
implementing turn must include the latest self-test summary line
and the latest test-suite summary line.

---

## 10. Conservative alternative

If empirical confidence is desired before source change:

```
APPROVE_STANDALONE_BOT_FIBER_ONLY_OWNED_MEETING_SMOKE_TEST_EXECUTION
```

Plus a meeting with waiting-room enabled and operator admitting
the bot manually. The bot's behaviour (does it correctly detect WR
and admission via fiber?) is the empirical evidence Phase 5
implementation needs but cannot generate itself.

---

## Appendix A — References

| Item                                             | Path                                                                                                                              |
| ------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------- |
| Current waiting-room XPATH                       | [`bot.py:293-299`](../bot.py)                                                                                                     |
| Current join-flow call sites                     | [`bot.py:1012, 1024`](../bot.py)                                                                                                  |
| Fiber `inWR` extractor (per-participant)         | [`bot_fiber.py`](../bot_fiber.py) — `_FIBER_PARTICIPANTS_JS` extractParticipant() `inWaitingRoom` field                            |
| Fiber `inWaitingRoom` (meeting-state)            | [`bot_fiber.py`](../bot_fiber.py) — `_FIBER_MEETING_STATE_JS` ingest() flag                                                       |
| `check_bot_alive` already-fiber WR consumer      | [`bot.py:2292`](../bot.py)                                                                                                        |
| Phase 10 plan (legacy DOM removal)               | [`docs/FIBER_ONLY_REMOVE_LEGACY_DOM_PATHS_PLAN.md`](FIBER_ONLY_REMOVE_LEGACY_DOM_PATHS_PLAN.md)                                   |
| Phase 10 implementation commit                   | `710eeb4`                                                                                                                          |

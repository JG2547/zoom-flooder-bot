# Fiber-Only Owned-Meeting Smoke Test — Checklist (No-Execution Plan)

Operator-facing plan for empirically validating
`DETECTION_MODE=fiber_only` against a Zoom meeting **you own and
control**. This document is a written plan — **do not execute it
without explicit approval**.

Approval phrase that authorizes execution:
`APPROVE_STANDALONE_BOT_FIBER_ONLY_OWNED_MEETING_SMOKE_TEST_EXECUTION`

Companion documents:
- [`docs/SETUP.md`](SETUP.md) — install + run instructions
- [`docs/DETECTION_ARCHITECTURE.md`](DETECTION_ARCHITECTURE.md) — fiber
  detection design, migration plan, and the non-live dry-run harness

---

## 1. Purpose

This checklist exists to:

- Validate that the four detection callers in `bot.py`
  (`read_chat_messages`, `get_participant_count`, `get_participants`,
  `check_bot_alive`) behave correctly against a real Zoom Web SDK build
  when `DETECTION_MODE=fiber_only` is set.
- Catch any drift between Zoom's React fiber shape and the ZMT-ported
  walkers in `bot_fiber.py` (the dry-run harness only validates the
  Python mapping — it cannot validate that fiber state is actually
  present in the current Zoom client version).
- Confirm there is no DOM-fallback leakage in `fiber_only` mode under
  real load.

This is **NOT**:

- A load test.
- A spam / flood test.
- An anti-detection / evasion test.
- A production rollout.
- A test against a meeting you do not own.

---

## 2. Prerequisites

| Requirement                                                              | How to verify                                                                                  |
| ------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| Clean checkout of `zoom-flooder-bot-main` at commit `ca7555e` or later    | `git rev-parse HEAD` matches an ancestor of `ca7555e`                                          |
| Python 3.10+ available                                                    | `python --version`                                                                              |
| Python dependencies installed                                             | `python -m pip install -r requirements.txt`                                                    |
| Google Chrome installed                                                   | Local Chrome binary present; `webdriver-manager` will auto-pull chromedriver                   |
| You own a Zoom meeting                                                    | You can start, end, and admit/remove participants from this meeting                            |
| Optional: a second account or device you also control                     | Useful for participant count / chat mapping checks                                              |
| No third-party participants present                                       | Meeting is closed to you (and your second device) only                                          |
| Working tree clean                                                        | `git status --short` is empty                                                                  |
| Default `DETECTION_MODE` is still `hybrid`                                 | `grep '"DETECTION_MODE", "hybrid"' config.py` returns the default-init line                     |
| Dry-run harness passes first                                              | `python -m unittest discover -s tests -p "test_*fiber*.py" -v` reports `Ran 73 tests ... OK`   |

If any row above fails, **stop**. Resolve the precondition before
proceeding. Do not start an owned-meeting test on a broken baseline.

---

## 3. Environment setup

Set `DETECTION_MODE` for the current shell session only — do not
persist it as a user/system env var.

**Windows `cmd.exe`:**

```cmd
set DETECTION_MODE=fiber_only
```

**PowerShell:**

```powershell
$env:DETECTION_MODE = "fiber_only"
```

**macOS / Linux `bash`/`zsh`:**

```bash
export DETECTION_MODE=fiber_only
```

Rollback (always available, used in step 5.10 below):

```cmd
set DETECTION_MODE=hybrid
```

```powershell
$env:DETECTION_MODE = "hybrid"
```

```bash
export DETECTION_MODE=hybrid
```

Do **not** commit env values. Do **not** publish real meeting IDs,
passcodes, account names, or Zoom session tokens. Env var **names**
are fine to share; env var **values** are not.

---

## 4. Pre-flight checks

Run from the repo root in the same shell where `DETECTION_MODE` is
set, **before** opening any browser:

```bash
python -m py_compile bot.py bot_fiber.py config.py
python -m unittest discover -s tests -p "test_*fiber*.py" -v
git status --short
```

All three must report clean. The unit tests must report
`Ran 73 tests ... OK` (or higher if Phase 5+ adds more). If any test
fails, stop and file a follow-up under
`APPROVE_STANDALONE_BOT_FIBER_ONLY_FIXUP_SLICE`.

Optional sanity check that the env var was picked up by Python:

```bash
python -c "import config; print(config.DETECTION_MODE)"
```

Expected output: `fiber_only`. If it prints `hybrid`, you set the env
in a different shell — re-run the export and retry.

---

## 5. Owned-meeting test procedure

Each step has an explicit observable so a `PASS/FAIL` decision can be
made without re-reading the spec. Stop on any `FAIL`.

### 5.1 Start an owned Zoom meeting

- **Action:** Start the Zoom meeting from the Zoom client on your
  primary host device. Enable any features you intend to exercise
  (waiting room on/off, chat enabled/disabled).
- **Observe:** Meeting is open, you are the host.
- **Pass:** Yes / No.

### 5.2 Join from a second owned device (optional)

- **Action:** Join the same meeting from a second device or account
  you also own (different browser, phone, etc.).
- **Observe:** Second participant visible in the host's participant
  panel.
- **Pass:** Yes / No / Skipped.

### 5.3 Start the bot in the safest supported mode

- **Action:** With `DETECTION_MODE=fiber_only` set in the shell, run
  the dashboard binary in localhost-only mode:

  ```bash
  python web_app.py
  ```

- **Observe:** Dashboard binds to `127.0.0.1:5000`. No 0.0.0.0 bind
  warning in the log. ZMT / Discord / Telegram integration logs are
  expected if those env vars are set, otherwise silent.
- **Pass:** Dashboard listening on `127.0.0.1:5000`, no errors,
  process foreground.

If you prefer the CLI path, run `python main.py` instead and follow
its interactive prompts; the rest of the checklist applies identically.

### 5.4 Confirm fiber-only mode is active

- **Action:** In a second terminal, run:

  ```bash
  python -c "import config; print('DETECTION_MODE =', config.DETECTION_MODE)"
  ```

- **Observe:** Prints `DETECTION_MODE = fiber_only`.
- **Pass:** Exactly `fiber_only`.

In `bot.log`, scan for any line matching `fiber_only`, `fiber chat`,
`fiber participants`, or `fiber meeting-state`. These are the
`log.debug` lines emitted by the wired callers on non-OK fiber
outcomes; their **presence is normal** during transient `EMPTY` /
`DEADLINE_EXCEEDED` outcomes. A flood of `DRIVER_ERROR` lines is not
normal.

### 5.5 Validate `check_bot_alive`

- **Action:** Launch a single bot at the owned meeting through the
  dashboard's "Stage" + "Deploy" flow (do not select chat-spam mode,
  do not enable reactions). Watch the dashboard's live tile for that
  bot.
- **Observe (while in meeting):** Tile turns green (`JOINED`).
- **Observe (after host ends meeting):** Tile transitions to
  `DISCONNECTED` within ~1-2 poll intervals (default 30 s).
- **Pass:**
  - `check_bot_alive` returns `True` while the bot is in the meeting.
  - `check_bot_alive` returns `False` within 2 poll intervals after
    the host ends the meeting.
  - No `XPATH` selector errors in `bot.log` (those would indicate a
    legacy DOM fallback was reached — a fiber-only regression).

### 5.6 Validate `get_participant_count`

- **Action:** With one bot in the meeting, hit
  `http://127.0.0.1:5000/api/participants` from a browser or `curl`.
  Then have the second owned device join and re-hit the endpoint.
  Then have the second device leave.
- **Observe:**
  - 1 participant when only the bot is in the meeting (or 2 if the
    host counts as a participant in your Zoom build).
  - Count increments by 1 when the second owned device joins.
  - Count decrements by 1 when the second owned device leaves.
- **Pass:** All three transitions observed within ~1 poll interval of
  the underlying state change. If count is stuck at 0 when there is
  clearly someone in the meeting, fiber may not be surfacing
  participants in the current Zoom build — log and stop.

### 5.7 Validate `get_participants`

- **Action:** Same `GET /api/participants` endpoint as 5.6 — inspect
  the `participants` array.
- **Observe (per row):**
  - `name` is non-empty and matches a real display name in the
    meeting.
  - `role == "host"` for your host account; `"participant"` otherwise.
    `"cohost"` if you promoted the second device.
  - `isSelf` is `True` for exactly one row (the bot's own row),
    `False` for all others.
  - `videoOff` matches the camera state of each row.
  - `audioMuted` matches the mute state of each row.
  - `handRaised` becomes `True` within 1-2 poll intervals after the
    second device raises hand.
  - `_source == "fiber"` on every row (proves no DOM fallback ran).
- **Pass:** All fields above accurate for every participant. If
  `_source == "dom"` appears in fiber-only mode, that's a regression —
  open a fixup slice.

### 5.8 Validate `read_chat_messages`

- **Action:** From the second owned device, send **one** harmless test
  message (e.g. "smoke-test message 1"). Wait 1-2 poll intervals. Hit
  any endpoint that surfaces chat (or read `bot.log` for chat-monitor
  output if you set `chat-monitor-target` to the second account's
  display name in the dashboard's start payload).
- **Observe:**
  - `{sender, text}` mapping shows `sender` = second device's display
    name, `text` = "smoke-test message 1".
  - No `XPATH` or `[class*="chat-message"]` selector logs in
    `bot.log` (those would indicate DOM fallback).
- **Pass:** Exact text and sender match. **Do not** loop the test
  message. Do not enable spam-monitor delete. One message in, one
  mapping out — that's the test.

### 5.9 Stop the bot

- **Action:** Click "Stop" on the dashboard. Wait for the bot tile to
  show `LEFT`. End the meeting from the host side.
- **Observe:** Bot process drains, Chrome closes, dashboard logs
  `All N bots left and exited`. No orphaned chromedriver processes.
- **Pass:** Clean shutdown.

### 5.10 Reset `DETECTION_MODE`

- **Action:** Reset the shell variable back to `hybrid`:

  ```cmd
  set DETECTION_MODE=hybrid
  ```

  ```powershell
  $env:DETECTION_MODE = "hybrid"
  ```

  ```bash
  export DETECTION_MODE=hybrid
  ```

  Or simply close the shell — the env var is per-session.
- **Observe:** A fresh shell, `python -c "import config; print(config.DETECTION_MODE)"` prints `hybrid`.
- **Pass:** Confirmed.

---

## 6. Pass / Fail criteria

The overall smoke test passes only when **all** of the following hold:

- [ ] No uncaught exceptions in `bot.log` or the dashboard console.
- [ ] No DOM-fallback log lines surface in `fiber_only` mode. Specifically:
  - No `XPATH` selector errors from `check_bot_alive`.
  - No `participants-li` aria-label parsing logs.
  - No `[class*="chat-message"]` container fallback logs.
- [ ] The bot **never sends chat** during the smoke test unless the
      operator explicitly enabled an existing safe chat-monitor mode
      against the owned second device — and even then, only one
      auto-reply round-trip should occur.
- [ ] Participant count tracks state changes within ~1 poll interval.
- [ ] Every participant row carries a stable non-empty `name` (or a
      `userId`/`persistentId` if `name` is intentionally hidden).
- [ ] Chat mapping returns exact `{sender, text}` pairs for messages
      that actually occurred during the test window.
- [ ] `check_bot_alive` transitions from `True` (in meeting) to
      `False` (after host ends) within 2 poll intervals.
- [ ] Observed `FiberResult.outcome` values are `OK` for the majority
      of polls. Transient `EMPTY` is acceptable during meeting join /
      panel mount. Sustained `UNSUPPORTED` on participants or chat is
      a failure — fiber state isn't surfaced in the current Zoom
      build for those signals.
- [ ] Observed `elapsed_ms` is typically `< 200 ms` per fiber call.
      Sustained `elapsed_ms > 500 ms` for any single payload is a
      latency regression — open a fixup slice.

A single `FAIL` on any of the above means the overall test fails.
Stop, collect artifacts, and decide between the fixup slice and the
default-flip path (§10).

---

## 7. Rollback

If anything goes sideways at any step:

1. Click "Stop" on the dashboard (or `Ctrl+C` the foreground process).
2. End the owned Zoom meeting from the host client.
3. Confirm no orphaned `chromedriver` processes remain. On Windows:
   `taskkill /f /im chromedriver.exe` only if you see chromedriver in
   Task Manager.
4. Reset `DETECTION_MODE=hybrid` (or close the shell).
5. Re-run the dry-run tests:
   ```bash
   python -m unittest discover -s tests -p "test_*fiber*.py" -v
   ```
   All 73 must still pass.
6. If a runtime change introduced the failure, revert only the
   latest runtime commit on a backup branch — do **not**
   force-push, do **not** rewrite `master` history.

Do not delete `bot.log` or the dashboard console output until §8
artifacts are copied.

---

## 8. Artifacts to collect

For each smoke-test run, collect (locally, not in this repo):

| Artifact                                          | Source                                | Redact before sharing                                  |
| ------------------------------------------------- | ------------------------------------- | ------------------------------------------------------ |
| Sanitized console log                              | Dashboard / CLI stdout                 | Meeting ID, passcode, account names                    |
| `bot.log` excerpt covering the test window        | `bot.log` in the repo root             | Meeting ID, passcode, account names, tokens             |
| `FiberResult` outcome distribution                 | Grep `bot.log` for outcome names       | None — outcomes are not secrets                         |
| `elapsed_ms` samples (median + max per call type)  | Grep `bot.log` for `elapsed_ms`         | None                                                    |
| Participant count observations                     | Manual notes during steps 5.6-5.7      | Replace real names with `Account A`, `Account B`        |
| Chat mapping observations                          | Manual notes during step 5.8           | Replace real names; the test message can stay verbatim  |
| Git commit hash                                    | `git rev-parse HEAD`                   | None                                                    |
| Environment variable **names** only                | The shell where the test ran           | Never paste values                                      |
| Screenshot of the dashboard if relevant            | OS screen-capture                       | Crop or blur participant names and meeting IDs          |

Do **not** push the artifacts to this repo. Keep them in a local
notes / private store. The repo only carries this plan, not test
output.

---

## 9. No-go list (what NOT to do)

- ❌ Do **not** test against a meeting you do not own.
- ❌ Do **not** send spam / chat floods. One verification message per
  step is enough.
- ❌ Do **not** capture private participant data beyond what's needed
  for the mapping check. Replace real display names with placeholders
  before sharing any artifact.
- ❌ Do **not** publish raw logs containing meeting IDs, passcodes,
  account names, or tokens.
- ❌ Do **not** change Railway, Botify-Network, or any production
  service as part of this test.
- ❌ Do **not** flip the `DETECTION_MODE` default in `config.py` to
  `fiber_only` yet — that's a separate approval phase
  (`APPROVE_STANDALONE_BOT_FIBER_ONLY_DEFAULT_FLIP_PLAN`).
- ❌ Do **not** remove the hybrid fallback paths in `bot.py` —
  Phase 5+ work, separately approved.
- ❌ Do **not** start a 0.0.0.0 dashboard bind during the test.
  `DASHBOARD_HOST=127.0.0.1` is the default and the only authorized
  bind during smoke testing.
- ❌ Do **not** enable the spam-monitor delete-attempt feature unless
  you are explicitly testing host-only delete behavior on your
  owned second account.

---

## 10. Next decision after plan execution

Once the operator runs this checklist end-to-end:

| Outcome                                  | Next phrase                                                                                                             |
| ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| All §6 criteria pass                     | `APPROVE_STANDALONE_BOT_FIBER_ONLY_DEFAULT_FLIP_PLAN` — unlocks authoring the Phase 5 plan to flip the default to `fiber_only` and start removing legacy DOM paths under that flag |
| Some criteria pass, others need rework   | `APPROVE_STANDALONE_BOT_FIBER_ONLY_FIXUP_SLICE` — unlocks a small, focused code change addressing the specific gap the test surfaced |
| `UNSUPPORTED` outcomes dominate chat or meeting-state | `APPROVE_STANDALONE_BOT_FIBER_ONLY_FIXUP_SLICE` — fiber payload heuristics need to be re-aimed at the actual Zoom shape |
| Total failure / regression               | Stay on `hybrid` default, file a follow-up summary, and do not advance the migration                                    |

Approval phrase suggestions (do **not** invoke without operator
confirmation):

- `APPROVE_STANDALONE_BOT_FIBER_ONLY_OWNED_MEETING_SMOKE_TEST_EXECUTION` — authorizes running this checklist as written.
- `APPROVE_STANDALONE_BOT_FIBER_ONLY_FIXUP_SLICE` — authorizes one targeted code change to address a single observed gap.
- `APPROVE_STANDALONE_BOT_FIBER_ONLY_DEFAULT_FLIP_PLAN` — authorizes the Phase 5 default-flip plan (still plan-only at the gate point).

---

## Appendix A — Source-of-truth references

- `config.DETECTION_MODE` declared in [`config.py`](../config.py) — defaults to `hybrid`.
- Fiber detector module [`bot_fiber.py`](../bot_fiber.py) — `FiberOutcome`, `FiberResult`, three payloads, three wrappers.
- Wired callers in [`bot.py`](../bot.py):
  - `read_chat_messages` (line ~1456)
  - `get_participant_count` (~2163)
  - `get_participants` (~2199)
  - `check_bot_alive` (~2352)
- Dry-run harness: [`tests/test_bot_fiber.py`](../tests/test_bot_fiber.py),
  [`tests/test_bot_fiber_wiring.py`](../tests/test_bot_fiber_wiring.py),
  [`tests/test_fiber_only_dry_run.py`](../tests/test_fiber_only_dry_run.py),
  [`tests/fixtures/`](../tests/fixtures).
- ZMT reference design: `services/zmt-electron-client/enforcer/fiber_result.py`, `enforcer/zoom_auto_kick.py`, `agent/src/enforcement/adapters/PlaywrightAdapter.js` in the Botify-Network repo.

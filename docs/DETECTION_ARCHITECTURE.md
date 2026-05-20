# Detection Architecture — current state and fiber-only migration plan

Reference: ZMT agents in
`C:\Users\JG\Documents\Bots\Botify-Network\services\zmt-electron-client\`.

This document is the single source of truth for **how the standalone bot
reads Zoom meeting state**, what's wrong with the current model, and how
we are going to move to a fiber-only adapter that matches the ZMT agent
contract.

---

## 1. What "fiber detection" means

Zoom's Web client is a React app. The Web SDK and the in-meeting UI both
hang their participant list, chat messages, and meeting status off React
component state. Each DOM node React rendered carries `__reactFiber$xxx`
and `__reactProps$xxx` keys pointing into the live fiber tree. Walking
that tree gives the canonical state object regardless of how the UI is
rendered (virtualised list, collapsed panel, scrolled out of view).

"Fiber detection" reads state from the fiber tree. It is:

- **Stable** — does not depend on class-name churn between Zoom releases.
- **Complete** — sees every participant, including those scrolled out of
  the virtualised list (the DOM only renders the visible window).
- **Fast** — single JS call, no panel scrolling, no scroll-into-view.
- **Side-effect free** — does not move the user's cursor, does not flip
  panels open/closed, does not generate Selenium events that look like
  user interaction.

"DOM detection" reads `document.querySelectorAll(...)` matches. It is
the opposite on every axis.

---

## 2. ZMT reference model

| Concern                  | ZMT location                                                                                                                              | Behaviour                                                                                          |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| Fiber JS payload         | `services/zmt-electron-client/enforcer/zoom_auto_kick.py:3092-3370` (`JS_FIBER_PARTICIPANTS`)                                            | 3-path walker: (A) walk up from participant container, (B) BFS from `#root`, (C) global store scan |
| Time budget              | enforcer 40 ms; Playwright 100 ms                                                                                                          | Hard `Date.now() + N` deadline checked inside every loop                                           |
| Discriminated result     | `services/zmt-electron-client/enforcer/fiber_result.py`                                                                                   | `FiberOutcome` enum: `OK`, `EMPTY`, `DEADLINE_EXCEEDED`, `PARSE_ERROR`, `DRIVER_ERROR`           |
| Wrapper + fallback policy | `zoom_auto_kick.py:5806-5904` (`get_all_participants`)                                                                                   | Enforcer: fiber → DOM scroll-scan fallback. Playwright adapter: fiber-only.                       |
| Selenium adapter         | `agent/src/enforcement/adapters/BrowserAdapter.js:266-279` (`_seleniumCaptureParticipants`)                                              | Calls enforcer `GET /participants` over HTTP, tags `_source: 'fiber'`                              |
| Playwright adapter       | `agent/src/enforcement/adapters/PlaywrightAdapter.js:206-413` (`_doCaptureParticipants`)                                                  | Hard-disabled scrolling, fiber-only, 100 ms deadline, returns `{participants, source, count, durationMs, dataComplete}` |
| Action registry          | `agent/src/enforcement/ActionRegistry.js:386-419` (`capture_participants`)                                                                | `method: 'browser'`, `extractionMethod: 'fiber'`, `fiberTimeoutMs: 100`                          |

Key design choices we are adopting:

1. **One JS payload, three search paths.** Don't pick a single anchor; if
   path A fails, fall through to B, then C. Each is bounded by the same
   deadline.
2. **Hard deadline inside the JS.** Every loop checks `Date.now() >=
   deadline`. No infinite walks, no DOM-scan creep.
3. **Discriminated result on the Python side.** Callers see `OK([])` vs
   `EMPTY` vs `DEADLINE_EXCEEDED` and treat them differently — the
   current `list | None` shape conflates "no participants" with "lookup
   failed".
4. **Fiber-only on the read path.** DOM scroll-scan is reserved for the
   enforcer's HTTP fallback, not for routine reads. Reading without
   scrolling means the participant panel state is preserved.

---

## 3. Current standalone detection map

| Source location                                          | What it reads                  | Strategy                                                            | Fiber-compliant?              |
| -------------------------------------------------------- | ------------------------------ | ------------------------------------------------------------------- | ----------------------------- |
| `bot.py:302-331` `_check_captcha`                        | Captcha iframe presence        | DOM `querySelectorAll('iframe')` + `src` substring match            | No — DOM only                 |
| `bot.py:1581-1636` `read_chat_messages`                  | Chat panel messages            | DOM 2-strategy selector walk on `[class*="chat-message"]`           | **No — pure DOM**             |
| `bot.py:1639-1674` `monitor_and_reply`                   | New chat from a target user    | Delegates to `read_chat_messages`                                   | Transitively no               |
| `bot.py:1801-1913` `monitor_chat_spam`                   | Repeated chat msg detection    | Delegates to `read_chat_messages`                                   | Transitively no               |
| `bot.py:2153-2178` `get_participant_count`               | Toolbar badge count            | DOM button text + badge child + section header                      | **No — pure DOM**             |
| `bot.py:2181-2323` `get_participants`                    | Full participant list          | **Hybrid** — fiber walk first (depth ≤ 50), DOM `.participants-li` aria-label fallback | Partial — fiber path exists  |
| `bot.py:2328-2356` `check_bot_alive`                     | Bot still in meeting           | URL check + XPATH for "meeting has ended" + leave-button query     | No — DOM only                 |
| `bot.py:979-996` waiting-room poll                       | Waiting-room indicator         | XPATH `_WAITING_ROOM_SELECTORS`                                     | No — DOM only                 |
| `bot.py:37`, `bot.py:672` `driver.save_screenshot`       | Captcha debug + post-join shot | Best-effort screenshot to disk                                     | N/A — diagnostic only         |

**No OCR**, **no canvas pixel reads**, **no Tesseract** anywhere in the
codebase. Good — that floor is already where the ZMT model wants it.

The only existing fiber path is `get_participants`'s strategy-1 walk
(lines 2188-2278). It is functional but:

- Uses depth-50 with no time deadline — can theoretically walk forever.
- Only takes Path A (walk up from a single container); no root BFS, no
  global store scan.
- Returns a thinner schema than ZMT (`{name, role, isSelf, videoOff,
  audioMuted, handRaised}`) — missing `ariaLabel`, `isSharing`,
  `isSpotlight`, `inWR`, `uid`.
- Falls through to DOM aria-label parsing on miss (lines 2285-2314),
  which scrolls and triggers Zoom's virtualised-list behaviour.

---

## 4. Design critique

| Area                        | Current standalone                                                                       | ZMT fiber model                                                          | Gap                                                       | Fix                                                                                                                                                  |
| --------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Participant detection       | Hybrid fiber+DOM; depth 50 with no deadline; thin schema                                 | 3-path fiber walker; 40-100 ms hard deadline; rich schema                | Wrong walker shape; unbounded; missing fields              | Replace with ZMT's `JS_FIBER_PARTICIPANTS` payload, copy field schema verbatim                                                                       |
| Participant count           | Toolbar badge DOM scrape — drifts with class-name churn                                  | `len(fiber_participants)`                                                | Independent DOM read that can disagree with list           | Derive count from fiber list result                                                                                                                  |
| Chat reading                | Pure DOM 2-strategy walk; misses virtualised messages                                    | (ZMT does not have a fiber chat reader — uses DOM enforcer paths)        | Standalone is at parity here, but we still want fiber.    | Optional: build a fiber chat walker that mirrors the participant walker shape (Phase 3, behind `DETECTION_MODE`)                                     |
| Stale DOM refs              | `window.__spamMatches` cached across iterations; ActionChains hover on detached els      | Re-queries every iteration                                               | Cached refs can detach mid-loop                            | Re-query inside the iteration body (already flagged in earlier review; tracked but not blocking the fiber migration)                                |
| Polling / retry             | `bot_manager.py` calls per-bot probes every `persist_interval` (default 30 s)            | Polls every 5 s with cached snapshot fallback                            | Long interval misses fast-moving meetings                  | Out of scope here; behaviour preserved                                                                                                              |
| Fallback chain              | `get_participants` does fiber→DOM; everything else is DOM-only                           | Playwright adapter is fiber-only, fail closed                            | Standalone leaks DOM fallback into every reader            | Phase 3: switch each reader to fiber, fall through to a single typed `EMPTY` result instead of DOM                                                  |
| Unsafe selectors            | `By.XPATH` text-match in `check_bot_alive` is locale-dependent ("meeting has ended" en) | URL + fiber meeting-state probe                                          | XPATH breaks on non-EN clients                            | Phase 3: replace with fiber meeting-state probe (URL stays as cheap sanity check)                                                                  |
| Performance                 | Each reader is a separate `execute_script`; `get_participant_count` and `get_participants` duplicate work | Single payload returns everything; cached for `5 s` poll cadence       | 2-3× extra round-trips per cycle                          | Phase 3: fold count into participant call                                                                                                            |
| Result typing               | `list | None`, callers default to `[]`                                                  | `FiberResult(outcome, participants, error)`                              | Cannot tell "0 participants" from "lookup broke"           | Phase 2: introduce `FiberResult` mirror in `bot_fiber.py`                                                                                            |
| Maintainability             | All JS lives inline inside `bot.py` (≈2 400 lines)                                       | JS payloads + result type live in dedicated modules                      | bot.py is unmanageable; JS is buried in Python strings    | Phase 2: extract `bot_fiber.py` with `_FIBER_PARTICIPANTS_JS`, `_FIBER_MEETING_STATE_JS`, `FiberResult`                                              |
| Setup reproducibility       | `requirements.txt` unpinned; no `package.json` in SDK signer; no `start.sh`              | Reproducible installs across services                                    | Versions drift; OS-specific scripts only                  | Phase 1: pin versions, add `zoom-sdk-client/server/package.json`, add `start.sh`                                                                     |
| Dashboard UX                | Single-column → 2-pane shell already landed this session                                 | N/A (dashboard is standalone-specific)                                   | Already in good shape; minor a11y polish queued            | See prior design critique (focus-visible, `prefers-reduced-motion`)                                                                                  |

---

## 5. Detection-mode flag

`config.py` now declares:

```python
DETECTION_MODE = os.environ.get("DETECTION_MODE", "hybrid").strip().lower()
# allowed: "hybrid" (current), "fiber_only" (target — Phase 3)
```

This is **declarative only** in this pass. No reader consumes it yet.
Phase 3 will:

1. Move the fiber JS + wrappers into `bot_fiber.py`.
2. Make `get_participants` branch on `DETECTION_MODE`.
3. Default the flag to `fiber_only` once Phase 4 fixtures pass.

---

## 6. Migration plan

| Phase | Goal                              | Files touched                                                                                            | Risk | Validation                                                       |
| ----- | --------------------------------- | -------------------------------------------------------------------------------------------------------- | ---- | ---------------------------------------------------------------- |
| 1     | Inventory + guardrails (this PR)  | `docs/SETUP.md`, `docs/DETECTION_ARCHITECTURE.md`, `config.py` (`DETECTION_MODE` constant), TODO comments in `bot.py` | Low  | `py_compile`, doc review                                         |
| 2     | Extract fiber adapter             | New `bot_fiber.py` with `_FIBER_PARTICIPANTS_JS`, `_FIBER_CHAT_JS`, `_FIBER_MEETING_STATE_JS`, `FiberResult` dataclass; `bot.py` imports but does NOT call yet | Low  | `py_compile`, adapter unit tests against saved fiber fixtures    |
| 3     | Switch readers behind `DETECTION_MODE` | `bot.py:get_participants`, `get_participant_count`, `read_chat_messages`, `check_bot_alive` → branch on flag (default `hybrid`) | Med  | Live smoke against a meeting you own; compare fiber vs DOM counts |
| 4     | Flip default + remove DOM paths   | `config.py` default → `fiber_only`; delete `get_participants` DOM strategy; delete `read_chat_messages` DOM walker; thin `check_bot_alive` | Med  | Full regression: chat monitor, spam monitor, persist mode, restart cycles |
| 5     | Cleanup                           | Remove `_xpath_escape` (dead since :1123); collapse `_WAITING_ROOM_SELECTORS` to a fiber `inWR` probe    | Low  | `py_compile`, dashboard smoke                                   |

Phases 2-5 each require a separate approval phrase
(`APPROVE_STANDALONE_BOT_FIBER_ONLY_DETECTOR_IMPLEMENTATION` for Phase 2).

---

## 6a. Safe dry-run validation

Phase 3 wired the readers behind `DETECTION_MODE`. Phase 4 ships a
fixture-driven dry-run harness that proves the fiber-only path can be
exercised end-to-end **without joining a Zoom meeting, sending chat,
starting `BotManager`, or binding the dashboard port**.

### Run it

```bash
cd zoom-flooder-bot-main
python -m unittest discover -s tests -p "test_*fiber*.py" -v
```

The suite contains three layers:

1. **`tests/test_bot_fiber.py`** — unit tests for `bot_fiber.py` types,
   JS payload string sanity, outcome mapping. No `bot.py` import path.
2. **`tests/test_bot_fiber_wiring.py`** — verifies `bot.py`'s four
   detection callers route through `bot_fiber.capture_*()` when
   `DETECTION_MODE="fiber_only"` and do NOT call any legacy DOM
   selector. Uses a `_SentinelDriver` whose `execute_script` raises
   `AssertionError` so a DOM leak fails loudly.
3. **`tests/test_fiber_only_dry_run.py`** — fixture-driven smoke tests:
   - JSON fixtures under `tests/fixtures/` simulate canned `OK` /
     `EMPTY` / `UNSUPPORTED` / `DEADLINE_EXCEEDED` payloads.
   - A `FakeFiberDriver` returns the canned payload based on a marker
     substring of the JS payload (`participants-ul`, `chat-virtualized-list`,
     `meetingNumber`) and raises `LegacyDomLeak` for `find_element*`.
   - Tests confirm `read_chat_messages`, `get_participant_count`,
     `get_participants`, and `check_bot_alive` map the fixtures to the
     legacy return shape.
   - Import smoke: `bot`, `bot_fiber`, `config`, `bot_manager`, `web_app`
     all import cleanly with `DETECTION_MODE="fiber_only"` set.
     `web_app` does NOT bind a port at import — `socketio.run()` is
     gated behind `if __name__ == "__main__":`.
   - Phase-4 invariants: the hybrid path is NOT deleted, and the
     `config.py` default for `DETECTION_MODE` remains `"hybrid"`.

### What the dry-run does NOT do

| Action                                       | Status |
| -------------------------------------------- | ------ |
| Join a Zoom meeting                          | NO     |
| Send chat                                    | NO     |
| Start `BotManager` thread pool                | NO     |
| Bind `0.0.0.0:5000` or `127.0.0.1:5000`       | NO     |
| Launch Telegram / Discord clients             | NO     |
| Contact Railway                              | NO     |
| Open a real Chrome via Selenium              | NO     |
| Read or print env values                     | NO     |

### When to flip `DETECTION_MODE=fiber_only` for real

Until Phase 5 lands, `fiber_only` should only be set against a meeting
you own and can stop instantly. The Phase 4 harness validates the
mapping logic; only a controlled live meeting can validate that fiber
state is actually present in the Zoom Web SDK build you are targeting.

For the controlled owned-meeting procedure, see the no-execution
checklist at
[`docs/FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md`](FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md).
That document is a written plan and requires
`APPROVE_STANDALONE_BOT_FIBER_ONLY_OWNED_MEETING_SMOKE_TEST_EXECUTION`
to actually run.

### Alternative self-test when an owned meeting is unavailable

When you can't (or don't want to) schedule an owned-meeting test, run
the non-live self-test CLI. It exercises the same four wired
`bot.py` callers through fake drivers + JSON fixtures and emits a
sanitized PASS/FAIL report.

```bash
# Quick interactive run (prints per-check status, exits 0 on full pass)
python tools/fiber_only_self_test.py

# Same, but also write a sanitized Markdown report
python tools/fiber_only_self_test.py --write-report

# Also run the unit suite that covers the same surface from multiple angles
python -m unittest discover -s tests -p "test_*fiber*.py" -v
```

The CLI:

- sets `config.DETECTION_MODE = "fiber_only"` in-process only — the
  on-disk default in `config.py` stays `hybrid`,
- patches `bot_fiber.capture_*` to return canned `FiberResult` values
  derived from `tests/fixtures/*.json`,
- exercises `read_chat_messages`, `get_participant_count`,
  `get_participants`, `check_bot_alive`,
- raises `LegacyDomLeak` if any of the wrappers leak into a DOM
  selector method in fiber-only mode,
- writes a sanitized report to
  `reports/self_tests/fiber_only_self_test_<YYYYMMDD_HHMMSS>.md` —
  never includes meeting IDs, passcodes, real participant names,
  real chat content, env values, or tokens.

This does **not** replace empirical owned-meeting validation. The CLI
proves the Python-side wiring + legacy mapping; it cannot prove that
the React fiber tree in the current Zoom Web SDK build still exposes
the fields the walkers expect. Until the owned-meeting smoke test
runs and passes, `DETECTION_MODE` should stay at the `hybrid` default
in production.

Conservative criteria for advancing without a live meeting:

- All 12 self-test checks `PASS`.
- All `tests/test_*fiber*.py` checks `PASS` (currently 73, growing).
- No `LegacyDomLeak` ever raised across the whole suite.
- Sanitized report secret-scan clean.

If those four hold, a default-flip plan may be authored under
`APPROVE_STANDALONE_BOT_FIBER_ONLY_DEFAULT_FLIP_PLAN_WITHOUT_LIVE_MEETING`.
The owned-meeting test stays available as the stricter gate and
remains the recommended path before flipping the default.

The default-flip plan is at
[`docs/FIBER_ONLY_DEFAULT_FLIP_PLAN.md`](FIBER_ONLY_DEFAULT_FLIP_PLAN.md).
That document is a written plan — authoring it does NOT carry the
implementation authorization. The operator must issue
`APPROVE_STANDALONE_BOT_FIBER_ONLY_DEFAULT_FLIP_IMPLEMENTATION`
separately before the on-disk default in `config.py` is changed.

**Status:** the flip is implemented in commit `280cc44`. The on-disk
default is now `"fiber_only"`; `"hybrid"` remains a permitted choice
via env override. The first stable cycle after the flip is monitored
per [`docs/FIBER_ONLY_POST_FLIP_MONITORING.md`](FIBER_ONLY_POST_FLIP_MONITORING.md),
which documents the signals to watch, the three-tier rollback
procedure, and the exit criteria that gate the future Phase 10
legacy-DOM cleanup. The Phase 10 cleanup plan itself is now
authored at
[`docs/FIBER_ONLY_REMOVE_LEGACY_DOM_PATHS_PLAN.md`](FIBER_ONLY_REMOVE_LEGACY_DOM_PATHS_PLAN.md) —
plan only; deletion requires its own approval phrase.

---

## 7. Acceptance criteria for fiber-only readiness

A reader is "fiber-only ready" when:

- [ ] It returns a `FiberResult` (not bare `list` / `None`).
- [ ] Its JS payload checks `Date.now() >= deadline` inside every loop.
- [ ] It does not call `querySelectorAll` outside the path-A anchor.
- [ ] It does not call `scrollIntoView`, `scroll`, `click`, or any
      action with a side effect on Zoom's UI.
- [ ] The wrapper logs `OK` / `EMPTY` / `DEADLINE_EXCEEDED` separately
      so an operator can tell why a meeting looks empty.
- [ ] A fixture file under `tests/fixtures/fiber/` reproduces the
      reader's output deterministically.

---

## 8. References

- ZMT enforcer (Python): `services/zmt-electron-client/enforcer/zoom_auto_kick.py`
- ZMT Playwright adapter (Node): `services/zmt-electron-client/agent/src/enforcement/adapters/PlaywrightAdapter.js`
- ZMT discriminated-result type: `services/zmt-electron-client/enforcer/fiber_result.py`
- ZMT action registry: `services/zmt-electron-client/agent/src/enforcement/ActionRegistry.js`
- Standalone hybrid reader (current): `bot.py:2181-2323`
- This document: `docs/DETECTION_ARCHITECTURE.md`

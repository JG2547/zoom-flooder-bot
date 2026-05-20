# Fiber-Only Post-Flip Monitoring Runbook

Operator-facing runbook for the first stable cycle after the
fiber-only default flip (commit `280cc44`). Tells you what to watch,
what counts as "healthy", and exactly how to roll back if something
goes wrong.

Companion documents:

- [`docs/DETECTION_ARCHITECTURE.md`](DETECTION_ARCHITECTURE.md) — detection design + migration plan.
- [`docs/FIBER_ONLY_DEFAULT_FLIP_PLAN.md`](FIBER_ONLY_DEFAULT_FLIP_PLAN.md) — Phase 7 plan that this flip implements.
- [`docs/FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md`](FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md) — owned-meeting checklist (still recommended as stricter gate).

---

## 1. Purpose

The on-disk default for `config.DETECTION_MODE` is now `"fiber_only"`.
Bots launched in a fresh shell with no env override take the fiber
path through `bot_fiber.capture_*()` for participants, chat, and
meeting-state probes. The legacy DOM paths in `bot.py` remain in
source as the documented rollback (set `DETECTION_MODE=hybrid`).

This runbook covers:

- Quick health checks an operator can run any time.
- Signals to watch during the first stable cycle.
- How to collect sanitized evidence.
- Three-tier rollback procedure.
- Escalation paths and the no-go list.

Hybrid remains available via `DETECTION_MODE=hybrid` env override.
Do not delete the legacy DOM code until the criteria in §8 are met.

---

## 2. Current status

| Item                                                       | Value                                                                                                   |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| Flip implementation commit                                  | `280cc44  Flip standalone bot detection default to fiber-only`                                          |
| Phase 7 plan commit                                        | `7207f84  Document fiber-only default flip plan`                                                        |
| On-disk default for `DETECTION_MODE`                       | `"fiber_only"`                                                                                           |
| Rollback (per-shell env override)                          | `DETECTION_MODE=hybrid`                                                                                  |
| Validator fallback (unknown values)                        | `"fiber_only"` — unknown values warn + fall back to the new default                                      |
| Unit-test count after flip                                 | 87/87 passing                                                                                            |
| Non-live self-test                                         | 12/12 PASS, exit 0 (`python tools/fiber_only_self_test.py --quiet`)                                     |
| Owned-meeting live validation                              | **NOT yet run** — see §7 escalation if you can schedule one                                              |
| Legacy DOM paths in `bot.py`                                | Still present and reachable when `DETECTION_MODE=hybrid`                                                 |

---

## 3. Quick health checks

Run any time from the repo root:

```bash
# 1. Non-live self-test — should be 12/12 PASS, exit 0
python tools/fiber_only_self_test.py --quiet

# 2. Full fiber unit suite
python -m unittest discover -s tests -p "test_*fiber*.py" -v

# 3. Confirm the on-disk default — should print 'fiber_only' in a
#    clean shell, or 'hybrid' if you've set the rollback env var
python - <<'PY'
import config
print(config.DETECTION_MODE)
PY
```

Expected outcomes:

- `12/12 PASS` and exit 0 from the self-test.
- `Ran 104 tests in <2s ... OK` from the full suite (count grows
  forward, never shrinks).
- `fiber_only` from the config print, unless you intentionally set
  `DETECTION_MODE=hybrid` in the shell.

If any of these fail, treat as a regression. Stop, capture the
output sanitized, and escalate per §7.

## 3a. Normal operation workflow (health-gated launcher)

Phase 9 added a small launcher that bundles the health gate with the
dashboard start. It is the recommended way to operate the bot day to
day — it never touches Zoom by itself, it never persists env vars, and
it refuses to start the dashboard if the self-test fails.

**Cross-platform launcher** (`tools/run_with_detection_health.py`):

```bash
# Health check only — no dashboard started
python tools/run_with_detection_health.py --no-start

# Health-gated start in default mode (honors on-disk fiber_only)
python tools/run_with_detection_health.py --mode default

# Health-gated start in hybrid rollback mode (one process only)
python tools/run_with_detection_health.py --mode hybrid

# Skip the health gate (NOT recommended)
python tools/run_with_detection_health.py --mode default --force
```

**Windows operator wrappers** (`scripts/*.bat`):

| Script                                       | What it does                                                                                       |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `scripts\fiber_health_check.bat`             | Runs the self-test only. Exits 0 on PASS, 1 on FAIL.                                                |
| `scripts\start_dashboard_fiber_default.bat`  | Health-gated start with the on-disk default DETECTION_MODE (currently `fiber_only`).                |
| `scripts\start_dashboard_hybrid_rollback.bat`| Health-gated start with `DETECTION_MODE=hybrid` for the child process only. Operator shell env untouched. |

Each wrapper just delegates to the Python launcher; they exist so
operators can double-click or schedule them without typing the full
command.

Exit codes from `run_with_detection_health.py`:

- `0` — health pass + (if requested) dashboard exited cleanly.
- `1` — health gate failed, dashboard NOT started.
- `2` — bad arguments.
- otherwise — the dashboard's own exit code.

The launcher's env handling is deliberate:

- `--mode default` **scrubs** `DETECTION_MODE` from the child env so
  `config.py`'s on-disk default is honored. The operator's shell env
  is not modified.
- `--mode hybrid` **sets** `DETECTION_MODE=hybrid` in the child env
  only. Same shell-env-not-modified guarantee.

Recommended normal operation:

1. Run `scripts\fiber_health_check.bat` (Windows) or
   `python tools/run_with_detection_health.py --no-start`.
2. If PASS → start with `scripts\start_dashboard_fiber_default.bat`
   (or the Python launcher equivalent).
3. Observe signals per §4 of this runbook.
4. If a regression appears at runtime → stop the dashboard, switch
   to `scripts\start_dashboard_hybrid_rollback.bat` for the same
   session, then file evidence per §5-§7.

---

## 4. Signals to watch during the first stable cycle

These are what to grep `bot.log` for and how to interpret.

| Signal                                                          | Source / how to read                                                                                                              | Normal range                                                                  | Warning threshold                                                            | Action                                                                                                                          |
| --------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------- | ---------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `FiberOutcome.OK` rate (participants)                            | Implicit — bot proceeds normally; participant counts move with the meeting                                                          | Majority of polls                                                              | Sub-50% across multiple polls                                                | Capture `bot.log` chunk; if sustained, roll back to `hybrid` via env override.                                                  |
| `FiberOutcome.EMPTY` (participants)                              | `log.debug` lines: `fiber participants returned EMPTY:`                                                                            | Brief windows during panel mount / page load                                  | Sustained >30s with bot believed to be in the meeting                        | Capture log; possible Zoom UI rework. Roll back; file fixup.                                                                    |
| `FiberOutcome.UNSUPPORTED` (participants)                        | `log.debug` lines: `fiber participants returned UNSUPPORTED:`                                                                       | Should be ~0 for participants                                                  | Any sustained occurrence                                                     | Fiber participant walker not finding the collection on this Zoom build. Roll back; file fixup with `bot_fiber.py` candidate fix. |
| `FiberOutcome.UNSUPPORTED` (chat)                                | `log.debug` lines: `fiber chat returned UNSUPPORTED:`                                                                              | Possible steady-state — chat collection isn't always exposed                  | Operator expected chat monitor to fire but it doesn't                        | Acceptable noise unless chat-monitor / spam-monitor visibly broken. If so, env-rollback hybrid for chat-heavy meetings.        |
| `FiberOutcome.UNSUPPORTED` (meeting-state)                       | `log.debug` lines: `fiber meeting-state returned UNSUPPORTED:`                                                                     | Possible — `check_bot_alive` falls back to URL-only sanity check + safe re-probe | Repeated false-positive "alive" on a dead bot                                | Roll back if rejoin logic is masked.                                                                                            |
| `FiberOutcome.DEADLINE_EXCEEDED`                                 | `log.debug` lines: `... returned DEADLINE_EXCEEDED:`                                                                              | Rare                                                                          | >5% of any reader type                                                       | Latency regression. Capture machine load + Chrome version. Consider raising `timeout_ms` in `bot_fiber.py` (Phase 5 fixup).      |
| `FiberOutcome.PARSE_ERROR`                                       | `log.debug` lines: `... returned PARSE_ERROR:`                                                                                    | Should be 0                                                                    | Any occurrence                                                              | JS payload returned unexpected shape — Zoom internals shifted. Roll back; file fixup.                                            |
| `FiberOutcome.DRIVER_ERROR`                                      | `log.debug` lines: `... returned DRIVER_ERROR:`                                                                                   | Should be 0 except during crash recovery                                       | Sustained across multiple polls                                              | Selenium / chromedriver crash. Check `chromedriver.exe` version and process status; restart bot.                                |
| `elapsed_ms` per call                                            | Currently surfaces only on errors; for richer telemetry add a debug log line in a Phase 5 fixup                                    | < 200 ms typical                                                               | Sustained > 500 ms for any reader                                            | Performance regression. Profile JS payload size; consider tightening selectors in `bot_fiber.py`.                                |
| Participant count drift (fiber vs reality)                       | Manual: compare dashboard count to actual Zoom participant panel on a side-by-side run                                            | Within ±1 across a poll cycle                                                  | Sustained ±2 or more                                                          | Fiber walker is undercounting / overcounting. Roll back; file fixup with target-collection fingerprint.                          |
| Chat mapping failures                                            | Chat-monitor target never triggers although the second account sent the matching message                                          | 0 drops                                                                        | Any drop on a known-good message                                              | Likely chat-fiber collection not surfaced. Acceptable temporarily; long-term needs a different chat walker.                      |
| `check_bot_alive` false positives (reports alive on dead bot)    | Bot tile stays green but participant panel shows the bot gone, or you have `kicked` the bot from host                              | 0                                                                              | Any occurrence                                                                | Most serious failure mode — bot won't rejoin. Roll back to hybrid immediately.                                                  |
| `check_bot_alive` false negatives (reports dead on live bot)     | Bot tile flips `DISCONNECTED` then `RECONNECTING` while still in meeting                                                            | 0-1 transient per session                                                      | Repeated cycles                                                              | Rejoin storm risk. Roll back; file fixup.                                                                                       |
| Spam-monitor self-disables on `[]` chat results                  | Log line: `── Spam monitor enabled but no replies loaded — disabling. ──` or similar                                              | Expected when no replies are configured                                        | Self-disables despite replies loaded                                          | Means `read_chat_messages` returned `[]` enough times to look broken. Investigate; possibly chat-fiber miss.                     |

> Every entry above presumes you are running against a meeting **you
> own**. Do not collect signals from non-owned meetings.

---

## 5. Sanitized artifact collection

Collect locally — do not push artifacts to this repo (the `reports/`
directory is `.gitignore`d on purpose).

### Self-test report

```bash
python tools/fiber_only_self_test.py --write-report
```

Writes to `reports/self_tests/fiber_only_self_test_<YYYYMMDD_HHMMSS>.md`.
The report carries the commit hash, timestamp, per-check pass/fail
table, and the "what this report does NOT include" block. Built-in
`_sanitize()` strips long digit runs, `Bearer` tokens, `postgres://`
URIs, GitHub PATs, and Telegram bot tokens as a safety net.

### Sanitized `bot.log` excerpts

When pasting log snippets into an escalation:

| Replace                                                | With                                                                            |
| ------------------------------------------------------ | ------------------------------------------------------------------------------- |
| Meeting IDs (9-12 digit numbers)                        | `[REDACTED_MEETING_ID]`                                                          |
| Passcodes                                              | `[REDACTED_PASSCODE]`                                                            |
| Real participant display names                          | `[REDACTED_PARTICIPANT_<n>]`                                                     |
| Real chat text                                          | `[REDACTED_CHAT_TEXT]`                                                           |
| Tokens / signatures                                    | `[REDACTED_TOKEN]`                                                               |
| Account email addresses                                | `[REDACTED_EMAIL]`                                                               |

### Detector outcome summary

A simple `grep` over `bot.log` produces a sanitized outcome rollup:

```bash
grep -oE "(OK|EMPTY|UNSUPPORTED|DEADLINE_EXCEEDED|PARSE_ERROR|DRIVER_ERROR)" bot.log | sort | uniq -c
```

That output is safe to share — it carries only outcome names + counts.

### Participant count notes

Track as numbers only — e.g. `1 → 2 → 3 → 2 → 0` across a session.
Do not paste participant names.

### What NOT to share

- Meeting IDs / passcodes.
- Real participant names.
- Raw chat content.
- Tokens (Zoom SDK secret, Telegram, Discord, GitHub).
- Env values of any kind.
- Anything from a meeting you do not own.

---

## 6. Rollback

Three rollback levels, lightest first. Use the cheapest one that
resolves the symptom.

### 6.1 Per-shell env override (instant, no code change)

**Windows `cmd.exe`:**

```cmd
set DETECTION_MODE=hybrid
python web_app.py
```

**PowerShell:**

```powershell
$env:DETECTION_MODE = "hybrid"
python web_app.py
```

**Bash / Git Bash:**

```bash
DETECTION_MODE=hybrid python web_app.py
```

The legacy DOM paths in `bot.py` activate immediately. No commit, no
push, no restart sequence beyond the bot process itself.

### 6.2 Per-host persistent override

If multiple shells / cron jobs / supervisors run the bot on the same
host, set `DETECTION_MODE=hybrid` in the user/system environment so
every future process picks it up:

- Windows: `setx DETECTION_MODE hybrid` (new shells only).
- Systemd unit: `Environment=DETECTION_MODE=hybrid` in the unit file.
- Shell profile: `export DETECTION_MODE=hybrid` in `~/.bashrc` /
  `~/.zshrc`.

### 6.3 Source rollback (heaviest, only if env override is insufficient)

If `DETECTION_MODE=hybrid` somehow doesn't take effect (unlikely —
the unit tests prove the override path works in subprocess), revert
the flip commit cleanly:

```bash
cd zoom-flooder-bot-main
git revert 280cc44
git push
```

Do not force-push. Do not rewrite `master` history. The revert
commit will be reviewable and reversible.

### Always

- Copy any sanitized self-test reports / `bot.log` excerpts **before**
  doing source rollback.
- Do **not** delete generated reports until evidence is captured.
- Do **not** remove legacy hybrid code at this stage. The Phase 5
  cleanup that deletes legacy paths is gated separately
  (`APPROVE_STANDALONE_BOT_FIBER_ONLY_REMOVE_LEGACY_DOM_PATHS_PLAN`).

---

## 7. Escalation / fixup path

If monitoring shows a regression that the env-rollback addresses but
suggests a real bot_fiber gap:

1. Capture sanitized evidence per §5.
2. Use approval phrase:

   ```
   APPROVE_STANDALONE_BOT_FIBER_ONLY_FIXUP_SLICE
   ```

3. In the fixup-slice turn, include:
   - The sanitized observations (outcome rollup + participant count
     numbers + brief description of the failure mode).
   - The current commit hash (`git rev-parse HEAD`).
   - Whether `DETECTION_MODE=hybrid` env-rollback resolved the
     symptom in the meantime.

If a live-meeting validation window is available, prefer running
the owned-meeting smoke test first under:

```
APPROVE_STANDALONE_BOT_FIBER_ONLY_OWNED_MEETING_SMOKE_TEST_EXECUTION
```

The live test produces stronger evidence than this runbook's
post-hoc signals and can promote the migration toward Phase 5 (legacy
DOM path removal).

---

## 8. Exit criteria for the first stable cycle

The first stable cycle is "done" — and Phase 5 legacy-path removal
becomes plannable — when **all** of the following hold:

- [ ] At least one operator-owned validation run (smoke test or
      sustained real usage) without rolling back to hybrid.
- [ ] Non-live self-test stays at 12/12 across the cycle.
- [ ] Full unit suite stays at 87/87 (or higher).
- [ ] No sustained `UNSUPPORTED` outcome on the participant reader
      across multiple sessions.
- [ ] No sustained `DRIVER_ERROR` or `PARSE_ERROR` on any reader.
- [ ] Chat reader behaviour is understood: either it works in this
      Zoom build (great) or it consistently returns `[]` and the
      spam monitor self-disables cleanly (acceptable).
- [ ] `check_bot_alive` has produced 0 false positives (claiming a
      dead bot is alive).
- [ ] Sanitized monitoring notes collected for the cycle.

Only after all eight checkboxes hold should the operator consider
proposing Phase 5 (`APPROVE_STANDALONE_BOT_FIBER_ONLY_REMOVE_LEGACY_DOM_PATHS_PLAN`).
Until then, the hybrid paths stay in source.

---

## 9. No-go list

- ❌ Do **not** test or monitor against meetings you don't own.
- ❌ Do **not** publish raw `bot.log` or dashboard screenshots
  without redacting per §5.
- ❌ Do **not** remove the legacy hybrid code in `bot.py`. Phase 5
  has its own approval gate.
- ❌ Do **not** flip the on-disk default back to `"hybrid"` in
  source unless env rollback is provably insufficient. Env override
  is the documented rollback; the on-disk default is the long-term
  intent.
- ❌ Do **not** bundle unrelated changes with a fixup-slice commit.
  The fixup slice is for a single observed regression.
- ❌ Do **not** force-push, rewrite `master` history, or skip git
  hooks.
- ❌ Do **not** stage `reports/` — the directory is `.gitignore`d
  on purpose so sanitized diagnostics stay local.

---

## Appendix A — Source-of-truth references

| Item                                       | Path                                                                                                                                       |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Detection mode constant                    | [`config.py`](../config.py) — `DETECTION_MODE = os.environ.get("DETECTION_MODE", "fiber_only")...`                                         |
| Wired callers                              | [`bot.py`](../bot.py): `read_chat_messages`, `get_participant_count`, `get_participants`, `check_bot_alive`                                  |
| Fiber adapter                              | [`bot_fiber.py`](../bot_fiber.py)                                                                                                          |
| Self-test CLI                              | [`tools/fiber_only_self_test.py`](../tools/fiber_only_self_test.py)                                                                         |
| Self-test report dir (gitignored)          | `reports/self_tests/` (collect locally; never commit)                                                                                       |
| Architecture doc                           | [`docs/DETECTION_ARCHITECTURE.md`](DETECTION_ARCHITECTURE.md)                                                                              |
| Default-flip plan                          | [`docs/FIBER_ONLY_DEFAULT_FLIP_PLAN.md`](FIBER_ONLY_DEFAULT_FLIP_PLAN.md) — implemented in `280cc44`                                       |
| Owned-meeting smoke checklist              | [`docs/FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md`](FIBER_ONLY_OWNED_MEETING_SMOKE_TEST.md)                                                    |
| Flip implementation commit                 | `280cc44  Flip standalone bot detection default to fiber-only`                                                                              |

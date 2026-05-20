# Standalone Bot — Setup Map

Working-tree inventory and setup guide for `zoom-flooder-bot-main`.
Companion: [DETECTION_ARCHITECTURE.md](DETECTION_ARCHITECTURE.md).

> **Authorized use only.** This tool is for testing meetings you own or have
> explicit permission to test against. Do not use it to disrupt third-party
> meetings.

---

## 1. Component map

| Component             | Files                                                       | Role                                                  |
| --------------------- | ----------------------------------------------------------- | ----------------------------------------------------- |
| Core runtime          | `bot.py` (≈108 KB, ~2 400 lines)                            | Selenium-driven join flow, chat, reactions, monitors  |
| Orchestration         | `bot_manager.py`                                            | Threaded bot lifecycle, stats, restart/persist loops  |
| Browser glue          | `browser.py`                                                | Chrome WebDriver build, proxy, profile, headless opts |
| Dashboard backend     | `web_app.py`                                                | Flask + SocketIO, REST endpoints, log streaming       |
| Dashboard frontend    | `templates/dashboard.html`, `static/style.css`, `static/app.js` | Tactical instrument-cluster UI                    |
| Zoom Web SDK client   | `zoom-sdk-client/index.html`, `zoom-sdk-client/js/*`, `zoom-sdk-client/server/signature.mjs` | Resilient SDK demo + JWT signer (Node)                |
| CLI entry             | `main.py`                                                   | Interactive prompts → `BotManager.start()`            |
| Scheduler             | `scheduler.py`                                              | SQLite-backed scheduled raids (`raids.db`)            |
| Integrations          | `discord_bot.py`, `telegram_bot.py`                         | Optional command surfaces                             |
| Control plane client  | `zmt_client.py`                                             | Outbound ZMT registration/heartbeat/commands          |
| Config & inputs       | `config.py`, `.env.example`, `names.txt`, `proxies.txt`, `spam_responses.txt`, `default.txt` (optional) | Inputs                                                |
| Dependencies          | `requirements.txt`                                          | Python deps                                           |
| Scripts               | `start.bat`                                                 | Windows launcher                                      |

No `package.json` exists for `zoom-sdk-client/` — the JWT signer pulls
`express` via `npm i express` per its file header.

---

## 2. Runtime requirements

| Need               | Version / note                                                                |
| ------------------ | ----------------------------------------------------------------------------- |
| Python             | 3.10+ (tested with 3.14 here). `tuple-style` type hints used in `fiber_result` style code. |
| Google Chrome      | Stable channel installed locally. `webdriver-manager` auto-pulls chromedriver. |
| Node.js            | Only for `zoom-sdk-client/server/signature.mjs` (≥ 18 for `base64url`).        |
| OS                 | Cross-platform Python. `start.bat` is Windows-only. `main.py` uses optional `keyboard` module. |
| Network            | Outbound HTTPS to `zoom.us`. Optional inbound on `5000` (dashboard) and `8787` (signature server). |

---

## 3. Install

```bash
# 1. Python deps
python -m pip install -r requirements.txt

# 2. Optional CLI hotkey support (Windows or run as admin on Linux)
python -m pip install keyboard

# 3. Node deps for the SDK signer (only if you use zoom-sdk-client/)
cd zoom-sdk-client/server
npm i express
```

---

## 4. Configure

Copy `.env.example` → `.env` and fill the optional integration tokens. The
core bot does NOT need any env vars to run — env is only consumed for
integrations and the dashboard's safety rails.

| Env var                       | Used by                       | Default                                  | Notes                                              |
| ----------------------------- | ----------------------------- | ---------------------------------------- | -------------------------------------------------- |
| `DISCORD_BOT_TOKEN`           | `discord_bot.py`              | unset (integration off)                  | Optional                                           |
| `DISCORD_GUILD_ID`            | `discord_bot.py`              | unset                                    | Faster slash-cmd sync                              |
| `TELEGRAM_BOT_TOKEN`          | `telegram_bot.py`             | unset (integration off)                  | Optional                                           |
| `ALLOWED_DISCORD_CHANNELS`    | `discord_bot.py`              | unset                                    | Comma-separated channel allowlist                  |
| `ALLOWED_TELEGRAM_USERS`      | `telegram_bot.py`             | unset                                    | Comma-separated user allowlist                     |
| `ZMT_ENABLED`                 | `web_app.py` → `zmt_client.py`| `false`                                  | Enable ZMT control plane                           |
| `ZMT_CP_URL`                  | `zmt_client.py`               | unset                                    | Control plane URL                                  |
| `ZMT_REGISTRATION_KEY`        | `zmt_client.py`               | unset                                    | Auth token (do not commit)                         |
| `ZMT_AGENT_NAME`              | `zmt_client.py`               | unset                                    | Agent display name                                 |
| `DASHBOARD_HOST`              | `web_app.py`                  | `127.0.0.1`                              | Set to `0.0.0.0` only if you've added an auth layer |
| `DASHBOARD_PORT`              | `web_app.py`                  | `5000`                                   | Override port                                      |
| `DASHBOARD_CORS_ORIGINS`      | `web_app.py`                  | localhost only                           | Comma-separated allowlist or `*` to opt back wide  |
| `ALLOWED_ORIGINS`             | `zoom-sdk-client/server/signature.mjs` | `http://localhost:8787,http://127.0.0.1:8787` | CORS allowlist                                     |
| `DETECTION_MODE`              | `config.py`                   | `hybrid`                                 | `hybrid` or `fiber_only`. See [DETECTION_ARCHITECTURE](DETECTION_ARCHITECTURE.md). |
| `PORT`                        | `signature.mjs`               | `8787`                                   | Signature server port                              |
| `ZOOM_SDK_KEY` / `ZOOM_SDK_SECRET` | `signature.mjs`          | required for signer                      | Server-side only — never ship to the browser       |

> Do not print env values to logs.

---

## 5. Run

### Dashboard (recommended)

```bash
python web_app.py
# → http://127.0.0.1:5000
```

The dashboard starts the BotManager, the scheduler, optional Discord/Telegram
integrations, and the ZMT client. It binds `127.0.0.1` by default; override
with `DASHBOARD_HOST=0.0.0.0` only if you've added auth.

### CLI

```bash
python main.py
```

Interactive prompts collect meeting ID, count, etc. Press `Alt+Ctrl+Shift+E`
(or Enter if `keyboard` is not installed) to drain bots and exit.

### Zoom SDK signer (optional, used by `zoom-sdk-client/`)

```bash
ZOOM_SDK_KEY=… ZOOM_SDK_SECRET=… node zoom-sdk-client/server/signature.mjs
# → http://127.0.0.1:8787/api/signature
```

Serve `zoom-sdk-client/index.html` from any static host (or via a reverse
proxy on `/`) and the page hits `/api/signature` at the same origin.

---

## 6. Ports & hosts

| Service              | Default host       | Port  | Bound by             |
| -------------------- | ------------------ | ----- | -------------------- |
| Dashboard            | `127.0.0.1`        | 5000  | `web_app.py`         |
| Signature server     | `0.0.0.0`          | 8787  | `signature.mjs`      |

---

## 7. Dry-run / safety

There is **no documented dry-run mode** in the current codebase. A bot only
becomes safe to "test" by pointing it at a meeting you control. Treat every
run as live until a mock mode lands (Phase 1 of the detection migration —
see [DETECTION_ARCHITECTURE.md §Migration plan](DETECTION_ARCHITECTURE.md#7-migration-plan)).

When developing detection logic specifically, you can run the fiber payload
against a saved HTML/Fiber fixture in the browser console — `bot.py` does
not yet expose a fixture harness.

---

## 8. Known gaps

| Gap                                                                                              | Fix track                  |
| ------------------------------------------------------------------------------------------------ | -------------------------- |
| No pinned versions in `requirements.txt`                                                         | Phase 1                    |
| No `package.json` in `zoom-sdk-client/server/` despite `npm i express` in header                  | Phase 1                    |
| `start.bat` is Windows-only; no `start.sh` for macOS/Linux                                       | Phase 1                    |
| No tests directory                                                                               | Phase 4 of migration       |
| `_xpath_escape()` in `bot.py:1123` is defined but never called                                   | Cleanup pass               |
| No detection-mode env var consumed yet — `DETECTION_MODE` is declarative only until Phase 2      | Phase 2                    |

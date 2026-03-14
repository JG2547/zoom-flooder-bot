# -*- coding: utf-8 -*-

"""Zoom Flooder Bot — Web Dashboard (Flask + SocketIO)."""

import logging
import os

os.environ["FLASK_SKIP_DOTENV"] = "1"

from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO, emit

from config import build_config, get_defaults_dict
from bot_manager import BotManager, BotStatus

# ── App setup ────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24)
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_FILE = "bot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
    ],
)

for name in ("selenium", "urllib3", "webdriver_manager", "werkzeug", "engineio"):
    logging.getLogger(name).setLevel(logging.WARNING)

log = logging.getLogger(__name__)


# ── Custom handler: stream logs to browser via WebSocket ─────────────────────
class SocketIOLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            socketio.emit("log", {"message": msg, "level": record.levelname})
        except Exception:
            pass


_sio_handler = SocketIOLogHandler()
_sio_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
)
logging.getLogger().addHandler(_sio_handler)


# ── Singleton BotManager ────────────────────────────────────────────────────
manager = BotManager()


def _serialize_stats(stats):
    """Convert a stats dict to JSON-safe format."""
    s = dict(stats)
    s["bot_statuses"] = {
        str(k): v.value if isinstance(v, BotStatus) else str(v)
        for k, v in stats.get("bot_statuses", {}).items()
    }
    s.pop("join_times", None)
    return s


def _on_bot_update(bot_id, status, elapsed):
    socketio.emit("bot_update", {
        "bot_id": bot_id,
        "status": status.value if isinstance(status, BotStatus) else str(status),
        "elapsed": round(elapsed, 1),
    })


def _on_stats_update(stats):
    socketio.emit("stats_update", _serialize_stats(stats))


manager.on_bot_update = _on_bot_update
manager.on_stats_update = _on_stats_update


# ── Flask routes ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/defaults")
def api_defaults():
    return jsonify(get_defaults_dict())


@app.route("/api/status")
def api_status():
    return jsonify(_serialize_stats(manager.get_stats()))


# ── SocketIO events ──────────────────────────────────────────────────────────
@socketio.on("connect")
def handle_connect():
    log.info("Dashboard client connected.")
    emit("stats_update", _serialize_stats(manager.get_stats()))


@socketio.on("start")
def handle_start(data):
    try:
        cfg = build_config(
            meeting_id=data["meeting_id"],
            passcode=data["passcode"],
            thread_count=data.get("thread_count", 1),
            num_bots=data.get("num_bots", 1),
            custom_name=data.get("custom_name", ""),
            use_proxies=data.get("use_proxies", False),
            chat_message=data.get("chat_message", ""),
        )
        manager.start(cfg)
        emit("status", {"ok": True, "message": "Launch started."})
    except RuntimeError as exc:
        emit("status", {"ok": False, "message": str(exc)})
    except Exception as exc:
        emit("status", {"ok": False, "message": f"Config error: {exc}"})


@socketio.on("stop")
def handle_stop():
    manager.stop()
    emit("status", {"ok": True, "message": "Stop signal sent."})


@socketio.on("set_auto_restart")
def handle_auto_restart(data):
    enabled = bool(data.get("enabled", False))
    delay = int(data.get("delay", 5))
    manager.set_auto_restart(enabled, delay)
    emit("status", {"ok": True, "message": f"Auto-restart {'enabled' if enabled else 'disabled'}."})


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting web dashboard on http://localhost:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, load_dotenv=False)

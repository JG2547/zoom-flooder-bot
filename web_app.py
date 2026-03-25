# -*- coding: utf-8 -*-

"""Zoom Flooder Bot — Web Dashboard (Flask + SocketIO)."""

import logging
import os

os.environ["FLASK_SKIP_DOTENV"] = "1"

import glob
import re

from flask import Flask, render_template, jsonify, send_from_directory
from flask_socketio import SocketIO, emit

from config import build_config, get_defaults_dict, load_proxies, check_proxy_health
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


SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "screenshots")


@app.route("/screenshots/<path:filename>")
def serve_screenshot(filename):
    return send_from_directory(SCREENSHOT_DIR, filename)


@app.route("/api/screenshots")
def api_screenshots():
    """Return a JSON list of screenshots with parsed metadata."""
    pattern = os.path.join(SCREENSHOT_DIR, "bot*.png")
    files = sorted(glob.glob(pattern), key=os.path.getmtime)
    result = []
    for fpath in files:
        fname = os.path.basename(fpath)
        # Parse: bot{N}_{label}_{timestamp}.png
        m = re.match(r"bot(\d+)_(.+?)_(\d{8}-\d{6})\.png", fname)
        if m:
            result.append({
                "filename": fname,
                "bot_id": int(m.group(1)),
                "label": m.group(2),
                "timestamp": m.group(3),
            })
    return jsonify(result)


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
            chat_recipient=data.get("chat_recipient", ""),
            chat_message=data.get("chat_message", ""),
            waiting_room_timeout=data.get("waiting_room_timeout", 60),
            reactions=data.get("reactions", []),
            reaction_count=data.get("reaction_count", 0),
            reaction_delay=data.get("reaction_delay", 1.0),
            persist_mode=data.get("persist_mode", False),
            persist_interval=data.get("persist_interval", 30),
            persist_chat_interval=data.get("persist_chat_interval", 0),
            persist_reaction_interval=data.get("persist_reaction_interval", 0),
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


@socketio.on("check_proxies")
def handle_check_proxies():
    """Test all proxies and emit results."""
    import threading

    def _run_check():
        proxies = load_proxies()
        if not proxies:
            socketio.emit("proxy_health_result", {"alive": [], "dead": [], "results": {}, "error": "No proxies found in proxies.txt"})
            return
        log.info("Testing %d proxies…", len(proxies))
        result = check_proxy_health(proxies)
        log.info("Proxy check: %d alive, %d dead.", len(result["alive"]), len(result["dead"]))
        socketio.emit("proxy_health_result", result)

    threading.Thread(target=_run_check, daemon=True).start()
    emit("status", {"ok": True, "message": "Proxy health check started…"})


# ── Scheduled raids ──────────────────────────────────────────────────────────
_schedules = {}  # id -> {config, scheduled_time, timer}
_schedule_counter = 0


@socketio.on("schedule_raid")
def handle_schedule_raid(data):
    global _schedule_counter
    from datetime import datetime

    try:
        scheduled_time = datetime.fromisoformat(data["scheduled_time"])
        delay = (scheduled_time - datetime.now()).total_seconds()
        if delay <= 0:
            emit("status", {"ok": False, "message": "Scheduled time must be in the future."})
            return

        _schedule_counter += 1
        sid = str(_schedule_counter)

        cfg = build_config(
            meeting_id=data["meeting_id"],
            passcode=data["passcode"],
            thread_count=data.get("thread_count", 1),
            num_bots=data.get("num_bots", 1),
            custom_name=data.get("custom_name", ""),
            use_proxies=data.get("use_proxies", False),
            chat_recipient=data.get("chat_recipient", ""),
            chat_message=data.get("chat_message", ""),
            reactions=data.get("reactions", []),
            reaction_count=data.get("reaction_count", 0),
            reaction_delay=data.get("reaction_delay", 1.0),
        )

        def _fire():
            log.info("Scheduled raid %s firing now!", sid)
            try:
                manager.start(cfg)
            except RuntimeError as exc:
                log.warning("Scheduled raid %s failed: %s", sid, exc)
            _schedules.pop(sid, None)
            socketio.emit("schedule_update", _get_schedules_list())

        import threading
        timer = threading.Timer(delay, _fire)
        timer.daemon = True
        timer.start()

        _schedules[sid] = {
            "id": sid,
            "meeting_id": data["meeting_id"],
            "scheduled_time": data["scheduled_time"],
            "num_bots": data.get("num_bots", 1),
            "timer": timer,
        }

        socketio.emit("schedule_update", _get_schedules_list())
        emit("status", {"ok": True, "message": f"Raid scheduled for {data['scheduled_time']}."})
    except Exception as exc:
        emit("status", {"ok": False, "message": f"Schedule error: {exc}"})


@socketio.on("cancel_schedule")
def handle_cancel_schedule(data):
    sid = data.get("id")
    sched = _schedules.pop(sid, None)
    if sched and sched.get("timer"):
        sched["timer"].cancel()
        emit("status", {"ok": True, "message": f"Schedule {sid} cancelled."})
    else:
        emit("status", {"ok": False, "message": "Schedule not found."})
    socketio.emit("schedule_update", _get_schedules_list())


@socketio.on("list_schedules")
def handle_list_schedules():
    emit("schedule_update", _get_schedules_list())


def _get_schedules_list():
    return [{"id": s["id"], "meeting_id": s["meeting_id"],
             "scheduled_time": s["scheduled_time"], "num_bots": s["num_bots"]}
            for s in _schedules.values()]


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting web dashboard on http://localhost:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, load_dotenv=False)

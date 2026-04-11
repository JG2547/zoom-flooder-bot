# -*- coding: utf-8 -*-

"""Zoom Flooder Bot — Web Dashboard (Flask + SocketIO)."""

import logging
import os

os.environ["FLASK_SKIP_DOTENV"] = "1"

import glob
import re

from flask import Flask, render_template, jsonify, send_from_directory
from flask_socketio import SocketIO, emit

from config import build_config, get_defaults_dict, load_proxies, check_proxy_health, load_integration_config
from bot_manager import BotManager, BotStatus
from scheduler import RaidScheduler

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


# ── ZMT Control Plane integration ────────────────────────────────────────────
_zmt_cp = None

def _init_zmt_cp():
    global _zmt_cp
    integration = load_integration_config()
    if not integration.get("zmt_enabled"):
        return
    cp_url = integration.get("zmt_cp_url", "")
    if not cp_url:
        return
    try:
        from zmt_client import ZMTClient
        _zmt_cp = ZMTClient(
            server_url=cp_url,
            registration_key=integration.get("zmt_registration_key", ""),
            bot_name=integration.get("zmt_agent_name") or f"flooder-{__import__('platform').node()}",
            bot_mode="flooder",
            version="1.0",
            on_command=_handle_zmt_command,
            log_func=lambda msg, lvl="info": log.info(msg) if lvl != "warn" else log.warning(msg),
        )
        _zmt_cp.connect()
        log.info("ZMT Control Plane client started")
    except Exception as e:
        log.warning(f"ZMT CP init failed: {e}")


def _handle_zmt_command(command):
    """Handle commands from ZMT Control Plane (Deploy Flooder, Stop, Status)."""
    action = command.get("action", "")
    command_id = command.get("commandId", "")
    params = command.get("params", command.get("data", {}))

    if action == "flooder:start":
        try:
            cfg = build_config(
                meeting_id=params.get("meeting_id", ""),
                passcode=params.get("passcode", ""),
                thread_count=int(params.get("thread_count", 1)),
                num_bots=int(params.get("num_bots", 1)),
                custom_name=params.get("custom_name", ""),
                use_proxies=bool(params.get("use_proxies", False)),
                chat_recipient=params.get("chat_recipient", ""),
                chat_message=params.get("chat_message", ""),
            )
            manager.start(cfg)
            if _zmt_cp:
                _zmt_cp.send_command_response(command_id, True, {"message": "Flooder started"})
            log.info(f"ZMT: Flooder started via control plane")
        except Exception as e:
            log.error(f"ZMT flooder start error: {e}")
            if _zmt_cp:
                _zmt_cp.send_command_response(command_id, False, error=str(e))

    elif action == "flooder:stop":
        manager.stop()
        if _zmt_cp:
            _zmt_cp.send_command_response(command_id, True, {"message": "Flooder stopped"})
        log.info("ZMT: Flooder stopped via control plane")

    elif action == "flooder:status":
        stats = _serialize_stats(manager.get_stats())
        if _zmt_cp:
            _zmt_cp.send_command_response(command_id, True, stats)

    elif action == "ping":
        if _zmt_cp:
            _zmt_cp.send_command_response(command_id, True, {"pong": True})


# Wire stats updates to also push to ZMT
_original_on_stats = _on_stats_update

def _on_stats_update_with_zmt(stats):
    _original_on_stats(stats)
    if _zmt_cp and _zmt_cp.is_connected:
        _zmt_cp.send_event("flooder:stats", _serialize_stats(stats))

manager.on_stats_update = _on_stats_update_with_zmt

# Initialize ZMT CP (non-blocking)
_init_zmt_cp()


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


@app.route("/api/participants")
def api_participants():
    """Get participant list from the first active bot (fiber + DOM fallback)."""
    from bot import get_participants, get_participant_count
    drivers = list(manager._active_drivers) if hasattr(manager, '_active_drivers') else []
    if not drivers:
        return jsonify({"participants": [], "badge_count": 0, "error": "No active bots"})
    bot_id_1, driver = drivers[0]
    try:
        badge = get_participant_count(driver)
        plist = get_participants(driver, bot_id_1 - 1)
        return jsonify({
            "participants": plist,
            "list_count": len(plist),
            "badge_count": badge,
            "complete": len(plist) >= badge if badge > 0 else True,
        })
    except Exception as exc:
        return jsonify({"participants": [], "badge_count": 0, "error": str(exc)})


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
            passcode=data.get("passcode", ""),
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
            chat_repeat_count=data.get("chat_repeat_count", 0),
            chat_repeat_delay=data.get("chat_repeat_delay", 2.0),
            chat_monitor_target=data.get("chat_monitor_target", ""),
            chat_monitor_reply=data.get("chat_monitor_reply", ""),
        )
        manager.start(cfg)
        emit("status", {"ok": True, "message": "Launch started."})
    except RuntimeError as exc:
        emit("status", {"ok": False, "message": str(exc)})
    except Exception as exc:
        emit("status", {"ok": False, "message": f"Config error: {exc}"})


@socketio.on("stage")
def handle_stage(data):
    """Launch bots but hold them at the form — waiting for deploy signal."""
    try:
        cfg = build_config(
            meeting_id=data["meeting_id"],
            passcode=data.get("passcode", ""),
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
            chat_repeat_count=data.get("chat_repeat_count", 0),
            chat_repeat_delay=data.get("chat_repeat_delay", 2.0),
            chat_monitor_target=data.get("chat_monitor_target", ""),
            chat_monitor_reply=data.get("chat_monitor_reply", ""),
        )
        manager.stage(cfg)
        emit("status", {"ok": True, "message": "Staging started — bots will wait for deploy."})
    except RuntimeError as exc:
        emit("status", {"ok": False, "message": str(exc)})
    except Exception as exc:
        emit("status", {"ok": False, "message": f"Config error: {exc}"})


@socketio.on("deploy")
def handle_deploy():
    """Release all staged bots to click Join simultaneously."""
    try:
        manager.deploy()
        emit("status", {"ok": True, "message": "Deploy signal sent!"})
    except Exception as exc:
        emit("status", {"ok": False, "message": f"Deploy error: {exc}"})


@socketio.on("stop")
def handle_stop():
    try:
        manager.stop()
        emit("status", {"ok": True, "message": "Stop signal sent."})
    except Exception as exc:
        emit("status", {"ok": False, "message": f"Stop error: {exc}"})


@socketio.on("set_auto_restart")
def handle_auto_restart(data):
    try:
        enabled = bool(data.get("enabled", False))
        delay = int(data.get("delay", 5))
        manager.set_auto_restart(enabled, delay)
        emit("status", {"ok": True, "message": f"Auto-restart {'enabled' if enabled else 'disabled'}."})
    except Exception as exc:
        emit("status", {"ok": False, "message": f"Error: {exc}"})


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


# ── SQLite-backed Scheduled Raids ────────────────────────────────────────────

def _broadcast_schedules():
    """Push the current schedule list to all connected dashboard clients."""
    socketio.emit("schedule_update", raid_scheduler.list_pending())


raid_scheduler = RaidScheduler(bot_manager=manager, on_update=_broadcast_schedules)


@socketio.on("schedule_raid")
def handle_schedule_raid(data):
    try:
        raid_id = raid_scheduler.schedule_raid(data, data["scheduled_time"], source="web")
        emit("status", {"ok": True, "message": f"Raid #{raid_id} scheduled for {data['scheduled_time']}."})
    except ValueError as exc:
        emit("status", {"ok": False, "message": str(exc)})
    except Exception as exc:
        emit("status", {"ok": False, "message": f"Schedule error: {exc}"})


@socketio.on("cancel_schedule")
def handle_cancel_schedule(data):
    raid_id = data.get("id")
    try:
        raid_id = int(raid_id)
    except (TypeError, ValueError):
        emit("status", {"ok": False, "message": "Invalid raid ID."})
        return
    if raid_scheduler.cancel_raid(raid_id):
        emit("status", {"ok": True, "message": f"Raid #{raid_id} cancelled."})
    else:
        emit("status", {"ok": False, "message": "Schedule not found or already fired."})


@socketio.on("list_schedules")
def handle_list_schedules():
    emit("schedule_update", raid_scheduler.list_pending())


# ── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Start the persistent scheduler
    raid_scheduler.start()

    # Optional integrations (Discord / Telegram)
    _integrations = load_integration_config()

    if _integrations.get("discord_token"):
        try:
            from discord_bot import start_discord_bot
            start_discord_bot(
                _integrations["discord_token"],
                manager,
                raid_scheduler,
                guild_id=_integrations.get("discord_guild_id"),
                allowed_channels=_integrations.get("allowed_discord_channels"),
            )
            log.info("Discord bot started.")
        except ImportError:
            log.warning("discord.py not installed — Discord integration disabled.")
        except Exception as exc:
            log.warning("Discord bot failed to start: %s", exc)

    if _integrations.get("telegram_token"):
        try:
            from telegram_bot import start_telegram_bot
            start_telegram_bot(
                _integrations["telegram_token"],
                manager,
                raid_scheduler,
                allowed_users=_integrations.get("allowed_telegram_users"),
            )
            log.info("Telegram bot started.")
        except ImportError:
            log.warning("python-telegram-bot not installed — Telegram integration disabled.")
        except Exception as exc:
            log.warning("Telegram bot failed to start: %s", exc)

    log.info("Starting web dashboard on http://localhost:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=False, load_dotenv=False)

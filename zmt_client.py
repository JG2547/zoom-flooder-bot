"""
ZMT Control Plane Client — Shared module for Python bots.

Handles agent registration, heartbeats, blocklist sync, and WebSocket
command dispatch with the Zoom Control Plane dashboard.

Usage:
    from zmt_client import ZMTClient

    client = ZMTClient(
        server_url="https://your-zmt-server.railway.app",
        registration_key="your-key",
        bot_name="auto-kick-WIN10",
        bot_mode="hostbot",
        version="4.0",
        on_command=my_command_handler,
    )
    client.connect()
    # ... bot logic ...
    client.disconnect()

Works standalone — all errors are caught and logged, never crash the bot.
"""

import json
import hashlib
import logging
import os
import platform
import re
import threading
import time
import uuid

try:
    import requests as _requests
    _REQUESTS_OK = True
except ImportError:
    _REQUESTS_OK = False

try:
    import websocket as _ws_lib
    _WS_OK = True
except ImportError:
    _WS_OK = False

_TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".zmt_agent_token.json")
_VERSION = "1.0.0"

logger = logging.getLogger("zmt_client")


class ZMTClient:
    """Persistent connection to the ZMT Control Plane."""

    def __init__(
        self,
        server_url,
        registration_key="",
        bot_name="",
        bot_mode="hostbot",
        version="1.0",
        on_command=None,
        on_blocklist_updated=None,
        log_func=None,
    ):
        self.server_url = server_url.rstrip("/") if server_url else ""
        self.registration_key = registration_key
        self.bot_name = bot_name or f"{bot_mode}-{platform.node()}"
        self.bot_mode = bot_mode  # "hostbot" or "flooder"
        self.version = version
        self.on_command = on_command
        self.on_blocklist_updated = on_blocklist_updated
        self._log_func = log_func

        # State
        self._agent_id = None
        self._auth_token = None
        self._machine_id = self._compute_machine_id()
        self._connected = False
        self._start_time = None
        self._shutdown = threading.Event()
        self._lock = threading.Lock()

        # Blocklist cache
        self._blocklist = []
        self._blocklist_hash = ""

        # WebSocket
        self._ws = None
        self._ws_thread = None

        # Threads
        self._heartbeat_thread = None
        self._blocklist_thread = None

        # Reconnection
        self._consecutive_failures = 0
        self._max_backoff = 30

    # ── Public API ────────────────────────────────────

    def connect(self):
        """Register with ZMT and start background threads. Non-blocking."""
        if not self.server_url:
            self._log("ZMT client: no server URL configured, skipping", "warn")
            return False
        if not _REQUESTS_OK:
            self._log("ZMT client: 'requests' library not installed", "warn")
            return False

        self._shutdown.clear()
        self._start_time = time.time()

        # Try cached token first, then register
        if not self._try_cached_token():
            if not self._register():
                self._log("ZMT: registration failed, will retry in background", "warn")

        # Start background threads
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="zmt-heartbeat"
        )
        self._heartbeat_thread.start()

        if self.bot_mode == "hostbot":
            self._blocklist_thread = threading.Thread(
                target=self._blocklist_sync_loop, daemon=True, name="zmt-blocklist"
            )
            self._blocklist_thread.start()

        # WebSocket (optional — only if websocket-client is installed)
        if _WS_OK and self._auth_token:
            self._start_ws()

        return self._connected

    def disconnect(self):
        """Graceful shutdown of all threads."""
        self._shutdown.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._connected = False
        self._log("ZMT: disconnected")

    @property
    def is_connected(self):
        return self._connected

    @property
    def agent_id(self):
        return self._agent_id

    def get_blocklist(self):
        """Return cached blocklist (thread-safe copy)."""
        with self._lock:
            return list(self._blocklist)

    def check_name(self, name):
        """Check if a participant name matches any blocklist pattern.
        Returns the matched pattern dict or None."""
        with self._lock:
            patterns = list(self._blocklist)
        name_lower = name.lower()
        for p in patterns:
            if not p.get("is_active", True):
                continue
            pattern = p.get("name_pattern", "")
            ptype = p.get("pattern_type", "literal")
            try:
                if ptype == "regex":
                    if re.search(pattern, name, re.IGNORECASE):
                        return p
                else:
                    # Literal: substring match (bidirectional, case-insensitive)
                    if pattern.lower() in name_lower or name_lower in pattern.lower():
                        return p
            except re.error:
                continue
        return None

    def send_event(self, event_type, data=None):
        """Send event over WebSocket to control plane."""
        if self._ws and self._connected:
            try:
                msg = {"type": event_type, **(data or {})}
                self._ws.send(json.dumps(msg))
            except Exception as e:
                self._log(f"ZMT WS send error: {e}", "warn")

    def send_command_response(self, command_id, success, result=None, error=None):
        """Respond to a command received via WebSocket."""
        msg = {
            "type": "command:response",
            "commandId": command_id,
            "success": success,
        }
        if result is not None:
            msg["result"] = result
        if error is not None:
            msg["error"] = error
        self.send_event("command:response", msg)

    # ── Registration ──────────────────────────────────

    def _register(self):
        """POST /api/agents/register"""
        try:
            r = _requests.post(
                f"{self.server_url}/api/agents/register",
                json={
                    "registration_key": self.registration_key,
                    "name": self.bot_name,
                    "machine_id": self._machine_id,
                    "capabilities": {"mode": self.bot_mode},
                },
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("ok"):
                    self._agent_id = data.get("agent_id")
                    self._auth_token = data.get("auth_token")
                    self._connected = True
                    self._consecutive_failures = 0
                    self._save_token()
                    self._log(f"ZMT: registered as {self.bot_name} (agent={self._agent_id})", "success")
                    return True
                else:
                    self._log(f"ZMT register rejected: {data.get('error', 'unknown')}", "warn")
            else:
                self._log(f"ZMT register HTTP {r.status_code}: {r.text[:200]}", "warn")
        except _requests.ConnectionError:
            self._log("ZMT: server unreachable (connection refused)", "warn")
        except Exception as e:
            self._log(f"ZMT register error: {e}", "warn")
        return False

    def _try_cached_token(self):
        """Load persisted token and verify it's still valid."""
        try:
            if not os.path.exists(_TOKEN_FILE):
                return False
            with open(_TOKEN_FILE, "r") as f:
                cached = json.load(f)
            if cached.get("server_url") != self.server_url:
                return False
            self._agent_id = cached.get("agent_id")
            self._auth_token = cached.get("auth_token")
            if not self._auth_token:
                return False
            # Verify with a heartbeat
            r = _requests.post(
                f"{self.server_url}/api/bots/heartbeat",
                json={"service": self.bot_name, "status": "online", "version": self.version},
                headers=self._auth_headers(),
                timeout=10,
            )
            if r.status_code == 200:
                self._connected = True
                self._log(f"ZMT: reconnected with cached token (agent={self._agent_id})", "success")
                return True
            elif r.status_code == 401:
                self._log("ZMT: cached token expired, re-registering", "warn")
                os.remove(_TOKEN_FILE)
        except Exception:
            pass
        return False

    def _save_token(self):
        try:
            with open(_TOKEN_FILE, "w") as f:
                json.dump({
                    "agent_id": self._agent_id,
                    "auth_token": self._auth_token,
                    "server_url": self.server_url,
                    "registered_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }, f)
        except Exception:
            pass

    def _auth_headers(self):
        return {"Authorization": f"Bearer {self._auth_token}"} if self._auth_token else {}

    # ── Heartbeat ─────────────────────────────────────

    def _heartbeat_loop(self):
        while not self._shutdown.is_set():
            try:
                if not self._auth_token:
                    # Try to register
                    self._register()
                    if self._auth_token and _WS_OK and not self._ws:
                        self._start_ws()
                elif self._auth_token:
                    uptime = int(time.time() - self._start_time) if self._start_time else 0
                    r = _requests.post(
                        f"{self.server_url}/api/bots/heartbeat",
                        json={
                            "service": self.bot_name,
                            "status": "online",
                            "uptime": uptime,
                            "version": self.version,
                        },
                        headers=self._auth_headers(),
                        timeout=10,
                    )
                    if r.status_code == 200:
                        self._connected = True
                        self._consecutive_failures = 0
                    elif r.status_code == 401:
                        self._log("ZMT: heartbeat 401, re-registering", "warn")
                        self._auth_token = None
                        self._connected = False
                    else:
                        self._on_failure()
            except _requests.ConnectionError:
                self._on_failure()
            except Exception as e:
                self._log(f"ZMT heartbeat error: {e}", "warn")
                self._on_failure()

            # Wait 30s (or shorter intervals when failing)
            interval = 30 if self._consecutive_failures < 3 else 60
            self._shutdown.wait(interval)

    # ── Blocklist sync ────────────────────────────────

    def _blocklist_sync_loop(self):
        # Initial delay to let registration complete
        self._shutdown.wait(5)

        while not self._shutdown.is_set():
            if self._auth_token:
                try:
                    r = _requests.get(
                        f"{self.server_url}/api/name-enforcement/blocklist",
                        headers=self._auth_headers(),
                        timeout=10,
                    )
                    if r.status_code == 200:
                        data = r.json()
                        patterns = data.get("patterns", [])
                        # Check if changed
                        new_hash = hashlib.md5(
                            json.dumps(patterns, sort_keys=True).encode()
                        ).hexdigest()
                        if new_hash != self._blocklist_hash:
                            with self._lock:
                                self._blocklist = patterns
                                self._blocklist_hash = new_hash
                            active = sum(1 for p in patterns if p.get("is_active", True))
                            self._log(f"ZMT: blocklist synced ({active} active patterns)")
                            if self.on_blocklist_updated:
                                try:
                                    self.on_blocklist_updated(patterns)
                                except Exception as e:
                                    self._log(f"ZMT blocklist callback error: {e}", "warn")
                except Exception as e:
                    self._log(f"ZMT blocklist sync error: {e}", "warn")

            self._shutdown.wait(60)

    # ── WebSocket ─────────────────────────────────────

    def _start_ws(self):
        if not _WS_OK or not self._auth_token:
            return
        self._ws_thread = threading.Thread(
            target=self._ws_loop, daemon=True, name="zmt-ws"
        )
        self._ws_thread.start()

    def _ws_loop(self):
        backoff = 1
        while not self._shutdown.is_set():
            try:
                ws_url = self.server_url.replace("http://", "ws://").replace("https://", "wss://")
                ws_url = f"{ws_url}/agent-ws"

                ws = _ws_lib.WebSocketApp(
                    ws_url,
                    header={"Authorization": f"Bearer {self._auth_token}"},
                    on_open=self._ws_on_open,
                    on_message=self._ws_on_message,
                    on_error=self._ws_on_error,
                    on_close=self._ws_on_close,
                )
                self._ws = ws
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                self._log(f"ZMT WS error: {e}", "warn")

            if self._shutdown.is_set():
                break

            # Reconnect with backoff
            self._shutdown.wait(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    def _ws_on_open(self, ws):
        self._log("ZMT: WebSocket connected")
        # Send auth message
        ws.send(json.dumps({
            "type": "auth",
            "token": self._auth_token,
            "agentId": self._agent_id,
            "mode": self.bot_mode,
        }))

    def _ws_on_message(self, ws, message):
        try:
            data = json.loads(message)
            msg_type = data.get("type", "")

            if msg_type == "auth":
                if data.get("success"):
                    self._log("ZMT: WebSocket authenticated")
                else:
                    self._log(f"ZMT: WS auth failed: {data.get('error')}", "warn")

            elif msg_type == "pong":
                pass  # heartbeat ack

            elif msg_type == "command":
                # Inbound command from control plane
                if self.on_command:
                    try:
                        self.on_command(data)
                    except Exception as e:
                        self._log(f"ZMT command handler error: {e}", "warn")
                        cmd_id = data.get("commandId")
                        if cmd_id:
                            self.send_command_response(cmd_id, False, error=str(e))
        except json.JSONDecodeError:
            pass

    def _ws_on_error(self, ws, error):
        if not self._shutdown.is_set():
            self._log(f"ZMT WS error: {error}", "warn")

    def _ws_on_close(self, ws, close_status_code, close_msg):
        if not self._shutdown.is_set():
            self._log("ZMT: WebSocket disconnected, will reconnect")
        self._ws = None

    # ── Helpers ────────────────────────────────────────

    def _compute_machine_id(self):
        raw = f"{platform.node()}-{uuid.getnode()}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _on_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= 6:
            self._connected = False

    def _log(self, msg, level="info"):
        if self._log_func:
            self._log_func(msg, level)
        else:
            if level == "warn":
                logger.warning(msg)
            elif level == "success":
                logger.info(msg)
            else:
                logger.info(msg)

# -*- coding: utf-8 -*-

"""Thread-safe bot orchestration engine.

Designed to be driven by either the CLI (main.py) or the web dashboard (web_app.py).
Only one launch session is permitted at a time (enforced by a Lock).
"""

import atexit
import concurrent.futures
import enum
import logging
import random
import threading
import time

from bot import (launch_bot, init_name_pool, leave_meeting,
                 check_bot_alive, send_chat_message, spam_reactions)

log = logging.getLogger(__name__)


class BotStatus(enum.Enum):
    PENDING = "pending"
    JOINING = "joining"
    JOINED = "joined"
    LEAVING = "leaving"
    LEFT = "left"
    FAILED = "failed"
    CAPTCHA = "captcha"
    WAITING_ROOM = "waiting_room"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"


class BotManager:
    """Manages the full lifecycle of a bot-launch session."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active_drivers = []
        self._running = False
        self._stop_event = threading.Event()
        self._run_thread = None
        self._auto_restart = False
        self._restart_delay = 5        # seconds between restart cycles

        # Live stats — guarded by _stats_lock
        self._stats_lock = threading.Lock()
        self._bot_statuses = {}
        self._join_times = []
        self._succeeded = 0
        self._failed = 0
        self._total = 0
        self._cycle = 0                # restart cycle counter

        # Callback hooks (set by the consumer: CLI or web layer)
        self.on_bot_update = None      # fn(bot_id, status, elapsed)
        self.on_stats_update = None    # fn(stats_dict)

        atexit.register(self._emergency_cleanup)

    # ── Public read-only snapshot ────────────────────────────────────────────
    def get_stats(self):
        with self._stats_lock:
            jt = list(self._join_times)
            return {
                "total": self._total,
                "succeeded": self._succeeded,
                "failed": self._failed,
                "join_times": jt,
                "avg_time": (sum(jt) / len(jt)) if jt else 0,
                "fastest": min(jt) if jt else 0,
                "slowest": max(jt) if jt else 0,
                "bot_statuses": dict(self._bot_statuses),
                "running": self._running,
                "auto_restart": self._auto_restart,
                "cycle": self._cycle,
            }

    @property
    def is_running(self):
        return self._running

    # ── Auto-restart control ────────────────────────────────────────────────
    def set_auto_restart(self, enabled, delay=5):
        """Enable or disable auto-restart between cycles."""
        self._auto_restart = bool(enabled)
        self._restart_delay = max(1, int(delay))
        log.info("Auto-restart %s (delay: %ds)", "enabled" if self._auto_restart else "disabled", self._restart_delay)

    # ── Start ────────────────────────────────────────────────────────────────
    def start(self, cfg):
        """Begin launching bots.  Returns immediately; work runs in a daemon thread.

        Raises RuntimeError if a session is already active.
        """
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("A launch session is already active.")

        self._stop_event.clear()
        self._running = True
        self._active_drivers.clear()
        self._cycle = 0

        # Reset stats
        with self._stats_lock:
            self._bot_statuses = {i: BotStatus.PENDING for i in range(cfg["num_bots"])}
            self._join_times = []
            self._succeeded = 0
            self._failed = 0
            self._total = cfg["num_bots"]

        self._notify_stats()

        self._run_thread = threading.Thread(target=self._run, args=(cfg,), daemon=True)
        self._run_thread.start()

    # ── Background launch loop ───────────────────────────────────────────────
    def _run(self, cfg):
        try:
            while True:
                self._cycle += 1
                self._run_single_cycle(cfg)

                # Persistence mode: keep bots alive and monitor them
                if cfg.get("persist_mode") and self._active_drivers and not self._stop_event.is_set():
                    self._persistence_loop(cfg)

                # Check if we should auto-restart
                if self._stop_event.is_set() or not self._auto_restart:
                    break

                # Shut down current drivers before restarting
                log.info("── Auto-restart: shutting down cycle %d drivers…", self._cycle)
                self._shutdown_drivers()

                # Wait before restarting (interruptible)
                log.info("── Auto-restart: waiting %ds before cycle %d…", self._restart_delay, self._cycle + 1)
                self._stop_event.wait(timeout=self._restart_delay)
                if self._stop_event.is_set():
                    break

                # Reset stats for new cycle
                num_bots = cfg["num_bots"]
                with self._stats_lock:
                    self._bot_statuses = {i: BotStatus.PENDING for i in range(num_bots)}
                    self._join_times = []
                    self._succeeded = 0
                    self._failed = 0
                    self._total = num_bots
                self._notify_stats()
        finally:
            if self._stop_event.is_set():
                self._shutdown_drivers()
            self._running = False
            self._lock.release()
            self._notify_stats()

    # ── Persistence loop ──────────────────────────────────────────────────────
    def _persistence_loop(self, cfg):
        """Keep bots alive, send periodic messages/reactions, and rejoin if kicked."""
        interval = cfg.get("persist_interval", 30)
        chat_interval = cfg.get("persist_chat_interval", 0)
        reaction_interval = cfg.get("persist_reaction_interval", 0)
        chat_msg = cfg.get("chat_message", "")
        reactions = cfg.get("reactions", [])
        reaction_count = cfg.get("reaction_count", 0)

        log.info("── Persistence mode active (interval: %ds) ──", interval)
        last_chat = time.monotonic()
        last_reaction = time.monotonic()

        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=interval)
            if self._stop_event.is_set():
                break

            now = time.monotonic()
            dead_bots = []

            for bot_id_1, driver in list(self._active_drivers):
                bot_id = bot_id_1 - 1
                try:
                    alive = check_bot_alive(driver)
                except Exception:
                    alive = False

                if not alive:
                    log.warning("Bot %d: Disconnected from meeting.", bot_id_1)
                    self._set_status(bot_id, BotStatus.DISCONNECTED)
                    dead_bots.append((bot_id_1, driver))
                    continue

                # Periodic chat
                if chat_interval > 0 and chat_msg and (now - last_chat) >= chat_interval:
                    try:
                        send_chat_message(driver, bot_id, chat_msg)
                    except Exception:
                        pass

                # Periodic reactions
                if (reaction_interval > 0 and reactions and reaction_count > 0 and
                        (now - last_reaction) >= reaction_interval):
                    try:
                        spam_reactions(driver, bot_id, reactions, 1, 0.5)
                    except Exception:
                        pass

            if chat_interval > 0 and (now - last_chat) >= chat_interval:
                last_chat = now
            if reaction_interval > 0 and (now - last_reaction) >= reaction_interval:
                last_reaction = now

            # Clean up dead bots
            for bot_id_1, driver in dead_bots:
                self._active_drivers.remove((bot_id_1, driver))
                try:
                    driver.quit()
                except Exception:
                    pass

                # Attempt rejoin
                bot_id = bot_id_1 - 1
                self._set_status(bot_id, BotStatus.RECONNECTING)
                log.info("Bot %d: Attempting rejoin…", bot_id_1)
                try:
                    new_driver, elapsed = launch_bot(
                        bot_id=bot_id,
                        meeting_id=cfg["meeting_id"],
                        passcode=cfg["passcode"],
                        names_list=cfg["names_list"],
                        custom_name=cfg["custom_name"],
                        stop_event=self._stop_event,
                        proxies=cfg.get("proxies"),
                        status_callback=self._bot_status_callback,
                        persist_mode=True,
                    )
                    if new_driver and new_driver not in ("left", "captcha"):
                        self._active_drivers.append((bot_id_1, new_driver))
                        self._set_status(bot_id, BotStatus.JOINED)
                        log.info("Bot %d: Rejoined successfully.", bot_id_1)
                    else:
                        self._set_status(bot_id, BotStatus.FAILED)
                        log.warning("Bot %d: Rejoin failed.", bot_id_1)
                except Exception as exc:
                    self._set_status(bot_id, BotStatus.FAILED)
                    log.warning("Bot %d: Rejoin error: %s", bot_id_1, exc)

            # If all bots are dead and rejoin failed, exit persistence
            if not self._active_drivers:
                log.info("── All bots disconnected, exiting persistence mode ──")
                break

    def _run_single_cycle(self, cfg):
        """Execute one full launch-all-bots cycle."""
        num_bots = cfg["num_bots"]
        batch_size = min(cfg["thread_count"], num_bots)
        total_batches = (num_bots + batch_size - 1) // batch_size

        init_name_pool(cfg["names_list"])
        log.info("Cycle %d: Launching %d bots in %d batch(es)…", self._cycle, num_bots, total_batches)

        with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as pool:
            for batch_idx in range(total_batches):
                if self._stop_event.is_set():
                    log.info("Launch cancelled by user.")
                    break

                start = batch_idx * batch_size
                end = min(start + batch_size, num_bots)

                log.info(
                    "Batch %d/%d (Bots %d–%d)…",
                    batch_idx + 1, total_batches, start + 1, end,
                )

                futures = {}
                for i in range(start, end):
                    self._set_status(i, BotStatus.JOINING)
                    futures[pool.submit(
                        launch_bot,
                        bot_id=i,
                        meeting_id=cfg["meeting_id"],
                        passcode=cfg["passcode"],
                        names_list=cfg["names_list"],
                        custom_name=cfg["custom_name"],
                        stop_event=self._stop_event,
                        proxies=cfg.get("proxies"),
                        chat_recipient=cfg.get("chat_recipient", ""),
                        chat_message=cfg.get("chat_message", ""),
                        status_callback=self._bot_status_callback,
                        waiting_room_timeout=cfg.get("waiting_room_timeout", 60),
                        reactions=cfg.get("reactions"),
                        reaction_count=cfg.get("reaction_count", 0),
                        reaction_delay=cfg.get("reaction_delay", 1.0),
                        persist_mode=cfg.get("persist_mode", False),
                    )] = i

                for future in concurrent.futures.as_completed(futures):
                    bot_id = futures[future]
                    try:
                        driver, elapsed = future.result()
                    except Exception as exc:
                        log.error("Bot %d: Unexpected error: %s", bot_id + 1, exc)
                        driver, elapsed = None, 0.0

                    with self._stats_lock:
                        self._join_times.append(elapsed)
                        if driver == "left":
                            # Bot joined, sent message, and already left gracefully
                            self._succeeded += 1
                            self._bot_statuses[bot_id] = BotStatus.LEFT
                        elif driver == "captcha":
                            # Bot hit a captcha/challenge — don't retry
                            self._failed += 1
                            self._bot_statuses[bot_id] = BotStatus.CAPTCHA
                        elif driver:
                            self._active_drivers.append((bot_id + 1, driver))
                            self._succeeded += 1
                            self._bot_statuses[bot_id] = BotStatus.JOINED
                        else:
                            self._failed += 1
                            self._bot_statuses[bot_id] = BotStatus.FAILED

                    self._notify_bot(bot_id, elapsed)
                    self._notify_stats()

                # Inter-batch delay (interruptible)
                if batch_idx < total_batches - 1 and not self._stop_event.is_set():
                    delay = random.uniform(1, 2)
                    log.info("Batch done. Waiting %.1fs…", delay)
                    self._stop_event.wait(timeout=delay)

        self._log_summary()

    # ── Stop / Shutdown ──────────────────────────────────────────────────────
    def stop(self):
        """Signal the launch loop to stop.  Shutdown happens inside _run."""
        self._stop_event.set()
        log.info("Stop signal sent — waiting for active bots to finish…")

    def _shutdown_drivers(self):
        drivers = list(self._active_drivers)
        if not drivers:
            return
        drivers.sort(key=lambda x: x[0])

        def _leave_and_quit(item):
            bot_id, driver = item
            try:
                self._set_status(bot_id - 1, BotStatus.LEAVING)
                log.info("Bot %d: Leaving meeting…", bot_id)
                leave_meeting(driver, bot_id)
                self._set_status(bot_id - 1, BotStatus.LEFT)
            except Exception:
                log.debug("Bot %d: Graceful leave failed, force-quitting.", bot_id)
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(len(drivers), 1)) as pool:
            list(pool.map(_leave_and_quit, drivers))

        self._active_drivers.clear()
        log.info("All %d bots left and exited.", len(drivers))

    # ── Internals ────────────────────────────────────────────────────────────
    def _bot_status_callback(self, bot_id, status_str):
        """Called from launch_bot threads for mid-flight status updates."""
        status_map = {
            "waiting_room": BotStatus.WAITING_ROOM,
            "disconnected": BotStatus.DISCONNECTED,
            "reconnecting": BotStatus.RECONNECTING,
        }
        status = status_map.get(status_str)
        if status:
            self._set_status(bot_id, status)

    def _set_status(self, bot_id, status):
        with self._stats_lock:
            self._bot_statuses[bot_id] = status
        self._notify_bot(bot_id, 0)
        self._notify_stats()

    def _notify_bot(self, bot_id, elapsed):
        if self.on_bot_update:
            try:
                self.on_bot_update(
                    bot_id,
                    self._bot_statuses.get(bot_id, BotStatus.PENDING),
                    elapsed,
                )
            except Exception:
                pass

    def _notify_stats(self):
        if self.on_stats_update:
            try:
                self.on_stats_update(self.get_stats())
            except Exception:
                pass

    def _log_summary(self):
        stats = self.get_stats()
        log.info("─── Results ───")
        log.info("  Succeeded : %d / %d", stats["succeeded"], stats["total"])
        log.info("  Failed    : %d / %d", stats["failed"], stats["total"])
        if stats["join_times"]:
            log.info("  Avg time  : %.1fs", stats["avg_time"])
            log.info("  Fastest   : %.1fs", stats["fastest"])
            log.info("  Slowest   : %.1fs", stats["slowest"])
        log.info("───────────────")

    def _emergency_cleanup(self):
        for _, driver in self._active_drivers:
            try:
                driver.quit()
            except Exception:
                pass

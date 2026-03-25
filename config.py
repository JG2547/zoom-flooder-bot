# -*- coding: utf-8 -*-

import concurrent.futures
import logging
import os
import sys
import time
import urllib.request

log = logging.getLogger(__name__)

DEFAULT_CONFIG_FILE = "default.txt"
NAMES_FILE = "names.txt"
PROXIES_FILE = "proxies.txt"
FALLBACK_NAMES = ["User", "Participant", "Student", "Guest", "Attendee"]

# RAM estimate per Chrome instance in MB
RAM_PER_BOT_MB = 200
RAM_WARN_THRESHOLD_MB = 4000


def load_defaults(path=DEFAULT_CONFIG_FILE):
    """Load default thread count, meeting ID, and passcode from config file.

    Raises FileNotFoundError if the config file is missing.
    """
    with open(path, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f.readlines()]
    if len(lines) < 3:
        log.warning("Config file has fewer than 3 lines, some defaults may be missing.")
        lines.extend([""] * (3 - len(lines)))
    return lines


def load_names(path=NAMES_FILE):
    """Load randomized bot names from file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            names = [name.strip() for name in f if name.strip()]
        if not names:
            log.warning("'%s' is empty, using fallback names.", path)
            return list(FALLBACK_NAMES)
        return names
    except FileNotFoundError:
        log.warning("'%s' not found, using fallback names.", path)
        return list(FALLBACK_NAMES)


def load_proxies(path=PROXIES_FILE):
    """Load proxy list from file. Returns empty list if none found."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            proxies = [
                line.strip() for line in f
                if line.strip() and not line.strip().startswith("#")
            ]
        if proxies:
            log.info("Loaded %d proxies from '%s'.", len(proxies), path)
        return proxies
    except FileNotFoundError:
        return []


# ── CLI helpers (used by main.py) ────────────────────────────────────────────

def _prompt_int(prompt, default=None):
    """Prompt for an integer with optional default. Retries on invalid input."""
    safe_default = None
    if default is not None:
        try:
            safe_default = int(default)
        except (ValueError, TypeError):
            safe_default = None

    while True:
        raw = input(prompt).strip()
        if not raw and safe_default is not None:
            return safe_default
        if not raw and safe_default is None:
            print("No default available. Please enter a number.")
            continue
        try:
            value = int(raw)
            if value <= 0:
                print("Please enter a positive number.")
                continue
            return value
        except ValueError:
            print("Invalid number. Please try again.")


def get_user_config():
    """Interactive CLI prompt. Returns a config dict."""
    try:
        defaults = load_defaults()
    except FileNotFoundError:
        log.error("Config file '%s' not found!", DEFAULT_CONFIG_FILE)
        sys.exit(1)

    names = load_names()

    thread_count = _prompt_int(
        "Thread Count (Blank for Default): ", default=defaults[0]
    )
    meeting_id = (
        input("Meeting ID (Blank for Default): ").strip() or defaults[1]
    )
    passcode = (
        input("Meeting Passcode (Blank for Default): ").strip() or defaults[2]
    )
    num_bots = _prompt_int("Number of Bots: ")
    custom_name = input("Bot Name (Blank for Random): ").strip()

    estimated_ram = num_bots * RAM_PER_BOT_MB
    if estimated_ram > RAM_WARN_THRESHOLD_MB:
        log.warning(
            "%d bots may use ~%d MB of RAM. Proceed with caution.",
            num_bots,
            estimated_ram,
        )

    os.system("cls" if os.name == "nt" else "clear")

    return {
        "thread_count": thread_count,
        "meeting_id": meeting_id,
        "passcode": passcode,
        "num_bots": num_bots,
        "custom_name": custom_name,
        "names_list": names,
        "proxies": load_proxies(),
    }


# ── Programmatic helpers (used by web_app.py) ────────────────────────────────

def build_config(meeting_id, passcode, thread_count, num_bots, custom_name="",
                  use_proxies=False, chat_recipient="", chat_message="",
                  waiting_room_timeout=60, reactions=None, reaction_count=0,
                  reaction_delay=1.0, persist_mode=False, persist_interval=30,
                  persist_chat_interval=0, persist_reaction_interval=0):
    """Build a config dict from explicit values — no input() calls."""
    names = load_names()

    thread_count = int(thread_count)
    num_bots = int(num_bots)

    if thread_count <= 0 or num_bots <= 0:
        raise ValueError("thread_count and num_bots must be positive integers.")

    estimated_ram = num_bots * RAM_PER_BOT_MB
    if estimated_ram > RAM_WARN_THRESHOLD_MB:
        log.warning(
            "%d bots may use ~%d MB of RAM. Proceed with caution.",
            num_bots,
            estimated_ram,
        )

    proxies = load_proxies() if use_proxies else []
    if use_proxies and not proxies:
        log.warning("Proxy rotation enabled but proxies.txt is empty or missing.")

    return {
        "thread_count": thread_count,
        "meeting_id": str(meeting_id).strip(),
        "passcode": str(passcode).strip(),
        "num_bots": num_bots,
        "custom_name": str(custom_name).strip(),
        "names_list": names,
        "proxies": proxies,
        "chat_recipient": str(chat_recipient).strip() if chat_recipient else "",
        "chat_message": str(chat_message).strip() if chat_message else "",
        "waiting_room_timeout": max(10, int(waiting_room_timeout)),
        "reactions": reactions or [],
        "reaction_count": max(0, int(reaction_count)),
        "reaction_delay": max(0.1, float(reaction_delay)),
        "persist_mode": bool(persist_mode),
        "persist_interval": max(5, int(persist_interval)),
        "persist_chat_interval": max(0, int(persist_chat_interval)),
        "persist_reaction_interval": max(0, int(persist_reaction_interval)),
    }


def get_defaults_dict():
    """Return defaults as a dict for pre-populating the web form."""
    try:
        lines = load_defaults()
    except FileNotFoundError:
        return {"thread_count": 1, "meeting_id": "", "passcode": ""}

    safe_thread = 1
    try:
        safe_thread = int(lines[0])
    except (ValueError, TypeError):
        pass

    mid = lines[1] if lines[1] and not lines[1].startswith("Replace") else ""
    pwd = lines[2] if lines[2] and not lines[2].startswith("Replace") else ""

    return {
        "thread_count": safe_thread,
        "meeting_id": mid,
        "passcode": pwd,
    }


# ── Proxy health checking ──────────────────────────────────────────────────

def _test_one_proxy(proxy, timeout=5):
    """Test a single proxy. Returns (proxy, ok, latency_ms)."""
    handler = urllib.request.ProxyHandler({"https": proxy, "http": proxy})
    opener = urllib.request.build_opener(handler)
    t0 = time.monotonic()
    try:
        opener.open("https://zoom.us", timeout=timeout)
        latency = int((time.monotonic() - t0) * 1000)
        return (proxy, True, latency)
    except Exception:
        return (proxy, False, 0)


def check_proxy_health(proxies, timeout=5, max_workers=10):
    """Test a list of proxies in parallel.

    Returns {"alive": [...], "dead": [...], "results": {proxy: {"ok": bool, "latency_ms": int}}}.
    """
    alive, dead, results = [], [], {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(max_workers, len(proxies) or 1)) as pool:
        futures = {pool.submit(_test_one_proxy, p, timeout): p for p in proxies}
        for future in concurrent.futures.as_completed(futures):
            proxy, ok, latency = future.result()
            results[proxy] = {"ok": ok, "latency_ms": latency}
            if ok:
                alive.append(proxy)
            else:
                dead.append(proxy)
    return {"alive": alive, "dead": dead, "results": results}

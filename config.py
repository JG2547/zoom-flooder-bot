# -*- coding: utf-8 -*-

import logging
import os
import sys

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
    with open(path, "r") as f:
        lines = [line.strip() for line in f.readlines()]
    if len(lines) < 3:
        log.warning("Config file has fewer than 3 lines, some defaults may be missing.")
        lines.extend([""] * (3 - len(lines)))
    return lines


def load_names(path=NAMES_FILE):
    """Load randomized bot names from file."""
    try:
        with open(path, "r") as f:
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
        with open(path, "r") as f:
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
                  use_proxies=False, chat_message=""):
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
        "chat_message": str(chat_message).strip() if chat_message else "",
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

# -*- coding: utf-8 -*-

import logging
import os
import shutil
import subprocess
import threading

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

try:
    from webdriver_manager.chrome import ChromeDriverManager

    _HAS_MANAGER = True
except ImportError:
    _HAS_MANAGER = False

log = logging.getLogger(__name__)

PAGE_LOAD_TIMEOUT = 30

# ── Cached driver path (resolved once, reused for every bot) ─────────────────
_driver_path_cache = None
_cache_lock = threading.Lock()


def _resolve_driver_path():
    """Resolve the ChromeDriver executable path once and cache it."""
    global _driver_path_cache
    if _driver_path_cache is not None:
        return _driver_path_cache

    with _cache_lock:
        if _driver_path_cache is not None:
            return _driver_path_cache

        # Try system chromedriver first to avoid unnecessary downloads
        system_name = "chromedriver.exe" if os.name == "nt" else "chromedriver"
        system_path = shutil.which(system_name)
        if system_path:
            _driver_path_cache = system_path
            log.info("ChromeDriver resolved via system PATH: %s", system_path)
            return _driver_path_cache

        if _HAS_MANAGER:
            try:
                _driver_path_cache = ChromeDriverManager().install()
                log.info("ChromeDriver resolved via webdriver-manager.")
                return _driver_path_cache
            except Exception as exc:
                log.warning(
                    "webdriver-manager failed (%s), falling back to local %s",
                    exc,
                    system_name,
                )

        # Fallback: system chromedriver (Linux) or local chromedriver.exe (Windows)
        _driver_path_cache = system_name
        return _driver_path_cache


def _resolve_chrome_binary():
    """Resolve the Chrome/Chromium binary path for Selenium."""
    # Check env vars first (Docker / CI environments)
    for env_key in ("CHROME_BIN", "PUPPETEER_EXECUTABLE_PATH"):
        val = os.environ.get(env_key)
        if val and os.path.isfile(val):
            return val
    # Linux fallback: common Chromium paths
    if os.name != "nt":
        for candidate in (
            "/usr/bin/chromium-browser",
            "/usr/bin/chromium",
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
        ):
            if os.path.isfile(candidate):
                return candidate
    return None


def _build_chrome_options(proxy=None):
    """Build a fresh Chrome Options instance with optional proxy."""
    options = Options()

    # Set Chrome binary location if not auto-detected
    chrome_bin = _resolve_chrome_binary()
    if chrome_bin:
        options.binary_location = chrome_bin
        log.info("Chrome binary: %s", chrome_bin)

    # Logging suppression
    options.add_argument("--log-level=3")
    options.add_argument("--silent")
    options.add_experimental_option("excludeSwitches", ["enable-logging"])

    # Stability
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--headless=new")  # modern headless (faster + more stable)
    options.add_argument("--incognito")

    # Anti-automation detection
    options.add_argument("--disable-blink-features=AutomationControlled")

    # Performance — prevent Chrome from throttling headless/background tabs
    options.add_argument("--disable-background-timer-throttling")
    options.add_argument("--disable-backgrounding-occluded-windows")
    options.add_argument("--disable-renderer-backgrounding")
    options.page_load_strategy = "eager"  # don't wait for images/css, just DOM

    # GPU / rendering — save memory
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-webgl")
    options.add_argument("--use-gl=swiftshader")  # replaces deprecated --enable-unsafe-swiftshader
    options.add_argument("--window-size=800,600")  # small but usable viewport
    options.add_argument("--blink-settings=imagesEnabled=false")  # skip image decoding

    # Memory optimization
    options.add_argument("--js-flags=--max-old-space-size=128")
    options.add_argument("--disable-features=TranslateUI,PreloadingHeuristics,WebRtcHideLocalIpsWithMdns")

    # Audio / media
    options.add_argument("--mute-audio")
    options.add_argument("--autoplay-policy=no-user-gesture-required")

    # Proxy
    if proxy:
        options.add_argument(f"--proxy-server={proxy}")
        log.debug("Using proxy: %s", proxy.split("@")[-1] if "@" in proxy else proxy)

    # Permissions
    options.add_experimental_option(
        "prefs",
        {
            "profile.default_content_setting_values.media_stream_mic": 2,
            "profile.default_content_setting_values.media_stream_camera": 2,
            "profile.default_content_setting_values.notifications": 2,
        },
    )

    return options


def create_driver(proxy=None):
    """Create and return a configured Chrome WebDriver instance."""
    options = _build_chrome_options(proxy=proxy)
    service = Service(_resolve_driver_path())

    if os.name == "nt":
        service.creationflags = subprocess.CREATE_NO_WINDOW

    driver = webdriver.Chrome(service=service, options=options)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    return driver

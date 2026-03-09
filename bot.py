# -*- coding: utf-8 -*-

import logging
import os
import random
import threading
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    TimeoutException,
    ElementClickInterceptedException,
    NoSuchFrameException,
)

from browser import create_driver

log = logging.getLogger(__name__)

ELEMENT_WAIT_TIMEOUT = 15
MAX_ATTEMPTS = 3
INPUT_SETTLE_DELAY = 0.25
POST_JOIN_DELAY = 1.0
JOIN_URL = "https://zoom.us/wc/join/{meeting_id}"

# Selectors Zoom has used across different versions of the web client
_NAME_SELECTORS = [
    (By.ID, "input-for-name"),
    (By.ID, "inputname"),
    (By.CSS_SELECTOR, "input[name='name']"),
    (By.CSS_SELECTOR, "input[placeholder*='name' i]"),
    (By.CSS_SELECTOR, "input[type='text']"),
]
_PWD_SELECTORS = [
    (By.ID, "input-for-pwd"),
    (By.ID, "inputpasscode"),
    (By.CSS_SELECTOR, "input[name='password']"),
    (By.CSS_SELECTOR, "input[placeholder*='passcode' i]"),
    (By.CSS_SELECTOR, "input[placeholder*='password' i]"),
    (By.CSS_SELECTOR, "input[type='password']"),
]
_JOIN_SELECTORS = [
    (By.XPATH, "//button[contains(@class, 'preview-join-button')]"),
    (By.XPATH, "//button[contains(@class, 'join-btn')]"),
    (By.CSS_SELECTOR, "button.btn-join"),
    (By.CSS_SELECTOR, "#joinBtn"),
    (By.XPATH, "//button[contains(text(), 'Join')]"),
]

# ── Thread-safe unique-name pool ────────────────────────────────────────────
_name_pool = []
_name_lock = threading.Lock()


def init_name_pool(names_list):
    """Shuffle the names list once so each bot gets a unique name."""
    global _name_pool
    _name_pool = list(names_list)
    random.shuffle(_name_pool)


def _pick_unique_name():
    """Pop a unique name from the pool; fall back to random suffix if empty."""
    with _name_lock:
        if _name_pool:
            return _name_pool.pop()
    return f"User_{random.randint(1000, 9999)}"


# ── Error detection XPaths / selectors ──────────────────────────────────────
_ERROR_SELECTORS = [
    (By.XPATH, "//*[contains(text(), 'meeting password is wrong')]"),
    (By.XPATH, "//*[contains(text(), 'This meeting has been ended')]"),
    (By.XPATH, "//*[contains(text(), 'meeting ID is not valid')]"),
    (By.XPATH, "//*[contains(text(), 'Unable to join')]"),
    (By.XPATH, "//*[contains(text(), 'The meeting has not started')]"),
]


def _check_join_errors(driver):
    """Return an error message if the page shows a Zoom error banner, else None."""
    for by, selector in _ERROR_SELECTORS:
        elems = driver.find_elements(by, selector)
        if elems:
            return elems[0].text
    return None


# ── Element helpers ─────────────────────────────────────────────────────────
def _find_element_multi(driver, selectors):
    """Try multiple selectors and return the first visible element, or None."""
    for by, sel in selectors:
        try:
            el = driver.find_element(by, sel)
            if el.is_displayed():
                return el
        except Exception:
            continue
    return None


def _switch_to_zoom_content(driver, bot_id):
    """Try to switch into an iframe that contains the join form.

    Zoom's web client sometimes nests the form inside one or more iframes.
    Tries the main document first, then each iframe recursively (one level).
    Returns True if form inputs were found in the current context.
    """
    # Check main document first
    if _find_element_multi(driver, _NAME_SELECTORS):
        log.debug("Bot %d: Form found in main document.", bot_id + 1)
        return True

    # Try each iframe
    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    log.debug("Bot %d: Found %d iframes, checking each…", bot_id + 1, len(iframes))

    for idx, iframe in enumerate(iframes):
        try:
            driver.switch_to.frame(iframe)
            if _find_element_multi(driver, _NAME_SELECTORS):
                log.info("Bot %d: Form found in iframe #%d.", bot_id + 1, idx)
                return True
            # Check nested iframes (one level deep)
            nested = driver.find_elements(By.TAG_NAME, "iframe")
            for nidx, nested_frame in enumerate(nested):
                try:
                    driver.switch_to.frame(nested_frame)
                    if _find_element_multi(driver, _NAME_SELECTORS):
                        log.info(
                            "Bot %d: Form found in nested iframe #%d.%d.",
                            bot_id + 1, idx, nidx,
                        )
                        return True
                    driver.switch_to.parent_frame()
                except (NoSuchFrameException, Exception):
                    try:
                        driver.switch_to.parent_frame()
                    except Exception:
                        pass
            driver.switch_to.default_content()
        except (NoSuchFrameException, Exception):
            try:
                driver.switch_to.default_content()
            except Exception:
                pass

    return False


def _debug_dump(driver, bot_id):
    """Log page state for debugging when form can't be found."""
    page_title = driver.title
    page_url = driver.current_url
    log.info(
        "Bot %d: Form not visible (title=%r, url=%s).",
        bot_id + 1, page_title, page_url,
    )
    # Switch back to main doc for full dump
    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    # Count iframes
    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    log.info("Bot %d: Page has %d iframes in main document.", bot_id + 1, len(iframes))
    for i, fr in enumerate(iframes):
        log.info(
            "Bot %d: iframe #%d src=%r id=%r",
            bot_id + 1, i,
            (fr.get_attribute("src") or "")[:120],
            fr.get_attribute("id"),
        )

    # Log inputs/buttons from ALL frames
    frames_to_check = [None] + list(range(len(iframes)))
    for fidx in frames_to_check:
        try:
            if fidx is None:
                driver.switch_to.default_content()
                ctx = "main"
            else:
                driver.switch_to.default_content()
                driver.switch_to.frame(iframes[fidx])
                ctx = f"iframe#{fidx}"
            inputs = driver.find_elements(By.TAG_NAME, "input")
            buttons = driver.find_elements(By.TAG_NAME, "button")
            if inputs or buttons:
                for j, el in enumerate(inputs):
                    log.info(
                        "Bot %d [%s]: <input #%d> id=%r name=%r type=%r placeholder=%r",
                        bot_id + 1, ctx, j,
                        el.get_attribute("id"),
                        el.get_attribute("name"),
                        el.get_attribute("type"),
                        el.get_attribute("placeholder"),
                    )
                for j, el in enumerate(buttons):
                    log.info(
                        "Bot %d [%s]: <button #%d> id=%r text=%r",
                        bot_id + 1, ctx, j,
                        el.get_attribute("id"),
                        el.text[:80] if el.text else "",
                    )
        except Exception:
            pass

    try:
        driver.switch_to.default_content()
    except Exception:
        pass

    # Save screenshot
    try:
        shot_dir = os.path.dirname(os.path.abspath(__file__))
        shot_path = os.path.join(shot_dir, f"debug_bot{bot_id + 1}.png")
        driver.save_screenshot(shot_path)
        log.info("Bot %d: Screenshot saved to %s", bot_id + 1, shot_path)
    except Exception:
        pass


def _verify_input_fields(driver, bot_name, passcode):
    """Re-check that name and passcode fields have values, refill if empty."""
    try:
        name_el = _find_element_multi(driver, _NAME_SELECTORS)
        pwd_el = _find_element_multi(driver, _PWD_SELECTORS)
        if not name_el or not pwd_el:
            return False

        if not name_el.get_attribute("value"):
            name_el.clear()
            name_el.send_keys(bot_name)
            time.sleep(INPUT_SETTLE_DELAY)

        if not pwd_el.get_attribute("value"):
            pwd_el.clear()
            pwd_el.send_keys(passcode)
            time.sleep(INPUT_SETTLE_DELAY)

        return bool(
            name_el.get_attribute("value") and pwd_el.get_attribute("value")
        )
    except Exception as exc:
        log.debug("Field verification error: %s", exc)
        return False


def _dismiss_gates(driver, bot_id):
    """Click through cookie banners, disclaimers, and other pre-join dialogs."""
    # 1. Accept cookies (OneTrust banner)
    _COOKIE_ACCEPT = [
        (By.ID, "onetrust-accept-btn-handler"),
        (By.CSS_SELECTOR, "#onetrust-accept-btn-handler"),
        (By.CSS_SELECTOR, ".onetrust-close-btn-handler"),
        (By.XPATH, "//button[contains(text(), 'Accept All')]"),
        (By.XPATH, "//button[contains(text(), 'Accept Cookies')]"),
        (By.XPATH, "//button[contains(@id, 'accept')]"),
    ]
    for by, sel in _COOKIE_ACCEPT:
        try:
            btn = driver.find_element(by, sel)
            if btn.is_displayed():
                btn.click()
                log.info("Bot %d: Accepted cookies.", bot_id + 1)
                time.sleep(1)
                break
        except Exception:
            continue

    # 2. Accept Zoom disclaimer / terms of service (use JS click — may be behind overlay)
    try:
        btn = driver.find_element(By.ID, "disclaimer_agree")
        driver.execute_script("arguments[0].click();", btn)
        log.info("Bot %d: Accepted disclaimer.", bot_id + 1)
        time.sleep(2)
    except Exception:
        # Fallback: try text-based selectors
        for by, sel in [
            (By.XPATH, "//button[contains(text(), 'Agree')]"),
            (By.XPATH, "//button[contains(text(), 'Accept')]"),
        ]:
            try:
                btn = driver.find_element(by, sel)
                driver.execute_script("arguments[0].click();", btn)
                log.info("Bot %d: Accepted disclaimer (fallback).", bot_id + 1)
                time.sleep(2)
                break
            except Exception:
                continue

    # 3. Handle "Continue" button (audio/video prompt)
    try:
        btn = WebDriverWait(driver, 2).until(
            EC.element_to_be_clickable((By.CLASS_NAME, "continue"))
        )
        btn.click()
        log.info("Bot %d: Clicked continue.", bot_id + 1)
        time.sleep(1)
    except TimeoutException:
        pass


# ── Main bot launcher ───────────────────────────────────────────────────────
def launch_bot(bot_id, meeting_id, passcode, names_list, custom_name=""):
    """Launch a single bot that joins the given Zoom meeting.

    Returns (driver, elapsed_seconds) on success or (None, elapsed_seconds) on failure.
    """
    driver = None
    t_start = time.monotonic()

    for attempt in range(MAX_ATTEMPTS):
        try:
            driver = create_driver()
            wait = WebDriverWait(driver, ELEMENT_WAIT_TIMEOUT)

            driver.get(JOIN_URL.format(meeting_id=meeting_id))

            bot_name = custom_name or _pick_unique_name()

            # Wait for page to fully load (Zoom SPA needs time)
            time.sleep(5)

            # ── Dismiss pre-join gates (cookies, disclaimer, etc.) ──────
            driver.switch_to.default_content()
            _dismiss_gates(driver, bot_id)

            # Wait for the web client to load after dismissing gates
            time.sleep(3)

            # Switch into the correct frame (main doc or iframe)
            if not _switch_to_zoom_content(driver, bot_id):
                # Dump diagnostics on first failure only
                if attempt == 0:
                    _debug_dump(driver, bot_id)
                else:
                    log.info("Bot %d: Form not visible, retrying…", bot_id + 1)
                driver.quit()
                driver = None
                time.sleep(3)
                continue

            # Now we're in the right frame context — find fields
            name_el = _find_element_multi(driver, _NAME_SELECTORS)
            pwd_el = _find_element_multi(driver, _PWD_SELECTORS)

            if not name_el or not pwd_el:
                log.info("Bot %d: Fields disappeared after frame switch.", bot_id + 1)
                driver.quit()
                driver = None
                time.sleep(2)
                continue

            # Fill name
            name_el.clear()
            name_el.send_keys(bot_name)
            time.sleep(INPUT_SETTLE_DELAY)

            # Fill passcode
            pwd_el.clear()
            pwd_el.send_keys(passcode)
            time.sleep(INPUT_SETTLE_DELAY)

            # Verify before joining
            if not _verify_input_fields(driver, bot_name, passcode):
                log.info("Bot %d: Verification failed, retrying…", bot_id + 1)
                driver.quit()
                driver = None
                time.sleep(1)
                continue

            # Click join
            _click_join(driver, bot_id, bot_name, passcode)

            time.sleep(POST_JOIN_DELAY)

            # ── Verify join actually succeeded ──────────────────────────
            error_msg = _check_join_errors(driver)
            if error_msg:
                log.warning("Bot %d: Zoom error after join: %s", bot_id + 1, error_msg)
                driver.quit()
                driver = None
                elapsed = time.monotonic() - t_start
                return (None, elapsed)

            elapsed = time.monotonic() - t_start
            log.info("Bot %d joined! (%.1fs)", bot_id + 1, elapsed)
            return (driver, elapsed)

        except Exception as exc:
            if attempt < MAX_ATTEMPTS - 1:
                log.warning(
                    "Bot %d: Attempt %d/%d failed: %s",
                    bot_id + 1, attempt + 1, MAX_ATTEMPTS, exc,
                )
            else:
                log.error(
                    "Bot %d: Failed after %d attempts: %s",
                    bot_id + 1, MAX_ATTEMPTS, exc,
                )

            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = None

            time.sleep(2 * (attempt + 1))

    elapsed = time.monotonic() - t_start
    return (None, elapsed)


def _click_join(driver, bot_id, bot_name, passcode):
    """Locate and click the Zoom join button."""
    join_btn = _find_element_multi(driver, _JOIN_SELECTORS)

    if not join_btn:
        # Wait a bit longer and retry
        for by, sel in _JOIN_SELECTORS:
            try:
                join_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((by, sel))
                )
                break
            except TimeoutException:
                continue

    if not join_btn:
        raise RuntimeError("Could not find join button")

    try:
        driver.execute_script("arguments[0].click();", join_btn)
    except ElementClickInterceptedException:
        log.info("Bot %d: Join click intercepted, retrying…", bot_id + 1)
        if not _verify_input_fields(driver, bot_name, passcode):
            raise RuntimeError("Input fields validation failed")
        driver.execute_script("arguments[0].click();", join_btn)

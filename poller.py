#!/usr/bin/env python3
"""
Multi-movie BookMyShow booking watcher.

Reads one or more monitor definitions from config.json, checks each movie /
theatre independently, and sends Telegram alerts on False -> True transitions.
Each monitor has its own state entry in state.json.

The existing single-monitor config format is still supported.
"""

import json
import os
import re
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", ROOT / "config.json"))
STATE_PATH = Path(os.environ.get("STATE_PATH", ROOT / "state.json"))

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-IN,en-US;q=0.9,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
}


def load_json(path, default=None):
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def build_target_url(cfg):
    if cfg.get("url_template") and cfg.get("requested_date"):
        cfg["target_url"] = cfg["url_template"].format(
            date=cfg["requested_date"]
        )
    return cfg


def load_monitors():
    raw = load_json(CONFIG_PATH, default={}) or {}

    # Shared values can be defined once at the top level. GitHub Actions
    # secrets override file values for credentials.
    shared = {
        "telegram_bot_token": raw.get("telegram_bot_token"),
        "telegram_chat_id": raw.get("telegram_chat_id"),
        "headers": raw.get("headers", {}),
    }
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        shared["telegram_bot_token"] = os.environ["TELEGRAM_BOT_TOKEN"]
    if os.environ.get("TELEGRAM_CHAT_ID"):
        shared["telegram_chat_id"] = os.environ["TELEGRAM_CHAT_ID"]
    if os.environ.get("HEADERS_JSON"):
        shared["headers"] = json.loads(os.environ["HEADERS_JSON"])

    # Backward compatibility: the old config.json contained one monitor
    # directly at its root instead of a "monitors" array.
    if isinstance(raw.get("monitors"), list):
        monitor_items = raw["monitors"]
    else:
        monitor_items = [raw]

        # Preserve support for the old single-monitor environment overrides.
        legacy_env_map = {
            "TARGET_URL": "target_url",
            "THEATRE": "theatre",
            "MOVIE": "movie",
            "REQUESTED_DATE": "requested_date",
        }
        for env_key, cfg_key in legacy_env_map.items():
            if os.environ.get(env_key):
                raw[cfg_key] = os.environ[env_key]
        monitor_items = [raw]

    if not monitor_items:
        sys.exit("config.json must contain at least one monitor")

    monitors = []
    seen_ids = set()

    for index, item in enumerate(monitor_items, start=1):
        cfg = dict(shared)
        cfg.update(item)
        cfg = build_target_url(cfg)

        monitor_id = cfg.get("id") or f"monitor_{index}"
        if monitor_id in seen_ids:
            sys.exit(f"Duplicate monitor id: {monitor_id}")
        seen_ids.add(monitor_id)
        cfg["id"] = monitor_id

        detector = cfg.get("detector")
        required = ["target_url", "telegram_bot_token", "telegram_chat_id"]
        if detector in ("bms_date", "venue_date"):
            required.append("requested_date")
        elif detector != "venue_date":
            required.append("theatre")

        if detector == "venue_date" and not (
            cfg.get("venue_code") or cfg.get("venue_codes")
        ):
            sys.exit(
                f"Monitor '{monitor_id}': venue_date detector needs "
                "'venue_code' or 'venue_codes'"
            )

        missing = [key for key in required if not cfg.get(key)]
        if missing:
            sys.exit(
                f"Monitor '{monitor_id}' missing config: {', '.join(missing)}"
            )

        monitors.append(cfg)

    return monitors


def send_telegram(token, chat_id, message):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    response.raise_for_status()


def fetch(cfg):
    """
    Fetch the configured BookMyShow URL.

    SCRAPERAPI_KEY routes through an India endpoint and helps with
    anti-bot responses from GitHub Actions runners.
    """
    headers = dict(DEFAULT_HEADERS)
    headers.update(cfg.get("headers", {}))

    scraper_key = os.environ.get("SCRAPERAPI_KEY")
    if scraper_key:
        api_url = "https://api.scraperapi.com/?" + urllib.parse.urlencode(
            {
                "api_key": scraper_key,
                "country_code": "in",
                "url": cfg["target_url"],
            }
        )
        response = requests.get(api_url, timeout=90)
        response.raise_for_status()
        return response.text

    proxy = os.environ.get("PROXY_URL")
    proxies = {"http": proxy, "https": proxy} if proxy else None

    session = requests.Session()
    session.headers.update(headers)

    try:
        session.get(
            "https://in.bookmyshow.com/",
            timeout=30,
            proxies=proxies,
        )
    except requests.RequestException:
        pass

    response = session.get(
        cfg["target_url"],
        timeout=30,
        proxies=proxies,
        headers={"Referer": "https://in.bookmyshow.com/explore/movies-chennai"},
    )
    response.raise_for_status()
    return response.text


def is_available_bms_date(page_text, cfg):
    requested = cfg["requested_date"]
    floor = cfg.get("min_references", 10)

    tokens = re.findall(r"20\d{6}", page_text)
    if not tokens:
        return False

    counts = Counter(tokens)
    top_date, _ = counts.most_common(1)[0]
    return top_date == requested and counts.get(requested, 0) >= floor


def is_available_venue_date(page_text, cfg):
    """
    Match both known BMS link shapes:
      /<venue-code>/<date>
      /<venue-code>/<show-id>/<date>

    This searches the fetched response text; it does not make a separate
    request to every seat-layout URL.
    """
    date = re.escape(cfg["requested_date"])
    codes = cfg.get("venue_codes") or [cfg["venue_code"]]

    for code in codes:
        escaped_code = re.escape(code)
        pattern = (
            rf"/{escaped_code}/"
            rf"(?:[^/?#\s\"'<>]+/)?"
            rf"{date}(?=[/?#\"'<>]|$)"
        )
        if re.search(pattern, page_text):
            return True

    return False


def is_available_generic(page_text, cfg):
    haystack = re.sub(r"\s+", " ", page_text).lower()

    theatre = re.sub(r"\s+", " ", cfg["theatre"]).lower().strip()
    if theatre not in haystack:
        return False

    movie = cfg.get("movie")
    if movie and re.sub(r"\s+", " ", movie).lower().strip() not in haystack:
        return False

    open_signals = cfg.get(
        "open_signals",
        ["book tickets", "book now", '"showtimes"', "showtime", "select seats"],
    )
    closed_signals = cfg.get("closed_signals", ["notify me", "coming soon"])

    has_open = any(signal.lower() in haystack for signal in open_signals)
    only_closed = (
        any(signal.lower() in haystack for signal in closed_signals)
        and not has_open
    )
    return has_open and not only_closed


def is_available(page_text, cfg):
    detector = cfg.get("detector")
    if detector == "venue_date":
        return is_available_venue_date(page_text, cfg)
    if detector == "bms_date":
        return is_available_bms_date(page_text, cfg)
    return is_available_generic(page_text, cfg)


def load_states(monitors):
    """
    New format:
      {"doomsday": {"available": false}, "dune_part_three": {...}}

    Migrate the previous single-monitor state.json automatically. The first
    configured monitor is Doomsday in the supplied config, so its old state
    is retained rather than generating a duplicate alert on the next run.
    """
    raw = load_json(STATE_PATH, default={}) or {}

    if isinstance(raw.get("monitors"), dict):
        return raw["monitors"]

    if "available" in raw:
        first_id = monitors[0]["id"]
        return {first_id: raw}

    return raw


def build_alert_message(cfg):
    requested_date = cfg.get("requested_date")
    if requested_date and len(requested_date) == 8:
        pretty_date = (
            f"{requested_date[6:8]}-"
            f"{requested_date[4:6]}-"
            f"{requested_date[0:4]}"
        )
    else:
        pretty_date = requested_date or "See booking page"

    venue = (
        cfg.get("venue_label")
        or cfg.get("venue_code")
        or ", ".join(cfg.get("venue_codes", []))
        or cfg.get("theatre")
    )
    venue_line = f"Theatre: {venue}\n" if venue else ""

    return (
        "🎬 Booking just OPENED!\n\n"
        f"{cfg.get('movie', 'Movie')}\n"
        f"{venue_line}"
        f"Date: {pretty_date}\n\n"
        f"Book here: {cfg['target_url']}"
    )


def main():
    monitors = load_monitors()
    states = load_states(monitors)
    changed = False

    for cfg in monitors:
        monitor_id = cfg["id"]
        state = states.get(monitor_id, {"available": False})
        label = (
            f"{cfg.get('movie', monitor_id)} @ "
            f"{cfg.get('venue_label') or cfg.get('venue_code') or cfg.get('requested_date', 'target')}"
        )

        try:
            page = fetch(cfg)
        except requests.RequestException as exc:
            print(f"[{label}] fetch failed: {exc}")
            continue

        available = is_available(page, cfg)
        was_available = bool(state.get("available", False))
        print(f"[{label}] available={available} (was {was_available})")

        if available and not was_available:
            try:
                send_telegram(
                    cfg["telegram_bot_token"],
                    cfg["telegram_chat_id"],
                    build_alert_message(cfg),
                )
                print(f"[{label}] notification sent")
            except requests.RequestException as exc:
                # Do not mark it available if Telegram failed; the next poll
                # can retry the alert.
                print(f"[{label}] Telegram notification failed: {exc}")
                continue

        if available != was_available:
            state["available"] = available
            state["checked_at"] = int(time.time())
            states[monitor_id] = state
            changed = True

    if changed:
        save_json(STATE_PATH, states)
    else:
        print("No state change.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Apple stock alert.

Polls Apple's fulfillment-messages API (the same one apple.com/shop uses) for
one or more part numbers, and notifies you when something goes from
unavailable -> available (store pickup and/or delivery).

Usage:
    python3 apple_stock_alert.py              # run forever using config.json
    python3 apple_stock_alert.py --once       # check once, print status, exit
    python3 apple_stock_alert.py --test-notify  # send a test notification

Only needs Python 3.8+. No third-party packages.
"""

import argparse
import json
import os
import platform
import random
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

API = "https://www.apple.com/shop/fulfillment-messages"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


def ts():
    return datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------- fetching
def fetch(part, zip_code, country_path=""):
    """Return parsed JSON from Apple for one part number."""
    params = {
        "pl": "true",
        "mts.0": "regular",
        "mts.1": "compact",
        "parts.0": part,
        "location": zip_code,
    }
    base = API
    if country_path:  # e.g. "ca" -> https://www.apple.com/ca/shop/...
        base = f"https://www.apple.com/{country_path}/shop/fulfillment-messages"
    url = base + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ----------------------------------------------------------------- parsing
def parse_availability(data, part):
    """
    Returns {"pickup": [store names with stock], "delivery": bool, "delivery_quote": str}.
    Defensive: Apple changes this payload now and then, so every access is guarded.
    """
    content = (data.get("body") or {}).get("content") or {}
    result = {"pickup": [], "delivery": False, "delivery_quote": ""}

    stores = (content.get("pickupMessage") or {}).get("stores") or []
    for store in stores:
        info = (store.get("partsAvailability") or {}).get(part) or {}
        display = str(info.get("pickupDisplay", "")).lower()
        buyable = (info.get("buyability") or {}).get("isBuyable")
        if display == "available" or buyable is True:
            name = store.get("storeName", "Unknown store")
            when = info.get("pickupSearchQuote") or ""
            result["pickup"].append(f"{name} ({when})" if when else name)

    # Apple nests delivery info under deliveryMessage[part]["regular"|"compact"]
    dm = (content.get("deliveryMessage") or {}).get(part) or {}
    delivery = dm.get("regular") or dm.get("compact") or dm
    if delivery.get("isBuyable") is True:
        result["delivery"] = True
        result["delivery_quote"] = delivery.get("quote") or ""
        if not result["delivery_quote"]:
            opts = delivery.get("deliveryOptions") or []
            if opts:
                result["delivery_quote"] = opts[0].get("date", "")

    return result


# ------------------------------------------------------------ notifications
def notify_desktop(title, message):
    system = platform.system()
    try:
        if system == "Darwin":
            script = f'display notification {json.dumps(message)} with title {json.dumps(title)} sound name "Glass"'
            subprocess.run(["osascript", "-e", script], check=False)
        elif system == "Linux":
            subprocess.run(["notify-send", title, message], check=False)
        else:
            print("\a", end="")  # terminal bell fallback (Windows etc.)
    except Exception as e:
        print(f"[{ts()}] desktop notify failed: {e}")


def notify_ntfy(topic, title, message, click_url=None):
    """Free push notifications to your phone via the ntfy app (ntfy.sh)."""
    headers = {"Title": title, "Priority": "urgent", "Tags": "apple,rotating_light"}
    if click_url:
        headers["Click"] = click_url
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=message.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[{ts()}] ntfy failed: {e}")


def notify_discord(webhook, message):
    req = urllib.request.Request(
        webhook,
        data=json.dumps({"content": message}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": HEADERS["User-Agent"]},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:
        print(f"[{ts()}] discord failed: {e}")


def notify_telegram(token, chat_id, title, message, click_url=None):
    text = f"{title}\n{message}" + (f"\n{click_url}" if click_url else "")
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    try:
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10
        ).read()
    except Exception as e:
        print(f"[{ts()}] telegram failed: {e}")


def notify_all(cfg, title, message, click_url=None):
    n = cfg.get("notify", {})
    print(f"\n*** {title}: {message}\n")
    # Secrets can come from env vars (GitHub Actions) or config.json (local use)
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN") or n.get("telegram_bot_token")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID") or n.get("telegram_chat_id")
    ntfy = os.environ.get("NTFY_TOPIC") or n.get("ntfy_topic")
    discord = os.environ.get("DISCORD_WEBHOOK") or n.get("discord_webhook")
    if n.get("desktop", False):
        notify_desktop(title, message)
    if tg_token and tg_chat:
        notify_telegram(tg_token, tg_chat, title, message, click_url)
    if ntfy:
        notify_ntfy(ntfy, title, message, click_url)
    if discord:
        notify_discord(discord, f"**{title}**\n{message}\n{click_url or ''}")


# -------------------------------------------------------------------- main
def check_product(cfg, product):
    part = product["part"]
    data = fetch(part, cfg["zip"], cfg.get("country_path", ""))
    avail = parse_availability(data, part)
    want_pickup = cfg.get("check_pickup", True)
    want_delivery = cfg.get("check_delivery", False)

    in_stock = (want_pickup and bool(avail["pickup"])) or (want_delivery and avail["delivery"])
    return in_stock, avail


def describe(avail, cfg):
    bits = []
    if cfg.get("check_pickup", True) and avail["pickup"]:
        bits.append("Pickup: " + "; ".join(avail["pickup"][:5]))
    if cfg.get("check_delivery", False) and avail["delivery"]:
        bits.append("Delivery: " + (avail["delivery_quote"] or "available"))
    return " | ".join(bits) or "not available"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--duration", type=int, default=0, help="stop after N seconds (for scheduled CI runs)")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification and exit")
    args = ap.parse_args()

    with open(args.config) as f:
        cfg = json.load(f)
    if os.environ.get("ZIP"):
        cfg["zip"] = os.environ["ZIP"]  # keep ZIP out of a public repo

    if args.test_notify:
        notify_all(cfg, "Apple Stock Alert test", "If you can read this, notifications work.")
        return

    products = cfg["products"]
    lo = float(cfg.get("interval_min", 10))
    hi = float(cfg.get("interval_max", 15))
    last_state = {p["part"]: False for p in products}
    last_quote = {}
    backoff = 0
    start = time.time()
    fail_streak = 0
    warned = False

    print(f"[{ts()}] Watching {len(products)} product(s) near {cfg['zip']}, every {lo:g}-{hi:g}s. Ctrl+C to stop.")

    while True:
        blocked = False
        round_failed = False
        for p in products:
            name = p.get("name", p["part"])
            try:
                in_stock, avail = check_product(cfg, p)
                backoff = 0
            except urllib.error.HTTPError as e:
                print(f"[{ts()}] {name}: HTTP {e.code}")
                round_failed = True
                if e.code in (403, 429, 541):
                    blocked = True
                continue
            except Exception as e:
                print(f"[{ts()}] {name}: error: {e}")
                round_failed = True
                continue

            status = describe(avail, cfg)
            print(f"[{ts()}] {name}: {'IN STOCK - ' + status if in_stock else 'out of stock'}")

            if in_stock and not last_state[p["part"]]:
                buy = p.get("url") or "https://www.apple.com/shop/buy"
                notify_all(cfg, f"IN STOCK: {name}", status, click_url=buy)
            last_state[p["part"]] = in_stock

            # Delivery is usually "buyable" with a far-off ship date. Alert when that date changes.
            if cfg.get("alert_delivery_date_change", True) and avail["delivery"]:
                q = avail["delivery_quote"]
                prev = last_quote.get(p["part"])
                if prev is not None and q != prev:
                    notify_all(cfg, f"Ship date changed: {name}", f"Delivery was {prev}, now {q}",
                               click_url=p.get("url"))
                elif prev is None:
                    print(f"[{ts()}] {name}: current delivery estimate {q or 'unknown'}")
                last_quote[p["part"]] = q

        if args.once:
            return

        # If every check keeps failing (e.g. Apple blocks the host), tell the user once
        fail_streak = fail_streak + 1 if round_failed else 0
        if fail_streak >= 12 and not warned:
            notify_all(cfg, "Apple monitor is failing", "12 checks in a row errored/blocked. It may be rate limited or blocked from this host.")
            warned = True
        if not round_failed:
            fail_streak, warned = 0, False

        if args.duration and time.time() - start >= args.duration:
            print(f"[{ts()}] Duration reached, exiting.")
            return

        if blocked:
            # Apple is rate limiting us: back off exponentially (30s, 60s, ... max 10 min)
            backoff = min(backoff + 1, 5)
            wait = min(30 * 2 ** (backoff - 1), 600)
            print(f"[{ts()}] Rate limited, backing off {wait}s")
        else:
            wait = random.uniform(lo, hi)
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)

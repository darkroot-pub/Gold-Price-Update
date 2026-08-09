#!/usr/bin/env python3
"""
fetch_gold.py
Fetches Fine Gold (9999 / Hallmark) price per tola for Nepal and writes it to
Firebase Firestore. Runs via GitHub Actions twice daily — 11:30 AM + 7:00 PM NPT.

WHY THIS VERSION EXISTS
------------------------
fenegosida.org (the official source) migrated its frontend to a client-side
rendered React app. A plain HTTP GET to the page now returns an (almost) empty
HTML shell — there is no price text anywhere in the markup to regex against.
Any scraper that parses the raw response body, no matter how good the regex,
will always fail against that page until it's fetched with a JS-executing
browser (which GitHub Actions runners don't have installed by default).

So instead of trying to out-regex a moving target, this script uses a
SOURCE CHAIN: an ordered list of independent sources, each with its own
fetch + parse strategy. It tries sources top to bottom and uses the first
one that returns a plausible price. If fenegosida.org ever adds
server-side rendering back, it'll be picked up automatically since it's
still tried first.

Each source is fully isolated: a parsing failure or structure change in
one source can never corrupt or block another.
"""

import os
import re
import sys
import json
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ── Firebase config (injected from GitHub Secrets) ──
PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID")
API_KEY = os.environ.get("FIREBASE_API_KEY")

if not PROJECT_ID or not API_KEY:
    print("❌ Missing FIREBASE_PROJECT_ID or FIREBASE_API_KEY env vars.")
    sys.exit(1)

# Sane bounds for a Fine Gold / Hallmark price per tola, in NPR.
# Wide enough to survive years of price drift, tight enough to reject junk.
MIN_PRICE = 100_000
MAX_PRICE = 700_000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 GoldBot/2.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json",
}


# ───────────────────────── HTTP helpers ─────────────────────────

def fetch_url(url, timeout=15, retries=3, backoff=2):
    """GET a URL with retries + exponential backoff. Returns response text or None."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise urllib.error.HTTPError(url, resp.status, "bad status", None, None)
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001 - want to catch+retry everything network related
            last_err = e
            print(f"   ⚠️  attempt {attempt}/{retries} failed for {url}: {e}")
            if attempt < retries:
                time.sleep(backoff ** attempt)
    print(f"   ❌ all {retries} attempts failed for {url}: {last_err}")
    return None


def strip_html(html):
    """Collapse HTML down to plain, whitespace-normalized text."""
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<!--[\s\S]*?-->", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    text = text.replace("&#2352;", "\u0930")  # र
    text = text.replace("&#2369;", "\u0941")  # ु
    text = re.sub(r"\s+", " ", text).strip()
    return text


def in_range(n):
    return MIN_PRICE < n < MAX_PRICE


# ───────────────────────── Source 1: fenegosida.org ─────────────────────────
# Tries (a) any embedded JSON app-state blob, then (b) the old anchored
# plain-text regex, in case they ever bring back server-side rendering.

def parse_embedded_json(html):
    """Look for common SPA state-injection patterns and search them for a price."""
    patterns = [
        r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]*?)</script>',
        r'window\.__INITIAL_STATE__\s*=\s*(\{[\s\S]*?\});',
        r'window\.__NUXT__\s*=\s*(\{[\s\S]*?\});',
        r'<script[^>]*type=["\']application/json["\'][^>]*>([\s\S]*?)</script>',
    ]
    for pat in patterns:
        for m in re.finditer(pat, html, flags=re.I):
            blob = m.group(1)
            try:
                data = json.loads(blob)
            except (json.JSONDecodeError, TypeError):
                continue
            price = search_json_for_gold_price(data)
            if price:
                return price
    return None


def search_json_for_gold_price(obj, depth=0):
    """Recursively walk a parsed JSON structure looking for a plausible
    fine-gold-per-tola number, using nearby key names as a hint."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for key, val in obj.items():
            key_l = str(key).lower()
            if isinstance(val, (int, float)) and in_range(val):
                if any(hint in key_l for hint in ("tola", "gold", "hallmark", "fine", "rate", "price")):
                    if "silver" not in key_l and "tejabi" not in key_l and "tajabi" not in key_l:
                        return int(val)
            result = search_json_for_gold_price(val, depth + 1)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = search_json_for_gold_price(item, depth + 1)
            if result:
                return result
    return None


def parse_anchored_text(html):
    """Legacy strategy: strip tags, anchor on the first 'per 1 tola' occurrence
    (historically always the FINE GOLD row), then look for a nearby रु/Nrs price."""
    text = strip_html(html)

    tola_idx = re.search(r"per\s*1\s*tola", text, flags=re.I)
    if tola_idx:
        window = text[tola_idx.start(): tola_idx.start() + 200]
        m = re.search(r"(?:[\u0930\u0941]+|Nrs)\s*\*{0,2}(\d{4,7})(?:\.\d+)?\*{0,2}", window, flags=re.I)
        if m:
            price = int(m.group(1))
            if in_range(price):
                return price

    fine = re.search(
        r"FINE GOLD[\s\S]{0,600}?per\s*1\s*tola[\s\S]{0,200}?(?:[\u0930\u0941]+|Nrs)\s*\*{0,2}(\d{4,7})",
        text, flags=re.I,
    )
    if fine:
        price = int(fine.group(1))
        if in_range(price):
            return price

    return None


def source_fenegosida():
    for url in ("https://fenegosida.org/", "https://www.fenegosida.org/"):
        print(f"🌐 [fenegosida.org] fetching {url}")
        html = fetch_url(url, retries=2)
        if not html:
            continue
        print(f"   📄 page size: {round(len(html) / 1024)}KB")

        price = parse_embedded_json(html)
        if price:
            print(f"   ✅ price found via embedded JSON state: {price}")
            return price

        price = parse_anchored_text(html)
        if price:
            print(f"   ✅ price found via anchored text parse: {price}")
            return price

        print("   ⚠️  no price found in this response (site is likely client-rendered JS — expected)")
    return None


# ───────────────────────── Source 2: kokil.com.np mirror ─────────────────────────
# Server-rendered page that republishes official FENEGOSIDA rates daily.
# Confirmed structure (2026-08): "Gold Hallmark ... Fine Gold 9999 · 24K
# Rs. 301,700 per tola Rs. 258,660 / 10g"

def source_kokil():
    url = "https://kokil.com.np/tools/nepal-gold-silver-price"
    print(f"🌐 [kokil.com.np] fetching {url}")
    html = fetch_url(url)
    if not html:
        return None
    print(f"   📄 page size: {round(len(html) / 1024)}KB")

    text = strip_html(html)

    m = re.search(
        r"Gold Hallmark.{0,120}?Rs\.?\s*([\d,]{5,9})\s*per\s*tola",
        text, flags=re.I | re.S,
    )
    if not m:
        # Fallback: anchor on "Fine Gold 9999" directly if the "Gold Hallmark"
        # label wording ever changes
        m = re.search(
            r"Fine Gold\s*9999.{0,120}?Rs\.?\s*([\d,]{5,9})\s*per\s*tola",
            text, flags=re.I | re.S,
        )
    if m:
        price = int(m.group(1).replace(",", ""))
        if in_range(price):
            print(f"   ✅ Hallmark per-tola price found: {price}")
            return price
        print(f"   ⚠️  parsed value {price} out of plausible range, discarding")

    print("   ⚠️  no price found in this response")
    return None


# Ordered source chain — tried top to bottom, first plausible result wins.
SOURCES = [
    ("fenegosida.org", source_fenegosida),
    ("kokil.com.np (FENEGOSIDA mirror)", source_kokil),
]


def get_gold_price():
    for name, fn in SOURCES:
        try:
            price = fn()
        except Exception as e:  # noqa: BLE001 - one bad source must not kill the run
            print(f"   ❌ source '{name}' raised an unexpected error: {e}")
            price = None
        if price:
            return price, name
    return None, None


# ───────────────────────── Firestore ─────────────────────────

def firestore_request(path, method, fields):
    body = json.dumps({"fields": fields}).encode("utf-8")
    url = f"https://firestore.googleapis.com/v1/projects/{PROJECT_ID}/databases/(default)/documents/{path}?key={API_KEY}"
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode("utf-8")


def write_to_firestore(price, source_name):
    fields = {
        "rate": {"integerValue": str(price)},
        "updatedBy": {"stringValue": "auto@github-actions"},
        "lastUpdated": {"stringValue": datetime.now(timezone.utc).isoformat()},
        "source": {"stringValue": source_name},
        "note": {"stringValue": "Fine Gold (9999) per tola"},
    }
    status, data = firestore_request("global_data/gold_info", "PATCH", fields)
    if status != 200:
        raise RuntimeError(f"Firestore error {status}: {data}")
    return data


def log_to_history(price, source_name):
    fields = {
        "rate": {"integerValue": str(price)},
        "updatedBy": {"stringValue": "auto@github-actions"},
        "timestamp": {"stringValue": datetime.now(timezone.utc).isoformat()},
        "source": {"stringValue": source_name},
        "note": {"stringValue": "Fine Gold (9999) per tola"},
    }
    try:
        status, data = firestore_request("gold_history", "POST", fields)
        if status not in (200, 201):
            print(f"⚠️  history log returned status {status}: {data}")
        else:
            print("📋 Logged to gold_history collection")
    except Exception as e:  # noqa: BLE001 - history logging is best-effort
        print(f"⚠️  history log skipped: {e}")


# ───────────────────────── Main ─────────────────────────

def main():
    print("🕘 Gold price auto-fetch starting...")
    print(f"📅 Time (UTC): {datetime.now(timezone.utc).isoformat()}")
    print(f"🌐 Source chain: {' → '.join(name for name, _ in SOURCES)}")

    price, source_name = get_gold_price()
    if not price:
        print("❌ All sources exhausted. Could not obtain a gold price.")
        sys.exit(1)

    print(f"💰 Fine Gold price (per tola): NPR {price:,}  [source: {source_name}]")

    try:
        write_to_firestore(price, source_name)
        print("🔥 Successfully written to Firestore!")
    except Exception as e:
        print(f"❌ Firestore write failed: {e}")
        sys.exit(1)

    log_to_history(price, source_name)

    print(f"✅ Done. gold_info.rate = {price}")


if __name__ == "__main__":
    main()

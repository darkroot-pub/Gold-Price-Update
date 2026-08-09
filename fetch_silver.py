#!/usr/bin/env python3
"""
fetch_silver.py
Fetches Silver price per tola for Nepal and writes it to Firebase Firestore.
Runs via GitHub Actions twice daily — 10:30 AM + 11:10 AM NPT.

Same source-chain design as fetch_gold.py — see that file's docstring for why.
This version also carries over the proxy-fallback layer from the original
fetch-silver.js (fenegosida.org direct fetches were sometimes IP-blocked,
separately from the JS-rendering problem), so within the fenegosida.org
source itself there are several fetch attempts before giving up on it.
"""

import os
import re
import sys
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone

# ── Firebase config (injected from GitHub Secrets) ──
PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID")
API_KEY = os.environ.get("FIREBASE_API_KEY")

if not PROJECT_ID or not API_KEY:
    print("❌ Missing FIREBASE_PROJECT_ID or FIREBASE_API_KEY env vars.")
    sys.exit(1)

# Sane bounds for a silver price per tola, in NPR.
MIN_PRICE = 500
MAX_PRICE = 50_000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 SilverBot/2.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json",
    "Accept-Language": "en-US,en;q=0.5",
    "Cache-Control": "no-cache",
}


# ───────────────────────── HTTP helpers ─────────────────────────

def fetch_url(url, timeout=15, retries=2, backoff=2):
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
            print(f"      ⚠️  attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(backoff ** attempt)
    print(f"      ❌ all {retries} attempts failed: {last_err}")
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


# ───────────────────────── Source 1: fenegosida.org (+ proxy fallbacks) ─────────────────────────
# fenegosida.org is now a client-rendered JS SPA, so a direct fetch usually
# returns an empty shell with no price text — the proxies below don't fix
# that (they can't execute JS either), but they're kept because the original
# script needed them for straight IP-blocking issues independent of
# rendering, and this way that layer isn't lost if the site's rendering
# situation ever changes.

def build_proxy_targets():
    target = "https://fenegosida.org/"
    encoded = urllib.parse.quote(target, safe="")
    return [
        ("Direct", target),
        ("allorigins", f"https://api.allorigins.win/raw?url={encoded}"),
        ("corsproxy", f"https://corsproxy.io/?{encoded}"),
        ("codetabs", f"https://api.codetabs.com/v1/proxy?quest={encoded}"),
        ("thingproxy", f"https://thingproxy.freeboard.io/fetch/{target}"),
    ]


def parse_embedded_json(html):
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
            price = search_json_for_silver_price(data)
            if price:
                return price
    return None


def search_json_for_silver_price(obj, depth=0):
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for key, val in obj.items():
            key_l = str(key).lower()
            if isinstance(val, (int, float)) and in_range(val):
                if "silver" in key_l:
                    return int(val)
            result = search_json_for_silver_price(val, depth + 1)
            if result:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = search_json_for_silver_price(item, depth + 1)
            if result:
                return result
    return None


def parse_anchored_text(html):
    """Legacy strategy: anchor on 'SILVER ... per 1 tola', with a fallback
    that counts to the third 'per 1 tola' occurrence (Fine Gold, Tejabi,
    Silver, in that order on the page)."""
    text = strip_html(html)

    m = re.search(
        r"SILVER\s*per\s*1\s*tola[\s\S]{0,200}?(?:[\u0930\u0941]+|Nrs)\s*\*{0,2}(\d{3,6})(?:\.\d+)?\*{0,2}",
        text, flags=re.I,
    )
    if m:
        price = int(m.group(1))
        if in_range(price):
            return price

    tola_matches = list(re.finditer(r"per\s*1\s*tola", text, flags=re.I))
    if len(tola_matches) >= 3:
        idx = tola_matches[2].end()
        window = text[idx: idx + 200]
        wm = re.search(r"(?:[\u0930\u0941]+|Nrs)\s*\*{0,2}(\d{3,6})(?:\.\d+)?\*{0,2}", window, flags=re.I)
        if wm:
            price = int(wm.group(1))
            if in_range(price):
                return price

    return None


def source_fenegosida():
    for name, url in build_proxy_targets():
        print(f"🌐 [fenegosida.org via {name}] fetching")
        html = fetch_url(url, retries=1 if name != "Direct" else 2)
        if not html:
            continue
        print(f"   📄 page size: {round(len(html) / 1024)}KB")

        if "SILVER" not in html.upper():
            print("   ⚠️  no 'SILVER' text present — likely the client-rendered empty shell")
            continue

        price = parse_embedded_json(html)
        if price:
            print(f"   ✅ price found via embedded JSON state: {price}")
            return price

        price = parse_anchored_text(html)
        if price:
            print(f"   ✅ price found via anchored text parse: {price}")
            return price

        print("   ⚠️  page had 'SILVER' text but no parseable price")
    return None


# ───────────────────────── Source 2: kokil.com.np mirror ─────────────────────────
# Server-rendered page that republishes official FENEGOSIDA rates daily.
# Confirmed structure (2026-08): "Silver Chandi · Pure Silver Rs. 4,645
# per tola Rs. 3,983 / 10g"

def source_kokil():
    url = "https://kokil.com.np/tools/nepal-gold-silver-price"
    print(f"🌐 [kokil.com.np] fetching {url}")
    html = fetch_url(url)
    if not html:
        return None
    print(f"   📄 page size: {round(len(html) / 1024)}KB")

    text = strip_html(html)

    # Anchor on "Pure Silver" specifically — not just "Silver", which also
    # appears in the page's "Gold & Silver Rate" title far above the real
    # data block and would otherwise match the wrong (gold) price.
    candidates = list(re.finditer(
        r"Pure Silver.{0,80}?Rs\.?\s*([\d,]{3,7})\s*per\s*tola",
        text, flags=re.I | re.S,
    ))
    # Fallback anchor in case the "Pure Silver" wording ever changes.
    if not candidates:
        candidates = list(re.finditer(
            r"Silver.{0,80}?Rs\.?\s*([\d,]{3,7})\s*per\s*tola",
            text, flags=re.I | re.S,
        ))

    # Check every match in order, not just the first — an early match that
    # falls outside the plausible range shouldn't stop us from trying the
    # rest of the matches further down the page.
    for m in candidates:
        price = int(m.group(1).replace(",", ""))
        if in_range(price):
            print(f"   ✅ Silver per-tola price found: {price}")
            return price
        print(f"   ⚠️  candidate {price} out of plausible range, trying next match")

    print("   ⚠️  no price found in this response")
    return None


SOURCES = [
    ("fenegosida.org", source_fenegosida),
    ("kokil.com.np (FENEGOSIDA mirror)", source_kokil),
]


def get_silver_price():
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
        "note": {"stringValue": "Silver per tola"},
    }
    status, data = firestore_request("global_data/silver_info", "PATCH", fields)
    if status != 200:
        raise RuntimeError(f"Firestore error {status}: {data}")
    return data


def log_to_history(price, source_name):
    fields = {
        "rate": {"integerValue": str(price)},
        "updatedBy": {"stringValue": "auto@github-actions"},
        "timestamp": {"stringValue": datetime.now(timezone.utc).isoformat()},
        "source": {"stringValue": source_name},
        "note": {"stringValue": "Silver per tola"},
    }
    try:
        status, data = firestore_request("silver_history", "POST", fields)
        if status not in (200, 201):
            print(f"⚠️  history log returned status {status}: {data}")
        else:
            print("📋 Silver logged to silver_history collection")
    except Exception as e:  # noqa: BLE001 - history logging is best-effort
        print(f"⚠️  history log skipped: {e}")


# ───────────────────────── Main ─────────────────────────

def main():
    print("🥈 Silver price auto-fetch starting...")
    print(f"📅 Time (UTC): {datetime.now(timezone.utc).isoformat()}")
    print(f"🌐 Source chain: {' → '.join(name for name, _ in SOURCES)}")

    price, source_name = get_silver_price()
    if not price:
        print("❌ All sources exhausted. Could not obtain a silver price.")
        sys.exit(1)

    print(f"🥈 Silver price (per tola): NPR {price:,}  [source: {source_name}]")

    try:
        write_to_firestore(price, source_name)
        print("🔥 Silver written to Firestore → global_data/silver_info")
    except Exception as e:
        print(f"❌ Firestore write failed: {e}")
        sys.exit(1)

    log_to_history(price, source_name)

    print()
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"✅ Done! silver_info.rate = {price}")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")


if __name__ == "__main__":
    main()

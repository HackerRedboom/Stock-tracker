"""
Stock tracker for Flipkart / Amazon products.
Runs on GitHub Actions and posts to Discord when an item comes back in stock.

Products to watch are listed in products.json.
The last known status of each product is kept in state.json.
"""

import datetime
import json
import os
import re
import time

import requests

PRODUCTS_FILE = "products.json"
STATE_FILE = "state.json"
DEBUG_DIR = "debug"

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
TEST_MODE = os.environ.get("TEST_MODE", "").lower() == "true"
MENTION = os.environ.get("DISCORD_MENTION", "@everyone")

# Warn on Discord if a page can't be read for this many checks in a row
# (36 checks x 10 minutes = about 6 hours)
UNKNOWN_WARN_AFTER = 36

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}

IN, OUT, UNKNOWN = "IN_STOCK", "OUT_OF_STOCK", "UNKNOWN"


def now_ist():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=5, minutes=30)))


# ---------------------------------------------------------------- fetching
def fetch(url):
    """Return (status_code, html). Retries a few times on errors."""
    last = (0, "")
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            last = (r.status_code, r.text)
            if r.status_code == 200:
                return last
        except requests.RequestException as e:
            print(f"  fetch error: {e}")
        time.sleep(5 * (attempt + 1))
    return last


# ---------------------------------------------------------------- parsers
def check_flipkart(html, pid):
    low = html.lower()

    # 0) Page data: "availabilityStatus":"OUT_OF_STOCK" right after our listing id
    if pid:
        for m in re.finditer(re.escape(pid.lower()) + r'[a-z0-9]*"', low):
            near = low[m.end():m.end() + 300]
            s = re.search(r'"availabilitystatus":"([a-z_]+)"', near)
            if s:
                val = s.group(1)
                if val in ("out_of_stock", "unavailable", "coming_soon"):
                    return OUT, f"page data says {val}"
                if val in ("in_stock", "available"):
                    return IN, f"page data says {val}"

    # 1) The colour/variant swatch for OUR product id.
    #    Flipkart labels an unavailable colour "Out of stock" inside its link.
    swatch_oos = None
    if pid:
        for m in re.finditer(re.escape(pid), html):
            end = low.find("</a>", m.start())
            if end == -1 or end - m.start() > 4000:
                continue
            segment = low[m.start():end]
            if "<img" not in segment:  # only look at swatch links (they have an image)
                continue
            swatch_oos = ("out of stock" in segment) or ("sold out" in segment)
            if swatch_oos:
                break
    if swatch_oos is True:
        return OUT, "colour marked Out of stock"
    if swatch_oos is False:
        return IN, "colour no longer marked Out of stock"

    # 2) Fallback: page-level wording
    if "currently unavailable" in low or "sold out" in low:
        return OUT, "page says Sold out / Currently unavailable"
    if "add to cart" in low or "buy now" in low:
        return IN, "Add to cart / Buy now found"
    return UNKNOWN, "could not find stock wording"


def check_amazon(html):
    low = html.lower()
    if "enter the characters you see below" in low or "captcha" in low:
        return UNKNOWN, "Amazon showed a captcha"
    m = re.search(r'id="availability".{0,1500}?</div>', low, re.S)
    avail = m.group(0) if m else ""
    if any(w in avail for w in ("currently unavailable", "out of stock", "temporarily out of stock")):
        return OUT, "availability says out of stock"
    if 'id="add-to-cart-button"' in low or 'id="buy-now-button"' in low:
        return IN, "Add to Cart button found"
    if "currently unavailable" in low:
        return OUT, "page says Currently unavailable"
    return UNKNOWN, "could not find stock wording"


def check_product(p):
    code, html = fetch(p["url"])
    if code != 200 or not html:
        return UNKNOWN, f"page did not load (HTTP {code})", html
    if p["site"] == "flipkart":
        status, why = check_flipkart(html, p.get("pid"))
    elif p["site"] == "amazon":
        status, why = check_amazon(html)
    else:
        status, why = UNKNOWN, f"unknown site {p['site']}"
    return status, why, html


# ---------------------------------------------------------------- discord
def discord(content, title=None, url=None, colour=0x2ECC71, description=None):
    if not WEBHOOK:
        print("  (no DISCORD_WEBHOOK_URL set, message not sent)")
        print("  " + content)
        return
    payload = {"content": content, "allowed_mentions": {"parse": ["everyone", "users", "roles"]}}
    if title:
        payload["embeds"] = [{
            "title": title[:250],
            "url": url,
            "description": description or "",
            "color": colour,
            "footer": {"text": now_ist().strftime("Checked %d %b %Y, %I:%M %p IST")},
        }]
    r = requests.post(WEBHOOK, json=payload, timeout=20)
    print(f"  Discord replied HTTP {r.status_code}")
    r.raise_for_status()


# ---------------------------------------------------------------- main
def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main():
    products = load(PRODUCTS_FILE, [])
    state = load(STATE_FILE, {})
    today = now_ist().strftime("%Y-%m-%d")

    for p in products:
        pid = p["id"]
        print(f"Checking {p['name']} ...")
        status, why, html = check_product(p)
        print(f"  -> {status} ({why})")

        prev = state.get(pid, {})
        prev_status = prev.get("status")
        entry = dict(prev)

        if TEST_MODE:
            label = {IN: "IN STOCK ✅", OUT: "Out of stock ❌", UNKNOWN: "Couldn't read the page ⚠️"}[status]
            discord("🧪 Test message from your stock tracker. The alert is working!",
                    title=p["name"], url=p["url"], colour=0x3498DB,
                    description=f"Current status: **{label}**\n({why})")

        if status == UNKNOWN:
            entry["unknown_count"] = prev.get("unknown_count", 0) + 1
            os.makedirs(DEBUG_DIR, exist_ok=True)
            with open(os.path.join(DEBUG_DIR, f"{pid}.html"), "w", encoding="utf-8") as f:
                f.write(html or "")
            if entry["unknown_count"] == UNKNOWN_WARN_AFTER:
                discord("⚠️ The stock tracker hasn't been able to read this page for about 6 hours. "
                        "The site may be blocking checks. Please check the product yourself.",
                        title=p["name"], url=p["url"], colour=0xF1C40F, description=why)
        else:
            entry["unknown_count"] = 0
            entry["status"] = status

            if status == IN and prev_status != IN:
                discord(f"{MENTION} 🟢 **BACK IN STOCK!** Buy it now before it sells out again.",
                        title=p["name"], url=p["url"], colour=0x2ECC71,
                        description=f"[Open on {p['site'].title()}]({p['url']})")
            elif status == OUT and prev_status is None and not TEST_MODE:
                discord("👀 Stock tracker started. This product is out of stock right now. "
                        "I'll message here the moment it's back.",
                        title=p["name"], url=p["url"], colour=0x95A5A6)
            elif status == OUT and prev_status == IN:
                discord("🔴 Sold out again. I'll keep watching.",
                        title=p["name"], url=p["url"], colour=0xE74C3C)

        # Once-a-day date keeps the repo active so GitHub doesn't pause the schedule
        entry["last_checked_day"] = today
        state[pid] = entry

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
        f.write("\n")


if __name__ == "__main__":
    main()

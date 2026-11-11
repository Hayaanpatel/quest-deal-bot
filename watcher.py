#!/usr/bin/env python3
"""Meta Quest 3S 256GB deal watcher. Standard library only."""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("QUEST_CONFIG", os.path.join(HERE, "config.json"))
STATE_PATH = os.environ.get("QUEST_STATE", os.path.join(HERE, "state.json"))
FAIL_LIMIT = 5
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ---------- fetching ----------
def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        charset = r.headers.get_content_charset() or "utf-8"
    return raw.decode(charset, errors="replace")


# ---------- parsing ----------
class _Scan(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ld, self.meta = [], {}
        self._in_ld = False
        self._buf = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_ld, self._buf = True, []
        elif tag in ("meta", "span", "div", "p", "data", "link") and "content" in a:
            key = a.get("property") or a.get("itemprop") or a.get("name")
            if key:
                self.meta.setdefault(key.lower(), a["content"])

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self._in_ld = False
            self.ld.append("".join(self._buf))


def to_price(value):
    """'£1,299.99' / 299 / '299.00' -> float, else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(value))
    if not m:
        return None
    try:
        p = float(m.group(0).replace(",", ""))
    except ValueError:
        return None
    return p if p > 0 else None


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def parse_jsonld(blocks):
    """Return (price, in_stock) from JSON-LD offers; (None, None) if absent.
    in_stock is False only when every offer with an availability is OutOfStock-ish."""
    prices, avail = [], []
    for text in blocks:
        try:
            data = json.loads(text.strip())
        except (ValueError, TypeError):
            continue
        for node in _walk(data):
            if "offers" not in node:
                continue
            offers = node["offers"]
            for off in (offers if isinstance(offers, list) else [offers]):
                if not isinstance(off, dict):
                    continue
                p = to_price(off.get("price"))
                if p is None:
                    p = to_price(off.get("lowPrice"))
                if p is None and isinstance(off.get("priceSpecification"), dict):
                    p = to_price(off["priceSpecification"].get("price"))
                if p is not None:
                    prices.append(p)
                a = off.get("availability")
                if a:
                    avail.append(str(a))
    if not prices:
        return None, None
    in_stock = None
    if avail:
        bad = ("outofstock", "soldout", "discontinued")
        in_stock = not all(any(b in x.lower() for b in bad) for x in avail)
    return min(prices), in_stock


def extract_price(html, price_regex=None):
    """Return dict(price, in_stock, source) or None."""
    scan = _Scan()
    try:
        scan.feed(html)
    except Exception:
        pass
    p, stock = parse_jsonld(scan.ld)
    if p is not None:
        return {"price": p, "in_stock": stock, "source": "json-ld"}
    for key in ("og:price:amount", "product:price:amount", "price"):
        p = to_price(scan.meta.get(key))
        if p is not None:
            return {"price": p, "in_stock": None, "source": "meta:" + key}
    if price_regex:
        m = re.search(price_regex, html, re.S)
        if m:
            p = to_price(m.group(1) if m.groups() else m.group(0))
            if p is not None:
                return {"price": p, "in_stock": None, "source": "regex"}
    return None


def check_target(t):
    """Fetch + parse one target, trying its fallback if the main page fails.
    Returns (result_or_None, error_text_or_None)."""
    attempts = [(t["url"], t.get("price_regex"))]
    if str(t.get("fallback_url", "")).startswith("http"):
        attempts.append((t["fallback_url"], t.get("fallback_price_regex")))
    err = None
    for i, (url, rx) in enumerate(attempts):
        try:
            res = extract_price(fetch(url), rx)
            if res:
                if i:
                    res["source"] += " (fallback)"
                return res, None
            err = "no price found on page"
        except urllib.error.HTTPError as e:
            err = "HTTP %s" % e.code
        except Exception as e:  # network, timeout, TLS...
            err = "%s: %s" % (type(e).__name__, e)
    return None, err


# ---------- notify ----------
def notify(message, click, dry_run=False, title="Quest deal alert"):
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if dry_run:
        print("[dry-run] would notify: %s (click %s)" % (message, click))
        return True
    if not topic:
        print("NTFY_TOPIC not set; cannot send: %s" % message, file=sys.stderr)
        return False
    headers = {"Title": title, "Priority": "high", "Tags": "tada"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request("https://ntfy.sh/" + topic,
                                 data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print("ntfy send failed: %s" % e, file=sys.stderr)
        return False


# ---------- state ----------
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)


# ---------- core ----------
def evaluate(t, defaults, st, res):
    """Update st in place; return alert message or None."""
    target = float(t.get("target_price", defaults.get("target_price", 299)))
    drop = float(t.get("drop_percent", defaults.get("drop_percent", 10)))
    price = res["price"]
    st["fails"], st["blocked_alerted"] = 0, False
    prev_max = st.get("max_price")
    st["max_price"] = max(price, prev_max or 0)
    st["last_price"], st["last_source"] = price, res["source"]

    last_alert = st.get("last_alert")
    if last_alert is not None and price > last_alert:
        last_alert = st["last_alert"] = None          # price went back up: re-arm
    below_target = price <= target
    big_drop = prev_max is not None and price <= prev_max * (1 - drop / 100.0)
    if res["in_stock"] is False or not (below_target or big_drop):
        if not (below_target or big_drop):
            st["last_alert"] = None
        return None
    if last_alert is not None and price >= last_alert:
        return None                                   # not a new low
    st["last_alert"] = price
    why = "at/below £%g target" % target if below_target else \
        "%g%%+ below the £%g high" % (drop, prev_max)
    return "%s: £%.2f (%s)" % (t.get("name", t["url"]), price, why)


def run_once(dry_run=False):
    cfg = load_json(CONFIG_PATH, {})
    defaults = cfg.get("defaults", {})
    state = load_json(STATE_PATH, {})
    for t in cfg.get("targets", []):
        url = str(t.get("url", ""))
        name = t.get("name", url)
        if not url.startswith("http"):
            print("skip %s: url does not start with http" % name)
            continue
        st = state.setdefault(url, {})
        res, err = check_target(t)
        if res is None:
            st["fails"] = st.get("fails", 0) + 1
            print("FAIL %s: %s (%d in a row)" % (name, err, st["fails"]))
            if st["fails"] >= FAIL_LIMIT and not st.get("blocked_alerted"):
                if notify("%s: could not read a price %d checks in a row (%s). "
                          "Site may be blocking the bot or the page changed."
                          % (name, st["fails"], err), url, dry_run):
                    st["blocked_alerted"] = True
            continue
        msg = evaluate(t, defaults, st, res)
        print("OK   %s: £%.2f via %s%s" % (
            name, res["price"], res["source"],
            " [OUT OF STOCK]" if res["in_stock"] is False else ""))
        if msg and not notify(msg, url, dry_run):
            st["last_alert"] = None                   # retry next time
    if not dry_run:
        save_state(state)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="check once (default)")
    ap.add_argument("--loop", type=float, metavar="MINUTES", help="check repeatedly")
    ap.add_argument("--test-notify", action="store_true", help="send a test push")
    ap.add_argument("--dry-run", action="store_true", help="print alerts, send nothing, keep state")
    a = ap.parse_args()
    if a.test_notify:
        ok = notify("Test message from quest-deal-bot - alerts are working.",
                    None, a.dry_run, title="Quest deal bot test")
        print("test notification %s" % ("sent" if ok else "FAILED"))
        return 0 if ok else 1
    if a.loop:
        while True:
            run_once(a.dry_run)
            time.sleep(a.loop * 60)
    run_once(a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

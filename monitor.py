#!/usr/bin/env python3
"""Daily Core Web Vitals monitor with Google Chat alerts.

Run: python3 monitor.py  (config via env vars, see README)
Manual run (report to Chat, skip db): MANUAL_TRIGGER=true python3 monitor.py
"""
import json
import os
import sqlite3
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import date

# ---- CONFIG (env vars) ------------------------------------------------------
SITE_URLS = [u.strip() for u in os.environ["SITE_URLS"].split(",") if u.strip()]
PSI_API_KEY = os.environ["PSI_API_KEY"]
CHAT_WEBHOOK = os.environ["CHAT_WEBHOOK"]
DB_FILE = os.environ.get("DB_FILE", "data.db")
MANUAL_TRIGGER = os.environ.get("MANUAL_TRIGGER", "false").lower() == "true"

PSI_TIMEOUT = 60          # seconds per API call

# Thresholds — tune here if alerts get noisy
CWV_ABS = {"lcp_ms": 2500, "inp_ms": 200, "cls": 0.1}  # Google "Good" limits (p75)
CWV_REL_WORSE = 0.20      # CWV worse than 28d median by >20%
MIN_HISTORY = 7           # days of data required before relative checks

# ---- Fetchers ----------------------------------------------------------------

def fetch_cwv(url):
    """CrUX field data (p75) via PageSpeed Insights API for `url`.
    Returns {} if the URL has no CrUX field data (low traffic) or the API
    call failed/timed out — either way there's nothing to save/alert on."""
    q = urllib.parse.urlencode({"url": url, "key": PSI_API_KEY, "strategy": "mobile"})
    try:
        with urllib.request.urlopen(
                f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?{q}", timeout=PSI_TIMEOUT) as r:
            data = json.load(r)
    except Exception as e:  # network error, timeout, rate limit, malformed JSON
        print(f"PSI call failed for {url}: {e}", file=sys.stderr)
        return {}
    m = data.get("loadingExperience", {}).get("metrics", {})
    if not m:
        return {}
    return {
        "lcp_ms": m.get("LARGEST_CONTENTFUL_PAINT_MS", {}).get("percentile"),
        "inp_ms": m.get("INTERACTION_TO_NEXT_PAINT", {}).get("percentile"),
        "cls": (m.get("CUMULATIVE_LAYOUT_SHIFT_SCORE", {}).get("percentile") or 0) / 100,
    }

# ---- Storage -----------------------------------------------------------------

SCHEMA = """CREATE TABLE IF NOT EXISTS daily (
  day TEXT NOT NULL,
  url TEXT NOT NULL,
  lcp_ms INTEGER, inp_ms INTEGER, cls REAL,
  PRIMARY KEY (day, url)
)"""

COLS = ["lcp_ms", "inp_ms", "cls"]


def save(db, day, url, metrics):
    db.execute(
        f"INSERT OR REPLACE INTO daily (day, url, {', '.join(COLS)}) VALUES (?, ?, {', '.join('?' * len(COLS))})",
        [day, url] + [metrics.get(c) for c in COLS])
    db.commit()


def history(db, day, url, n=28):
    """Rows for `url` strictly before `day`, most recent first, as list of dicts."""
    cur = db.execute(
        f"SELECT day, {', '.join(COLS)} FROM daily WHERE day < ? AND url = ? ORDER BY day DESC LIMIT ?",
        (day, url, n))
    return [dict(zip(["day"] + COLS, row)) for row in cur.fetchall()]

# ---- Anomaly checks ----------------------------------------------------------

def _median(hist, key):
    vals = [h[key] for h in hist if h.get(key) is not None]
    return statistics.median(vals) if vals else None


def check_anomalies(today, hist):
    msgs = []
    # Absolute CWV thresholds
    labels = {"lcp_ms": ("LCP", "ms"), "inp_ms": ("INP", "ms"), "cls": ("CLS", "")}
    for key, limit in CWV_ABS.items():
        v = today.get(key)
        if v is not None and v > limit:
            name, unit = labels[key]
            msgs.append(f"🔴 {name} = {v}{unit} vượt ngưỡng Good ({limit}{unit})")

    if len(hist) < MIN_HISTORY:
        return msgs  # not enough history for relative checks

    # CWV worse than 28d median
    for key in CWV_ABS:
        v, med = today.get(key), _median(hist, key)
        if v and med and v > med * (1 + CWV_REL_WORSE):
            name, unit = labels[key]
            msgs.append(f"🟠 {name} = {v}{unit}, xấu hơn {((v / med) - 1) * 100:.0f}% so với median 28 ngày ({med:g}{unit})")

    return msgs

# ---- Alert -------------------------------------------------------------------

def alert(text):
    print(f"Alerting Chat: {text}")
    req = urllib.request.Request(
        CHAT_WEBHOOK, json.dumps({"text": text}).encode(),
        {"Content-Type": "application/json; charset=UTF-8"})
    urllib.request.urlopen(req, timeout=30)

# ---- Main --------------------------------------------------------------------

def _fmt(v, unit=""):
    return f"{v}{unit}" if v is not None else "N/A"


def _report(url, metrics):
    return (f"*{url}* — LCP {_fmt(metrics.get('lcp_ms'), 'ms')}, "
            f"INP {_fmt(metrics.get('inp_ms'), 'ms')}, CLS {_fmt(metrics.get('cls'))}")


def check_url(url, day, db):
    """Fetch + evaluate one URL. Returns (metrics_or_None, report_lines) — does not alert itself.
    Manual trigger always reports the full LCP/INP/CLS line (N/A for missing fields), even on fetch failure."""
    metrics = fetch_cwv(url)
    if not metrics:
        if MANUAL_TRIGGER:
            return None, [_report(url, {}) + " (không có dữ liệu CrUX hoặc API lỗi/timeout)"]
        return None, [f"⚠️ {url}: không lấy được dữ liệu CWV (không có trong CrUX hoặc API lỗi/timeout)"]

    hist = [] if MANUAL_TRIGGER else history(db, day, url)
    if not MANUAL_TRIGGER:
        save(db, day, url, metrics)

    anomalies = check_anomalies(metrics, hist)
    if MANUAL_TRIGGER:
        return metrics, [_report(url, metrics)] + anomalies
    return metrics, anomalies


def main():
    day = date.today().isoformat()
    db = None if MANUAL_TRIGGER else sqlite3.connect(DB_FILE)
    if db:
        db.execute(SCHEMA)

    failures, chat_lines = 0, []
    for url in SITE_URLS:
        metrics, msgs = check_url(url, day, db)
        if metrics is None:
            failures += 1
            chat_lines += msgs
            print(msgs[0])
        elif MANUAL_TRIGGER:
            chat_lines += msgs
            print(f"{url}: {metrics}")
        elif msgs:
            chat_lines.append(f"*Cảnh báo {url} — {day}*\n" + "\n".join(msgs))
            print(f"{url}: alerted {len(msgs)} anomalies")
        else:
            print(f"{url} {day}: OK. Metrics: {metrics}")

    if db:
        db.close()

    if chat_lines:
        alert("\n\n".join(chat_lines))

    if failures == len(SITE_URLS):
        sys.exit(1)


if __name__ == "__main__":
    main()

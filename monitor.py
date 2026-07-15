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

sys.stdout.reconfigure(encoding="utf-8")  # Windows console defaults to cp1252, can't print emoji/tiếng Việt
sys.stderr.reconfigure(encoding="utf-8")

# ---- CONFIG (env vars) ------------------------------------------------------
SITE_URLS = [u.strip() for u in os.environ["SITE_URLS"].split(",") if u.strip()]
PSI_API_KEY = os.environ["PSI_API_KEY"]
CHAT_WEBHOOK = os.environ["CHAT_WEBHOOK"]
DB_FILE = os.environ.get("DB_FILE", "data.db")
MANUAL_TRIGGER = os.environ.get("MANUAL_TRIGGER", "false").lower() == "true"

PSI_TIMEOUT = 120         # seconds per API call — a full Lighthouse audit can take 60-90s+ on heavy pages

# Thresholds — Google "Good" limits (p75) by default, override via env vars to tune without a code change
CWV_ABS = {
    "lcp_ms": int(os.environ.get("CWV_LCP_MS", 2500)),
    "inp_ms": int(os.environ.get("CWV_INP_MS", 200)),
    "cls": float(os.environ.get("CWV_CLS", 0.1)),
}
CWV_REL_WORSE = 0.20      # CWV worse than 28d median by >20%
MIN_HISTORY = 7           # days of data required before relative checks

# ---- Fetchers ----------------------------------------------------------------

def _psi_fetch(url):
    """Raw PSI API response for `url`, or None on network error/timeout/malformed JSON."""
    q = urllib.parse.urlencode({"url": url, "key": PSI_API_KEY, "strategy": "mobile"})
    try:
        with urllib.request.urlopen(
                f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?{q}", timeout=PSI_TIMEOUT) as r:
            return json.load(r)
    except Exception as e:  # network error, timeout, rate limit, malformed JSON
        print(f"PSI call failed for {url}: {e}", file=sys.stderr)
        return None


def fetch_cwv(url):
    """CrUX field data (p75, real-user 28-day aggregate) via PSI API for `url`.
    Returns {} if the URL has no CrUX field data (low traffic) or the API
    call failed/timed out — either way there's nothing to save/alert on."""
    data = _psi_fetch(url)
    if data is None:
        return {}
    m = data.get("loadingExperience", {}).get("metrics", {})
    if not m:
        return {}
    return {
        "lcp_ms": m.get("LARGEST_CONTENTFUL_PAINT_MS", {}).get("percentile"),
        "inp_ms": m.get("INTERACTION_TO_NEXT_PAINT", {}).get("percentile"),
        "cls": (m.get("CUMULATIVE_LAYOUT_SHIFT_SCORE", {}).get("percentile") or 0) / 100,
    }


LAB_AUDITS = {  # report key -> Lighthouse audit id
    "fcp": "first-contentful-paint",
    "lcp": "largest-contentful-paint",
    "tbt": "total-blocking-time",
    "cls": "cumulative-layout-shift",
    "ttfb": "server-response-time",
    "rtt": "network-rtt",
}


def fetch_lab_cwv(url):
    """Lighthouse lab data (single simulated run against current page) via PSI API for `url`.
    Used for manual triggers: unlike CrUX field data it's always available (no real-traffic
    requirement) and reflects the page as it is right now rather than a 28-day rolling average.
    No INP here — a lab run has no real user interacting with the page — TBT is Lighthouse's
    lab proxy for interactivity instead, shown in lab_detail alongside FCP/TTFB/RTT."""
    data = _psi_fetch(url)
    if data is None:
        return {}
    lr = data.get("lighthouseResult", {})
    audits = lr.get("audits", {})
    if not audits:
        return {}
    lcp = audits.get("largest-contentful-paint", {}).get("numericValue")
    cls = audits.get("cumulative-layout-shift", {}).get("numericValue")
    score = lr.get("categories", {}).get("performance", {}).get("score")
    return {
        "lcp_ms": round(lcp) if lcp is not None else None,
        "cls": cls,
        "performance_score": round(score * 100) if score is not None else None,
        "lab_detail": {k: audits.get(v, {}).get("displayValue") for k, v in LAB_AUDITS.items()},
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


def _data_source_label():
    """lab data (manual trigger, Lighthouse) vs field data (scheduled run, CrUX)."""
    return "lab data" if MANUAL_TRIGGER else "field data (CrUX)"


def _fmt_metric(key, v):
    return f"{v:.3f}" if key == "cls" else f"{v:,.0f}"  # CLS: 3 decimals; ms metrics: comma thousands


def check_anomalies(today, hist):
    msgs = []
    # Absolute CWV thresholds
    labels = {"lcp_ms": ("LCP", "ms"), "inp_ms": ("INP", "ms"), "cls": ("CLS", "")}
    for key, limit in CWV_ABS.items():
        v = today.get(key)
        if v is not None and v > limit:
            name, unit = labels[key]
            msgs.append(f"🔴 {name} = {_fmt_metric(key, v)}{unit} vượt ngưỡng Good ({_fmt_metric(key, limit)}{unit})")

    if len(hist) < MIN_HISTORY:
        return msgs  # not enough history for relative checks

    # CWV worse than 28d median (history is only ever field data — manual trigger skips it)
    for key in CWV_ABS:
        v, med = today.get(key), _median(hist, key)
        if v and med and v > med * (1 + CWV_REL_WORSE):
            name, unit = labels[key]
            msgs.append(f"🟠 {name} = {_fmt_metric(key, v)}{unit}, xấu hơn {((v / med) - 1) * 100:.0f}% "
                        f"so với median 28 ngày ({_fmt_metric(key, med)}{unit})")

    return msgs

# ---- Alert -------------------------------------------------------------------

def alert(text):
    print(f"Alerting Chat: {text}")
    req = urllib.request.Request(
        CHAT_WEBHOOK, json.dumps({"text": text}).encode(),
        {"Content-Type": "application/json; charset=UTF-8"})
    urllib.request.urlopen(req, timeout=30)

# ---- Main --------------------------------------------------------------------

LAB_REPORT_LABELS = [("fcp", "FCP"), ("lcp", "LCP"), ("tbt", "TBT"), ("cls", "CLS"), ("ttfb", "TTFB"), ("rtt", "NRTT")]


def _report(url, metrics):
    score = metrics.get("performance_score")
    detail = metrics.get("lab_detail", {})
    line = " | ".join(f"{label}: {detail.get(key) or 'N/A'}" for key, label in LAB_REPORT_LABELS)
    # <url> delimits the link explicitly — Slack's bare-URL autolink otherwise swallows
    # a trailing "*" right after the URL, breaking the bold markup around it.
    return (f"*<{url}>* [{_data_source_label()}]\n"
            f"Performance: {score if score is not None else 'N/A'}/100\n{line}")


def check_url(url, day, db):
    """Fetch + evaluate one URL. Returns (metrics_or_None, report_lines) — does not alert itself.
    Manual trigger fetches Lighthouse lab data (always available, no history) and always reports
    the full Performance/FCP/LCP/TBT/CLS/TTFB/NRTT breakdown (N/A for missing fields), even on
    fetch failure. Scheduled runs fetch CrUX field data and only report on anomaly."""
    metrics = fetch_lab_cwv(url) if MANUAL_TRIGGER else fetch_cwv(url)
    if not metrics:
        if MANUAL_TRIGGER:
            return None, [_report(url, {}) + " (không lấy được lab data - API lỗi/timeout)"]
        return None, [f"⚠️ {url}: không lấy được dữ liệu CWV field (không có trong CrUX hoặc API lỗi/timeout)"]

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
    chat_lines.append(f"*[CWV Report]*\n") if MANUAL_TRIGGER else chat_lines.append(f"*[CWV Auto Alert]*\n")
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
            chat_lines.append(f"*Cảnh báo {url} — {day}* [{_data_source_label()}]\n" + "\n".join(msgs))
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

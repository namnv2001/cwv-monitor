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

# An unset GitHub Actions secret renders as "" rather than raising — check explicitly so a
# missing repo secret fails fast here instead of a cryptic urllib error deep in alert().
if not SITE_URLS or not PSI_API_KEY or not CHAT_WEBHOOK:
    sys.exit("Missing SITE_URLS / PSI_API_KEY / CHAT_WEBHOOK — check GitHub repo Secrets (or .env locally)")

PSI_TIMEOUT = 120         # seconds per API call — a full Lighthouse audit can take 60-90s+ on heavy pages
PSI_MAX_ATTEMPTS = 3      # attempts on timeout before giving up (only timeouts are retried)

# Every URL is measured on both strategies — PSI runs one strategy per call, so this doubles
# the number of API calls (and the run time) rather than being a single wider request.
STRATEGIES = ["mobile", "desktop"]
STRATEGY_LABELS = {"mobile": "📱 *MOBILE*", "desktop": "🖥️ *DESKTOP*"}

# Thresholds — Google "Good" limits (p75) by default, override via env vars to tune without a code change
CWV_ABS = {
    "lcp_ms": int(os.environ.get("CWV_LCP_MS", 2500)),
    "inp_ms": int(os.environ.get("CWV_INP_MS", 200)),
    "cls": float(os.environ.get("CWV_CLS", 0.1)),
}
CWV_REL_WORSE = 0.20      # CWV worse than 28d median by >20%
CWV_REL_BETTER = 0.20     # CWV better than 28d median by >20%
MIN_HISTORY = 7           # days of data required before relative checks

CWV_LABELS = {"lcp_ms": ("LCP", "ms"), "inp_ms": ("INP", "ms"), "cls": ("CLS", "")}

# ---- Fetchers ----------------------------------------------------------------

def _is_timeout(e):
    """A plain socket read timeout raises TimeoutError directly; a connect-phase timeout comes
    wrapped in urllib.error.URLError(reason=TimeoutError(...)) instead."""
    return isinstance(e, TimeoutError) or isinstance(getattr(e, "reason", None), TimeoutError)


def _psi_fetch(url, strategy="mobile"):
    """Raw PSI API response for `url` on `strategy` (mobile/desktop), or None on network
    error/timeout/malformed JSON. Retries up to PSI_MAX_ATTEMPTS times on timeout only — a
    4xx/malformed-JSON response won't succeed on retry, so those fail immediately without
    burning attempts."""
    q = urllib.parse.urlencode({"url": url, "key": PSI_API_KEY, "strategy": strategy})
    api_url = f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?{q}"
    for attempt in range(1, PSI_MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(api_url, timeout=PSI_TIMEOUT) as r:
                return json.load(r)
        except Exception as e:  # network error, timeout, rate limit, malformed JSON
            if _is_timeout(e) and attempt < PSI_MAX_ATTEMPTS:
                print(f"PSI call timed out for {url} (attempt {attempt}/{PSI_MAX_ATTEMPTS}), retrying: {e}",
                      file=sys.stderr)
                continue
            print(f"PSI call failed for {url}: {e}", file=sys.stderr)
            return None


def fetch_cwv(url, strategy="mobile"):
    """CrUX field data (p75, real-user 28-day aggregate) via PSI API for `url` on `strategy`.
    Returns {} if the URL has no CrUX field data for that device (low traffic) or the API
    call failed/timed out — either way there's nothing to save/alert on."""
    data = _psi_fetch(url, strategy)
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


def fetch_lab_cwv(url, strategy="mobile"):
    """Lighthouse lab data (single simulated run against current page) via PSI API for `url`
    on `strategy`. Used for manual triggers: unlike CrUX field data it's always available (no
    real-traffic requirement) and reflects the page as it is right now rather than a 28-day
    rolling average. No INP here — a lab run has no real user interacting with the page — TBT is
    Lighthouse's lab proxy for interactivity instead, shown in lab_detail alongside FCP/TTFB/RTT."""
    data = _psi_fetch(url, strategy)
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
  strategy TEXT NOT NULL DEFAULT 'mobile',
  lcp_ms INTEGER, inp_ms INTEGER, cls REAL,
  PRIMARY KEY (day, url, strategy)
)"""

COLS = ["lcp_ms", "inp_ms", "cls"]


def migrate(db):
    """Pre-desktop DBs have PK (day, url), which rejects a desktop row for a day/url already
    measured on mobile. SQLite can't ALTER a primary key, so rebuild the table and backfill the
    existing (mobile-only) rows. No-op once the strategy column exists."""
    if "strategy" in [r[1] for r in db.execute("PRAGMA table_info(daily)")]:
        return
    db.executescript(
        f"ALTER TABLE daily RENAME TO daily_old;"
        f"{SCHEMA};"
        f"INSERT INTO daily (day, url, strategy, {', '.join(COLS)}) "
        f"  SELECT day, url, 'mobile', {', '.join(COLS)} FROM daily_old;"
        f"DROP TABLE daily_old;")
    db.commit()


def save(db, day, url, strategy, metrics):
    db.execute(
        f"INSERT OR REPLACE INTO daily (day, url, strategy, {', '.join(COLS)}) "
        f"VALUES (?, ?, ?, {', '.join('?' * len(COLS))})",
        [day, url, strategy] + [metrics.get(c) for c in COLS])
    db.commit()


def history(db, day, url, strategy, n=28):
    """Rows for `url` on `strategy` strictly before `day`, most recent first, as list of dicts.
    Kept per-device: a desktop median must never be compared against mobile history."""
    cur = db.execute(
        f"SELECT day, {', '.join(COLS)} FROM daily WHERE day < ? AND url = ? AND strategy = ? "
        f"ORDER BY day DESC LIMIT ?",
        (day, url, strategy, n))
    return [dict(zip(["day"] + COLS, row)) for row in cur.fetchall()]

# ---- Anomaly checks ----------------------------------------------------------

def _median(hist, key):
    vals = [h[key] for h in hist if h.get(key) is not None]
    return statistics.median(vals) if vals else None


def _data_source_label():
    """lab data (manual trigger, Lighthouse) vs field data (scheduled run, CrUX)."""
    return "lab data" if MANUAL_TRIGGER else "field data (CrUX)"


def _is_monday(day):
    return date.fromisoformat(day).weekday() == 0


def _fmt_metric(key, v):
    return f"{v:.3f}" if key == "cls" else f"{v:,.0f}"  # CLS: 3 decimals; ms metrics: comma thousands


def check_anomalies(today, hist):
    msgs = []
    # Absolute CWV thresholds
    for key, limit in CWV_ABS.items():
        v = today.get(key)
        if v is not None and v > limit:
            name, unit = CWV_LABELS[key]
            msgs.append(f"🔴 {name} = {_fmt_metric(key, v)}{unit} vượt ngưỡng Good ({_fmt_metric(key, limit)}{unit})")

    if len(hist) < MIN_HISTORY:
        return msgs  # not enough history for relative checks

    # CWV worse than 28d median (history is only ever field data — manual trigger skips it)
    for key in CWV_ABS:
        v, med = today.get(key), _median(hist, key)
        if v and med and v > med * (1 + CWV_REL_WORSE):
            name, unit = CWV_LABELS[key]
            msgs.append(f"🟠 {name} = {_fmt_metric(key, v)}{unit}, xấu hơn {((v / med) - 1) * 100:.0f}% "
                        f"so với median 28 ngày ({_fmt_metric(key, med)}{unit})")

    return msgs


def check_improvements(today, hist):
    """Good news, mirrors check_anomalies: a metric recovering back under its Good threshold
    (vs the last recorded day), or beating the 28d median by a wide margin. Auto flow alerts on
    these too — a silent "no warnings" run still shouldn't hide meaningful improvement."""
    msgs = []
    # Recovered to Good threshold since the last recorded day
    if hist:
        prev = hist[0]
        for key, limit in CWV_ABS.items():
            v, pv = today.get(key), prev.get(key)
            if v is not None and pv is not None and pv > limit and v <= limit:
                name, unit = CWV_LABELS[key]
                msgs.append(f"✅ {name} = {_fmt_metric(key, v)}{unit} đã về ngưỡng Good ({_fmt_metric(key, limit)}{unit}), "
                            f"trước đó vượt ngưỡng ({_fmt_metric(key, pv)}{unit})")

    if len(hist) < MIN_HISTORY:
        return msgs  # not enough history for relative checks

    # CWV better than 28d median
    for key in CWV_ABS:
        v, med = today.get(key), _median(hist, key)
        if v and med and v < med * (1 - CWV_REL_BETTER):
            name, unit = CWV_LABELS[key]
            msgs.append(f"🟢 {name} = {_fmt_metric(key, v)}{unit}, cải thiện {((1 - v / med)) * 100:.0f}% "
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


def check_url(url, strategy, day, db):
    """Fetch + evaluate one URL on one device strategy. Returns (metrics_or_None, report_lines)
    — does not alert itself. Manual trigger fetches Lighthouse lab data (always available, no
    history) and always reports the full Performance/FCP/LCP/TBT/CLS/TTFB/NRTT breakdown (N/A for
    missing fields), even on fetch failure. Scheduled runs fetch CrUX field data and only report
    on anomaly or improvement, compared against that same device's history."""
    metrics = fetch_lab_cwv(url, strategy) if MANUAL_TRIGGER else fetch_cwv(url, strategy)
    if not metrics:
        if MANUAL_TRIGGER:
            return None, [_report(url, {}) + " (không lấy được lab data - API lỗi/timeout)"]
        return None, [f"⚠️ {url}: không lấy được dữ liệu CWV field (không có trong CrUX hoặc API lỗi/timeout)"]

    if MANUAL_TRIGGER:
        return metrics, [_report(url, metrics)] + check_anomalies(metrics, [])

    hist = history(db, day, url, strategy)
    save(db, day, url, strategy, metrics)
    return metrics, check_anomalies(metrics, hist) + check_improvements(metrics, hist)


def main():
    day = date.today().isoformat()
    db = None if MANUAL_TRIGGER else sqlite3.connect(DB_FILE)
    if db:
        db.execute(SCHEMA)
        migrate(db)

    failures, by_device = 0, {s: [] for s in STRATEGIES}
    for strategy in STRATEGIES:
        for url in SITE_URLS:
            metrics, msgs = check_url(url, strategy, day, db)
            if metrics is None:
                failures += 1
                by_device[strategy] += msgs
                print(f"[{strategy}] {msgs[0]}")
            elif MANUAL_TRIGGER:
                by_device[strategy] += msgs
                print(f"[{strategy}] {url}: {metrics}")
            elif msgs:
                by_device[strategy].append(f"*{url} — {day}* [{_data_source_label()}]\n" + "\n".join(msgs))
                print(f"[{strategy}] {url}: alerted {len(msgs)} item(s)")
            else:
                print(f"[{strategy}] {url} {day}: OK. Metrics: {metrics}")

    if db:
        db.close()

    # One section per device, devices with nothing to report are dropped entirely rather than
    # printed as an empty heading.
    chat_lines = [f"{STRATEGY_LABELS[s]}\n" + "\n\n".join(by_device[s]) for s in STRATEGIES if by_device[s]]

    # Warnings only alert on Monday — other days stay quiet unless there's an improvement to
    # report, which is worth surfacing regardless of day.
    if not MANUAL_TRIGGER and chat_lines and not _is_monday(day):
        has_improvement = any(m in line for line in chat_lines for m in ("✅", "🟢"))
        if not has_improvement:
            print(f"{day}: not Monday, no improvement to report — skipping auto alert")
            chat_lines = []

    # Manual trigger always reports (that's the point of a manual check). Auto/scheduled runs
    # only alert Chat when there's something notable — a warning/failure or an improvement —
    # silence on plain no-news days.
    if MANUAL_TRIGGER and chat_lines:
        alert("📌 *[CWV Report]*\n" + "\n\n".join(chat_lines))
    elif not MANUAL_TRIGGER and chat_lines:
        has_warning = any(m in line for line in chat_lines for m in ("🔴", "🟠", "⚠️"))
        header = "💀 *[CWV Auto Alert]*\n" if has_warning else "🎉 *[CWV Auto Update]*\n"
        alert(header + "\n\n".join(chat_lines))

    if failures == len(SITE_URLS) * len(STRATEGIES):
        sys.exit(1)


if __name__ == "__main__":
    main()

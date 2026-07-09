#!/usr/bin/env python3
"""Daily CWV + Google Search Console monitor with Google Chat alerts.

Run: python3 monitor.py  (config via env vars, see README)
"""
import json
import os
import sqlite3
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta

# ---- CONFIG (env vars) ------------------------------------------------------
SITE_URL = os.environ["SITE_URL"]                  # e.g. https://example.com/
PSI_API_KEY = os.environ["PSI_API_KEY"]
CHAT_WEBHOOK = os.environ["CHAT_WEBHOOK"]
GSC_SA_FILE = os.environ.get("GSC_SA_FILE", "sa.json")
DB_FILE = os.environ.get("DB_FILE", "data.db")

# Thresholds — tune here if alerts get noisy
CWV_ABS = {"lcp_ms": 2500, "inp_ms": 200, "cls": 0.1}  # Google "Good" limits (p75)
CWV_REL_WORSE = 0.20      # CWV worse than 28d median by >20%
GSC_DROP = 0.30           # clicks/impressions drop >30% vs same-weekday median
POS_WORSE = 0.20          # avg position worse by >20%
MIN_HISTORY = 7           # days of data required before relative checks

# ---- Fetchers ----------------------------------------------------------------

def fetch_cwv():
    """CrUX field data (p75) via PageSpeed Insights API. Returns dict or {} if no field data."""
    q = urllib.parse.urlencode({"url": SITE_URL, "key": PSI_API_KEY, "strategy": "mobile"})
    with urllib.request.urlopen(f"https://www.googleapis.com/pagespeedonline/v5/runPagespeed?{q}", timeout=120) as r:
        data = json.load(r)
    m = data.get("loadingExperience", {}).get("metrics", {})
    if not m:
        return {}  # site not in CrUX (low traffic)
    return {
        "lcp_ms": m.get("LARGEST_CONTENTFUL_PAINT_MS", {}).get("percentile"),
        "inp_ms": m.get("INTERACTION_TO_NEXT_PAINT", {}).get("percentile"),
        "cls": (m.get("CUMULATIVE_LAYOUT_SHIFT_SCORE", {}).get("percentile") or 0) / 100,
    }


def fetch_gsc(day):
    """Search analytics totals for one day. Requires google-api-python-client."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        GSC_SA_FILE, scopes=["https://www.googleapis.com/auth/webmasters.readonly"])
    svc = build("searchconsole", "v1", credentials=creds, cache_discovery=False)
    resp = svc.searchanalytics().query(
        siteUrl=SITE_URL, body={"startDate": day, "endDate": day}).execute()
    rows = resp.get("rows")
    if not rows:
        return {}
    r = rows[0]
    return {"clicks": r["clicks"], "impressions": r["impressions"],
            "ctr": r["ctr"], "position": r["position"]}

# ---- Storage -----------------------------------------------------------------

SCHEMA = """CREATE TABLE IF NOT EXISTS daily (
  day TEXT PRIMARY KEY,
  lcp_ms INTEGER, inp_ms INTEGER, cls REAL,
  clicks INTEGER, impressions INTEGER, ctr REAL, position REAL
)"""

COLS = ["lcp_ms", "inp_ms", "cls", "clicks", "impressions", "ctr", "position"]


def save(db, day, metrics):
    db.execute(
        f"INSERT OR REPLACE INTO daily (day, {', '.join(COLS)}) VALUES (?, {', '.join('?' * len(COLS))})",
        [day] + [metrics.get(c) for c in COLS])
    db.commit()


def history(db, day, n=28):
    """Rows strictly before `day`, most recent first, as list of dicts."""
    cur = db.execute(
        f"SELECT day, {', '.join(COLS)} FROM daily WHERE day < ? ORDER BY day DESC LIMIT ?", (day, n))
    return [dict(zip(["day"] + COLS, row)) for row in cur.fetchall()]

# ---- Anomaly checks ----------------------------------------------------------

def _median(hist, key, weekday=None):
    vals = [h[key] for h in hist
            if h.get(key) is not None
            and (weekday is None or date.fromisoformat(h["day"]).weekday() == weekday)]
    return statistics.median(vals) if vals else None


def check_anomalies(day, today, hist):
    msgs = []
    # Absolute CWV thresholds
    labels = {"lcp_ms": ("LCP", "ms"), "inp_ms": ("INP", "ms"), "cls": ("CLS", "")}
    for key, limit in CWV_ABS.items():
        v = today.get(key)
        if v is not None and v > limit:
            name, unit = labels[key]
            msgs.append(f"🔴 {name} p75 = {v}{unit} vượt ngưỡng Good ({limit}{unit})")

    if len(hist) < MIN_HISTORY:
        return msgs  # not enough history for relative checks

    # CWV worse than 28d median
    for key in CWV_ABS:
        v, med = today.get(key), _median(hist, key)
        if v and med and v > med * (1 + CWV_REL_WORSE):
            name, unit = labels[key]
            msgs.append(f"🟠 {name} = {v}{unit}, xấu hơn {((v / med) - 1) * 100:.0f}% so với median 28 ngày ({med:g}{unit})")

    # GSC drop vs same weekday
    wd = date.fromisoformat(day).weekday()
    for key in ("clicks", "impressions"):
        v, med = today.get(key), _median(hist, key, weekday=wd)
        if v is not None and med and v < med * (1 - GSC_DROP):
            msgs.append(f"🟠 {key} = {v}, giảm {(1 - v / med) * 100:.0f}% so với median cùng thứ ({med:g})")

    # Position worse (higher number = worse)
    v, med = today.get("position"), _median(hist, "position")
    if v and med and v > med * (1 + POS_WORSE):
        msgs.append(f"🟠 Vị trí trung bình = {v:.1f}, tệ hơn {((v / med) - 1) * 100:.0f}% so với median ({med:.1f})")

    return msgs

# ---- Alert -------------------------------------------------------------------

def alert(text):
    req = urllib.request.Request(
        CHAT_WEBHOOK, json.dumps({"text": text}).encode(),
        {"Content-Type": "application/json; charset=UTF-8"})
    urllib.request.urlopen(req, timeout=30)

# ---- Main --------------------------------------------------------------------

def main():
    day = (date.today() - timedelta(days=2)).isoformat()  # GSC data lags ~2 days
    metrics = {**fetch_cwv(), **fetch_gsc(day)}
    if not metrics:
        alert(f"⚠️ cwv-gsc-monitor: không lấy được dữ liệu nào cho {SITE_URL} ngày {day}")
        sys.exit(1)

    db = sqlite3.connect(DB_FILE)
    db.execute(SCHEMA)
    hist = history(db, day)
    save(db, day, metrics)

    msgs = check_anomalies(day, metrics, hist)
    if msgs:
        alert(f"*Cảnh báo {SITE_URL} — {day}*\n" + "\n".join(msgs))
        print(f"Alerted {len(msgs)} anomalies")
    else:
        print(f"{day}: OK, no anomalies. Metrics: {metrics}")


if __name__ == "__main__":
    main()

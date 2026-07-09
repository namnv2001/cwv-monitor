"""Smoke test for anomaly logic + storage. Run: python3 test_monitor.py"""
import os
import sqlite3
from datetime import date, timedelta

# Dummy env so monitor.py imports without real credentials
os.environ.setdefault("SITE_URL", "https://example.com/")
os.environ.setdefault("PSI_API_KEY", "x")
os.environ.setdefault("CHAT_WEBHOOK", "http://localhost/x")

import monitor


def make_db(days=28, clicks=100):
    db = sqlite3.connect(":memory:")
    db.execute(monitor.SCHEMA)
    for i in range(1, days + 1):
        d = (date(2026, 7, 7) - timedelta(days=i)).isoformat()
        monitor.save(db, d, {"lcp_ms": 2000, "inp_ms": 150, "cls": 0.05,
                             "clicks": clicks, "impressions": 5000, "ctr": 0.02, "position": 10.0})
    return db


def test():
    day = "2026-07-07"  # a Tuesday
    db = make_db()
    hist = monitor.history(db, day)
    assert len(hist) == 28 and hist[0]["day"] == "2026-07-06"

    # Healthy day -> no alerts
    good = {"lcp_ms": 2000, "inp_ms": 150, "cls": 0.05,
            "clicks": 100, "impressions": 5000, "ctr": 0.02, "position": 10.0}
    assert monitor.check_anomalies(day, good, hist) == []

    # Bad day -> absolute CWV, relative CWV, clicks drop, position worse
    bad = {"lcp_ms": 3000, "inp_ms": 250, "cls": 0.2,
           "clicks": 40, "impressions": 5000, "ctr": 0.008, "position": 15.0}
    msgs = monitor.check_anomalies(day, bad, hist)
    assert len(msgs) >= 5, msgs
    assert any("LCP" in m for m in msgs) and any("clicks" in m for m in msgs)

    # Too little history -> only absolute checks fire
    short_hist = monitor.history(make_db(days=3), day)
    msgs = monitor.check_anomalies(day, bad, short_hist)
    assert all(m.startswith("🔴") for m in msgs), msgs

    # Missing metrics don't crash
    assert monitor.check_anomalies(day, {}, hist) == []

    print("All checks passed")


if __name__ == "__main__":
    test()

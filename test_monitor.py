"""Smoke test for fetch defense + anomaly logic + storage. Run: python3 test_monitor.py"""
import os
import sqlite3
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

# Dummy env so monitor.py imports without real credentials
os.environ.setdefault("SITE_URLS", "https://example.com/")
os.environ.setdefault("PSI_API_KEY", "x")
os.environ.setdefault("CHAT_WEBHOOK", "http://localhost/x")

import monitor


def make_db(days=28, url="https://example.com/"):
    db = sqlite3.connect(":memory:")
    db.execute(monitor.SCHEMA)
    for i in range(1, days + 1):
        d = (date(2026, 7, 7) - timedelta(days=i)).isoformat()
        monitor.save(db, d, url, {"lcp_ms": 2000, "inp_ms": 150, "cls": 0.05})
    return db


def test_history_and_anomalies():
    day = "2026-07-07"
    db = make_db()
    hist = monitor.history(db, day, "https://example.com/")
    assert len(hist) == 28 and hist[0]["day"] == "2026-07-06"

    # Other URLs must not leak into this URL's history
    assert monitor.history(db, day, "https://other.com/") == []

    good = {"lcp_ms": 2000, "inp_ms": 150, "cls": 0.05}
    assert monitor.check_anomalies(good, hist) == []

    bad = {"lcp_ms": 3000, "inp_ms": 250, "cls": 0.2}
    msgs = monitor.check_anomalies(bad, hist)
    assert len(msgs) >= 3, msgs
    assert any("LCP" in m for m in msgs)

    # Too little history -> only absolute checks fire
    short_hist = monitor.history(make_db(days=3), day, "https://example.com/")
    msgs = monitor.check_anomalies(bad, short_hist)
    assert all(m.startswith("🔴") for m in msgs), msgs

    # Missing metrics don't crash
    assert monitor.check_anomalies({}, hist) == []


def _mock_psi_response(payload):
    fake_resp = MagicMock()
    fake_resp.__enter__.return_value = fake_resp
    return patch.object(monitor.urllib.request, "urlopen", return_value=fake_resp), \
        patch.object(monitor.json, "load", return_value=payload)


def test_fetch_cwv_parses_field_data():
    payload = {"loadingExperience": {"metrics": {
        "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 2000},
        "INTERACTION_TO_NEXT_PAINT": {"percentile": 150},
        "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 5},
    }}}
    p_urlopen, p_load = _mock_psi_response(payload)
    with p_urlopen, p_load:
        metrics = monitor.fetch_cwv("https://example.com/")
    assert metrics == {"lcp_ms": 2000, "inp_ms": 150, "cls": 0.05}


def test_fetch_cwv_empty_when_no_crux_data():
    p_urlopen, p_load = _mock_psi_response({})  # no loadingExperience — low-traffic URL
    with p_urlopen, p_load:
        assert monitor.fetch_cwv("https://example.com/") == {}


def test_fetch_cwv_defends_against_api_failure():
    with patch.object(monitor.urllib.request, "urlopen", side_effect=OSError("timed out")):
        assert monitor.fetch_cwv("https://example.com/") == {}


def test_manual_report_always_has_full_metrics_even_on_fetch_failure():
    with patch.object(monitor, "MANUAL_TRIGGER", True), \
         patch.object(monitor, "fetch_cwv", return_value={}):
        metrics, msgs = monitor.check_url("https://example.com/", "2026-07-07", db=None)
    assert metrics is None  # still counts as a failure for the exit-code check
    assert "LCP N/A" in msgs[0] and "INP N/A" in msgs[0] and "CLS N/A" in msgs[0]


def test_manual_report_shows_na_for_partial_metrics():
    with patch.object(monitor, "MANUAL_TRIGGER", True), \
         patch.object(monitor, "fetch_cwv", return_value={"lcp_ms": 2000, "inp_ms": None, "cls": 0.05}):
        metrics, msgs = monitor.check_url("https://example.com/", "2026-07-07", db=None)
    assert metrics is not None
    assert "LCP 2000ms" in msgs[0] and "INP N/A" in msgs[0] and "CLS 0.05" in msgs[0]


if __name__ == "__main__":
    test_history_and_anomalies()
    test_fetch_cwv_parses_field_data()
    test_fetch_cwv_empty_when_no_crux_data()
    test_fetch_cwv_defends_against_api_failure()
    test_manual_report_always_has_full_metrics_even_on_fetch_failure()
    test_manual_report_shows_na_for_partial_metrics()
    print("All checks passed")

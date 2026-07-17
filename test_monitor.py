"""Smoke test for fetch defense + anomaly logic + storage. Run: python3 test_monitor.py"""
import os
import sqlite3
import subprocess
import sys
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


def test_empty_secret_fails_fast_with_clear_message():
    # Simulates an unset GitHub Actions secret, which renders as "" rather than being absent.
    env = {**os.environ, "SITE_URLS": "https://example.com/", "PSI_API_KEY": "x", "CHAT_WEBHOOK": ""}
    result = subprocess.run([sys.executable, "-c", "import monitor"],
                             cwd=os.path.dirname(os.path.abspath(__file__)), env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "CHAT_WEBHOOK" in result.stderr


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


def test_check_improvements_recovered_to_good_threshold():
    # Yesterday breached LCP's Good threshold, today is back under it -> recovery message
    hist = [{"day": "2026-07-06", "lcp_ms": 3000, "inp_ms": 150, "cls": 0.05}]
    today = {"lcp_ms": 2000, "inp_ms": 150, "cls": 0.05}
    msgs = monitor.check_improvements(today, hist)
    assert any("✅" in m and "LCP" in m for m in msgs)

    # Already good yesterday -> no recovery message today
    hist_already_good = [{"day": "2026-07-06", "lcp_ms": 2000, "inp_ms": 150, "cls": 0.05}]
    assert monitor.check_improvements(today, hist_already_good) == []


def test_check_improvements_better_than_median():
    db = make_db()  # 28 days at lcp_ms=2000
    hist = monitor.history(db, "2026-07-07", "https://example.com/")
    much_better = {"lcp_ms": 1200, "inp_ms": 150, "cls": 0.05}
    msgs = monitor.check_improvements(much_better, hist)
    assert any("🟢" in m and "LCP" in m for m in msgs)

    # Too little history -> only recovery checks fire, not the median comparison
    short_hist = monitor.history(make_db(days=3), "2026-07-07", "https://example.com/")
    msgs = monitor.check_improvements(much_better, short_hist)
    assert all(m.startswith("✅") for m in msgs), msgs

    # Missing metrics don't crash
    assert monitor.check_improvements({}, hist) == []


def test_check_anomalies_formats_lcp_inp_with_thousands_separator_and_cls_3_decimals():
    msgs = monitor.check_anomalies({"lcp_ms": 34189, "inp_ms": 12345, "cls": 0.1107290548610793}, [])
    assert any("LCP = 34,189ms" in m for m in msgs)
    assert any("INP = 12,345ms" in m for m in msgs)
    assert any("CLS = 0.111" in m for m in msgs)


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


def test_fetch_lab_cwv_parses_lighthouse_audits():
    payload = {"lighthouseResult": {
        "categories": {"performance": {"score": 0.35}},
        "audits": {
            "first-contentful-paint": {"numericValue": 2800, "displayValue": "2.8 s"},
            "largest-contentful-paint": {"numericValue": 35100, "displayValue": "35.1 s"},
            "total-blocking-time": {"numericValue": 1560, "displayValue": "1,560 ms"},
            "cumulative-layout-shift": {"numericValue": 0.016, "displayValue": "0.016"},
            "server-response-time": {"numericValue": 1230, "displayValue": "Root document took 1,230 ms"},
            "network-rtt": {"numericValue": 280, "displayValue": "280 ms"},
        },
    }}
    p_urlopen, p_load = _mock_psi_response(payload)
    with p_urlopen, p_load:
        metrics = monitor.fetch_lab_cwv("https://example.com/")
    assert metrics["lcp_ms"] == 35100 and metrics["cls"] == 0.016 and metrics["performance_score"] == 35
    assert metrics["lab_detail"] == {
        "fcp": "2.8 s", "lcp": "35.1 s", "tbt": "1,560 ms",
        "cls": "0.016", "ttfb": "Root document took 1,230 ms", "rtt": "280 ms",
    }


def test_fetch_lab_cwv_empty_when_no_lighthouse_result():
    p_urlopen, p_load = _mock_psi_response({})
    with p_urlopen, p_load:
        assert monitor.fetch_lab_cwv("https://example.com/") == {}


def test_manual_report_always_has_full_metrics_even_on_fetch_failure():
    with patch.object(monitor, "MANUAL_TRIGGER", True), \
         patch.object(monitor, "fetch_lab_cwv", return_value={}):
        metrics, msgs = monitor.check_url("https://example.com/", "2026-07-07", db=None)
    assert metrics is None  # still counts as a failure for the exit-code check
    assert "lab data" in msgs[0]
    assert "Performance: N/A/100" in msgs[0]
    assert "FCP: N/A" in msgs[0] and "TTFB: N/A" in msgs[0] and "NRTT: N/A" in msgs[0]


def test_manual_report_shows_full_lab_breakdown():
    lab_metrics = {
        "lcp_ms": 35100, "cls": 0.016, "performance_score": 35,
        "lab_detail": {"fcp": "2.8 s", "lcp": "35.1 s", "tbt": "1,560 ms",
                       "cls": "0.016", "ttfb": "Root document took 1,230 ms", "rtt": "280 ms"},
    }
    with patch.object(monitor, "MANUAL_TRIGGER", True), \
         patch.object(monitor, "fetch_lab_cwv", return_value=lab_metrics):
        metrics, msgs = monitor.check_url("https://example.com/", "2026-07-07", db=None)
    assert metrics is not None
    assert "lab data" in msgs[0]
    assert "Performance: 35/100" in msgs[0]
    assert "FCP: 2.8 s" in msgs[0] and "LCP: 35.1 s" in msgs[0] and "TBT: 1,560 ms" in msgs[0]
    assert "CLS: 0.016" in msgs[0] and "TTFB: Root document took 1,230 ms" in msgs[0] and "NRTT: 280 ms" in msgs[0]
    # LCP way over the Good threshold -> absolute anomaly still fires on lab data
    assert "lab data" in msgs[0]  # source label is on the report header line
    assert any("🔴" in m and "LCP" in m for m in msgs)


def test_data_source_label_follows_manual_trigger():
    # main() stamps this label on the "Cảnh báo ..." header line for scheduled alerts;
    # check_anomalies itself doesn't repeat it on every 🔴/🟠 line.
    with patch.object(monitor, "MANUAL_TRIGGER", False):
        assert monitor._data_source_label() == "field data (CrUX)"
    with patch.object(monitor, "MANUAL_TRIGGER", True):
        assert monitor._data_source_label() == "lab data"


if __name__ == "__main__":
    test_empty_secret_fails_fast_with_clear_message()
    test_history_and_anomalies()
    test_check_improvements_recovered_to_good_threshold()
    test_check_improvements_better_than_median()
    test_check_anomalies_formats_lcp_inp_with_thousands_separator_and_cls_3_decimals()
    test_fetch_cwv_parses_field_data()
    test_fetch_cwv_empty_when_no_crux_data()
    test_fetch_cwv_defends_against_api_failure()
    test_fetch_lab_cwv_parses_lighthouse_audits()
    test_fetch_lab_cwv_empty_when_no_lighthouse_result()
    test_manual_report_always_has_full_metrics_even_on_fetch_failure()
    test_manual_report_shows_full_lab_breakdown()
    test_data_source_label_follows_manual_trigger()
    print("All checks passed")

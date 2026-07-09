# Kế hoạch triển khai — CWV & GSC Monitor

Script Python chạy hàng ngày: lấy Core Web Vitals (PageSpeed Insights API) và số liệu Google Search Console, lưu SQLite, so sánh với lịch sử, bắn alert vào Google Chat webhook khi bất thường.

## Kiến trúc

```
[cron / GitHub Actions, hàng ngày]
        │
   monitor.py
        ├── PSI API ──────────► CWV field data (LCP, INP, CLS) của SITE_URL
        ├── GSC API ──────────► clicks, impressions, ctr, position (ngày D-2)
        ├── SQLite (data.db) ─► lưu lịch sử daily
        ├── check_anomalies() ► so với ngưỡng tuyệt đối + median lịch sử
        └── Google Chat webhook ◄ alert nếu có bất thường
```

Không có server, không có dashboard, không có DB server. 1 file Python + 1 file SQLite.

## Phase 1 — Setup credentials (thủ công, ~30 phút)

1. **Google Cloud project**: tạo project (hoặc dùng project sẵn có), bật 2 API:
   - PageSpeed Insights API
   - Google Search Console API
2. **API key** cho PSI: Credentials → Create API key → giới hạn scope PSI API.
3. **Service account** cho GSC: Create service account → tạo key JSON → tải về.
4. **Add SA vào GSC**: Search Console → Settings → Users → thêm email service account, quyền *Restricted* (đọc là đủ).
5. **Google Chat webhook**: mở space → Apps & integrations → Webhooks → tạo, copy URL.

## Phase 2 — Script chính (`monitor.py`)

Một file, các hàm:

| Hàm | Việc | Nguồn |
|---|---|---|
| `fetch_cwv()` | GET PSI API, đọc `loadingExperience.metrics` (field data CrUX, percentile p75) | urllib, không cần lib |
| `fetch_gsc(day)` | `searchanalytics.query` cho ngày D-2 (GSC delay ~2 ngày) | google-api-python-client |
| `save(day, metrics)` | INSERT OR REPLACE vào bảng `daily` | sqlite3 (stdlib) |
| `history(n)` | Lấy n ngày gần nhất từ DB | sqlite3 |
| `check_anomalies(today, hist)` | Trả về list message vi phạm | thuần Python |
| `alert(messages)` | POST `{"text": "..."}` vào webhook | urllib |

Schema SQLite:

```sql
CREATE TABLE IF NOT EXISTS daily (
  day TEXT PRIMARY KEY,        -- YYYY-MM-DD (ngày dữ liệu GSC, D-2)
  lcp_ms INTEGER, inp_ms INTEGER, cls REAL,   -- CWV p75
  clicks INTEGER, impressions INTEGER, ctr REAL, position REAL
);
```

## Phase 3 — Luật phát hiện bất thường

Hai lớp, đều là threshold đơn giản:

**Tuyệt đối (CWV vượt ngưỡng "Good" của Google):**
- LCP p75 > 2500ms
- INP p75 > 200ms
- CLS p75 > 0.1

**Tương đối (so với lịch sử):**
- CWV xấu đi > 20% so với median 28 ngày
- clicks hoặc impressions giảm > 30% so với median cùng thứ-trong-tuần của 4 tuần trước (so cùng thứ để tránh false alert cuối tuần)
- position tệ đi > 20% (số tăng = tệ)

Cần ≥ 7 ngày dữ liệu mới bật so sánh tương đối; trước đó chỉ check tuyệt đối.

## Phase 4 — Deploy: GitHub Actions (khuyến nghị) hoặc cron

**GitHub Actions** (`.github/workflows/monitor.yml`):
- Schedule `0 9 * * *` (16h VN) — sau khi GSC có data D-2.
- Secrets: `GSC_SA_JSON`, `PSI_API_KEY`, `CHAT_WEBHOOK`, `SITE_URL`.
- `data.db` được commit lại vào repo sau mỗi run → storage miễn phí, có luôn lịch sử trong git.

**Cron trên server** (thay thế): `0 16 * * * cd /opt/cwv-monitor && python3 monitor.py` với env vars trong `/etc/environment` hoặc `.env`.

## Phase 5 — Vận hành

- Chạy tay lần đầu: `python3 monitor.py` → xác nhận nhận được message "monitor started OK" trong Chat.
- Tuần đầu để ý false alert → chỉnh ngưỡng trong phần CONFIG đầu file.
- Muốn theo dõi nhiều site: chạy script nhiều lần với `SITE_URL` khác nhau (matrix trong Actions).

## Không làm (YAGNI)

- Dashboard / UI — xem lịch sử bằng `sqlite3 data.db` hoặc export CSV khi cần.
- RUM script trên site — CrUX field data qua PSI là đủ.
- ML anomaly detection — threshold + median đủ; nâng cấp z-score nếu false alert nhiều.
- Theo dõi per-page / per-query — thêm dimension vào GSC query khi thực sự cần.

## Ước lượng

| Việc | Thời gian |
|---|---|
| Phase 1 (credentials) | ~30 phút thủ công |
| Phase 2–3 (code) | có sẵn trong repo này |
| Phase 4 (deploy) | ~15 phút |

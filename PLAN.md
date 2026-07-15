# Kế hoạch triển khai — CWV Monitor

Script Python chạy hàng ngày: với mỗi URL trong danh sách, lấy Core Web Vitals (CrUX field data qua PageSpeed Insights API), lưu SQLite, so sánh với lịch sử, bắn alert vào Google Chat webhook khi bất thường. Có thể trigger thủ công để test nhanh mà không lưu lịch sử.

## Kiến trúc

```
[cron / GitHub Actions, hàng ngày]        [workflow_dispatch, thủ công]
        │                                          │
        └──────────────────┬───────────────────────┘
                        monitor.py  (lặp qua từng URL trong SITE_URLS)
                            ├── fetch_cwv(url) → PSI API, đọc loadingExperience field data, {} nếu fail/timeout/không có CrUX
                            ├── SQLite (data.db) ─► lưu lịch sử daily, PK (day, url) — bỏ qua khi MANUAL_TRIGGER
                            ├── check_anomalies() ► so với ngưỡng tuyệt đối + median lịch sử theo url
                            └── Google Chat webhook ◄ alert khi bất thường (cron) hoặc luôn báo (manual)
```

Không có server, không có dashboard, không có DB server. 1 file Python + 1 file SQLite.

## Phase 1 — Setup credentials (thủ công, ~10 phút)

1. **Google Cloud project**: tạo project (hoặc dùng project sẵn có), bật **PageSpeed Insights API**.
2. **API key** cho PSI: Credentials → Create API key → giới hạn scope PSI API.
3. **Google Chat webhook**: mở space → Apps & integrations → Webhooks → tạo, copy URL.

## Phase 2 — Script chính (`monitor.py`)

Một file, các hàm:

| Hàm | Việc | Nguồn |
|---|---|---|
| `fetch_cwv(url)` | GET PSI API, đọc `loadingExperience.metrics` (field data CrUX, percentile p75). Trả `{}` nếu lỗi/timeout hoặc URL không có trong CrUX | urllib, không cần lib |
| `save(day, url, metrics)` | INSERT OR REPLACE vào bảng `daily` | sqlite3 (stdlib) |
| `history(day, url, n)` | Lấy n ngày gần nhất của `url` từ DB | sqlite3 |
| `check_anomalies(today, hist)` | Trả về list message vi phạm | thuần Python |
| `alert(text)` | POST `{"text": "..."}` vào webhook | urllib |
| `check_url(url, day, db)` | Gộp fetch + lưu (nếu không phải manual) + check cho 1 URL | thuần Python |

Schema SQLite (đổi từ single-URL sang multi-URL, vẫn dùng field data):

```sql
CREATE TABLE IF NOT EXISTS daily (
  day TEXT NOT NULL,           -- YYYY-MM-DD
  url TEXT NOT NULL,
  lcp_ms INTEGER, inp_ms INTEGER, cls REAL,   -- CWV field data (CrUX), p75
  PRIMARY KEY (day, url)
);
```

`fetch_cwv()` gọi PSI API 1 lần/URL — không retry nhiều lần lấy điểm cao nhất, vì field data CrUX là số liệu tổng hợp 28 ngày, gọi lại trong cùng ngày cho ra kết quả giống nhau. Việc gọi API lỗi/timeout được defense bằng try/except, trả `{}` (đã đủ nghĩa "không có dữ liệu" — không phân biệt với trường hợp URL không có trong CrUX, vì UX xử lý (log cảnh báo) là như nhau).

## Phase 3 — Luật phát hiện bất thường

Hai lớp, đều là threshold đơn giản, tính riêng theo từng URL:

**Tuyệt đối (CWV vượt ngưỡng "Good" của Google):**
- LCP p75 > 2500ms
- INP p75 > 200ms
- CLS p75 > 0.1

**Tương đối (so với lịch sử của cùng URL):**
- CWV xấu đi > 20% so với median 28 ngày

Cần ≥ 7 ngày dữ liệu mới bật so sánh tương đối; trước đó chỉ check tuyệt đối. Luồng trigger thủ công (xem Phase 4b) không lưu lịch sử nên chỉ check tuyệt đối.

## Phase 4 — Deploy: GitHub Actions (khuyến nghị) hoặc cron

**GitHub Actions** (`.github/workflows/monitor.yml`):
- Schedule `0 9 * * *` (16h VN).
- Secrets: `PSI_API_KEY`, `CHAT_WEBHOOK`, `SITE_URLS` (phân tách bằng dấu phẩy).
- `data.db` được commit lại vào repo sau mỗi run theo lịch → storage miễn phí, có luôn lịch sử trong git.

**Cron trên server** (thay thế): `0 16 * * * cd /opt/cwv-monitor && python3 monitor.py` với env vars trong `/etc/environment` hoặc `.env`.

### Phase 4b — Trigger thủ công

Dùng sẵn `workflow_dispatch` (nút **Run workflow**) làm cơ chế trigger thủ công — không cần thêm CLI flag/parser. Workflow set `MANUAL_TRIGGER=true` khi `github.event_name == 'workflow_dispatch'`; `monitor.py` đọc biến này và:
- Bỏ qua `save()`/`history()` — không đụng `data.db`.
- Luôn gửi kết quả (LCP, INP, CLS) của mọi URL về Chat, không chỉ khi có bất thường.

Chạy local tương đương: `MANUAL_TRIGGER=true python3 monitor.py`.

## Phase 5 — Vận hành

- Chạy tay lần đầu: `python3 monitor.py` → xác nhận nhận được alert/report trong Chat.
- Tuần đầu để ý false alert → chỉnh ngưỡng trong phần CONFIG đầu file.
- Thêm/bớt site: sửa `SITE_URLS` (secret hoặc env var), không cần đổi code hay matrix trong Actions.

## Không làm (YAGNI)

- Dashboard / UI — xem lịch sử bằng `sqlite3 data.db` hoặc export CSV khi cần.
- RUM script trên site — CrUX field data qua PSI là đủ.
- ML anomaly detection — threshold + median đủ; nâng cấp z-score nếu false alert nhiều.
- Retry nhiều lần / lab data (Lighthouse) — field data không đổi trong ngày nên retry không giúp gì; nếu sau này cần theo dõi URL traffic thấp (không có CrUX), mới cần cân nhắc lab data.
- CLI argparse cho manual trigger — `workflow_dispatch` + 1 env var là đủ.

## Ước lượng

| Việc | Thời gian |
|---|---|
| Phase 1 (credentials) | ~30 phút thủ công |
| Phase 2–3 (code) | có sẵn trong repo này |
| Phase 4 (deploy) | ~15 phút |

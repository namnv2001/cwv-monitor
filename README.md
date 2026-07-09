# cwv-gsc-monitor

Monitor Core Web Vitals + Google Search Console hàng ngày, alert vào Google Chat khi bất thường. Một script Python, lưu lịch sử bằng SQLite. Chi tiết thiết kế: xem [PLAN.md](PLAN.md).

## Setup

1. Google Cloud: bật **PageSpeed Insights API** + **Search Console API**, tạo API key (PSI) và service account key JSON (GSC).
2. Search Console → Settings → Users: thêm email service account, quyền Restricted.
3. Google Chat space → Apps & integrations → Webhooks: tạo webhook, copy URL.

## Chạy local

```bash
pip install -r requirements.txt
export SITE_URL=https://example.com/   # đúng property trong GSC (hoặc sc-domain:example.com)
export PSI_API_KEY=...
export CHAT_WEBHOOK=https://chat.googleapis.com/v1/spaces/...
export GSC_SA_FILE=sa.json             # default
python3 monitor.py
```

Test logic (không cần credentials): `python3 test_monitor.py`

## Chạy tự động (GitHub Actions)

Push repo lên GitHub, thêm 4 secrets: `SITE_URL`, `PSI_API_KEY`, `CHAT_WEBHOOK`, `GSC_SA_JSON` (nội dung file sa.json). Workflow chạy 16h VN hàng ngày, commit `data.db` lại vào repo làm lịch sử.

## Luật cảnh báo

- 🔴 CWV vượt ngưỡng Good: LCP > 2500ms, INP > 200ms, CLS > 0.1 (p75, mobile)
- 🟠 CWV xấu đi >20% so với median 28 ngày
- 🟠 Clicks/impressions giảm >30% so với median cùng thứ-trong-tuần
- 🟠 Position tệ đi >20% so với median

Chỉnh ngưỡng ở phần CONFIG đầu `monitor.py`. Cần ≥7 ngày dữ liệu mới bật so sánh tương đối.

# cwv-monitor

Monitor Core Web Vitals hàng ngày cho nhiều URL, alert vào Google Chat khi bất thường. Một script Python, lưu lịch sử bằng SQLite.

## Setup

1. Google Cloud: bật **PageSpeed Insights API**, tạo API key.
2. Google Chat space → Apps & integrations → Webhooks: tạo webhook, copy URL.

## Chạy local

```bash
export SITE_URLS=https://example.com/,https://example.com/pricing   # nhiều URL, phân tách bằng dấu phẩy
export PSI_API_KEY=...
export CHAT_WEBHOOK=https://chat.googleapis.com/v1/spaces/...
python3 monitor.py
```

Hoặc gom vào file `.env` (đã có trong `.gitignore`) rồi `source` trước khi chạy — không cần cài `python-dotenv`:

```bash
# .env
SITE_URLS=https://example.com/,https://example.com/pricing
PSI_API_KEY=...
CHAT_WEBHOOK=https://chat.googleapis.com/v1/spaces/...
```

```bash
set -a; source .env; set +a
python3 monitor.py
```

Mỗi URL lấy CrUX field data (p75, số liệu tổng hợp 28 ngày từ người dùng thật) qua PSI API — 1 lần gọi mỗi URL, không cần retry nhiều lần vì field data không đổi trong ngày. Một URL lỗi API/timeout hoặc không có trong CrUX (traffic thấp) sẽ báo cảnh báo riêng, các URL khác vẫn tiếp tục chạy.

Không có dependency ngoài stdlib. Test logic: `python3 test_monitor.py` — CI chạy test này tự động trên mỗi push/PR (xem `.github/workflows/test.yml`), không cần setup gì thêm.

## Trigger thủ công

Set `MANUAL_TRIGGER=true` (hoặc chạy workflow bằng nút **Run workflow** trên GitHub Actions — đã tự set biến này). Luồng này khác luồng chạy theo lịch:
- **Lấy số liệu từ lab data (Lighthouse)** thay vì field data (CrUX) — một lần chạy Lighthouse mô phỏng ngay lúc trigger, phản ánh đúng trạng thái trang hiện tại, không cần đợi đủ traffic thật như CrUX.
- **Luôn báo đủ breakdown về Chat** cho từng URL, không chỉ khi có bất thường:
  ```
  *https://example.com/* [lab data]
  Performance: 35/100
  FCP: 2.8 s | LCP: 35.1 s | TBT: 1,560 ms | CLS: 0.016 | TTFB: Root document took 1,230 ms | NRTT: 280 ms
  ```
  TBT (Total Blocking Time) thay cho INP làm proxy tương tác — lab không mô phỏng tương tác người dùng thật nên không có INP. Thiếu chỉ số nào hiện `N/A`.
- **Không lưu vào `data.db`** — dùng để test nhanh, không ảnh hưởng lịch sử/so sánh median.
- Mỗi dòng report/alert đều gắn nhãn `[lab data]` hoặc `[field data (CrUX)]` để phân biệt rõ nguồn số liệu.

## Chạy tự động (GitHub Actions)

Push repo lên GitHub, thêm 3 secrets (Settings → Secrets and variables → Actions → tab **Secrets**): `SITE_URLS`, `PSI_API_KEY`, `CHAT_WEBHOOK`. Workflow có 2 lịch chạy tự động, cộng nút **Run workflow** để chạy thủ công bất kỳ lúc nào:
- **16h VN hàng ngày** — luồng field data (CrUX), lưu `data.db`.
- **9h sáng thứ 2 hàng tuần** — luồng manual trigger (lab data, Lighthouse), không lưu `data.db` — xem mục trên.

Muốn tune ngưỡng CWV mà không đổi code: thêm **Repository variables** (cùng chỗ, đổi qua tab **Variables**) `CWV_LCP_MS`, `CWV_INP_MS`, `CWV_CLS` — không cần thiết lập cả 3, thiếu cái nào workflow tự dùng mặc định của cái đó.

## Luật cảnh báo

- 🔴 CWV vượt ngưỡng Good: LCP > 2500ms, INP > 200ms, CLS > 0.1 (p75, mobile)
- 🟠 CWV xấu đi >20% so với median 28 ngày (theo từng URL)

Luồng auto (chạy theo lịch) chỉ gửi Chat khi có điều gì đó đáng nói — cảnh báo, lỗi fetch, hoặc tin tốt (xem dưới) — không thì im lặng, không alert. Luồng manual trigger luôn báo cáo đầy đủ về Chat, kể cả khi không có cảnh báo (đó là mục đích của việc trigger thủ công).

Tin tốt (chỉ áp dụng cho luồng auto, cần lịch sử field data):
- ✅ Chỉ số vừa quay lại ngưỡng Good, ngày trước đó còn vượt ngưỡng.
- 🟢 Chỉ số tốt hơn median 28 ngày >20% (đối xứng với ngưỡng 🟠 xấu đi, tune qua hằng số `CWV_REL_BETTER` đầu `monitor.py`, không có env var riêng).

Cuối tuần (thứ 7, chủ nhật), luồng auto skip alert nếu chỉ có cảnh báo/lỗi fetch — không ai trực Chat để xử lý ngay. Nếu có ít nhất 1 tin tốt (✅/🟢) thì vẫn báo bình thường như ngày thường.

Header của message Chat tự đổi theo nội dung: 💀 `[CWV Auto Alert]` nếu có ít nhất 1 cảnh báo/lỗi fetch, 🎉 `[CWV Auto Update]` nếu toàn tin tốt.

Ngưỡng tuyệt đối (🔴) tune được qua env var, không cần sửa code:

| Env var | Mặc định | Ý nghĩa |
|---|---|---|
| `CWV_LCP_MS` | `2500` | Ngưỡng LCP (ms) |
| `CWV_INP_MS` | `200` | Ngưỡng INP (ms) |
| `CWV_CLS` | `0.1` | Ngưỡng CLS |

`CWV_REL_WORSE` (%so median 28 ngày) và `MIN_HISTORY` (số ngày dữ liệu tối thiểu) vẫn ở phần CONFIG đầu `monitor.py`. Cần ≥7 ngày dữ liệu mới bật so sánh tương đối.

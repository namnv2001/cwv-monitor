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

Mỗi URL lấy CrUX field data (p75, số liệu tổng hợp 28 ngày từ người dùng thật) qua PSI API. Gọi API timeout sẽ tự retry tối đa `PSI_MAX_ATTEMPTS` lần (mặc định 3, chỉ áp dụng cho timeout — lỗi khác như 4xx/JSON hỏng không retry vì thử lại cũng không thành công). Một URL vẫn lỗi sau khi retry hoặc không có trong CrUX (traffic thấp) sẽ báo cảnh báo riêng, các URL khác vẫn tiếp tục chạy.

## Mobile & desktop

Mỗi URL được đo trên **cả 2 device**: `mobile` và `desktop` (hằng số `STRATEGIES` đầu `monitor.py`). PSI chỉ nhận 1 strategy mỗi lần gọi, nên số lần gọi API (và thời gian chạy) gấp đôi so với chỉ đo mobile — lưu ý nếu API key có quota chặt.

Lịch sử trong `data.db` tách riêng theo device (khoá chính `(day, url, strategy)`), median 28 ngày của desktop chỉ so với desktop, không bao giờ lẫn với mobile. DB cũ (khoá chính `(day, url)`, chỉ có số liệu mobile) tự động migrate ở lần chạy đầu tiên: bảng được dựng lại và toàn bộ dữ liệu cũ gán `strategy = 'mobile'`, không mất dòng nào.

Message Chat chia thành từng khối theo device, device nào không có gì để báo thì bỏ hẳn khối đó:

```
💀 *[CWV Auto Alert]*
📱 *MOBILE*
*https://example.com/ — 2026-08-17* [field data (CrUX)]
🚨 LCP = 3,543ms vượt ngưỡng Good (2,500ms) — trước đó 2,410ms

🖥️ *DESKTOP*
*https://example.com/ — 2026-08-17* [field data (CrUX)]
🟢 LCP = 1,100ms, cải thiện 45% so với median 28 ngày (2,000ms)
```

Không có dependency ngoài stdlib. Test logic: `python3 test_monitor.py` — CI chạy test này tự động trên mỗi push/PR (xem `.github/workflows/test.yml`), không cần setup gì thêm.

## Trigger thủ công

Set `MANUAL_TRIGGER=true` (hoặc chạy workflow bằng nút **Run workflow** trên GitHub Actions — đã tự set biến này). Luồng này khác luồng chạy theo lịch:
- **Lấy số liệu từ lab data (Lighthouse)** thay vì field data (CrUX) — một lần chạy Lighthouse mô phỏng ngay lúc trigger, phản ánh đúng trạng thái trang hiện tại, không cần đợi đủ traffic thật như CrUX.
- **Luôn báo đủ breakdown về Chat** cho từng URL trên từng device, không chỉ khi có bất thường:
  ```
  📌 *[CWV Report]*
  📱 *MOBILE*
  *https://example.com/* [lab data]
  Performance: 35/100
  FCP: 2.8 s | LCP: 35.1 s | TBT: 1,560 ms | CLS: 0.016 | TTFB: Root document took 1,230 ms | NRTT: 280 ms

  🖥️ *DESKTOP*
  *https://example.com/* [lab data]
  Performance: 78/100
  FCP: 1.1 s | LCP: 2.4 s | TBT: 210 ms | CLS: 0.004 | TTFB: Root document took 420 ms | NRTT: 40 ms
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

- 🚨 CWV **vừa** vượt ngưỡng Good — ngày có số liệu gần nhất còn dưới ngưỡng. Đây là sự cố thật, gửi Chat bất kể thứ mấy.
- 🔴 CWV vượt ngưỡng Good: LCP > 2500ms, INP > 200ms, CLS > 0.1 (p75) — cùng ngưỡng cho cả mobile lẫn desktop — nhưng đã vượt từ trước rồi. Field data là p75 rolling 28 ngày nên con số này gần như không đổi từng ngày; báo lại mỗi ngày không thêm thông tin, nên nó chỉ vào digest thứ 2.
- 🟠 CWV xấu đi >20% so với median 28 ngày (theo từng URL, từng device), **và** đang vượt ngưỡng Good. Chỉ so tương đối khi chỉ số đã bad: cả 2 chuỗi đều là cùng một rolling 28 ngày, và CLS lượng tử hoá ở 0.01 nên 0.06 → 0.10 đọc thành "+67% xấu hơn" dù hai giá trị đều còn Good.

Luồng auto (chạy theo lịch) chỉ gửi Chat khi có điều gì đó đáng nói — cảnh báo, lỗi fetch, hoặc tin tốt (xem dưới) — không thì im lặng, không alert. Luồng manual trigger luôn báo cáo đầy đủ về Chat, kể cả khi không có cảnh báo (đó là mục đích của việc trigger thủ công).

Tin tốt (chỉ áp dụng cho luồng auto, cần lịch sử field data):
- ✅ Chỉ số vừa quay lại ngưỡng Good, ngày trước đó còn vượt ngưỡng.
- 🟢 Chỉ số tốt hơn median 28 ngày >20% — không gate theo ngưỡng Good như 🟠: một chỉ số vẫn đang bad nhưng đang hồi phục rõ rệt (INP 300ms, tốt hơn 30% so với median 430ms) vẫn là tin đáng báo. Tune qua hằng số `CWV_REL_BETTER` đầu `monitor.py`, không có env var riêng.

Luồng auto vẫn chạy và lưu `data.db` mỗi ngày, nhưng gửi Chat theo 2 chế độ:
- **Thứ 2** — digest đầy đủ: mọi thứ ở trên, gồm cả 🔴 đang vượt ngưỡng và 🟠/🟢 so với median.
- **Các ngày khác** — chỉ những dòng báo *thay đổi trạng thái*: 🚨 vừa vượt ngưỡng và ✅ vừa về ngưỡng Good (hằng số `URGENT` trong `monitor.py`). Lọc theo từng dòng, từng URL, từng device — một tin tốt ở URL này không mở cổng cho cảnh báo cũ của URL khác. Không có dòng nào như vậy thì im lặng hoàn toàn.

Đo trên 40 ngày history thật: luật cũ gửi 39/40 ngày (9–14 dòng mỗi lần), luật này gửi 11/40 (6 digest thứ 2 + 5 ngày có đúng 1 sự kiện thật).

Header của message Chat tự đổi theo nội dung: 💀 `[CWV Auto Alert]` nếu có ít nhất 1 cảnh báo (🚨/🔴/🟠) hoặc lỗi fetch, 🎉 `[CWV Auto Update]` nếu toàn tin tốt.

Ngưỡng tuyệt đối (🔴) tune được qua env var, không cần sửa code:

| Env var | Mặc định | Ý nghĩa |
|---|---|---|
| `CWV_LCP_MS` | `2500` | Ngưỡng LCP (ms) |
| `CWV_INP_MS` | `200` | Ngưỡng INP (ms) |
| `CWV_CLS` | `0.1` | Ngưỡng CLS |

`CWV_REL_WORSE` (%so median 28 ngày) và `MIN_HISTORY` (số ngày dữ liệu tối thiểu) vẫn ở phần CONFIG đầu `monitor.py`. Cần ≥7 ngày dữ liệu mới bật so sánh tương đối.

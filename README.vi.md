# hermes-cliproxyapi-provider

[English](README.md) | Tiếng Việt | [简体中文](README.zh-CN.md)

> Bản dịch này có thể chậm hơn bản tiếng Anh. Khi có khác biệt, [README.md](README.md) là bản chuẩn.

Plugin nhà cung cấp mô hình (model-provider) cho
[Hermes Agent](https://github.com/NousResearch/hermes-agent), dùng với
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (CPA).

CPA là một gateway: một instance đứng trước nhiều nhà cung cấp (Claude, Codex,
Gemini, xAI, Kimi, Devin, các upstream tương thích OpenAI, các nhà cung cấp dạng
plugin như Kiro) và tự quản lý tài khoản, OAuth, xoay vòng và chuyển đổi định
dạng của chúng. Nếu chỉ có Hermes, gateway như vậy chỉ là một endpoint tùy chỉnh
duy nhất, trên một wire duy nhất, với metadata phải ghim bằng tay.

Plugin này phản chiếu CPA vào Hermes. Trỏ nó tới một instance CPA, nó sẽ đăng ký
một provider Hermes cho mỗi nhà cung cấp CPA, mỗi provider dùng wire mà executor
của CPA cho nhà cung cấp đó chấp nhận với ít chuyển đổi nhất, kèm danh sách mô
hình và metadata theo từng mô hình mà CPA công bố. Plugin không thêm bất cứ thứ gì
CPA không phục vụ.

## Cài đặt

```bash
hermes plugins install duu261/hermes-cliproxyapi-provider
```

Yêu cầu Hermes 0.21.7 trở lên. Plugin model-provider hoạt động ngay sau khi cài,
không cần bật. Khởi động lại các Hermes gateway đang chạy để chúng nạp plugin.

## Cấu hình

Không cần cấu hình: đặt `CPA_LOCAL_API_KEY` (một client key của CPA) trong
`~/.hermes/.env`. Bạn sẽ có `cpa-local` cho CPA tại `http://127.0.0.1:8317`; đổi
URL bằng `CPA_LOCAL_BASE_URL`.

Với instance có tên hoặc instance từ xa, thêm vào `~/.hermes/config.yaml`:

```yaml
cliproxyapi:
  instances:
    main:
      base_url: https://cpa.example.com   # with or without /v1
      key_env: CPA_MAIN_API_KEY           # env var holding a CPA client key
      mgmt_key_env: CPA_MAIN_MGMT_KEY     # optional, see "Exact grouping"
```

Sau đó chạy `hermes model` (hoặc bất kỳ lệnh nào liệt kê mô hình) một lần. Plugin
đọc danh sách mô hình của instance, đăng ký các provider tìm thấy và lưu cache, nên
các lần chạy sau đăng ký chúng ngay lúc khởi động mà không cần gọi mạng:

- `cpa-main`: mọi mô hình trên instance, trên wire chat.
- `cpa-main-<provider>`: một provider cho mỗi nhà cung cấp CPA, ví dụ
  `cpa-main-claude`, `cpa-main-codex`, `cpa-main-xai`, `cpa-main-kimi`.

`hermes doctor` và các thiết lập nhiều tiến trình (`plugins.isolation: host`) chỉ
thấy provider xuất hiện sau lúc khởi động khi đã khởi động lại.

## Wire theo nhà cung cấp

| Nhà cung cấp CPA | Hermes `api_mode` | Nguồn trong CPA |
|---|---|---|
| `claude` | `anthropic_messages` | Executor Claude gửi Anthropic Messages |
| `kimi` | `anthropic_messages` | Executor Kimi chuyển định dạng Claude tới `/v1/messages` của Kimi |
| `kiro` (plugin) | `anthropic_messages` | Plugin Kiro nhận đầu vào dạng Claude |
| `codex`, `xai`, `meta` (Muse) | `codex_responses` | Các executor gửi định dạng Responses của CPA |
| `gemini`, `vertex`, `aistudio`, `gemini-interactions`, `antigravity`, `devin` | `chat_completions` | Wire Gemini và Interactions không khả dụng với Hermes cho host không phải của Google; CPA chuyển đổi |
| Tương thích OpenAI và mọi loại khác | `chat_completions` | Mọi executor của CPA đều chấp nhận |

Ghi đè theo từng nhà cung cấp bằng `api_mode` (xem bên dưới). Khi hai nhà cung cấp
phục vụ cùng một model id, id đó xuất hiện ở cả hai và CPA chọn tài khoản; khi đó
wire chỉ là lựa chọn phù hợp nhất.

## Nhóm chính xác (management key, tùy chọn)

Nhóm công khai là mặc định và đủ cho hầu hết thiết lập: trên ba pool CPA thật, nó
đặt mọi mô hình vào cùng wire với lần quét bằng management, và nó còn bao quát các
kênh API-key và tương thích OpenAI mà management API không thấy. Mô hình được nhóm
theo nhãn `owned_by` trên `/v1/models` công khai của CPA. Nhãn này ghi tên nhà sản
xuất mô hình, không phải nhà cung cấp CPA, nên có thể sai trong vài trường hợp:
Devin phục vụ mô hình Claude và GPT với nhãn `anthropic` và `openai`, `google` bao
gồm nhiều executor Gemini, và một id do hai nhà cung cấp phục vụ chỉ hiện một chủ
sở hữu.

Nếu gặp các trường hợp đó, hãy đặt `mgmt_key_env`. Khi đó plugin hỏi management API
của CPA xem nhà cung cấp nào phục vụ từng mô hình (mô hình nó không thấy giữ nhóm
công khai) và đọc định nghĩa mô hình của CPA để lấy giới hạn đầu ra, khả năng
vision và các mức reasoning. Thao tác này chỉ đọc và có giới hạn:

- Chỉ GET, tới danh sách credential, danh sách mô hình của vài tài khoản lấy mẫu
  cho mỗi nhà cung cấp, và danh sách định nghĩa mô hình cho mỗi nhà cung cấp. Không
  bao giờ đụng tới quota, usage, tải xuống, OAuth hay bất kỳ thao tác ghi nào.
- Tối đa 32 request và 20 giây mỗi lượt, tối đa một lượt mỗi 15 phút cho mỗi
  instance, không bao giờ nằm trên đường đi của request.
- CPA chặn một địa chỉ sau năm lần đăng nhập management thất bại. Plugin dừng ở lỗi
  401 đầu tiên và không bao giờ thử lại key đó với CPA đó, từ bất kỳ instance nào và
  qua cả các lần khởi động lại, cho tới khi key thay đổi. Nếu không ghi được cache,
  plugin báo bằng cảnh báo, vì chốt chặn qua khởi động lại nằm ở đó. Mọi lỗi khác
  quay về nhóm công khai.
- CPA phải cho phép management từ địa chỉ của plugin (`remote-management` trong cấu
  hình CPA), trừ khi cả hai chạy trên cùng host.
- Một management key của CPA làm được nhiều hơn các thao tác đọc này rất nhiều (đổi
  cấu hình, thêm hoặc xóa tài khoản). Plugin không bao giờ gửi gì ngoài các GET ở
  trên, nhưng bất kỳ ai đọc được biến môi trường thì có thể. Hãy để trống
  `mgmt_key_env` nếu nhóm công khai đã đủ gần đúng.

## Ghi đè

```yaml
cliproxyapi:
  instances:
    main:
      base_url: http://127.0.0.1:8317
      key_env: CPA_MAIN_API_KEY
      groups:
        xai:
          api_mode: chat_completions      # change one provider's wire
        fast:
          models: ["gpt-*-mini", "claude-haiku-*"]   # your own group
          display_name: Fast models
```

Một nhóm có `models` sẽ thay thành viên của nhà cung cấp đó bằng các id khớp trong
danh sách hiện tại (mẫu kiểu shell, phân biệt hoa thường).

## Định tuyến qua gateway tính phí (gateway đứng trước CPA)

Nếu bạn chạy một gateway tính phí như New API trước cùng CPA đó, plugin có thể gửi
inference qua gateway trong khi vẫn học mọi thứ từ CPA:

```yaml
cliproxyapi:
  instances:
    main:
      base_url: http://127.0.0.1:8317     # discovery: model list, providers, metadata
      key_env: CPA_MAIN_API_KEY
      route_via: https://gateway.example  # inference goes here and is billed there
      route_key_env: GATEWAY_KEY          # the gateway's key
      groups:
        xai:
          route_key_env: GATEWAY_GROK_KEY # optional: a key for another billing group
          api_mode: chat_completions      # how this gateway channel expects requests
```

- Các profile trỏ tới `route_via` và dùng key của gateway. Key CPA chỉ dùng cho danh
  sách mô hình của CPA và không bao giờ gửi tới gateway; key gateway không bao giờ
  gửi tới CPA. Hai key phải là hai biến môi trường khác nhau.
- Chỉ giữ các mô hình mà gateway liệt kê cho key đó (`GET /v1/models`). Nếu lấy danh
  sách từ gateway thất bại, danh sách đã biết gần nhất được giữ lại; nếu chưa có,
  không có gì được định tuyến cho key đó.
- Gateway thường chuyển đổi định dạng request theo từng kênh, trừ khi bật chế độ
  chuyển thẳng body. Profile giữ wire CPA theo bảng ở trên; đặt `api_mode` theo nhóm
  khi kênh gateway cần wire khác.
- Context window và mức reasoning mô tả mô hình của CPA, không phải giới hạn mà
  gateway có thể thêm vào.
- Gateway có thể chuyển sang các kênh khác mà nó bán dưới cùng tên mô hình. Plugin
  không thấy và không ngăn được việc đó.
- Plugin chỉ dùng các endpoint dành cho client của gateway, không bao giờ dùng API
  admin, kênh, giá hay tính phí của nó.
- Giới hạn đã biết: Hermes chỉ gỡ khỏi tiến trình con của agent những tên key mà một
  provider đã đăng ký khai báo. Provider được định tuyến khai báo key gateway, nên
  `key_env` của CPA (cần cho discovery) vẫn hiện trong shell của agent trên một
  instance có định tuyến. `mgmt_key_env` không bao giờ được khai báo trên provider,
  nên nó không bị gỡ trên cả instance có hay không có định tuyến. Hiện Hermes chưa
  có hook cho plugin để khai báo một secret mà không biến nó thành credential cho
  inference.

## Metadata

Context window, vision, hỗ trợ reasoning, các mức reasoning-effort và giới hạn đầu ra
chỉ lấy từ CPA: định nghĩa mô hình qua management khi có, nếu không thì từ danh sách
mô hình công khai của CPA, khớp theo đúng id. Giá trị CPA không công bố thì giữ là
chưa biết. Hermes có thể bổ sung metadata của nhà sản xuất từ models.dev:

```yaml
providers:
  cpa-main-claude:
    catalog_provider: anthropic
```

Thứ tự ưu tiên, cao nhất trước: `model_overrides` của bạn, plugin này, models.dev.

## Bảo mật

- Key được tham chiếu theo tên biến môi trường và do Hermes phân giải.
- Chỉ cho phép `http://` không mã hóa khi mọi địa chỉ mà host phân giải ra đều là loopback,
  RFC 1918, 100.64/10 hoặc IPv6 ULA. Mọi trường hợp khác cần `https://`.
- Không theo redirect, giới hạn kích thước response, và lỗi chỉ được báo bằng mã
  trạng thái HTTP và loại lỗi, không bao giờ kèm body, URL hay key.
- Request lấy catalog gửi `User-Agent: hermes-cliproxyapi-provider/<version>`. Một
  request `/v1/models` bổ sung nối thêm `grok-shell-compatible`, vì đó là client mà
  CPA công bố context window và mức reasoning cho; các id của nó được khớp với danh
  sách thường, nên nó chỉ thêm metadata, không bao giờ thêm mô hình. Request
  inference mang header riêng của Hermes; plugin không đặt header nào.
- Cache tại `$HERMES_HOME/cache/cliproxyapi/` (mode 0600) chứa model id, chủ sở hữu,
  thành viên nhà cung cấp và metadata. Không có key, không có tên tài khoản. Một
  management key bị từ chối được ghi nhớ dưới dạng HMAC có salt, không phải key.

## Phát triển

```bash
python -m unittest discover -s tests -v          # core tests, no Hermes needed
PYTHONPATH=/path/to/hermes-agent python -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate .
```

## Giấy phép

MIT

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
hình và các khả năng của mô hình mà CPA công bố (context window, max output, đầu
vào hình ảnh, các mức reasoning). Plugin không thêm bất cứ thứ gì CPA không phục vụ.

## Cài đặt

Từ [Hermes plugin catalog](https://hermes-agent.nousresearch.com/docs/plugins/cliproxyapi-provider)
(khuyến nghị):

```bash
hermes plugins install cliproxyapi-provider
```

Lệnh này cài đúng commit mà maintainer của catalog đã review.
`hermes plugins update cliproxyapi-provider` chỉ lên bản mới sau khi pin trong
catalog được nâng.

Bản phát triển (`main` mới nhất, chưa qua review của catalog):

```bash
hermes plugins install duu261/hermes-cliproxyapi-provider
```

Yêu cầu Hermes 0.21.7 trở lên. Plugin model-provider hoạt động ngay sau khi cài,
không cần bật. Khởi động lại các Hermes gateway đang chạy để chúng nạp plugin.

Cài cả hai plugin, provider và `/cpa`, trong một bước bằng pack đã ghim
(cần terminal tương tác; hỏi xác nhận một lần):

```bash
curl -fsSLO https://raw.githubusercontent.com/duu261/hermes-cliproxyapi-provider/main/hermes-pack.yaml
hermes plugins pack show ./hermes-pack.yaml
hermes plugins pack install ./hermes-pack.yaml
```

Pack ghim cả hai plugin vào commit phát hành và bật chúng; pack cũng thêm
provider vào `plugins.enabled`, điều này không thay đổi gì với model-provider.
Đã cài rồi (nâng cấp)? Thêm `--force`:
`hermes plugins pack install --force ./hermes-pack.yaml`.

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
      mgmt_key_env: CPA_MAIN_MGMT_KEY     # optional, see "Public mode or management mode"
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

Giữ `xai` trên `codex_responses`. Grok 4.7 trả về phần suy luận dưới dạng một
mục đã mã hóa, và client phải gửi lại mục này ở lượt tiếp theo
([tài liệu xAI](https://docs.x.ai/developers/model-capabilities/text/reasoning)).
Hermes làm việc đó trên wire Responses. Chat Completions không có trường nào để
chứa nó, và CPA (kiểm tra ở v8.0.44) chỉ phát lại suy luận xAI đã lưu cho client
Claude và Responses. Thiếu nó, Grok 4.7 ngừng suy luận sau vài vòng gọi công cụ,
thường nói sẽ gọi công cụ rồi kết thúc lượt mà không gọi.

## Chế độ công khai hay chế độ management

Chế độ công khai là mặc định và chỉ cần client key. Chế độ management thêm
`mgmt_key_env` và đọc bản đồ nhà cung cấp cùng định nghĩa mô hình của chính CPA.

| | Công khai (client key) | Management (`mgmt_key_env`) |
|---|---|---|
| Nhà cung cấp của mỗi mô hình | `owned_by` trên `/v1/models`: nhãn nhà sản xuất | nhà cung cấp của tài khoản đang phục vụ |
| Devin, `google` (nhiều executor Gemini) | nhóm theo nhãn nhà sản xuất | đúng nhà cung cấp |
| Id do hai nhà cung cấp phục vụ | một nhóm | mọi nhóm phục vụ nó |
| Context window, mức reasoning có tên | có | có |
| Đầu vào hình ảnh | chỉ khi `/v1beta/models` liệt kê loại đầu vào của mô hình; nếu không thì chưa biết | từ định nghĩa mô hình |
| Reasoning trên mô hình dùng ngân sách token (không có mức tên) | không nhận ra | có |
| Max output | có, trừ mô hình có giới hạn thật đúng bằng 64.000 | có, tất cả |
| Nhà cung cấp dạng plugin (Kiro và các plugin khác) | có nhóm; metadata chỉ từ danh sách công khai | định nghĩa mô hình từ plugin (CPA v8.0.34+) |
| Kênh API-key và tương thích OpenAI | có | giữ từ chế độ công khai (management API không thấy chúng) |
| Key Hermes giữ | client key | client key và một key CPA toàn quyền admin |

Chế độ công khai là đủ khi mỗi mô hình chỉ có một nhà cung cấp và nhãn nhà sản xuất
của CPA khớp với nhà cung cấp. Chế độ management là chế độ chính xác; cái giá của nó
là key. Một management key của CPA có thể đổi cấu hình và thêm hoặc xóa tài khoản.
Plugin chỉ gửi các thao tác đọc bên dưới, nhưng Hermes không gỡ `mgmt_key_env` khỏi
shell của agent (xem Bảo mật), nên bất kỳ công cụ agent nào đọc được biến môi trường
của bạn cũng đọc được key này.

Chế độ management gửi những gì:

- Chỉ GET, tới danh sách credential, danh sách mô hình của vài tài khoản lấy mẫu
  cho mỗi nhà cung cấp, và danh sách định nghĩa mô hình cho mỗi nhà cung cấp (nhà
  cung cấp tích hợp sẵn trước, rồi tới nhà cung cấp dạng plugin theo tên; CPA trước
  v8.0.34 không có định nghĩa cho nhà cung cấp dạng plugin và plugin sẽ bỏ qua). Không
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

## Ghi đè

```yaml
cliproxyapi:
  instances:
    main:
      base_url: http://127.0.0.1:8317
      key_env: CPA_MAIN_API_KEY
      groups:
        kimi:
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
          api_mode: codex_responses       # how this gateway channel expects requests
```

- Các profile trỏ tới `route_via` và dùng key của gateway. Key CPA chỉ dùng cho danh
  sách mô hình của CPA và không bao giờ gửi tới gateway; key gateway không bao giờ
  gửi tới CPA. Hai key phải là hai biến môi trường khác nhau.
- Chỉ giữ các mô hình mà gateway liệt kê cho key đó (`GET /v1/models`). Nếu lấy danh
  sách từ gateway thất bại, danh sách đã biết gần nhất được giữ lại. Trước khi đọc
  được danh sách đầu tiên cho một key, profile của key đó dùng danh sách của CPA, nên
  các provider định tuyến được đăng ký ngay khi khởi động từ cache CPA; gọi mô hình mà
  key không bán sẽ lỗi tại gateway cho tới lần làm mới kế tiếp thu hẹp danh sách. Danh
  sách đã biết, kể cả rỗng, luôn lọc chặt.
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

Context window, max output, đầu vào hình ảnh, hỗ trợ reasoning và các mức
reasoning-effort chỉ lấy từ CPA: định nghĩa mô hình qua management khi có, nếu không
thì từ danh sách mô hình công khai của CPA, khớp theo đúng id. Giá trị CPA không công
bố thì giữ là chưa biết. Không có management key, max output lấy từ danh sách mô hình
định dạng Anthropic của CPA, danh sách này có mọi mô hình. CPA ghi 64.000 ở đó cho mô
hình không có giới hạn, nên plugin coi đúng 64.000 là chưa biết và Hermes quay về
models.dev; mô hình có giới hạn thật là 64.000 cũng mất giá trị theo cách đó.

Lệnh `/model` của Hermes chỉ hiện max output, PDF và structured output từ models.dev;
plugin không đặt được các giá trị này, và định nghĩa mô hình của CPA không có trường
PDF hay structured output. Để thấy chúng, bổ sung metadata của nhà sản xuất từ
models.dev:

```yaml
providers:
  cpa-main-claude:
    catalog_provider: anthropic
```

Thứ tự ưu tiên, cao nhất trước: `model_overrides` của bạn, plugin này, models.dev.

## Bảo mật

- Key được tham chiếu theo tên biến môi trường và do Hermes phân giải.
- Hermes chỉ gỡ khỏi tiến trình con của agent những tên biến môi trường mà một
  provider đã đăng ký khai báo. Mỗi profile khai báo key inference của nó (`key_env`,
  hoặc key gateway trên instance có định tuyến), nên `mgmt_key_env`, và trên instance
  có định tuyến là `key_env` của CPA, vẫn đọc được từ shell của agent.
- Chỉ cho phép `http://` không mã hóa khi mọi địa chỉ mà host phân giải ra đều là loopback,
  RFC 1918, 100.64/10 hoặc IPv6 ULA. Mọi trường hợp khác cần `https://`.
- Không theo redirect, giới hạn kích thước response, và lỗi chỉ được báo bằng mã
  trạng thái HTTP và loại lỗi, không bao giờ kèm body, URL hay key.
- Request lấy catalog gửi `User-Agent: hermes-cliproxyapi-provider/<version>`. Một
  request `/v1/models` bổ sung nối thêm `grok-shell-compatible`, vì đó là client mà
  CPA công bố context window và mức reasoning cho, và một request khác gửi
  `Anthropic-Version: 2023-06-01`, định dạng có giới hạn đầu ra. Các id của chúng được
  khớp với danh sách thường, nên chúng chỉ thêm metadata, không bao giờ thêm mô hình.
  Request inference mang header riêng của Hermes; plugin không đặt header nào.
- Cache tại `$HERMES_HOME/cache/cliproxyapi/` (mode 0600) chứa model id, chủ sở hữu,
  thành viên nhà cung cấp và metadata. Không có key, không có tên tài khoản. Một
  management key bị từ chối được ghi nhớ dưới dạng HMAC có salt, không phải key.

## /cpa: trạng thái pool (plugin đi kèm)

Repo có thêm `cpa-status`, plugin thứ hai với một lệnh `/cpa`, cho xem tình
trạng tài khoản của mọi instance đã cấu hình. Plugin model-provider không đăng
ký được lệnh, nên plugin này cài và bật riêng:

```bash
hermes plugins install duu261/hermes-cliproxyapi-provider#cpa-status
hermes plugins enable cpa-status
```

Cần `mgmt_key_env` trên instance muốn xem; instance không có sẽ hiện
`no management key`. Khởi động lại gateway đang chạy sau khi bật.

```
/cpa                    mọi instance: số tài khoản theo nhà cung cấp
/cpa pre                một instance, kèm số request trong 1 giờ qua
/cpa pre codex          mỗi tài khoản một dòng
```

- Chỉ đọc: mỗi lần chạy gửi một `GET /v8/management/credentials` cho mỗi
  instance (v0 `auth-files` chỉ sau 404), và chỉ khi bạn gõ `/cpa`. Không hẹn
  giờ, không thử lại. Gõ lại trong 20 giây sẽ trả từ bộ nhớ.
- Quota gần nhất (5h và tuần) là số CPA ghi lại từ traffic thật của tài khoản
  Claude và Codex. CPA không ghi cho nhà cung cấp khác. `/cpa` không bao giờ
  bảo CPA kiểm tra quota với upstream.
- Email trong tên tài khoản và thông báo được che; `email`, `account`,
  `id_token`, `project_id` và `path` không bao giờ được đọc.
- Chạy khi chat đang rảnh. Gửi lúc agent đang chạy thì Hermes xử lý như mọi
  lệnh plugin khác khi bận (xếp hàng hoặc chuyển cho agent).
- Khóa IP: CPA chặn một IP 30 phút sau năm lần key management sai, kể cả
  localhost. Sau khi CPA từ chối một key, `/cpa` không gửi lại key đó: lưu
  trong `$HERMES_HOME/cache/cpa-status/rejected.json` (HMAC có salt, quyền
  0600) và tôn trọng cả lần từ chối mà plugin provider đã ghi cho mọi instance
  trên cùng CPA. Provider gửi key bị từ chối tối đa một lần cho mỗi instance,
  nên nhiều instance trên một CPA, hoặc nhiều profile Hermes trên một máy, vẫn
  có thể cộng lên năm. Sửa key bị từ chối trước khi chạy `/cpa` từ profile
  khác.

## Phát triển

```bash
python -m unittest discover -s tests -v          # core tests, no Hermes needed
PYTHONPATH=/path/to/hermes-agent python -m unittest discover -s tests -v
python -m unittest discover -s cpa-status/tests -v
cmp cpa.py cpa-status/cpa.py                     # the companion ships a copy
hermes plugins doctor . --ci
hermes plugins validate .
hermes plugins doctor ./cpa-status --ci
hermes plugins validate ./cpa-status
```

## Giấy phép

MIT

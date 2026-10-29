# hermes-cliproxyapi-provider

[English](README.md) | Tiếng Việt | [简体中文](README.zh-CN.md)

> Bản dịch này có thể chậm hơn bản tiếng Anh. Khi có khác biệt, [README.md](README.md) là bản chuẩn.

Plugin model-provider cho
[Hermes Agent](https://github.com/NousResearch/hermes-agent), dùng với
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (CPA).

CPA là một gateway: một instance đứng trước nhiều provider (Claude, Codex,
Gemini, xAI, Kimi, Devin, các upstream tương thích OpenAI, các plugin provider như Kiro) và tự quản lý account, OAuth, xoay vòng và chuyển đổi định
dạng của chúng. Nếu chỉ có Hermes, gateway như vậy là một endpoint tùy chỉnh
trên một wire, với metadata được pin bằng tay.

Plugin này phản chiếu CPA vào Hermes. Trỏ nó tới một instance CPA, nó sẽ đăng ký
một provider Hermes cho mỗi provider CPA, mỗi provider dùng wire mà executor
của CPA cho provider đó chấp nhận với ít chuyển đổi nhất, kèm danh sách model
và các khả năng của model mà CPA công bố (context window, max output, đầu
vào hình ảnh, các mức reasoning). Plugin không thêm bất cứ thứ gì CPA không phục vụ.

## Cài đặt

Từ [Hermes plugin catalog](https://hermes-agent.nousresearch.com/docs/plugins/cliproxyapi-provider)
(khuyến nghị):

```bash
hermes plugins install cliproxyapi-provider
```

Lệnh này cài đúng commit mà maintainer của catalog đã review.
`hermes plugins update cliproxyapi-provider` chỉ lên release mới sau khi pin trong
catalog được nâng.

Bản phát triển (`main` mới nhất, chưa qua review của catalog):

```bash
hermes plugins install duu261/hermes-cliproxyapi-provider
```

Yêu cầu Hermes 0.21.7 trở lên. Plugin model-provider hoạt động ngay sau khi cài,
không cần bật. Khởi động lại các Hermes gateway đang chạy để chúng nạp plugin.

Cài cả hai plugin, provider và `/cpa`, trong một bước bằng pack đã pin
(cần terminal tương tác; hỏi xác nhận một lần):

```bash
curl -fsSLO https://raw.githubusercontent.com/duu261/hermes-cliproxyapi-provider/main/hermes-pack.yaml
hermes plugins pack show ./hermes-pack.yaml
hermes plugins pack install ./hermes-pack.yaml
```

Pack pin cả hai plugin vào release commit và bật chúng; pack cũng thêm
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

Sau đó chạy `hermes model` (hoặc bất kỳ lệnh nào liệt kê model) một lần. Plugin
đọc danh sách model của instance, đăng ký các provider tìm thấy và lưu cache, nên
các lần chạy sau đăng ký chúng ngay lúc khởi động mà không cần gọi mạng:

- `cpa-main`: mọi model trên instance, trên chat wire.
- `cpa-main-<provider>`: một provider cho mỗi provider CPA, ví dụ
  `cpa-main-claude`, `cpa-main-codex`, `cpa-main-xai`, `cpa-main-kimi`.

`hermes doctor` và các thiết lập nhiều tiến trình (`plugins.isolation: host`) chỉ
thấy provider xuất hiện sau lúc khởi động khi đã khởi động lại.

## Wire theo provider

| Provider CPA | Hermes `api_mode` | Nguồn trong CPA |
|---|---|---|
| `claude` | `anthropic_messages` | Executor Claude gửi Anthropic Messages |
| `kimi` | `anthropic_messages` | Executor Kimi chuyển định dạng Claude tới `/v1/messages` của Kimi |
| `kiro` (plugin) | `anthropic_messages` | Plugin Kiro nhận đầu vào dạng Claude |
| `codex`, `xai`, `meta` (Muse) | `codex_responses` | Các executor gửi định dạng Responses của CPA |
| `gemini`, `vertex`, `aistudio`, `gemini-interactions`, `antigravity`, `devin` | `chat_completions` | Gemini wire và Interactions wire không khả dụng với Hermes cho host không phải của Google; CPA chuyển đổi |
| Tương thích OpenAI và mọi loại khác | `chat_completions` | Mọi executor của CPA đều chấp nhận |

Ghi đè theo từng provider bằng `api_mode` (xem bên dưới). Khi hai provider
phục vụ cùng một model id, id đó xuất hiện ở cả hai và CPA chọn account; khi đó
wire là lựa chọn phù hợp nhất.

Giữ `xai` trên `codex_responses`. Grok 4.7 trả về phần reasoning dưới dạng một
mục đã mã hóa, và client phải gửi lại mục này ở lượt tiếp theo
([tài liệu xAI](https://docs.x.ai/developers/model-capabilities/text/reasoning)).
Hermes làm việc đó trên Responses wire. Chat Completions không có trường nào để
chứa nó, và CPA (kiểm tra ở v8.0.44) chỉ phát lại cached reasoning xAI cho client
Claude và Responses. Thiếu nó, Grok 4.7 ngừng reasoning sau vài vòng gọi công cụ,
thường nói sẽ gọi công cụ rồi kết thúc lượt mà không gọi.

## Chế độ công khai hay chế độ management

Chế độ công khai là mặc định và chỉ cần client key. Chế độ management thêm
`mgmt_key_env` và đọc bản đồ provider cùng định nghĩa model của chính CPA.

| | Công khai (client key) | Management (`mgmt_key_env`) |
|---|---|---|
| Provider của mỗi model | `owned_by` trên `/v1/models`: nhãn vendor | provider của account đang phục vụ |
| Devin, `google` (nhiều executor Gemini) | nhóm theo nhãn vendor | đúng provider |
| Id do hai provider phục vụ | một nhóm | mọi nhóm phục vụ nó |
| Context window, mức reasoning có tên | có | có |
| Đầu vào hình ảnh | chỉ khi `/v1beta/models` liệt kê loại đầu vào của model; nếu không thì chưa biết | từ định nghĩa model |
| Reasoning trên model dùng ngân sách token (không có mức tên) | không nhận ra | có |
| Max output | có, trừ model có giới hạn thật đúng bằng 64.000 | có, tất cả |
| Plugin provider (Kiro và các plugin khác) | có nhóm; metadata chỉ từ danh sách công khai | định nghĩa model từ plugin (CPA v8.0.34+) |
| Kênh API-key và tương thích OpenAI | có | giữ từ chế độ công khai (management API không thấy chúng) |
| Key Hermes giữ | client key | client key và một key CPA toàn quyền admin |

Chế độ công khai là đủ khi mỗi model có một provider và nhãn vendor
của CPA khớp với provider. Chế độ management là chế độ chính xác; cái giá của nó
là key. Một management key của CPA có thể đổi cấu hình và thêm hoặc xóa account.
Plugin chỉ gửi các thao tác đọc bên dưới, nhưng Hermes không gỡ `mgmt_key_env` khỏi
shell của agent (xem Bảo mật), nên bất kỳ công cụ agent nào đọc được biến môi trường
của bạn cũng đọc được key này.

Chế độ management gửi những gì:

- Chỉ GET, tới danh sách credential, danh sách model của vài account lấy mẫu
  cho mỗi provider, và danh sách định nghĩa model cho mỗi provider (provider
  tích hợp sẵn trước, rồi tới plugin provider theo tên; CPA trước
  v8.0.34 không có định nghĩa cho plugin provider và plugin sẽ bỏ qua). Không
  bao giờ đụng tới quota, usage, tải xuống, OAuth hay bất kỳ thao tác ghi nào.
- Tối đa 32 request và 20 giây mỗi lượt, tối đa một lượt mỗi 15 phút cho mỗi
  instance, không bao giờ nằm trên request path.
- Plugin dừng ở lỗi 401 đầu tiên và không bao giờ retry key đó với CPA đó, từ bất
  kỳ instance nào, từ plugin nào trong hai plugin, và qua cả các lần khởi động lại,
  cho tới khi key thay đổi. Xem
  [Không bao giờ bị CPA khóa](#không-bao-giờ-bị-cpa-khóa). Mọi lỗi khác fallback về
  nhóm công khai.
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

Một nhóm có `models` sẽ thay thành viên của provider đó bằng các id khớp trong
live list (mẫu kiểu shell, phân biệt hoa thường).

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
  sách model của CPA và không bao giờ gửi tới gateway; key gateway không bao giờ
  gửi tới CPA. Hai key phải là hai biến môi trường khác nhau.
- Chỉ giữ các model mà gateway liệt kê cho key đó (`GET /v1/models`). Nếu lấy danh
  sách từ gateway thất bại, danh sách đã biết gần nhất được giữ lại. Trước khi đọc
  được danh sách đầu tiên cho một key, profile của key đó dùng danh sách của CPA, nên
  các provider định tuyến được đăng ký ngay khi khởi động từ cache CPA; gọi model mà
  key không bán sẽ lỗi tại gateway cho tới refresh kế tiếp thu hẹp danh sách. Danh
  sách đã biết, kể cả rỗng, luôn lọc chặt.
- Gateway thường chuyển đổi định dạng request theo từng kênh, trừ khi bật chế độ
  chuyển thẳng body. Profile giữ CPA wire theo bảng ở trên; đặt `api_mode` theo nhóm
  khi kênh gateway cần wire khác.
- Context window và mức reasoning mô tả model của CPA, không phải giới hạn mà
  gateway có thể thêm vào.
- Gateway có thể chuyển sang các kênh khác mà nó bán dưới cùng tên model. Plugin
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
reasoning-effort chỉ lấy từ CPA: định nghĩa model qua management khi có, nếu không
thì từ danh sách model công khai của CPA, khớp theo đúng id. Giá trị CPA không công
bố thì giữ là chưa biết. Không có management key, max output lấy từ danh sách model
định dạng Anthropic của CPA, danh sách này có mọi model. CPA ghi 64.000 ở đó cho model
không có giới hạn, nên plugin coi đúng 64.000 là chưa biết và Hermes fallback về
models.dev; model có giới hạn thật là 64.000 cũng mất giá trị theo cách đó.

Lệnh `/model` của Hermes chỉ hiện max output, PDF và structured output từ models.dev;
plugin không đặt được các giá trị này, và định nghĩa model của CPA không có trường
PDF hay structured output. Để thấy chúng, bổ sung metadata vendor từ
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
- Chỉ cho phép plain `http://` (HTTP không mã hóa) khi mọi địa chỉ mà host phân giải ra đều là loopback,
  RFC 1918, 100.64/10 hoặc IPv6 ULA. Mọi trường hợp khác cần `https://`.
- Không theo redirect, giới hạn kích thước response, và lỗi chỉ được báo bằng mã
  trạng thái HTTP và loại lỗi, không bao giờ kèm body, URL hay key.
- Request lấy catalog gửi `User-Agent: hermes-cliproxyapi-provider/<version>`. Một
  request `/v1/models` bổ sung nối thêm `grok-shell-compatible`, vì đó là client mà
  CPA công bố context window và mức reasoning cho, và một request khác gửi
  `Anthropic-Version: 2023-06-01`, định dạng có giới hạn đầu ra. Các id của chúng được
  khớp với danh sách thường, nên chúng thêm metadata, không bao giờ thêm model.
  Request inference mang header riêng của Hermes; plugin không đặt header nào.
- Cache tại `$HERMES_HOME/cache/cliproxyapi/` (mode 0600) chứa model id, chủ sở hữu,
  thành viên provider và metadata. Không có key, không có tên account. Thư mục
  con `locks/` chứa một file lock rỗng cho mỗi CPA và `rejected.json`, danh sách
  management key bị từ chối dưới dạng HMAC có salt, không phải key.

## Không bao giờ bị CPA khóa

CPA đếm số lần đăng nhập management thất bại theo IP client, trong bộ nhớ, riêng
cho mỗi tiến trình CPA. Mỗi lần thiếu hoặc sai management key cộng một; lần thứ năm
liên tiếp sẽ chặn IP đó 30 phút, kể cả localhost, nên web UI của CPA và mọi công cụ
khác trên cùng IP cũng bị chặn theo. Một lần đăng nhập management thành công đưa bộ
đếm về 0, khởi động lại CPA xóa hết, và một IP không có lần thất bại nào trong hai
giờ sẽ bị quên. Các lần thất bại không tự hết hạn sau 30 phút. Các phản hồi
`remote management disabled` và `key not set` (403) không được tính.

Hai plugin làm gì, theo mỗi `HERMES_HOME` trên Linux và macOS:

- Mỗi lúc chỉ một lượt kiểm tra management cho mỗi CPA, giữa provider, `/cpa` và
  mọi tiến trình Hermes (CLI và gateway). Provider bỏ qua lượt của nó khi `/cpa`
  busy và thử lại ở lần refresh sau; `/cpa` chờ tối đa 25 giây rồi báo `busy`.
- Một key đã sai sẵn tốn một lần thử. Lần từ chối được ghi nhớ (HMAC có salt)
  qua các lần khởi động lại và cho cả hai plugin, và key đó không được gửi lại tới
  CPA đó cho tới khi key thay đổi.
- Một key bị thu hồi giữa chừng khi `/cpa quota` đang chạy có thể tốn tối đa 4
  lần: các request in flight (đã gửi đi). `/cpa quota` gửi một request đầu tiên một mình trước,
  nên một key đã sai sẵn không bao giờ tới được các request song song.
- Trên Windows chỉ có in-process lock; hai tiến trình Hermes ở đó chỉ phối
  hợp qua danh sách key bị từ chối đã ghi nhớ.

Bạn làm gì:

1. Chỉ đặt key qua `mgmt_key_env`, và đăng nhập web UI của CPA một lần bằng key đó
   trước khi khởi động Hermes. Một lần đăng nhập đúng cũng đưa bộ đếm về 0.
2. Rotate key (đổi key) trong một bước: đổi key của CPA, cập nhật biến môi trường, khởi động lại
   gateway. Key cũ vẫn được ghi nhớ là bị từ chối, nên Hermes im lặng thay vì retry
   nó.
3. Chạy Hermes trên cùng host với CPA, hoặc đặt `server.trusted-proxies` của CPA
   (cấu hình cũ: `trusted-proxies` ở cấp cao nhất) thành địa chỉ reverse proxy của
   bạn. Nếu không, mọi client dùng chung IP của proxy và một key sai sẽ chặn tất cả.
4. Công cụ khác trên cùng IP cũng được tính (CPA Usage Keeper, script, trình duyệt
   mở web UI). Sửa key của chúng trước.
5. Mỗi profile Hermes ghi nhớ lần từ chối riêng. Nếu từ năm profile trở lên trên một
   máy cùng giữ một key sai, hãy sửa key trước khi khởi động chúng.
6. Dùng một cách viết cho mỗi CPA trong `base_url` (`127.0.0.1` hoặc `localhost`,
   không dùng cả hai): hai plugin phối hợp theo URL.
7. Nâng cấp cả hai plugin cùng lúc; bản 0.4 trở về trước không dùng chung lock.
8. Nếu đã bị chặn: chờ 30 phút hoặc khởi động lại CPA, và sửa key trước khi Hermes
   thử lại (`/cpa` hiện `key rejected`).

## /cpa: trạng thái pool (plugin đi kèm)

Repo có thêm `cpa-status`, plugin thứ hai với một lệnh `/cpa`, cho xem tình
trạng account của mọi instance đã cấu hình. Plugin model-provider không đăng
ký được lệnh, nên plugin này cài và bật riêng:

```bash
hermes plugins install duu261/hermes-cliproxyapi-provider#cpa-status
hermes plugins enable cpa-status
```

Cần `mgmt_key_env` trên instance muốn xem; instance không có sẽ hiện
`no management key`. Khởi động lại gateway đang chạy sau khi bật.

```
/cpa                    mọi instance: số account theo provider
/cpa pre                một instance, kèm số request trong 1 giờ qua
/cpa pre codex          mỗi account một dòng
/cpa quota pre          live usage của mọi account có quota
/cpa quota pre codex    Codex only
```

```
pre
  codex 4: 3 ready · 1 cooling (next 17:20)
  xai 2: 2 ready
pre / codex · 4 accounts
codex-al…@…  ready · 1h 173/71 · 5h 22% · wk 0% (1m ago)
codex-bo…@…  cooling until 17:20 (quota)
```

- Chỉ đọc: mỗi lần chạy gửi một `GET /v8/management/credentials` cho mỗi
  instance (v0 `auth-files` chỉ sau 404), và chỉ khi bạn gõ `/cpa`. Không hẹn
  giờ, không retry. Gõ lại trong 20 giây sẽ trả từ bộ nhớ.
- Quota gần nhất (5h và tuần) là số CPA ghi lại từ traffic thật của account
  Claude và Codex. CPA không ghi cho provider khác. `/cpa` (không có `quota`)
  không bao giờ bảo CPA kiểm tra quota với upstream; `/cpa quota` thì có.
- Email trong tên account và thông báo được che; `email`, `account`,
  `project_id` và `path` không bao giờ được hiển thị (`/cpa quota` chỉ dùng
  `project_id` của Antigravity để dựng request).
- Chạy khi chat đang rảnh. Gửi lúc agent đang chạy thì Hermes xử lý như mọi
  lệnh plugin khác, theo busy handling của Hermes (xếp hàng hoặc chuyển cho agent).
- Lockout: xem [Không bao giờ bị CPA khóa](#không-bao-giờ-bị-cpa-khóa).

### /cpa quota: live usage

```
pre quota · live 21:40
codex · 4 accounts: 3 have room · 1 out (next free 22:10)
codex-xy…@…  plus  5h 100% → 22:10 · wk 60% → Nov 02  OUT
codex-bd…@…  pro  wk 31% → Oct 30
codex-ab…@…  plus  5h 23% → 22:40 · wk 69% → Oct 29
codex-cu…@…  pro  wk 40% → Nov 01
```

`/cpa quota <pool>` hỏi từng vendor xem mỗi account đã dùng bao nhiêu, giống
trang quota trên web UI của chính CPA. Với các vendor có sẵn, route `api-call`
của CPA gửi đúng request của web UI kèm token của account đó (CPA tự điền;
Hermes không bao giờ thấy token). Account của provider khác được đọc từ CPA
quota plugin của nó khi CPA có liệt kê. Mọi request, và khi nào chúng được gửi:

| Provider | Request cho mỗi account | Bạn thấy gì |
|---|---|---|
| Claude | GET `https://api.anthropic.com/api/oauth/usage` | `5h`, `wk`, window tuần `opus` / `sonnet` |
| Codex | GET `https://chatgpt.com/backend-api/wham/usage` | window theo độ dài của chính nó (`5h`, `wk`, `mo`), plan |
| xAI | GET `https://cli-chat-proxy.grok.com/v1/billing?format=credits`, rồi GET `https://cli-chat-proxy.grok.com/v1/billing` | credits tuần `wk`; chi tiêu tháng `mo` và plan (`supergrok` / `supergrok_heavy`) chỉ khi xAI báo monthly limit |
| Antigravity | POST `https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary` kèm `project_id` của account; host `daily-cloudcode-pa.sandbox` và `cloudcode-pa` chỉ khi host trước lỗi ở phía vendor hoặc không trả quota | theo từng nhóm model, `5h` và `wk` |
| Devin | POST `https://server.codeium.com/exa.seat_management_pb.SeatManagementService/GetUserStatus` | `day`, `wk`, plan |
| Kimi | GET `https://api.kimi.com/coding/v1/usages` (`api.kimi.ai` khi CPA liệt kê account là `kimi-ai`) | từng window giới hạn, tuần, `mo` |
| CPA plugin quota (vd. Kiro) | GET `/v8/management/plugins/<id>/quota` trên CPA; plugin tự hỏi vendor của nó | các meter và plan của plugin |

Không request nào tốn tiền hay dùng hết quota: đều là các lần đọc usage mà web
UI cũng làm. Cố ý không làm: bước kiểm tra chat trả phí 1 token của xAI
(fallback của web UI cho account xAI trả phí dùng API key, các account này sẽ
hiện lỗi billing), tra cứu danh tính và plan, tải file account, và mọi thao
tác reset.

Các header, kể cả `User-Agent` CLI của từng vendor, lấy từ web UI. Phần trăm là
phần đã dùng (vendor nào báo phần còn lại thì được đổi lại); không cộng gộp
giữa các plan. Một account là `OUT` khi có window ở 100% hoặc vendor báo nó
đang bị chặn.

- Chỉ khi bạn gõ lệnh. Mỗi lần chạy tối đa 20 account và 3 request cho mỗi
  account. Request đầu gửi một mình, sau đó tối đa 4 request cùng lúc; gõ lại
  trong 60 giây sẽ trả từ bộ nhớ. Bỏ qua account đã tắt.
- `/cpa quota <pool> <provider>` nhận `claude`, `codex`, `xai`, `antigravity`,
  `devin`, `kimi`, `plugin`, hoặc provider của một plugin như `kiro`.
- Token account hết hạn hiện `token rejected by vendor` và không bao giờ bị tính
  vào lockout của CPA. Management key bị từ chối sẽ dừng lượt chạy.
- Account index của CPA, account id của Codex, project id của Antigravity và
  plugin id của CPA chỉ dùng để dựng request, không bao giờ được hiển thị, ghi
  log hay lưu cache. Phản hồi của vendor được đọc theo từng trường; body và
  header không bao giờ được hiển thị.
- Không hỗ trợ: Meta (web UI đọc file credential gốc của nó), account Kimi
  `.ai` mà CPA chỉ liệt kê là `kimi` (bị gửi tới URL `.com` và hiện
  `token rejected by vendor`), giới hạn code-review và extra limit theo model
  của Codex, các số liệu summary của plugin.

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

Đóng góp: xem [CONTRIBUTING.md](CONTRIBUTING.md) để biết các safety rule mà mọi
thay đổi đều giữ, và [SECURITY.md](SECURITY.md) để báo cáo vấn đề bảo mật một cách
riêng tư.

## Giấy phép

MIT

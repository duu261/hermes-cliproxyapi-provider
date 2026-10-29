# hermes-cliproxyapi-provider

[English](README.md) | [Tiếng Việt](README.vi.md) | 简体中文

> 本译文可能落后于英文版。如有出入，以 [README.md](README.md) 为准。

一个面向 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的 model-provider plugin，用于 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)（CPA）。

CPA 是一个 gateway：一个 instance 可以代理多个 provider（Claude、Codex、Gemini、xAI、Kimi、
Devin、兼容 OpenAI 的上游，以及 Kiro 等 plugin provider），并自行管理它们的 account、OAuth、
轮换和格式转换。单靠 Hermes，这样的 gateway 只是一个自定义 endpoint，走一种 wire，元数据
需要手动 pin。

本 plugin 则把 CPA 映射到 Hermes 中。将它指向一个 CPA instance，它会为每个 CPA provider 注册
一个 Hermes provider，每个 provider 走该 provider 的 CPA executor 能以最少转换接受的那种 wire，
并带上 CPA 发布的 model 列表和 model 能力（context window、max output、图像输入、推理级别）。CPA 不提供的东西，本 plugin 一概不加。

## 安装

从 [Hermes plugin catalog](https://hermes-agent.nousresearch.com/docs/plugins/cliproxyapi-provider)安装（推荐）：

```bash
hermes plugins install cliproxyapi-provider
```

这会安装 catalog 维护者审核过的提交。只有在 catalog 中的 pin 更新后，
`hermes plugins update cliproxyapi-provider` 才会升级到新的 release。

开发版（最新 `main`，未经 catalog 审核）：

```bash
hermes plugins install duu261/hermes-cliproxyapi-provider
```

需要 Hermes 0.21.7 或更新版本。model-provider plugin 安装后即生效，无需启用。请重启
正在运行的 Hermes gateway，使其加载 plugin。

用 pinned pack 一步安装两个 plugin（provider 和 `/cpa`）（需要交互式终端；会询问
确认一次）：

```bash
curl -fsSLO https://raw.githubusercontent.com/duu261/hermes-cliproxyapi-provider/main/hermes-pack.yaml
hermes plugins pack show ./hermes-pack.yaml
hermes plugins pack install ./hermes-pack.yaml
```

pack 把两个 plugin 都 pin 到 release commit 并启用它们；pack 也会把 provider 加入
`plugins.enabled`，这对 model-provider 没有任何影响。已经安装过（升级）？加上
`--force`：`hermes plugins pack install --force ./hermes-pack.yaml`。

## 配置

零配置：在 `~/.hermes/.env` 中设置 `CPA_LOCAL_API_KEY`（一个 CPA client key），
即可获得指向 `http://127.0.0.1:8317` 上 CPA 的 `cpa-local`；可用
`CPA_LOCAL_BASE_URL` 覆盖 URL。

对于具名 instance 或远程 instance，在 `~/.hermes/config.yaml` 中添加：

```yaml
cliproxyapi:
  instances:
    main:
      base_url: https://cpa.example.com   # with or without /v1
      key_env: CPA_MAIN_API_KEY           # env var holding a CPA client key
      mgmt_key_env: CPA_MAIN_MGMT_KEY     # optional, see "Public mode or management mode"
```

然后运行一次 `hermes model`（或任何会列出 model 的命令）。plugin 会读取该 instance 的 model 列表，
注册找到的 provider 并写入 cache，之后每次启动时直接注册，无需网络请求：

- `cpa-main`：instance 上的全部 model，走 chat wire。
- `cpa-main-<provider>`：每个 CPA provider 一个，例如 `cpa-main-claude`、
  `cpa-main-codex`、`cpa-main-xai`、`cpa-main-kimi`。

`hermes doctor` 和多进程部署（`plugins.isolation: host`）只有在重启后才能看到启动后
新出现的 provider。

## 各 provider 的 wire

| CPA provider | Hermes `api_mode` | CPA 依据 |
|---|---|---|
| `claude` | `anthropic_messages` | Claude executor 发送 Anthropic Messages |
| `kimi` | `anthropic_messages` | Kimi executor 将 Claude 格式透传到 Kimi 的 `/v1/messages` |
| `kiro`（plugin） | `anthropic_messages` | Kiro plugin 接收 Claude 格式输入 |
| `codex`、`xai`、`meta`（Muse） | `codex_responses` | 这些 executor 发送 CPA 的 Responses 格式 |
| `gemini`、`vertex`、`aistudio`、`gemini-interactions`、`antigravity`、`devin` | `chat_completions` | 对非 Google 主机，Hermes 无法使用 Gemini 和 Interactions wire；由 CPA 转换 |
| 兼容 OpenAI 及其他所有 | `chat_completions` | 所有 CPA executor 都接受 |

可通过 `api_mode` 按 provider 覆盖（见下文）。当两个 provider 提供同一个 model id 时，它会
同时出现在两者中，由 CPA 选择 account；此时的 wire 只是最佳匹配。

请让 `xai` 保持 `codex_responses`。Grok 4.7 以加密条目的形式返回推理内容，客户端
必须在下一轮把它原样发回
（[xAI 文档](https://docs.x.ai/developers/model-capabilities/text/reasoning)）。
Hermes 在 Responses wire 上会这样做。Chat Completions 没有承载它的字段，而 CPA
（在 v8.0.44 核对）只为 Claude 和 Responses 客户端回放已 cache 的 xAI 推理。缺少这一步时，
Grok 4.7 在几轮工具调用后就不再推理，常常说要调用工具，随后却直接结束本轮。

## 公开模式与 management 模式

公开模式是默认模式，只需要 client key。management 模式额外设置 `mgmt_key_env`，读取
CPA 自己的 provider 映射和 model 定义。

| | 公开（client key） | Management（`mgmt_key_env`） |
|---|---|---|
| 每个 model 的 provider | `/v1/models` 上的 `owned_by`：厂商标签 | 实际提供该 model 的 account provider |
| Devin、`google`（多个 Gemini executor） | 按厂商标签分组 | 精确 provider |
| 由两个 provider 提供的 id | 一个分组 | 所有提供它的分组 |
| context window、命名推理级别 | 有 | 有 |
| 图像输入 | 仅当 `/v1beta/models` 列出该 model 的输入类型时；否则未知 | 来自 model 定义 |
| 按 token 预算推理的 model（无命名级别） | 识别不到 | 有 |
| max output | 有，真实上限恰为 64,000 的 model 除外 | 有，全部 |
| plugin provider（Kiro 等） | 可分组；元数据仅来自公开列表 | 来自 plugin 的 model 定义（CPA v8.0.34+） |
| API-key 和兼容 OpenAI 的渠道 | 有 | 沿用公开模式（management API 看不到它们） |
| Hermes 持有的 key | client key | client key 加一个拥有完整管理权限的 CPA key |

当每个 model 有一个 provider、且 CPA 的厂商标签与 provider 一致时，公开模式就够用。
management 模式是精确模式，代价是 key。CPA management key 可以修改配置、添加或删除
account。plugin 只发送下列读取，但 Hermes 不会从 agent shell 中剥离 `mgmt_key_env`
（见“安全”），所以任何能读取你环境变量的 agent 工具也都能读到它。

management 模式发送：

- 仅 GET，访问 credential 列表、每个 provider 若干抽样 account 的 model 列表，以及每个 provider 的 model 定义
  列表（先内置 provider，再按名称查询 plugin provider；v8.0.34 之前的 CPA 没有 plugin provider 的
  定义，plugin 会跳过）。绝不访问 quota、usage、下载、OAuth，也不做任何写操作。
- 每轮最多 32 个 request、20 秒；每个 instance 每 15 分钟最多一轮；绝不在 request 路径上执行。
- plugin 在第一次 401 时即停止，并且在 key 变更之前，无论从哪个 instance、哪一个 plugin、跨多少次
  重启，都不会再对该 CPA retry 该 key。见 [避免被 CPA 封禁](#避免被-cpa-封禁)。其他任何
  失败都会 fallback 到公开分组。
- 除非两者运行在同一主机上，否则 CPA 必须允许来自 plugin 地址的 management 访问（CPA
  配置中的 `remote-management`）。

## 覆盖配置

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

带有 `models` 的分组会用实时列表中匹配的 id 替换该 provider 的成员（shell 风格通配符，
区分大小写）。

## 计费路由（CPA 前面的 gateway）

如果你在同一个 CPA 前面运行 New API 等计费 gateway，plugin 可以通过该 gateway 发送推理，
同时仍从 CPA 获取所有信息：

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

- profile 指向 `route_via` 并使用 gateway key。CPA key 只用于获取 CPA 的 model 列表，绝不
  发送给 gateway；gateway key 也绝不发送给 CPA。两者必须是不同的环境变量。
- 只保留 gateway 为该 key 列出的 model（`GET /v1/models`）。如果获取 gateway 列表失败，则保留
  最近一次已知的列表。在首次读到某个 key 的 gateway 列表之前，该 key 的 profile 使用 CPA 的
  列表，因此路由 provider 会在启动时从 CPA cache 注册；调用该 key 未售卖的 model 会在 gateway 处
  失败，直到下一次 refresh 收窄列表。已知的列表（即使为空）始终严格过滤。
- 除非开启 body 透传，gateway 通常会按渠道转换 request 格式。profile 保持上表中的 CPA wire；
  当 gateway 渠道需要不同 wire 时，按分组设置 `api_mode`。
- context window 和推理级别描述的是 CPA 的 model，不包括 gateway 可能额外施加的限制。
- gateway 可能故障转移到它以同一 model 名出售的其他渠道。plugin 无法察觉也无法阻止。
- plugin 只使用 gateway 的 client endpoint，绝不使用其管理、渠道、定价或计费 API。
- 已知限制：Hermes 只会从 agent 子进程中剥离已注册 provider 所声明的 key 名。路由
  provider 声明的是 gateway key，因此在启用路由的 instance 上，CPA 的 `key_env`（discovery 需要）
  对 agent shell 仍然可见。`mgmt_key_env` 从不在 provider 上声明，所以无论是否启用
  路由都不会被剥离。Hermes 目前还没有 plugin hook 可以声明一个 secret 而不把它变成 inference credential。

## 元数据

context window、max output、图像输入、推理支持和 reasoning-effort 级别只来自 CPA：有 management
model 定义时使用它，否则使用 CPA 公开 model 列表并按精确 id 匹配。CPA 未公布的值保持
未知。没有 management key 时，max output 取自 CPA 的 Anthropic 格式 model 列表，它覆盖所有
model。CPA 会为没有上限的 model 在其中写入 64,000，因此 plugin 把恰好 64,000 视为未知，由
Hermes fallback 到 models.dev；真实上限恰为 64,000 的 model 也会因此丢失该值。

Hermes 的 `/model` 只从 models.dev 显示 max output、PDF 和结构化输出；plugin 无法设置
它们，CPA 的 model 定义也没有 PDF 或结构化输出字段。要显示它们，可从 models.dev 补充
厂商元数据：

```yaml
providers:
  cpa-main-claude:
    catalog_provider: anthropic
```

优先级从高到低：你的 `model_overrides`、本 plugin、models.dev。

## 安全

- key 通过环境变量名引用，由 Hermes 解析。
- Hermes 只会从 agent 子进程中剥离已注册 provider 所声明的环境变量名。每个 profile
  声明其 inference key（`key_env`，或启用路由的 instance 上的 gateway key），因此 `mgmt_key_env`，
  以及启用路由的 instance 上 CPA 的 `key_env`，对 agent shell 仍然可读。
- 只有当主机解析出的每个地址都属于 loopback、RFC 1918、100.64/10 或 IPv6 ULA 时，
  才允许使用 plain `http://`。其他情况必须使用 `https://`。
- 不跟随重定向，限制 response 大小，失败只报告 HTTP 状态码和错误类别，绝不包含 body、
  URL 或 key。
- catalog request 发送 `User-Agent: hermes-cliproxyapi-provider/<version>`。另有一个
  `/v1/models` request 会追加 `grok-shell-compatible`，因为 CPA 向该客户端发布 context window
  和推理级别；还有一个 request 发送 `Anthropic-Version: 2023-06-01`，这种格式带有
  输出上限。它们的 id 会与常规列表匹配，所以增加元数据，绝不增加 model。inference request
  携带 Hermes 自己的 header；plugin 不设置任何 header。
- 位于 `$HERMES_HOME/cache/cliproxyapi/` 的 cache（mode 0600）保存 model id、所有者、
  provider 成员关系和元数据。不含 key，也不含 account 名。其中的 `locks/` 子目录为每个 CPA
  保存一个空的 lock 文件，以及 `rejected.json`：被拒绝的 management key 以加盐 HMAC
  形式记录，而不是 key 本身。

## 避免被 CPA 封禁

CPA 按客户端 IP 统计 management 登录失败次数，保存在内存中，每个 CPA 进程各自计数。
每次缺少或错误的 management key 计一次；连续第五次会封禁该 IP 30 分钟，localhost
也不例外，CPA 的 web UI 以及同一 IP 上的其他工具也会一起被封。一次成功的
management 登录会把计数清零，重启 CPA 会清空所有计数，两小时内没有失败的 IP 会被
遗忘。失败次数不会在 30 分钟后自动过期。`remote management disabled` 和
`key not set`（403）这两种响应不计数。

两个 plugin 的做法（每个 `HERMES_HOME`，Linux 和 macOS）：

- 每个 CPA 同一时间只进行一次 management 检查，provider、`/cpa` 以及所有 Hermes
  进程（CLI 和 gateway）之间共享。`/cpa` busy 时 provider 跳过本次检查，下次 refresh 再试；
  `/cpa` 最多等待 25 秒，然后显示 `busy`。
- 已经失效的 key 消耗一次尝试。拒绝记录（加盐 HMAC）跨重启保留，两个 plugin 共享，
  在 key 变更之前不会再把它发给该 CPA。
- 在 `/cpa quota` 运行中途被吊销的 key 最多消耗 4 次：即 in flight 的请求（已经发出）。
  `/cpa quota` 会先单独发送第一个请求，因此已经失效的 key 永远到不了并发请求。
- 在 Windows 上只有进程内 lock；那里的两个 Hermes 进程只靠记住的拒绝记录协调。

你需要做的：

1. 只通过 `mgmt_key_env` 设置 key，并在启动 Hermes 之前用它登录一次 CPA 的
   web UI。一次正确的登录也会把计数清零。
2. 一步完成轮换：修改 CPA 的 key，更新环境变量，重启 gateway。旧 key 仍被记为已拒绝，
   所以 Hermes 会保持安静，而不是 retry 它。
3. 让 Hermes 与 CPA 运行在同一主机上，或者把 CPA 的 `server.trusted-proxies`
   （旧配置：顶层 `trusted-proxies`）设为你的反向代理地址。否则所有客户端共用代理
   的 IP，一个错误 key 就会封禁所有人。
4. 同一 IP 上的其他工具也会计数（CPA Usage Keeper、脚本、打开 web UI 的浏览器）。
   先修好它们的 key。
5. 每个 Hermes profile 各自记录拒绝。如果同一台机器上有五个或更多 profile 持有同一个
   错误 key，请先修好再启动它们。
6. 每个 CPA 在 `base_url` 中只用一种写法（`127.0.0.1` 或 `localhost`，不要两者都用）：
   两个 plugin 按 URL 协调。
7. 两个 plugin 一起升级；0.4 及更早版本不共享 lock。
8. 如果已被封禁：等待 30 分钟或重启 CPA，并在 Hermes 再次尝试之前修好 key
   （`/cpa` 会显示 `key rejected`）。

## /cpa：pool 状态（配套 plugin）

仓库里还有第二个 plugin `cpa-status`，提供一个命令 `/cpa`，显示所有已配置 instance 的
account 健康状况。model-provider plugin 不能注册命令，所以它需要单独安装并启用：

```bash
hermes plugins install duu261/hermes-cliproxyapi-provider#cpa-status
hermes plugins enable cpa-status
```

要查看的 instance 需要设置 `mgmt_key_env`；没有设置的 instance 显示 `no management key`。
启用后请重启正在运行的 gateway。

```
/cpa                    所有 instance：按 provider 统计 account
/cpa pre                单个 instance，附带最近一小时的 request 数
/cpa pre codex          每个 account 一行
/cpa quota pre          所有有 quota 的 account 的 live usage（实时用量）
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

- 只读：每次运行对每个 instance 发送一次 `GET /v8/management/credentials`（只有在
  404 之后才用 v0 `auth-files`），并且只在你输入 `/cpa` 时发送。没有定时任务，
  不 retry。20 秒内重复输入直接用内存结果。
- 最近一次的 quota（`5h` 和 `wk`）来自 CPA 在真实流量中为 Claude 和 Codex account
  记录的数据，其他 provider CPA 不记录。普通的 `/cpa` 从不让 CPA 去上游查询 quota；
  `/cpa quota` 会。
- account 名和消息里的邮箱会被遮盖；从不显示 `email`、`account`、`project_id` 和
  `path`（`/cpa quota` 只用 Antigravity 的 `project_id` 来构造它的 request）。
- 在对话空闲时可用。agent 正在运行时发送，会像其他 plugin 命令一样交给 Hermes 的
  busy 处理（排队或转给 agent）。
- IP 封禁：见 [避免被 CPA 封禁](#避免被-cpa-封禁)。

### /cpa quota：live usage

```
pre quota · live 21:40
codex · 4 accounts: 3 have room · 1 out (next free 22:10)
codex-xy…@…  plus  5h 100% → 22:10 · wk 60% → Nov 02  OUT
codex-bd…@…  pro  wk 31% → Oct 30
codex-ab…@…  plus  5h 23% → 22:40 · wk 69% → Oct 29
codex-cu…@…  pro  wk 40% → Nov 01
```

`/cpa quota <pool>` 向各厂商查询每个 account 已用了多少，做法与 CPA 自带 web UI 的
quota 页面相同。对内置的厂商，CPA 的 `api-call` 路由带着该 account 的 token（由 CPA
填入；Hermes 永远看不到）发送 web UI 的同一个请求。其他 provider 的 account，在 CPA
列出其 quota plugin 时，从该 plugin 读取。所有请求及其发送时机：

| Provider | 每个 account 的请求 | 显示内容 |
|---|---|---|
| Claude | GET `https://api.anthropic.com/api/oauth/usage` | `5h`, `wk`, 每周 `opus` / `sonnet` |
| Codex | GET `https://chatgpt.com/backend-api/wham/usage` | window 按自身长度标注 (`5h`, `wk`, `mo`), plan |
| xAI | GET `https://cli-chat-proxy.grok.com/v1/billing?format=credits`, 然后 GET `https://cli-chat-proxy.grok.com/v1/billing` | 每周 credits `wk`；只有 xAI 报告 monthly limit 时才显示每月花费 `mo` 和 plan（`supergrok` / `supergrok_heavy`） |
| Antigravity | POST `https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary` ，带该 account 的 `project_id`；只有前一个 host 在厂商侧失败或没有返回 quota 时，才依次请求 `daily-cloudcode-pa.sandbox` 和 `cloudcode-pa` | 按 model 分组，`5h` 和 `wk` |
| Devin | POST `https://server.codeium.com/exa.seat_management_pb.SeatManagementService/GetUserStatus` | `day`, `wk`, plan |
| Kimi | GET `https://api.kimi.com/coding/v1/usages` （CPA 把 account 列为 `kimi-ai` 时用 `api.kimi.ai`） | 每个限额 window、每周、`mo` |
| CPA plugin quota（如 Kiro） | GET `/v8/management/plugins/<id>/quota` （在 CPA 上；由 plugin 去问它的厂商） | plugin 的 meter 和 plan |

这些请求都不花钱，也不消耗 quota：都是 web UI 本身也会做的用量读取。刻意不做：xAI
付费的 1 token chat 检查（web UI 给使用 API key 的付费 xAI account 的 fallback，这类
account 会显示 billing 错误）、身份和 plan 查询、下载 account 文件，以及任何 reset。

header（包括各厂商 CLI 的 `User-Agent`）沿用 web UI。百分比是已用量（报告剩余量的
厂商会被换算）；不跨 plan 汇总。某个 window 达到 100% 或厂商标记该 account 为 blocked
时，该 account 显示 `OUT`。

- 只在你输入时运行。每次最多 20 个 account，每个 account 最多 3 个请求。第一个请求
  单独发送，之后最多 4 个并发；60 秒内重复输入直接用内存结果。跳过已禁用的 account。
- `/cpa quota <pool> <provider>` 接受 `claude`、`codex`、`xai`、`antigravity`、
  `devin`、`kimi`、`plugin`，或某个 plugin 的 provider（如 `kiro`）。
- account token 过期时显示 `token rejected by vendor`，永远不计入 CPA 的封禁次数。
  management key 被拒绝时整个运行停止。
- CPA 的 account 索引、Codex 的 account id、Antigravity 的 project id 和 CPA 的
  plugin id 只用于构造请求，从不显示、记录日志或 cache。厂商回复按字段读取；从不显示
  body 和 header。
- 不涵盖：Meta（web UI 要读它的原始 credential 文件）、CPA 只列为 `kimi` 的 Kimi
  `.ai` account（会发到 `.com` URL 并显示 `token rejected by vendor`）、Codex 的
  code-review 和按 model 的额外限额、plugin 的 summary 数字。

## 开发

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

贡献：请阅读 [CONTRIBUTING.md](CONTRIBUTING.md) 了解每个改动都必须遵守的 safety rules，
并通过 [SECURITY.md](SECURITY.md) 私下报告安全问题。

## 许可证

MIT

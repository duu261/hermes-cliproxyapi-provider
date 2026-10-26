# hermes-cliproxyapi-provider

[English](README.md) | [Tiếng Việt](README.vi.md) | 简体中文

> 本译文可能落后于英文版。如有出入，以 [README.md](README.md) 为准。

一个面向 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的模型提供方
（model-provider）插件，用于 [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)（CPA）。

CPA 是一个网关：一个实例可以代理多个提供方（Claude、Codex、Gemini、xAI、Kimi、
Devin、兼容 OpenAI 的上游，以及 Kiro 等插件提供方），并自行管理它们的账号、OAuth、
轮换和格式转换。单靠 Hermes，这样的网关只是一个自定义端点，只走一种 wire，元数据
需要手动固定。

本插件则把 CPA 映射到 Hermes 中。将它指向一个 CPA 实例，它会为每个 CPA 提供方注册
一个 Hermes provider，每个 provider 走该提供方的 CPA executor 能以最少转换接受的那种 wire，
并带上 CPA 发布的模型列表和模型能力（上下文窗口、最大输出、图像输入、推理级别）。CPA 不提供的东西，本插件一概不加。

## 安装

从 [Hermes 插件目录](https://hermes-agent.nousresearch.com/docs/plugins/cliproxyapi-provider)安装（推荐）：

```bash
hermes plugins install cliproxyapi-provider
```

这会安装目录维护者审核过的提交。只有在目录中的 pin 更新后，
`hermes plugins update cliproxyapi-provider` 才会升级到新版本。

开发版（最新 `main`，未经目录审核）：

```bash
hermes plugins install duu261/hermes-cliproxyapi-provider
```

需要 Hermes 0.21.7 或更新版本。model-provider 插件安装后即生效，无需启用。请重启
正在运行的 Hermes gateway，使其加载插件。

## 配置

零配置：在 `~/.hermes/.env` 中设置 `CPA_LOCAL_API_KEY`（一个 CPA 客户端密钥），
即可获得指向 `http://127.0.0.1:8317` 上 CPA 的 `cpa-local`；可用
`CPA_LOCAL_BASE_URL` 覆盖 URL。

对于具名实例或远程实例，在 `~/.hermes/config.yaml` 中添加：

```yaml
cliproxyapi:
  instances:
    main:
      base_url: https://cpa.example.com   # with or without /v1
      key_env: CPA_MAIN_API_KEY           # env var holding a CPA client key
      mgmt_key_env: CPA_MAIN_MGMT_KEY     # optional, see "Public mode or management mode"
```

然后运行一次 `hermes model`（或任何会列出模型的命令）。插件会读取该实例的模型列表，
注册找到的 provider 并写入缓存，之后每次启动时直接注册，无需网络请求：

- `cpa-main`：实例上的全部模型，走 chat wire。
- `cpa-main-<provider>`：每个 CPA 提供方一个，例如 `cpa-main-claude`、
  `cpa-main-codex`、`cpa-main-xai`、`cpa-main-kimi`。

`hermes doctor` 和多进程部署（`plugins.isolation: host`）只有在重启后才能看到启动后
新出现的 provider。

## 各提供方的 wire

| CPA 提供方 | Hermes `api_mode` | CPA 依据 |
|---|---|---|
| `claude` | `anthropic_messages` | Claude executor 发送 Anthropic Messages |
| `kimi` | `anthropic_messages` | Kimi executor 将 Claude 格式透传到 Kimi 的 `/v1/messages` |
| `kiro`（插件） | `anthropic_messages` | Kiro 插件接收 Claude 格式输入 |
| `codex`、`xai`、`meta`（Muse） | `codex_responses` | 这些 executor 发送 CPA 的 Responses 格式 |
| `gemini`、`vertex`、`aistudio`、`gemini-interactions`、`antigravity`、`devin` | `chat_completions` | 对非 Google 主机，Hermes 无法使用 Gemini 和 Interactions wire；由 CPA 转换 |
| 兼容 OpenAI 及其他所有 | `chat_completions` | 所有 CPA executor 都接受 |

可通过 `api_mode` 按提供方覆盖（见下文）。当两个提供方提供同一个 model id 时，它会
同时出现在两者中，由 CPA 选择账号；此时的 wire 只是最佳匹配。

## 公开模式与 management 模式

公开模式是默认模式，只需要客户端密钥。management 模式额外设置 `mgmt_key_env`，读取
CPA 自己的提供方映射和模型定义。

| | 公开（客户端密钥） | Management（`mgmt_key_env`） |
|---|---|---|
| 每个模型的提供方 | `/v1/models` 上的 `owned_by`：厂商标签 | 实际提供该模型的账号提供方 |
| Devin、`google`（多个 Gemini executor） | 按厂商标签分组 | 精确提供方 |
| 由两个提供方提供的 id | 一个分组 | 所有提供它的分组 |
| 上下文窗口、命名推理级别 | 有 | 有 |
| 图像输入 | 仅当 `/v1beta/models` 列出该模型的输入类型时；否则未知 | 来自模型定义 |
| 按 token 预算推理的模型（无命名级别） | 识别不到 | 有 |
| 最大输出 | 有，真实上限恰为 64,000 的模型除外 | 有，全部 |
| API-key 和兼容 OpenAI 的渠道 | 有 | 沿用公开模式（management API 看不到它们） |
| Hermes 持有的密钥 | 客户端密钥 | 客户端密钥加一个拥有完整管理权限的 CPA 密钥 |

当每个模型只有一个提供方、且 CPA 的厂商标签与提供方一致时，公开模式就够用。
management 模式是精确模式，代价是密钥。CPA management key 可以修改配置、添加或删除
账号。插件只发送下列读取请求，但 Hermes 不会从 agent shell 中剥离 `mgmt_key_env`
（见“安全”），所以任何能读取你环境变量的 agent 工具也都能读到它。

management 模式发送的请求：

- 仅 GET，访问凭据列表、每个提供方若干抽样账号的模型列表，以及每个提供方的模型定义
  列表。绝不访问配额、用量、下载、OAuth，也不做任何写操作。
- 每轮最多 32 个请求、20 秒；每个实例每 15 分钟最多一轮；绝不在请求路径上执行。
- CPA 会在五次 management 登录失败后封禁该地址。插件在第一次 401 时即停止，并且在
  密钥变更之前，无论从哪个实例、跨多少次重启，都不会再用该密钥尝试该 CPA。如果缓存
  无法写入，插件会给出警告，因为跨重启的锁存状态就保存在那里。其他任何失败都会回退
  到公开分组。
- 除非两者运行在同一主机上，否则 CPA 必须允许来自插件地址的 management 访问（CPA
  配置中的 `remote-management`）。

## 覆盖配置

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

带有 `models` 的分组会用实时列表中匹配的 id 替换该提供方的成员（shell 风格通配符，
区分大小写）。

## 计费路由（CPA 前面的网关）

如果你在同一个 CPA 前面运行 New API 等计费网关，插件可以通过该网关发送推理请求，
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
          api_mode: chat_completions      # how this gateway channel expects requests
```

- profile 指向 `route_via` 并使用网关密钥。CPA 密钥只用于获取 CPA 的模型列表，绝不
  发送给网关；网关密钥也绝不发送给 CPA。两者必须是不同的环境变量。
- 只保留网关为该密钥列出的模型（`GET /v1/models`）。如果获取网关列表失败，则保留
  最近一次已知的列表。在首次读到某个密钥的网关列表之前，该密钥的 profile 使用 CPA 的
  列表，因此路由 provider 会在启动时从 CPA 缓存注册；调用该密钥未售卖的模型会在网关处
  失败，直到下一次刷新收窄列表。已知的列表（即使为空）始终严格过滤。
- 除非开启请求体透传，网关通常会按渠道转换请求格式。profile 保持上表中的 CPA wire；
  当网关渠道需要不同 wire 时，按分组设置 `api_mode`。
- 上下文窗口和推理级别描述的是 CPA 的模型，不包括网关可能额外施加的限制。
- 网关可能故障转移到它以同一模型名出售的其他渠道。插件无法察觉也无法阻止。
- 插件只使用网关的客户端端点，绝不使用其管理、渠道、定价或计费 API。
- 已知限制：Hermes 只会从 agent 子进程中剥离已注册 provider 所声明的密钥名。路由
  provider 声明的是网关密钥，因此在启用路由的实例上，CPA 的 `key_env`（discovery 需要）
  对 agent shell 仍然可见。`mgmt_key_env` 从不在 provider 上声明，所以无论是否启用
  路由都不会被剥离。Hermes 目前还没有插件 hook 可以声明一个 secret 而不把它变成推理
  凭据。

## 元数据

上下文窗口、最大输出、图像输入、推理支持和推理强度级别只来自 CPA：有 management
模型定义时使用它，否则使用 CPA 公开模型列表并按精确 id 匹配。CPA 未公布的值保持
未知。没有 management key 时，最大输出取自 CPA 的 Anthropic 格式模型列表，它覆盖所有
模型。CPA 会为没有上限的模型在其中写入 64,000，因此插件把恰好 64,000 视为未知，由
Hermes 回退到 models.dev；真实上限恰为 64,000 的模型也会因此丢失该值。

Hermes 的 `/model` 只从 models.dev 显示最大输出、PDF 和结构化输出；插件无法设置
它们，CPA 的模型定义也没有 PDF 或结构化输出字段。要显示它们，可从 models.dev 补充
厂商元数据：

```yaml
providers:
  cpa-main-claude:
    catalog_provider: anthropic
```

优先级从高到低：你的 `model_overrides`、本插件、models.dev。

## 安全

- 密钥通过环境变量名引用，由 Hermes 解析。
- Hermes 只会从 agent 子进程中剥离已注册 provider 所声明的环境变量名。每个 profile
  声明其推理密钥（`key_env`，或启用路由的实例上的网关密钥），因此 `mgmt_key_env`，
  以及启用路由的实例上 CPA 的 `key_env`，对 agent shell 仍然可读。
- 只有当主机解析出的每个地址都属于 loopback、RFC 1918、100.64/10 或 IPv6 ULA 时，
  才允许使用明文 `http://`。其他情况必须使用 `https://`。
- 不跟随重定向，限制响应大小，失败只报告 HTTP 状态码和错误类别，绝不包含响应体、
  URL 或密钥。
- 目录请求发送 `User-Agent: hermes-cliproxyapi-provider/<version>`。另有一个
  `/v1/models` 请求会追加 `grok-shell-compatible`，因为 CPA 向该客户端发布上下文
  窗口和推理级别；还有一个请求发送 `Anthropic-Version: 2023-06-01`，这种格式带有
  输出上限。它们的 id 会与常规列表匹配，所以只增加元数据，绝不增加模型。推理请求
  携带 Hermes 自己的 header；插件不设置任何 header。
- 位于 `$HERMES_HOME/cache/cliproxyapi/` 的缓存（mode 0600）保存 model id、所有者、
  提供方成员关系和元数据。不含密钥，也不含账号名。被拒绝的 management key 以加盐
  HMAC 形式记录，而不是密钥本身。

## 开发

```bash
python -m unittest discover -s tests -v          # core tests, no Hermes needed
PYTHONPATH=/path/to/hermes-agent python -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate .
```

## 许可证

MIT

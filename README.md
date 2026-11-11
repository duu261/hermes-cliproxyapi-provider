# hermes-cliproxyapi-provider

English | [Tiếng Việt](README.vi.md) | [简体中文](README.zh-CN.md)

[Hermes Agent](https://github.com/NousResearch/hermes-agent) plugins for
[CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (CPA): a
model-provider, and a companion [`/cpa`](#cpa-pool-status-companion-plugin)
chat command for pool health and live quota.

CPA is a gateway: one instance fronts many providers (Claude, Codex, Gemini,
xAI, Kimi, Devin, OpenAI-compatible upstreams, plugin providers such as Kiro)
and owns their accounts, OAuth, rotation and translation. Hermes alone sees
such a gateway as one custom endpoint on one wire with hand-pinned metadata.

This plugin mirrors CPA into Hermes instead. Point it at a CPA instance and it
registers one Hermes provider per CPA provider, each on the wire that CPA's
executor for that provider accepts with the least translation, with the model
list and the model capabilities CPA publishes (context window, max output,
image input, reasoning levels). It adds nothing CPA does not serve.

The companion `/cpa` shows each pool's accounts in chat (active, limited,
cooling, error, disabled) and, on request, each account's live quota (Claude,
Codex, xAI, Antigravity, Devin, Kimi, Kiro). Read-only and lockout-safe.

## Install

From the [Hermes plugin catalog](https://hermes-agent.nousresearch.com/docs/plugins/cliproxyapi-provider)
(recommended), the provider and its [`/cpa`](#cpa-pool-status-companion-plugin)
companion:

```bash
hermes plugins install cliproxyapi-provider
hermes plugins install cpa-status
```

This installs the commits the catalog maintainers reviewed.
`hermes plugins update <name>` moves to a newer release only after its
catalog pin is bumped. Update both together: they share one lock per CPA.

Development (latest `main`, not reviewed by the catalog):

```bash
hermes plugins install duu261/hermes-cliproxyapi-provider
hermes plugins install duu261/hermes-cliproxyapi-provider#cpa-status
```

Requires Hermes 0.21.7 or newer. Model-provider plugins are active once
installed; there is nothing to enable. `cpa-status` needs enabling (see
[/cpa](#cpa-pool-status-companion-plugin)). Restart running Hermes gateways so
they load the plugins.

## Configure

Zero config: set `CPA_LOCAL_API_KEY` (a CPA client key) in `~/.hermes/.env`.
That gives `cpa-local` for a CPA at `http://127.0.0.1:8317`; override the URL
with `CPA_LOCAL_BASE_URL`.

For named or remote instances, add to `~/.hermes/config.yaml`:

```yaml
cliproxyapi:
  instances:
    main:
      base_url: https://cpa.example.com   # with or without /v1
      key_env: CPA_MAIN_API_KEY           # env var holding a CPA client key
      mgmt_key_env: CPA_MAIN_MGMT_KEY     # optional, see "Public mode or management mode"
```

Then run `hermes model` (or anything that lists models) once. The plugin reads
the instance's model lists, registers the providers it finds, and caches them,
so later runs register them at startup with no network calls:

- `cpa-main`: every model on the instance, each on the wire of the provider
  that serves it (Hermes 0.22.6 or later; older Hermes keeps every model on
  the chat wire).
- `cpa-main-<provider>`: one per CPA provider, for example `cpa-main-claude`,
  `cpa-main-codex`, `cpa-main-xai`, `cpa-main-kimi`.

On Hermes 0.22.6 or later:

- The provider pickers of `hermes model` and Telegram `/model` show one row
  per instance, named after it (`display_name`, else `CLIProxyAPI main`), with
  `cpa-main` and its providers inside. Before the first listing an instance
  has only `cpa-main`, which stays a plain row. Typed provider names are
  unchanged.
- `hermes model --refresh` and `/model --refresh` also drop the plugin's own
  memo of the model list, so the next listing reads CPA again. Without it,
  one listing serves every provider of the instance for 30 seconds. The
  management pass keeps its 15 minute pace.
- When the key variable is empty, Hermes' "No usable credentials" message
  also says which key it takes: a CPA client key, not the management key.
  With a billing route (`route_via`) it names the gateway key variable and the
  CPA one.

`hermes doctor` and multi-process setups (`plugins.isolation: host`) see
providers that appeared after startup only after a restart.

A new instance on the same CPA `base_url` as an instance already cached starts
from that cache, so its providers register at the next startup without a
listing. It writes its own cache after its first successful listing.

## Wire per provider

| CPA provider | Hermes `api_mode` | CPA source |
|---|---|---|
| `claude` | `anthropic_messages` | Claude executor posts Anthropic Messages |
| `kimi` | `anthropic_messages` | Kimi executor passes Claude format to Kimi's `/v1/messages` |
| `kiro` (plugin) | `anthropic_messages` | Kiro plugin takes Claude input |
| `codex`, `xai`, `meta` (Muse) | `codex_responses` | executors post CPA's Responses format |
| `gemini`, `vertex`, `aistudio`, `gemini-interactions`, `antigravity`, `devin` | `chat_completions` | Gemini and Interactions wires are not available to Hermes for non-Google hosts; CPA translates |
| OpenAI-compatible and anything else | `chat_completions` | accepted by every CPA executor |

Override per provider with `api_mode` (see below). When two providers serve the
same model id, it appears in both and CPA picks the account; the wire is then a
best match.

The instance-wide provider (`cpa-main`) uses the same table per model on
Hermes 0.22.6 or later: a Claude, Kimi or Kiro model goes out on
`anthropic_messages`, a Codex, xAI or Meta model on `codex_responses`, the
rest on `chat_completions`. A
model that providers on two different wires serve, and a model the plugin has
not listed yet, stay on `chat_completions`. An `api_mode` override on a
provider applies here too. A group built from `models` patterns decides a
model's wire only when the group sets `api_mode`, or has the name of a
provider with a non-chat wire in the table. Older Hermes sends every model of
`cpa-main` on `chat_completions`.

Hermes saves a wire with the default model (`model.api_mode`) and uses a saved
wire when it starts a session. `hermes model` saves `chat_completions` for
`cpa-main`, whatever model you pick. To get the wire of the model at session
start, run `hermes config unset model.api_mode`, or pick the model in a chat
with `/model <id> --global`, which saves the wire of that model.

Keep `xai` on `codex_responses`. Grok 4.7 returns its reasoning as an
encrypted item that the client must send back on the next turn
([xAI docs](https://docs.x.ai/developers/model-capabilities/text/reasoning)).
Hermes does that on the Responses wire. Chat Completions has no field for it,
and CPA (checked at v8.0.44) replays cached xAI reasoning only for Claude and
Responses clients. Without it, Grok 4.7 stops reasoning after a few tool rounds
and tends to say it will call a tool, then end the turn without calling it.

### Using a profile as a fallback

Hermes builds a `fallback_providers` entry without asking the provider's
profile which wire it uses ([hermes-agent#143933](https://github.com/NousResearch/hermes-agent/issues/143933)).
Without `api_mode` the entry lands on `chat_completions`, so a Claude fallback
posts to `<root>/chat/completions` and fails. Until that is fixed, set
`api_mode` on every entry that names one of these profiles, using the wire
from the table above:

```yaml
fallback_providers:
  - provider: cpa-main-claude
    model: claude-sonnet-4-6
    api_mode: anthropic_messages
delegation:
  fallback_providers:
    - provider: cpa-main-xai
      model: grok-4.6
      api_mode: codex_responses
```

## Public mode or management mode

Public mode is the default and needs only a client key. Management mode adds
`mgmt_key_env` and reads CPA's own provider map and model definitions.

| | Public (client key) | Management (`mgmt_key_env`) |
|---|---|---|
| Provider per model | `owned_by` on `/v1/models`: the vendor label | the account provider that serves it |
| Devin, `google` (several Gemini executors) | grouped by vendor label | exact provider |
| Id served by two providers | one group | every group that serves it |
| Context window, named reasoning levels | yes | yes |
| Image input | only when `/v1beta/models` lists the model's input types; else unknown | from model definitions |
| Reasoning on budget models (no named levels) | not detected | yes |
| Max output | yes, except models whose real limit is exactly 64,000 | yes, all |
| Plugin providers (Kiro and others) | grouped; metadata only from public lists | model definitions from the plugin (CPA v8.0.34+) |
| API-key and OpenAI-compatible channels | yes | kept from public mode (the management API cannot see them) |
| Key held by Hermes | client key | client key plus a full-admin CPA key |

Public mode is enough when each model has one provider and CPA's vendor labels
match its providers. Management mode is the precise one; its cost is the key.
A CPA management key can change config and add or remove accounts. The plugin
only sends the reads below, but Hermes does not strip `mgmt_key_env` from agent
shells, so any agent tool that can read your environment can read it too,
unless the `cpa-status` plugin hides it (Hermes 0.23.1 or later, see Security).

What management mode sends:

- GET only, to the credentials list, a few sampled accounts' model lists per
  provider, and the model-definitions list per provider (built-in providers
  first, then plugin providers by name; CPA before v8.0.34 has none for
  plugin providers and the plugin skips them). Never quota, usage,
  downloads, OAuth or any write.
- At most 32 requests and 20 seconds per pass, at most one pass per 15 minutes
  per instance, never on the request path.
- The plugin stops at the first 401 and never retries that key against that
  CPA, from any instance, either plugin or across restarts, until the key
  changes. See [Never get locked out of CPA](#never-get-locked-out-of-cpa).
  Any other failure falls back to public grouping.
- CPA must allow management from the plugin's address (`remote-management`
  in CPA's config) unless both run on the same host.

## Overrides

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

A group with `models` replaces that provider's membership with the matching
ids from the live list (shell-style patterns, case-sensitive).

## Billing route (gateway in front of your CPA)

If you run a billing gateway such as New API in front of the same CPA, the
plugin can send inference through it while still learning everything from
CPA:

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

- Profiles point at `route_via` and use the gateway key. The CPA key is used
  only for CPA's model lists and never sent to the gateway; the gateway key is
  never sent to CPA. They must be different env vars.
- Only models the gateway lists for that key (`GET /v1/models`) are kept. If
  the gateway list fails, the last known list is kept. Until the first list
  for a key is read, its profiles use CPA's list: routed providers register
  at startup from the instance's cached CPA list, or from another configured
  instance's cache on the same CPA `base_url`. A routed instance on a CPA no
  instance has listed yet needs one model listing first, as above. A call to
  a model the key does not sell fails at the gateway until the next refresh
  narrows the list. A known list, even an empty one, filters strictly.
- Gateways often convert request formats per channel unless body pass-through
  is on. Profiles keep the CPA wire from the table above; set `api_mode` per
  group where the gateway channel needs a different one.
- Context windows and reasoning levels describe CPA's model, not limits the
  gateway may add.
- The gateway may fail over to other channels it sells under the same model
  name. The plugin cannot see or prevent that.
- The plugin uses only the gateway's client endpoints, never its admin,
  channel, pricing or billing APIs.
- Known limit: Hermes strips from agent subprocesses only the key names a
  registered provider declares. Routed providers declare the gateway key, so
  Hermes leaves the CPA `key_env` (needed for discovery) visible to agent
  shells on a routed instance. `mgmt_key_env` is never declared on a provider,
  so Hermes does not strip it on routed or unrouted instances. The
  `cpa-status` plugin hides both from the commands of a chat session on
  Hermes 0.23.1 or later; Security says what that covers and what it does
  not.

## Metadata

Context window, max output, image input, reasoning support and reasoning-effort
levels come only from CPA: management model definitions when available, else
CPA's public model lists matched by exact id. Values CPA does not publish stay
unknown. Without a management key, max output comes from CPA's Anthropic-format
model list, which covers every model. CPA writes 64,000 there for a model with
no limit, so the plugin treats exactly 64,000 as unknown and Hermes falls back
to models.dev; a model whose real limit is 64,000 loses it the same way.

Hermes `/model` shows max output, PDF and structured output only from
models.dev; a plugin cannot set them, and CPA's model definitions have no PDF
or structured-output field. To see them, add vendor metadata from models.dev:

```yaml
providers:
  cpa-main-claude:
    catalog_provider: anthropic
```

Precedence, highest first: your `model_overrides`, this plugin, models.dev.

## Security

- Keys are referenced by environment variable name and resolved by Hermes.
- Hermes strips from agent subprocesses only the env names a registered
  provider declares. Every profile declares its inference key (`key_env`, or
  the gateway key on a routed instance), so Hermes leaves `mgmt_key_env`, and
  on a routed instance the CPA `key_env`, readable by agent shells.
- The `cpa-status` plugin hides those variables on Hermes 0.23.1 or later. It
  removes them from the environment of the commands a chat session runs:
  terminal commands in the foreground and background, the ones `execute_code`
  starts included, and the helper processes Hermes starts from its cleaned
  environment (language servers, lint, text to speech and similar). It does
  not cover shell hooks, cron job scripts, processes Hermes starts with the
  profile's credentials (a `key_cmd` helper, for example), or any process
  started with no session bound: those can still see the variables. Checked
  on the local terminal backend with one profile only. Remote backends
  (docker, ssh and others) do not apply the hiding: a variable that is
  forwarded to one is not hidden there. It hides environment variables and
  nothing else: a command that can read the file Hermes loads keys from
  (`~/.hermes/.env`) can still read them. A variable whose name starts with
  `HERMES_`, or that another plugin already manages, stays visible, and a log
  warning names it. The names are read when the plugin loads, so a
  `mgmt_key_env` or `route_via` added later is hidden only after the next
  Hermes restart. On older Hermes, or without `cpa-status` enabled, nothing
  is hidden.
- Hiding is on by default. A script you run through the agent that reads one
  of these variables stops finding it. To turn hiding off, run
  `hermes config set plugins.entries.cpa-status.settings.hide_keys_from_shells false`
  and restart Hermes.
- Plain `http://` is allowed only when every address the host resolves to is
  loopback, RFC 1918, 100.64/10 or IPv6 ULA. Anything else needs `https://`.
- No redirects, capped responses, and failures reported as an HTTP status and
  category only, never bodies, URLs or keys.
- Catalog requests send `User-Agent: hermes-cliproxyapi-provider/<version>`.
  One extra `/v1/models` request appends `grok-shell-compatible`, because that is
  the client CPA publishes context windows and reasoning levels to, and another
  sends `Anthropic-Version: 2023-06-01`, the format that carries output limits.
  Their ids are matched against the normal list, so they add metadata, never
  models. Inference requests carry Hermes' own headers; the plugin sets none.
- The cache at `$HERMES_HOME/cache/cliproxyapi/` (mode 0600) holds model ids,
  owners, provider membership and metadata. No keys, no account names. Its
  `locks/` subdirectory holds one empty lock file per CPA and
  `rejected.json`, the rejected management keys as salted HMACs, not keys.

## Never get locked out of CPA

CPA counts failed management logins per client IP, in memory, per CPA
process. A missing or wrong management key adds one; the fifth in a row bans
that IP for 30 minutes, localhost included, which also locks out CPA's web UI
and any other tool on the same IP. A successful management login resets the
count, a CPA restart clears it, and an IP with no failures for two hours is
forgotten. Failures do not expire after 30 minutes. Answers `remote
management disabled` and `key not set` (403) do not count.

What both plugins do, per `HERMES_HOME` on Linux and macOS:

- One management check per CPA at a time, across the provider, `/cpa` and
  every Hermes process (CLI and gateway). The provider skips its check when
  `/cpa` is busy and tries again on the next refresh; `/cpa` waits up to 25
  seconds, then says `busy`.
- A key that is already bad costs one try. The rejection is remembered (salted
  HMAC) across restarts and both plugins, and that key is not sent to that
  CPA again until it changes.
- A key revoked in the middle of `/cpa quota` can cost up to 4: the requests
  already in flight. `/cpa quota` first sends one request alone, so a key
  that is already bad never reaches the parallel requests.
- On Windows only the in-process lock applies; two Hermes processes there are
  coordinated by the remembered rejections alone.

What you do:

1. Set the key through `mgmt_key_env` only, and log in to CPA's web UI once
   with it before Hermes starts. A good login also resets the count.
2. Rotate in one step: change CPA's key, update the env var, restart the
   gateway. The old key stays remembered as rejected, so Hermes goes quiet
   instead of retrying it.
3. Run Hermes on the same host as CPA, or set CPA's `server.trusted-proxies`
   (older configs: top-level `trusted-proxies`) to your reverse proxy's
   address. Otherwise every client shares the proxy's IP and one bad key bans
   all of them.
4. Other tools on the same IP count too (CPA Usage Keeper, scripts, a browser
   on the web UI). Fix their keys first.
5. Each Hermes profile remembers rejections on its own. With five or more
   profiles holding the same bad key on one host, fix it before starting
   them.
6. Use one spelling per CPA in `base_url` (`127.0.0.1` or `localhost`, not
   both): the plugins coordinate by URL.
7. Upgrade both plugins together; 0.4 and earlier do not share the lock.
8. If banned: wait 30 minutes or restart CPA, and fix the key before Hermes
   tries again (`/cpa` shows `key rejected`).

## /cpa: pool status (companion plugin)

The repo also ships `cpa-status`, a second plugin with one slash command,
`/cpa`, that shows account health for every configured instance. Model-provider
plugins cannot register commands, so it installs and enables separately:

```bash
hermes plugins install cpa-status
hermes plugins enable cpa-status
```

It needs `mgmt_key_env` on the instances it reports on; instances without one
show `no management key`, unless another instance on the same CPA `base_url`
has one: then they are listed under it as `same CPA`. Restart running
gateways after enabling.

```
/cpa                    every instance: accounts per provider
/cpa pre                one instance, plus the last hour of requests
/cpa pre codex          one line per account
/cpa quota pre          live usage of every account with quota
/cpa quota pre codex    Codex only
```

```
pre · v8.0.51
  codex 4: 3 active · 1 cooling (next 17:20)
  xai 2: 2 active
CPA v8.0.52 available (running v8.0.51)
pre / codex · 4 accounts · total = since CPA start
codex-al…@…  active · prio 1 · 1h 173/71 · total 2140/96 · 5h 22% · wk 0% (1m ago)
codex-bo…@…  cooling until 17:20 (vendor limit reached)
```

- Read-only: one `GET /v8/management/credentials` per instance per run (v0
  `auth-files` only after a 404), and only when you type `/cpa`. No timers, no
  retries. A repeat within 20 seconds answers from memory.
- The CPA version next to each instance is the `X-CPA-VERSION` header of that
  same reply. On the plain `/cpa` overview, `CPA <version> available` comes
  from CPA's own update route (`GET /v8/management/server/latest-version`,
  which asks GitHub): one request per Hermes process every 6 hours, 10
  minutes after a failure. A failure only hides the line.
- Last-seen quota comes from what CPA recorded. For Claude and Codex accounts
  (5h and weekly) CPA records it on real traffic. For Devin accounts (day and
  week) CPA records it when the account signs in and when CPA refreshes the
  account's status, so a Devin figure can be old; the line shows its age. CPA
  records none for other providers. Plain `/cpa` never asks CPA to check quota
  upstream; `/cpa quota` does.
- `/cpa pre codex` also shows each account's priority when CPA lists one
  (`prio 1`) and its successful/failed requests: `1h` is the last hour,
  `total` is since CPA started. CPA keeps the totals in memory only, so they
  start again at zero when CPA restarts.
- Emails in account names and messages are masked. `email`, `account`,
  `project_id` and `path` are never shown (`/cpa quota` uses the Antigravity
  `project_id` only to build its request); the account's project id is
  replaced by `…` where CPA repeats it in the label or file name, and where a
  message names it.
- URLs, host names, IP addresses and file paths in messages are masked on a
  best effort: the patterns cover the common forms, not every one, so part of
  an address can still show. A host name with no dot is masked only where an
  error gives it as an address (`dial tcp myhost:8080`, `lookup myhost`). Do
  not rely on this masking in a chat where those must stay hidden.
- Works when the chat is idle. Sent while an agent turn is running, it goes to
  Hermes' busy handling (queued or sent to the agent) like any other plugin
  command.
- Lockout: see [Never get locked out of CPA](#never-get-locked-out-of-cpa).

### /cpa quota: live usage

```
pre quota · live 21:40

codex · 4 accounts: 3 have room · 1 out (next free 22:10)
OUT codex-xy…@… plus  5h 100% → 22:10 · wk 60%
    codex-ab…@… plus  5h 23% · wk 85% → Oct 29
    codex-cu…@… pro   wk 40%
    codex-bd…@… pro   wk 31%

antigravity · 1 account: 1 have room
antigravity-01…@… pro  out: claude and gpt
  gemini          5h 0% · wk 12%
  claude and gpt  5h 100% → 23:10 · wk 64%
```

`/cpa quota <pool>` asks each vendor how much of each account is used, the way
CPA's own web UI quota page does. For the built-in vendors, CPA's `api-call`
route sends the web UI's request with that account's token (CPA fills it in;
Hermes never sees it). Accounts of other providers are read from their CPA
quota plugin when CPA lists one. Every request, and when it is sent:

| Provider | Requests per account | What you see |
|---|---|---|
| Claude | GET `https://api.anthropic.com/api/oauth/usage`, then GET `https://api.anthropic.com/api/oauth/profile` for the plan | `5h`, `wk`; weekly model and app limits when sent (`fable`, `opus`, `sonnet`, `apps`, `cowork`); extra usage when on, with spend/limit; credit pools (`$ left`); plan (`team`, `max20`, `max5`, `max`, `pro`, `free`) |
| Codex | GET `https://chatgpt.com/backend-api/wham/usage` | windows by their own length (`5h`, `wk`, `mo`), plan, credits when the account has them; limits on one feature or model when sent (`<name> wk`); banked limit resets as a count (`2 resets banked`); the day the subscription ends (`until Nov 16`) when CPA's listing has it, at no extra request |
| xAI | GET `https://cli-chat-proxy.grok.com/v1/billing?format=credits`, then GET `https://cli-chat-proxy.grok.com/v1/billing` | weekly credits `wk` (`wk no figure yet` until xAI reports a percent); monthly spend `mo` only when xAI reports a monthly limit; when xAI sends them, the share of the weekly credits each product used (`grokbuild 30%, api 6%`), pay as you go (`payg off`, or spend/cap as `payg $3.00/$50.00`) and a prepaid balance (`prepaid $25.00`) |
| Antigravity | POST `https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary` with the account's `project_id`; the `daily-cloudcode-pa.sandbox` and `cloudcode-pa` hosts only when the one before failed at the vendor or returned no quota; then POST `https://daily-cloudcode-pa.googleapis.com/v1internal:loadCodeAssist` for the plan | per model group, `5h` and `wk`; plan (`free`, `pro`, `ultra`, `ultra-lite`) |
| Devin | POST `https://server.codeium.com/exa.seat_management_pb.SeatManagementService/GetUserStatus` | `day`, `wk`, plan |
| Kimi | GET `https://api.kimi.com/coding/v1/usages` (`api.kimi.ai` when CPA lists the account as `kimi-ai`) | each limit window, weekly, `mo` |
| CPA plugin quota (e.g. Kiro) | GET `/v8/management/plugins/<id>/quota` on CPA; the plugin asks its vendor | the plugin's meters and plan |

None of these cost money or use up quota: they are the same reads the web UI
makes for its quota page. Left out on purpose: xAI's paid 1-token chat check
(the web UI's fallback for paid API-key accounts, which then show the billing
error), every other identity or plan lookup, downloading account files, and
every reset.

The headers, including each vendor's CLI `User-Agent`, are the web UI's. The
percent is what is used (vendors that report what is left are converted);
there are no totals across plans. A window shows when it resets (`→ 22:10`)
only at 80% used or more. An account is `OUT`, at the start of its line, when
a window is at 100% or the vendor says it is blocked. Antigravity is per model
group, one line per group under the account (`gemini`, `claude and gpt`): CPA
holds back only the models that ran out, so an account is `OUT` only when
every group is full; otherwise the full groups show as `out: <group>`.
Claude's model-scoped weekly limits (Fable, Opus, Sonnet, apps, Cowork) work
the same way: a full one shows as `out: fable` and the account keeps serving
other models. So do Codex limits on one feature or model.

- On Telegram, Discord, Slack, Matrix and Mattermost the reply is sent as a
  code block, so the columns line up. This needs Hermes 0.22.3 or later, which
  tells the command the platform; elsewhere, and on older Hermes, the reply is
  plain text.
- The plan of a Claude or Antigravity account costs one more request per
  account (the profile and `loadCodeAssist` requests in the table), sent only
  after that account's usage was read. Only the plan fields of the reply are
  read, and the plan is shown as one of the fixed words in the table; a value
  that is not on the list shows no plan. The replies also carry a name, an
  email, an organization name and the project id: none of them is read, shown
  or kept. When the request fails, the line has no plan and nothing else
  changes; it is not retried.
- Only when you type it. At most 20 accounts and 4 requests per account per
  run. The first request goes alone, then at most 4 at a time; a repeat within
  60 seconds answers from memory. Disabled accounts are skipped. A run sends
  no new request after 60 seconds; accounts it did not reach show
  `not checked`.
- `/cpa quota <pool> <provider>` takes `claude`, `codex`, `xai`,
  `antigravity`, `devin`, `kimi`, `plugin`, or a plugin's provider such as
  `kiro`.
- An expired account token shows `token rejected by vendor` and never counts
  against CPA's lockout. A rejected management key stops the run.
- `until Nov 16` after a Codex plan is the day the subscription ends. It is
  read from CPA's account listing (the `chatgpt_subscription_active_until`
  claim of the ID token CPA stores), not asked from the vendor, so it is as
  old as that token: a date that has passed shows as `until Oct 03 (past)`.
- CPA's account index, the Codex account id, the Antigravity project id and
  the CPA plugin id are used only to build requests and are never shown,
  logged or cached. Vendor replies are read field by field; bodies and
  headers are never shown.
- Not covered: Meta (the web UI reads its raw credential file), Kimi `.ai`
  accounts that CPA lists only as `kimi` (they get the `.com` URL and show
  `token rejected by vendor`), Codex code-review and per-model extra limits,
  plugin summary figures.

## CPA versions

The plugins follow CPA releases and set no maximum. Features that need a
newer CPA:

| Feature | CPA |
|---|---|
| Public grouping by `owned_by` (`/v1/models`) | v4.1.1+ |
| Management mode (credentials, credential models, model definitions) | v6.7.26+ (v8 routes from v8.0.0) |
| Model definitions for plugin providers (Kiro and others) | v8.0.34+ |
| `/cpa` | v5.0.0+ (v0 `auth-files`; v8 `credentials` from v8.0.0) |
| `/cpa` last-seen quota | v7.2.142+ |
| `/cpa` last-seen quota for Devin | v7.3.0+ |
| `/cpa` CPA version | v6.3.36+ |
| `/cpa` update hint | v6.5.42+ |
| `/cpa quota` built-in vendors (`api-call` with `$TOKEN$`) | v6.6.63+ |
| `/cpa quota` plugin quota (e.g. Kiro) | v8.0.25+ |

Older releases answer the missing route with 404, and that feature is
skipped.

## Development

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

Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md) for the safety rules
every change keeps, and [SECURITY.md](SECURITY.md) to report a problem
privately.

## License

MIT

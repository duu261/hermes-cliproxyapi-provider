# Hermes CLIProxyAPI Provider - Agent Instructions

## Scope

Public Hermes Agent `kind: model-provider` plugin that bridges CLIProxyAPI
(CPA) and Hermes. For each configured CPA instance it registers one Hermes
provider per CPA provider (claude, codex, xai, kimi, kiro, ...), each on the
Hermes wire that CPA's executor for that provider accepts with the least
translation, plus one instance-wide chat provider. CPA owns accounts, OAuth,
rotation, routing and translation; this plugin mirrors what CPA serves. Build
for any CPA operator: derive behavior from CPA and Hermes source, never from
one deployment's config. No hand-written model tables, routing, retries
across instances or account logic.

An instance may send inference through a billing gateway the operator runs in
front of the same CPA (New API or similar, see Billing route). CPA stays the
only source of what is served; the gateway only carries and bills requests.

The repo also ships `cpa-status/`, a separate `kind: standalone` plugin whose
only job is the `/cpa` status command (see `/cpa status` below). Model-provider
plugins cannot register commands, hence two plugins in one repo.

## Grouping (which models belong to which provider)

1. Public mode (default, client key only): group the public `GET /v1/models`
   list by `owned_by`, mapped to CPA provider keys via CPA's static catalogs
   (anthropic->claude, openai->codex, xai->xai, meta->meta, moonshot->kimi,
   antigravity->antigravity, google->gemini). Any other label is used as is
   (openai-compat names, plugin providers such as kiro). Document the limits:
   Devin labels models by vendor, `google` covers several executors, an id
   served by two providers shows one owner.
2. Management mode (opt-in, instance sets `mgmt_key_env`): provider per model
   from the CPA management API, see Management below. Ids the management pass
   cannot see (API-key and openai-compat channels) keep their public group.
3. Operator overrides per instance (`groups:`) win over both.
An id served by several providers appears in each; CPA, not this plugin,
picks the account. Document that the wire is then a best match.

## Wire map

Each row cites CPA source (executor and `RequestToFormat`/translator target)
or a live check. Re-verify on CPA upgrades; unknown providers get
chat_completions, which every CPA executor accepts.

- claude -> anthropic_messages
- kimi -> anthropic_messages (Claude source is passed through to Kimi's
  /v1/messages)
- kiro (plugin) -> anthropic_messages (declares claude input; builds its
  upstream payload from Claude format)
- codex, xai, meta -> codex_responses (CPA reshapes to its Codex format).
  xai must stay on Responses: grok-4.7 needs its encrypted reasoning items
  replayed each turn, Chat Completions cannot carry them, and CPA v8.0.44
  replays cached xAI reasoning only for Claude/Responses sources
  (`xai_reasoning_replay.go` `xaiReasoningReplayEnabledForSource`). On Chat,
  reasoning drops to zero after a few tool rounds and the model announces a
  tool call without making it. Do not use xai in chat_completions examples.
- gemini, vertex, aistudio, gemini-interactions, antigravity, devin ->
  chat_completions (Gemini-native and Interactions are not Hermes wires for
  non-Google hosts; CPA translates)
- openai-compatibility and unknown -> chat_completions
Operators may override per provider with `api_mode`.

## Metadata

Fill `model_capabilities`, max tokens and reasoning efforts only from CPA:
management model definitions when available, else the public model lists
matched by exact id. Never invent a value. Precedence, highest first: user
`model_overrides`, this plugin, models.dev via `catalog_provider`.

## Registration

Register at import from config plus the on-disk catalog cache; no network at
import. With no cache, register only the instance-wide chat provider.
`fetch_models` refreshes the catalog, writes the cache and registers newly
seen providers in the running process.

## Management (opt-in)

Off unless an instance sets `mgmt_key_env`. Public mode is the documented
default: on three live pools it put every model on the same wire as the
management pass, and covered API-key channels the management pass cannot
see. Management mode is for the cases public grouping gets wrong (Devin, one
id served by several providers) and adds output limits and vision. Document
that the key is full-admin on CPA and that leaving it unset is recommended.
Prior art (CPA Usage Keeper, AI-gateway, cpa-usage, oh-my-cpa) holds the same
key; match their safety.
- Read-only GET allowlist under /v8/management (v0 fallback on 404 only):
  credentials, credentials/models, config/api-keys,
  config/routing/force-model-prefix, routing/model-definitions/:channel,
  plugins. Never api-call, usage queue, quota, download, config.yaml, auth
  URLs or any write method.
- Bounded: sample at most a few accounts per provider, never one call per
  account; cap total calls and concurrency per refresh; refresh-time only,
  cached with a TTL, never on the inference path.
- Stop the management pass on the first 401 and latch management off for
  that instance until its key changes. Treat "remote management disabled",
  "key not set" and any other failure as unavailable and fall back to public
  mode without retrying.
- Every management pass takes `cpa.mgmt_guard` for its root without waiting
  (busy: skip the pass, keep previous results, leave `mgmt_checked_at` so the
  next refresh retries) and checks `cpa.key_rejected` first. A 401 calls
  `cpa.mark_rejected` before the guard is released. The guard (thread lock,
  then `fcntl.flock` on `cache/cliproxyapi/locks/<hash>.lock`) and the shared
  latch `cache/cliproxyapi/locks/rejected.json` are shared with `/cpa`: CPA
  bans a client IP after five failed management keys in a row, whichever
  plugin or process sent them.
- Parse responses into an allowlist of fields and drop the rest immediately;
  never log, cache or return key-bearing fields or bodies.
- Same transport rules as the client key.
- If CPA ships a client-key route for model-to-provider data, prefer it.

## Billing route

Optional per instance: `route_via` (gateway base URL) and `route_key_env`
(the gateway's key, by env var name). A group may set its own
`route_key_env`: gateway keys are often bound to one billing group, while one
CPA instance serves providers the gateway sells in different groups.

- Discovery, grouping and metadata always come from CPA (`base_url`,
  `key_env`). Inference for every profile of that instance goes to
  `route_via` with the group's route key, else the instance's.
- Never send the CPA key to the gateway or the gateway key to CPA.
- Keep only the ids the gateway lists for the route key (`GET /v1/models`);
  when that list is unavailable, keep the last cached result. Before the
  first list for a key is read, use CPA's ids, so routed groups register at
  import from CPA's cache. A known list, even empty, filters strictly.
- The gateway may translate between formats in ways the plugin cannot see.
  Profiles keep the CPA wire map; operators set `api_mode` per group where
  the gateway needs a different wire. Document this, and that context and
  reasoning metadata describe CPA's model, not the gateway's limits.
- Document that the gateway may fail over to non-CPA channels it sells under
  the same name; the plugin cannot see or prevent that.
- Use only the gateway's client endpoints (`/v1/models` and inference). Never
  read its admin, channel, pricing or billing APIs.
- Same transport rules as CPA.

## /cpa status (companion plugin)

`cpa-status/` registers `/cpa`: account health per CPA instance, provider and
account, and `/cpa quota`: live usage per account for every provider CPA's
web UI shows quota for, except Meta. It reuses
the provider's transport code through a checked copy (`cpa-status/cpa.py`
must equal root `cpa.py`; CI runs `cmp`).

- User-triggered only. One `GET credentials` per distinct root and key
  (v0 `auth-files` fallback on 404 only). No timers, no retries, 20 s
  in-memory cache.
- Before any request, take `cpa.mgmt_guard` for the root (wait at most 25 s,
  else answer busy) and skip a key `cpa.key_rejected` reports. On a 401,
  `cpa.mark_rejected` first, inside the guard. The shared latch
  `cache/cliproxyapi/locks/rejected.json` and lock files in the same `locks/`
  dir are the only files under the provider's cache dir the companion
  writes; never the per-instance caches. The 0.4 latch
  `cache/cpa-status/rejected.json` is still read, never written.
- Showing last-seen `quota.signals` that the credentials GET already returns
  is not a quota call.
- Default deny: `/cpa quota` sends only the requests listed here. Adding a
  vendor, URL, header or body needs its own plan, review and Duu's ok.
- `api-call` (v8 `requests/api-call`; v0 `api-call` only after a v8 404) for
  one purpose: a request in `QUOTA_REQUESTS`, this closed table, values fixed
  in code, no override from config or input:
  - claude: GET `https://api.anthropic.com/api/oauth/usage`
  - codex: GET `https://chatgpt.com/backend-api/wham/usage`
  - xai: GET `https://cli-chat-proxy.grok.com/v1/billing?format=credits`,
    then GET `https://cli-chat-proxy.grok.com/v1/billing`
  - antigravity: POST `v1internal:retrieveUserQuotaSummary` on
    `https://daily-cloudcode-pa.googleapis.com/`, then (only after an inner
    non-2xx or an empty or unparseable reply; an outer CPA error ends the
    account) `https://daily-cloudcode-pa.sandbox.googleapis.com/`, then
    `https://cloudcode-pa.googleapis.com/`; data `{"project":"<project_id>"}`
  - devin: POST `https://server.codeium.com/exa.seat_management_pb.SeatManagementService/GetUserStatus`
    with the fixed metadata data template (`apiKey` is `$TOKEN$`)
  - kimi: GET `https://api.kimi.com/coding/v1/usages`, or
    `https://api.kimi.ai/coding/v1/usages` when the listing says kimi-ai
  The request JSON holds only `auth_index`, `method`, `url`, `header` and,
  for antigravity and devin only, `data`; never `proxy_url` or a `Host`
  header. `$TOKEN$` only in `Authorization` or devin's `apiKey`; CPA fills it.
- One plugin route: `GET /v8/management/plugins/<quota_plugin_id>/quota?auth_index=`
  for entries whose provider is not one of the above and that CPA lists with
  `supports_quota` and a `quota_plugin_id`. No v0 fallback.
- Never: credentials download, any chat or completions call (no paid xAI
  ping), `api.x.ai/v1/me`, `loadCodeAssist`, `onboardUser`, reset routes
  (`DELETE`/`POST .../quota/reset`, `routing/cooldown/reset`), claim, profile,
  subscription, refresh or any write. Meta is unsupported (it needs the raw
  credential).
- Lockout: hold the guard once for the whole quota run (never nested). The
  probe is the first management request the run actually sends: it goes
  alone, and nothing else is sent until its status is known. Check the latch
  before every request, latch and stop on an outer 401, never retry v0 after
  a 401, never latch on a vendor (inner) 401. At most 4 requests in flight.
- `auth_index`, the Codex `id_token.chatgpt_account_id` claim, antigravity
  `project_id` and `quota_plugin_id` may be read for the entries that need
  them and held in memory only to build that request; never shown, logged or
  written to disk. Never read or show `email`, `account`, `path` or `id`, nor
  any other `id_token` claim. Mask emails and token-like runs in names,
  labels and `status_message`.
- Vendor and plugin replies: allowlisted fields only; never print a raw body
  or headers.
- At most 20 accounts and 3 requests per account per run, results cached
  60 s, only on command. The provider plugin keeps "Never api-call".
- Errors show status and category only, as in the provider.

## Secrets and transport

- Keys are referenced by env var name and read through Hermes. Never log,
  return or embed key values.
- HTTPS, or plain HTTP only when every resolved address is loopback,
  RFC1918, 100.64/10 or ULA (explicit list, not `is_private`).
- No redirects. Explicit `User-Agent: hermes-cliproxyapi-provider/<version>`
  on the plugin's own requests only; never set inference headers.
- Errors return status and category only, never bodies, URLs or headers.
- Use only the public `register_provider` / `ProviderProfile` surface. No
  `create_client`, no undocumented core imports.

## Development

Strict test-driven development. Before any push:

    python -m unittest discover -s tests -v
    hermes plugins doctor . --ci
    hermes plugins validate .
    git diff --check

Live tests are opt-in, run in a disposable HERMES_HOME, use inference and
read-only management endpoints only, and never print secrets, prompts or
private endpoints.

## Release

Code changes get one independent review before reaching main. Installing into
a live Hermes and restarting a gateway are operator actions; model-provider
plugins install with `--no-enable` and are never enabled. Conventional
Commits, imperative subject of 50 characters or fewer. Keep the repo
publish-safe: no private hostnames, IPs, pool names or home paths.

Listed in the Hermes plugin catalog as `cliproxyapi-provider`
(`plugin-catalog/cliproxyapi-provider.yaml` in NousResearch/hermes-agent). A
release reaches catalog users only when a separate pin-bump PR there merges:
one PR per release, updating `sha` and `version`, and the `description`
disclosure when network calls or the env-strip limit change. The catalog page
renders the README from the pinned commit.

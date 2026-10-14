# hermes-cliproxyapi-provider

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) model-provider
plugin for [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (CPA).

CPA is a gateway: one instance fronts many providers (Claude, Codex, Gemini,
xAI, Kimi, Devin, OpenAI-compatible upstreams, plugin providers such as Kiro)
and owns their accounts, OAuth, rotation and translation. Hermes alone sees
such a gateway as one custom endpoint on one wire with hand-pinned metadata.

This plugin mirrors CPA into Hermes instead. Point it at a CPA instance and it
registers one Hermes provider per CPA provider, each on the wire that CPA's
executor for that provider accepts with the least translation, with the model
list and per-model metadata CPA publishes. It adds nothing CPA does not serve.

## Install

```bash
hermes plugins install duu261/hermes-cliproxyapi-provider
```

Requires Hermes 0.21.7 or newer. Model-provider plugins are active once
installed; there is nothing to enable. Restart running Hermes gateways so they
load the plugin.

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
      mgmt_key_env: CPA_MAIN_MGMT_KEY     # optional, see "Exact grouping"
```

Then run `hermes model` (or anything that lists models) once. The plugin reads
the instance's model lists, registers the providers it finds, and caches them,
so later runs register them at startup with no network calls:

- `cpa-main`: every model on the instance, on the chat wire.
- `cpa-main-<provider>`: one per CPA provider, for example `cpa-main-claude`,
  `cpa-main-codex`, `cpa-main-xai`, `cpa-main-kimi`.

`hermes doctor` and multi-process setups (`plugins.isolation: host`) see
providers that appeared after startup only after a restart.

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

## Exact grouping (optional management key)

Public grouping is the default and is enough for most setups: on three live
CPA pools it put every model on the same wire as the management pass, and it
also covers API-key and OpenAI-compatible channels, which the management API
cannot see. Models are grouped by the `owned_by` label on CPA's public
`/v1/models`. That label names the model vendor, not the CPA provider, so it
can be wrong in a few cases: Devin serves Claude and GPT models labelled
`anthropic` and `openai`, `google` covers several Gemini executors, and an id
served by two providers shows only one owner.

If you run into those cases, set `mgmt_key_env`. The plugin then asks CPA's
management API which provider serves each model (models it cannot see keep
their public group) and reads CPA's model definitions for output limits,
vision and reasoning levels. It is read-only and bounded:

- GET only, to the credentials list, a few sampled accounts' model lists per
  provider, and the model-definitions list per provider. Never quota, usage,
  downloads, OAuth or any write.
- At most 32 requests and 20 seconds per pass, at most one pass per 15 minutes
  per instance, never on the request path.
- CPA blocks an address after five failed management logins. The plugin stops
  at the first 401 and never retries that key against that CPA, from any
  instance or across restarts, until the key changes. If the cache cannot be
  written, it says so in a warning, because the restart latch lives there. Any
  other failure falls back to public grouping.
- CPA must allow management from the plugin's address (`remote-management`
  in CPA's config) unless both run on the same host.
- A CPA management key can do far more than these reads (change config, add
  or remove accounts). The plugin never sends anything but the GETs above, but
  anyone who can read the env var can. Leave `mgmt_key_env` unset if public
  grouping is close enough.

## Overrides

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
          api_mode: chat_completions      # how this gateway channel expects requests
```

- Profiles point at `route_via` and use the gateway key. The CPA key is used
  only for CPA's model lists and never sent to the gateway; the gateway key is
  never sent to CPA. They must be different env vars.
- Only models the gateway lists for that key (`GET /v1/models`) are kept. If
  the gateway list fails, the last known list is kept; with none, nothing is
  routed for that key.
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
  the CPA `key_env` (needed for discovery) stays visible to agent shells on a
  routed instance. Hermes has no plugin hook to declare a secret without making
  it an inference credential yet.

## Metadata

Context window, vision, reasoning support, reasoning-effort levels and output
limits come only from CPA: management model definitions when available, else
CPA's public model lists matched by exact id. Values CPA does not publish stay
unknown. Hermes can add vendor metadata from models.dev:

```yaml
providers:
  cpa-main-claude:
    catalog_provider: anthropic
```

Precedence, highest first: your `model_overrides`, this plugin, models.dev.

## Security

- Keys are referenced by environment variable name and resolved by Hermes.
- Plain `http://` is allowed only when every address the host resolves to is
  loopback, RFC 1918, 100.64/10 or IPv6 ULA. Anything else needs `https://`.
- No redirects, capped responses, and failures reported as an HTTP status and
  category only, never bodies, URLs or keys.
- Catalog requests send `User-Agent: hermes-cliproxyapi-provider/<version>`.
  One extra `/v1/models` request appends `grok-shell-compatible`, because that is
  the client CPA publishes context windows and reasoning levels to; its ids are
  matched against the normal list, so it adds metadata, never models. Inference
  requests carry Hermes' own headers; the plugin sets none.
- The cache at `$HERMES_HOME/cache/cliproxyapi/` (mode 0600) holds model ids,
  owners, provider membership and metadata. No keys, no account names. A
  rejected management key is remembered as a salted HMAC, not the key.

## Development

```bash
python -m unittest discover -s tests -v          # core tests, no Hermes needed
PYTHONPATH=/path/to/hermes-agent python -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate .
```

## License

MIT

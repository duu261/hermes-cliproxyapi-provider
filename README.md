# hermes-cliproxyapi-provider

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) model-provider
plugin for [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) (CPA).

CPA is a gateway: one instance fronts many providers (Claude, Codex, Gemini,
xAI, OpenAI-compatible upstreams and plugin providers) and owns their accounts,
OAuth, rotation and translation. Hermes sees a gateway like that as one generic
custom endpoint with one wire format and hand-pinned model metadata.

This plugin mirrors CPA's own provider groups into Hermes instead. Each
(CPA instance x provider group) becomes a Hermes provider that speaks the wire
format that group's upstream speaks, so CPA translates as little as possible,
and each model carries the metadata CPA publishes for it.

## What you get

- One Hermes provider per declared group, for example `cpa-main-claude` on the
  Anthropic Messages wire and `cpa-main-codex` on the Responses wire, both
  behind the same CPA URL and key.
- Live model lists from the CPA instance, filtered to each group's patterns.
- Per-model context window, vision, reasoning support, reasoning-effort levels
  and output limits, taken from CPA's public model lists where CPA publishes
  them. Values CPA does not publish stay unknown; the plugin never guesses.
- A `cpa-local` provider for a local CPA on the chat wire. It is always
  registered, alongside any declared groups, and stays inert until its key is set.
- Any number of CPA instances.
- `plugins.isolation: host` works, with one limit: Hermes caches the extracted
  providers by plugin files, so after editing the `cliproxyapi` section, delete
  `$HERMES_HOME/cache/plugin_host/model-providers/cliproxyapi-provider-*.json`.

## Install

```bash
hermes plugins install duu261/hermes-cliproxyapi-provider
```

Restart running Hermes gateways afterwards so they load the new providers.

## Quick start (zero config)

Set `CPA_LOCAL_API_KEY` in `~/.hermes/.env` to a CPA client API key. The
`cpa-local` provider talks to `http://127.0.0.1:8317/v1` on the chat wire
(CPA translates to each upstream). Override the URL with `CPA_LOCAL_BASE_URL`.

```bash
hermes -z "hello" --provider cpa-local -m <model-id>
```

## Native-wire groups

Declare instances and groups in `~/.hermes/config.yaml`:

```yaml
cliproxyapi:
  instances:
    main:
      base_url: http://127.0.0.1:8317     # with or without /v1
      key_env: CPA_MAIN_API_KEY           # env var holding a CPA client key
      groups:
        claude:
          models: ["claude-*"]
        codex:
          models: ["gpt-*"]
        gemini:
          models: ["gemini-*"]
```

This registers `cpa-main-claude`, `cpa-main-codex` and `cpa-main-gemini`.
Group names follow CPA's provider names. `models` are exact ids or shell-style
patterns matched against the instance's live model list.

Wire per group (override with `api_mode:` on a group):

| Group | Hermes `api_mode` | Why |
|---|---|---|
| `claude` | `anthropic_messages` | CPA's Claude executor speaks Anthropic Messages |
| `codex` | `codex_responses` | CPA's Codex executor speaks Responses |
| anything else | `chat_completions` | Hermes has no other wire a plugin can select |

Notes:

- Gemini runs on chat, so CPA translates chat to Gemini. Hermes only uses its
  native Gemini client for Google's own hosts.
- xAI upstreams speak Responses. `api_mode: codex_responses` on an `xai` group
  avoids a translation; measure both before switching.
- Models that two groups both serve appear in both. CPA, not this plugin,
  decides which account answers.

## Vendor metadata from models.dev (optional)

Hermes can also take public vendor metadata from models.dev. Point a group's
provider at the vendor whose models it serves:

```yaml
providers:
  cpa-main-claude:
    catalog_provider: anthropic
```

Precedence, highest first: your own `model_overrides`, then this plugin's CPA
metadata, then models.dev.

## Security

- Keys are referenced by environment variable name only. Hermes resolves them.
- Plain `http://` is allowed only when every address the host resolves to is
  loopback, private (RFC 1918), shared CGNAT space (100.64/10) or IPv6 ULA.
  Anything else needs `https://`.
- Catalog requests refuse redirects, cap response size, and report failures as
  an HTTP status and category, never bodies or URLs.
- This version calls only CPA's public, client-key endpoints. Opt-in discovery
  through the CPA management API is planned; it will stay off by default.
- The model catalog is cached under `$HERMES_HOME/cache/cliproxyapi/` with mode
  0600. It holds model ids and metadata, no keys.

## Development

```bash
python -m unittest discover -s tests -v          # core tests, no Hermes needed
PYTHONPATH=/path/to/hermes-agent python -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate .
```

## License

MIT

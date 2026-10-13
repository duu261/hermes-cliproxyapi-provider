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

## Grouping (which models belong to which provider)

1. Management mode (instance sets `mgmt_key_env`): provider per model from the
   CPA management API, see Management below.
2. Public mode (client key only): group the public `GET /v1/models` list by
   `owned_by`, mapped to CPA provider keys via CPA's static catalogs
   (anthropic->claude, openai->codex, xai->xai, meta->meta, moonshot->kimi,
   antigravity->antigravity, google->gemini). Any other label is used as is
   (openai-compat names, plugin providers). Document the limits: Devin labels
   models by vendor, `google` covers several executors, an id served by two
   providers shows one owner.
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
- codex, xai, meta -> codex_responses (CPA reshapes to its Codex format)
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

## Management

Opt-in per instance via `mgmt_key_env`. Prior art (CPA Usage Keeper, AI-gateway,
cpa-usage, oh-my-cpa) holds the same key; match their safety.
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
- Parse responses into an allowlist of fields and drop the rest immediately;
  never log, cache or return key-bearing fields or bodies.
- Same transport rules as the client key.

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

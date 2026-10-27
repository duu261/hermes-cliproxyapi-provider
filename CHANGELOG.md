# Changelog

## Unreleased

- Docs: explain why `xai` stays on `codex_responses` (Grok 4.7 needs its
  encrypted reasoning replayed; Chat Completions cannot carry it). Override
  examples no longer put `xai` on `chat_completions`.

## 0.3.3

- Management mode reads model definitions for plugin providers (Kiro and
  others), not only CPA's built-in providers, so they get context window,
  max output, image input and reasoning levels. Needs CPA v8.0.34 or later;
  older CPA answers "unknown channel" and those providers stay as before.
  Built-in providers are read first, inside the same 32-request cap, and
  keep their definitions when a plugin provider serves the same model id.

## 0.3.2

- Billing route: routed providers register at startup from CPA's cached list
  before the gateway's list has ever been read, instead of only after the
  first refresh. Once a gateway list is known (even empty) it filters as
  before. Until then, a model the key does not sell fails at the gateway.

## 0.3.1

- Max output without a management key: one more public request,
  `GET /v1/models` with `Anthropic-Version`, reads CPA's output limit for every
  model. CPA's 64,000 fill-in for models with no limit is ignored, so those
  fall back to models.dev. Cloaked ids are decoded back to the served id.

## 0.3.0

- Billing route: optional `route_via` and `route_key_env` per instance (and
  `route_key_env` per group) send inference through an operator's gateway in
  front of the same CPA, such as New API. Discovery and metadata stay on CPA;
  only models the gateway lists for the key are registered. The CPA and gateway
  keys never cross.
- Known limit: on a routed instance Hermes does not strip the CPA `key_env`
  from agent subprocesses, because routed providers declare only the gateway
  key. `mgmt_key_env` has never been stripped (unchanged since 0.2.0).
- Public grouping is the documented default; the management key stays opt-in
  for Devin and multi-provider ids.

## 0.2.1

- Read `config.yaml` with `ruamel.yaml`, the parser Hermes depends on, instead of
  importing Hermes' private `hermes_yaml` module (plugin catalog rule 9).

## 0.2.0

Breaking: providers now come from what the CPA instance serves, not from
hand-declared groups.

- One Hermes provider per CPA provider found on the instance (`cpa-<instance>-<provider>`),
  plus `cpa-<instance>` with every model on the chat wire. Providers found after
  startup are registered in the running process and cached for the next start.
- Wire map from CPA v8.0.27 executors: `claude`, `kimi`, `kiro` on Anthropic Messages;
  `codex`, `xai`, `meta` on Responses; everything else on chat completions.
- Public grouping by `/v1/models` `owned_by`. Optional exact grouping and model
  definitions through a read-only, bounded management pass (`mgmt_key_env`) that stops
  at the first 401 and never retries a rejected key.
- `groups:` is now optional and only overrides `api_mode`, `models` or `display_name`.
- `cpa-local` is registered only when no instances are configured.
- Cache format v2; v1 caches are read.

## 0.1.1

- Manifest v2 metadata: `requires_hermes: ">=0.21.7"` (`hermes plugins install` refuses older
  Hermes; a manually copied plugin directory is not checked), license, homepage and tags.
- Test that `plugin.yaml`, `pyproject.toml` and the catalog User-Agent carry one version.
- README: when to use this plugin instead of a Hermes custom provider, and how
  `hermes doctor` checks each provider.
- SECURITY.md.

## 0.1.0

- One Hermes provider per CLIProxyAPI instance and provider group: `claude` on Anthropic
  Messages, `codex` on Responses, other groups on chat completions.
- Live model lists filtered per group, with context window, vision, reasoning levels and
  output limits from CLIProxyAPI's public model lists. Cached at 0600, no keys.
- Zero-config `cpa-local` provider.
- Plain HTTP only to loopback, private, shared CGNAT or ULA addresses; no redirects; capped
  responses; errors carry no URLs, bodies or keys.

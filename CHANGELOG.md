# Changelog

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

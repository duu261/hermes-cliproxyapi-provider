# Hermes CLIProxyAPI Provider - Agent Instructions

## Scope

Standalone Hermes Agent `kind: model-provider` plugin. It mirrors CLIProxyAPI
(CPA) provider groups into Hermes: one provider profile per (CPA instance x
CPA provider group), declared in operator config. CPA owns accounts, OAuth,
rotation, routing and translation. This plugin owns only the small map from
CPA provider group to Hermes `api_mode`. Do not add a model table, routing,
retries across instances, or account logic.

## Wire map

Send each group the wire its CPA executor speaks upstream, so CPA translates
as little as possible. Derive each row from CPA source, not notes.

- claude -> anthropic_messages
- codex -> codex_responses
- xai -> open (Responses vs chat, settle with a timed A/B)
- gemini -> chat_completions (forced translation: Hermes picks its native
  Gemini client only for Google hosts, and plugins may not supply clients)
- openai-compatibility -> chat_completions, one group, skipped when empty
- unknown groups -> chat_completions, skipped when empty

## Metadata

Precedence: models.dev catalog (user sets `catalog_provider` per profile),
then this plugin's `model_capabilities`, then user `model_overrides`.
Fill `model_capabilities` only from CPA's own data: the public model lists
(client key) or, when opted in, management model definitions. Match by exact
model id. Never invent a value.

## Audience

Built for any CPA operator, not one deployment. Mixed-vendor instances,
many instances, and empty or broken groups are normal inputs.

## Secrets and transport

- Keys are referenced by env var name and read through Hermes. Never log,
  return or embed key values.
- Management API: opt-in discovery only (`discovery: management`). Exact
  allowlist of read-only GET paths; never `api-call` or any write method.
  Send the key only to loopback, RFC1918, 100.64/10 or ULA addresses.
  Discovery-time only, cached, never on the inference path. Never retry
  after 401/403 (CPA bans the client IP after 5 failures); fall back to
  declared groups.
- Remote endpoints need HTTPS. Plain HTTP only for loopback, RFC1918,
  100.64/10 and ULA addresses (explicit list, not `is_private`).
- No redirects. Explicit `User-Agent: hermes-cliproxyapi-provider/<version>`.
- Errors return status and category only, never bodies, URLs or headers.
- Use only the public `register_provider` / `ProviderProfile` surface. No
  `create_client`, no undocumented core imports.

## Development

Strict test-driven development. Before any push:

```bash
python -m unittest discover -s tests -v
hermes plugins doctor . --ci
hermes plugins validate .
git diff --check
```

Live tests are opt-in by env flag, use inference endpoints only, and never
print secrets, prompts or private endpoints.

## Release

Code changes get one independent review before reaching main. Installing into
a live Hermes and restarting a gateway are operator actions. Conventional
Commits, imperative subject of 50 characters or fewer. Keep the repo
publish-safe: no private hostnames, IPs, pool names or home paths.

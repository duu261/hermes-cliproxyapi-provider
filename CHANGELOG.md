# Changelog

## 0.6.0

- `/cpa quota` covers every provider CPA's web UI shows quota for, except
  Meta: xAI (weekly credits; monthly spend and plan when xAI reports a
  monthly limit), Antigravity (per model group),
  Devin, Kimi, and any provider whose CPA quota plugin serves quota (e.g.
  Kiro), next to Claude and Codex. Each uses the web UI's own requests; none
  costs money, uses up quota, downloads an account file or resets anything.
  `/cpa quota <pool> <provider>` takes any of them, `plugin`, or a plugin's
  provider name.
- The probe is now one request, not one account: accounts that send nothing
  (e.g. Antigravity without a project id) do not count, and an account's
  second or third request waits until CPA has accepted the key. At most 3
  requests per account and 4 in flight per run.
- AGENTS.md lists every allowed quota request; anything else stays banned.
- Antigravity accounts are `OUT` only when every model group is full, because
  CPA holds back only the models that ran out; a full group shows as
  `out: <group>`.
- Fix: on a CPA without the v8 `api-call` route, two requests that both got
  a 404 at once could stop a healthy run with "no route". Each request now
  keeps its own path.

## 0.5.0

- `/cpa quota <pool> [claude|codex]`: live usage per Claude and Codex
  account, read from each vendor the way CPA's own web UI quota page does
  (CPA `api-call` to `api.anthropic.com/api/oauth/usage` or
  `chatgpt.com/backend-api/wham/usage` with the account's token). Raw vendor
  percent per window and a count of accounts with room, out and failed. On
  command only, at most 20 accounts per run, cached 60 seconds.
- Both plugins share one management lock per CPA (thread lock plus a lock
  file under `$HERMES_HOME/cache/cliproxyapi/locks/`) and one remembered list
  of rejected keys, so the provider's refresh, `/cpa` and every Hermes
  process on one home never talk to CPA's management API at the same time.
  The provider skips a pass while `/cpa` is busy and retries on the next
  refresh. Rejections remembered by 0.4 are still honored.
- README: new section "Never get locked out of CPA" (how CPA counts failed
  management logins and what to do about it).
- `hermes-pack.yaml`: install the provider and `/cpa` in one step, both
  pinned to the 0.4.0 release commit (`hermes plugins pack install`).
- `/cpa`: Codex quota windows are labelled from their own length
  (`Window-Minutes`), not their position. On Pro plans the first window is
  the weekly one, so `/cpa` showed weekly use as `5h` plus a fake `wk 0%`.
  Windows with no length are skipped.
- `/cpa`: an account whose last-seen 5h or weekly window is at 100% shows as
  `limited until HH:MM` instead of `ready`, and full windows show when they
  free again. Counted per provider as `N limited (next HH:MM)`.

## 0.4.0

- Docs: explain why `xai` stays on `codex_responses` (Grok 4.7 needs its
  encrypted reasoning replayed; Chat Completions cannot carry it). Override
  examples no longer put `xai` on `chat_completions`.
- New companion plugin `cpa-status` (subdirectory `cpa-status/`, `kind:
  standalone`): `/cpa` shows account health per instance, provider and
  account from one read-only management GET per instance, only when typed.
  Last-seen 5h and weekly quota for Claude and Codex accounts, masked names,
  and a persisted rejected-key latch that also honors the provider's.
  Install with `#cpa-status` and enable it; the provider is unchanged.

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

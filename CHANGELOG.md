# Changelog

## 0.8.0

- `/cpa <pool> <provider>`: a cooling account shows why in words, not as
  CPA's code: `vendor limit reached` (`quota`), `vendor limit reached, all
  models` (`credential_quota`), `vendor rate limited`
  (`transient_rate_limit`), `token rejected by vendor` (`unauthorized`),
  `refresh token rejected` (`invalid_grant`), `access refused or no credit`
  (`payment_required`: a 402, a 403 or a low balance), `model not supported`,
  `Cloudflare challenge`, `vendor HTTP 404` (`not_found`) and `temporary
  vendor error` (`transient_error`). CPA's `unknown` shows no reason. Text
  that is not one of these codes is shown as before, masked.
- `/cpa quota` Codex: the day the subscription ends is shown after the plan
  (`pro until Nov 16`), with the year when it is not this year. It is read
  from CPA's account listing (the `chatgpt_subscription_active_until` claim of
  the ID token CPA stores), so it costs no request and is as old as that
  token; a date that has passed shows as `until Oct 03 (past)`. No other
  claim of that token is read for display.
- `/cpa <pool> <provider>`: each account line shows the account's priority
  when CPA lists one (`prio 1`) and its successful/failed requests since CPA
  started (`total 2140/96`), next to the last hour (`1h 173/71`). The header
  says `total = since CPA start` when a total is shown: CPA keeps these counts
  in memory only, so they start again at zero when CPA restarts. No new
  request: the fields come with the listing `/cpa` already reads.
- `/cpa quota` xAI: no plan name is shown. Before, a monthly limit equal to
  one of two fixed amounts was shown as `supergrok` or `supergrok_heavy`.
  xAI sends no plan name and has changed its plans, so that was a guess.

## 0.7.4

Fixes only. No new requests.

- `/cpa`: a Google Cloud project id that CPA repeats in an account's label
  (`<project_id> (<email>)` on Vertex accounts) or file name
  (`vertex-<project_id>.json`) is replaced by `…`, and so is the account's
  project id in an error message or a cooldown reason. Before, it was shown
  in full.
- `/cpa`: an account that needs a new sign-in but still serves on its current
  access token shows `active · sign in again (serving until 17:20)` and
  counts as active, with `1 needs sign-in` on the pool line. Once that token
  has expired it shows `error: sign in again (expired)`. Before, both showed
  as an error. Needs CPA's `renewal_state` field; without it the account
  shows `error: sign in again` as before.
- `/cpa`: an account whose last-seen usage window is at 100% with no known
  reset time shows `limited`, not `active`.
- `/cpa`: URLs, `host:port` pairs, file paths, bare host names and IPv4 and
  IPv6 addresses in account messages are masked, on a best effort: the
  patterns cover the common forms, not every one. A host name with no dot is
  masked only where an error gives it as an address (`dial tcp myhost:8080`,
  `lookup myhost`). A message that CPA cut short (it ends in `...`) loses the
  word at the cut, which could be half a host name, an address or an email.
  Before, only emails and long token-like runs were masked.
- `/cpa`: an email address that is cut off after the `@` (`carol@exam`), as
  CPA can leave it in a shortened error, is masked like a whole one. Before,
  it was shown. Any other name in front of an `@` is masked the same way.
- `/cpa quota`: a run sends no new request after 60 seconds, and each
  request's timeout is cut to the time left. Accounts it did not reach show
  `not checked`, the second line says so, and that result is not kept in
  memory. Before, when no reply came back, the run tried every account one
  at a time and could hold the CPA's management lock for 5 minutes, during
  which other `/cpa` commands answered busy.
- `/cpa quota` xAI: client version `1.0.70`, as in CPA's web UI.
- `/cpa quota` Codex: hyphenated plans (`pro-lite`, `pro-max`) show their
  plan (`pro_lite`). Before, the plan was left out.
- CI tests Hermes v0.23.1 instead of v0.22.5.

## 0.7.3

- `/cpa`: an instance without `mgmt_key_env` on the same CPA `base_url` as
  one with it (for example the same CPA, once direct and once billed through
  a gateway) is that CPA, not another pool. The overview shows it under the
  managed instance as `same CPA: <name> (routed)` instead of
  `<name>: no management key`. `/cpa <name>` and `/cpa quota <name>` answer
  `same CPA as <managed>, see /cpa <managed>`. No new requests; the keyless
  instance never sent any.

## 0.7.2

- A new instance on the same CPA `base_url` as an instance already cached now
  registers its providers at startup from that cache. Before, it started with
  no models until its first listing, so a new routed instance's providers
  (for example `cpa-<new>-claude`) were unknown and `model.provider` pointing
  at one failed. The borrowed copy keeps what CPA serves (ids, owners,
  metadata, and management groups only when the new instance sets
  `mgmt_key_env`). It drops the other instance's gateway lists, management
  check time and rejected-key fingerprint, and is never written under the new
  name: the first successful listing writes the new instance's own cache.
- README "Billing route": routed providers register at startup only from a
  cache for that CPA; a routed instance on a CPA nothing has listed yet needs
  one listing first.

## 0.7.1

- `/cpa`: an account CPA marks `requires_sign_in` (CPA v8.0.5x, the refresh
  token is gone) shows `error: sign in again` instead of a cooldown or a
  vague message. Re-login on that CPA instance is the only fix.
- `/cpa quota` Claude: extra usage adds spend and limit when Claude sends
  them (`extra 25% ($12.50/$50.00)`, minor units as in CPA's web UI), and
  dollar credit pools show what is left (`additional credit balance $60.00
  left`).
- `/cpa quota` Codex: the account's credits balance when it has one
  (`credits 1,250.50`, `credits unlimited`).
- All from the replies `/cpa` already reads; no new requests.
- README intro and pack description name both plugins.

## 0.7.0

- `/cpa` shows each pool's CPA version (from the `X-CPA-VERSION` header CPA
  already sends on the credentials reply, no extra request) and, on the
  plain overview only, `CPA <version> available (running <versions>)` when
  a pool runs an older release. The check is CPA's own read-only
  `server/latest-version` route: one request per Hermes process every 6
  hours (10 minutes after a failure), and a failure only hides the hint.
- `/cpa` says `active` instead of `ready`, CPA's own word for the state.
- `/cpa quota` Claude: Fable's weekly limit (`fable`), the OAuth apps and
  Cowork weekly limits when Claude sends them, and extra usage when it is on
  (`extra 37%`, `extra limit reached`). Model-scoped limits (Fable, Opus,
  Sonnet, apps, Cowork) never mark the account `OUT`: a full one shows as
  `out: fable` while the account keeps serving other models. All from the
  same usage request as before.
- `/cpa quota`: an xAI account whose week is running but has no percent yet
  (xAI leaves it out until the account has used enough) shows
  `wk no figure yet → <reset>` and counts as having room. It used to count
  as failed with `no quota data`. No percent is ever made up.
- `/cpa quota xai` (a provider where the pool goes) now says to use
  `/cpa quota <pool> xai` instead of "Unknown pool".
- README: minimum CPA version per feature.

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

# cpa-status

`/cpa` for [Hermes Agent](https://github.com/NousResearch/hermes-agent): account
health for every [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
(CPA) pool in your config, and live per-account usage on request.
Companion to the
[`cliproxyapi-provider`](https://github.com/duu261/hermes-cliproxyapi-provider)
model-provider plugin; it reads the same `cliproxyapi:` section.

```bash
hermes plugins install cpa-status
hermes plugins enable cpa-status
```

From the [Hermes plugin catalog](https://hermes-agent.nousresearch.com/docs/plugins/cpa-status)
(reviewed pin). Development install, latest `main`:
`hermes plugins install duu261/hermes-cliproxyapi-provider#cpa-status`.

Requires Hermes 0.21.7 or newer and `mgmt_key_env` on each instance you want
to see (an instance without it on the same CPA `base_url` as one with it is
shown under that one as `same CPA`). Restart running gateways after enabling.
Upgrade it together with the provider plugin: both share one lock per CPA.

```
/cpa                       every instance: accounts per provider
/cpa <pool>                one instance, plus the last hour of requests
/cpa <pool> <provider>     one line per account
/cpa quota <pool> [provider]   live usage from each vendor
```

- `/cpa`: one read-only `GET /v8/management/credentials` per instance, only
  when you type it. No timers, no retries; repeats within 20 seconds use
  memory. Last-seen 5h and weekly quota for Claude and Codex accounts, as CPA
  recorded it on real traffic. `/cpa <pool> <provider>` also shows each
  account's priority when CPA lists one (`prio 1`) and its successful/failed
  requests since CPA started (`total 2140/96`).
- `/cpa quota`: like CPA's web UI quota page, for Claude, Codex, xAI,
  Antigravity, Devin and Kimi accounts through CPA's `api-call` route with the
  web UI's fixed requests (the account's token filled in by CPA), and for
  other providers through their CPA quota plugin (e.g. Kiro). A Codex line
  also shows the day the subscription ends (`until Nov 16`) when CPA's
  listing has it, at no extra request. On Telegram, Discord, Slack, Matrix
  and Mattermost the reply is a code block (Hermes 0.22.3 or later). Nothing paid,
  no downloads, no resets; the full request list is in the
  [README](../README.md#cpa-quota-live-usage). At most 20 accounts and 3
  requests per account per run, first request alone, then 4 at a time;
  repeats within 60 seconds use memory. A run sends no new request after
  60 seconds.
- Emails are masked. URLs, host names, IP addresses and file paths in
  messages are masked on a best effort: the common forms, not every one, and
  a host name with no dot only where an error gives it as an address
  (`dial tcp myhost:8080`, `lookup myhost`). `email`, `account` and `path`
  are never read. The account's project id is replaced by `…` where CPA
  repeats it in the label or file name, and where a message names it. CPA's
  account index, the Codex account id, the Antigravity project id and the CPA
  plugin id are used only to build the quota requests, never shown or stored.
- Idle chats only: sent during an agent turn it goes to Hermes' busy handling.
- The management key is full admin on CPA, and Hermes does not strip it from
  agent shells. CPA bans an IP after five failed management keys in a row; a
  rejected key is never sent again (salted HMAC in
  `$HERMES_HOME/cache/cliproxyapi/locks/rejected.json`). Read
  [Never get locked out of CPA](../README.md#never-get-locked-out-of-cpa).

Full documentation: the repository [README](../README.md#cpa-pool-status-companion-plugin).

# cpa-status

`/cpa` for [Hermes Agent](https://github.com/NousResearch/hermes-agent): account
health for every [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
(CPA) pool in your config, and live per-account usage on request.
Companion to the
[`cliproxyapi-provider`](https://github.com/duu261/hermes-cliproxyapi-provider)
model-provider plugin; it reads the same `cliproxyapi:` section.

```bash
hermes plugins install duu261/hermes-cliproxyapi-provider#cpa-status
hermes plugins enable cpa-status
```

Requires Hermes 0.21.7 or newer and `mgmt_key_env` on each instance you want
to see. Restart running gateways after enabling. Upgrade it together with the
provider plugin: both share one lock per CPA.

```
/cpa                       every instance: accounts per provider
/cpa <pool>                one instance, plus the last hour of requests
/cpa <pool> <provider>     one line per account
/cpa quota <pool> [provider]   live usage from each vendor
```

- `/cpa`: one read-only `GET /v8/management/credentials` per instance, only
  when you type it. No timers, no retries; repeats within 20 seconds use
  memory. Last-seen 5h and weekly quota for Claude and Codex accounts, as CPA
  recorded it on real traffic.
- `/cpa quota`: like CPA's web UI quota page, for Claude, Codex, xAI,
  Antigravity, Devin and Kimi accounts through CPA's `api-call` route with the
  web UI's fixed requests (the account's token filled in by CPA), and for
  other providers through their CPA quota plugin (e.g. Kiro). Nothing paid,
  no downloads, no resets; the full request list is in the
  [README](../README.md#cpa-quota-live-usage). At most 20 accounts and 3
  requests per account per run, first request alone, then 4 at a time;
  repeats within 60 seconds use memory.
- Emails are masked; `email`, `account` and `path` are never read. CPA's
  account index, the Codex account id, the Antigravity project id and the CPA
  plugin id are used only to build the quota requests, never shown or stored.
- Idle chats only: sent during an agent turn it goes to Hermes' busy handling.
- The management key is full admin on CPA, and Hermes does not strip it from
  agent shells. CPA bans an IP after five failed management keys in a row; a
  rejected key is never sent again (salted HMAC in
  `$HERMES_HOME/cache/cliproxyapi/locks/rejected.json`). Read
  [Never get locked out of CPA](../README.md#never-get-locked-out-of-cpa).

Full documentation: the repository [README](../README.md#cpa-pool-status-companion-plugin).

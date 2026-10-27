# cpa-status

`/cpa` for [Hermes Agent](https://github.com/NousResearch/hermes-agent): account
health for every [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
(CPA) pool in your config. Companion to the
[`cliproxyapi-provider`](https://github.com/duu261/hermes-cliproxyapi-provider)
model-provider plugin; it reads the same `cliproxyapi:` section.

```bash
hermes plugins install duu261/hermes-cliproxyapi-provider#cpa-status
hermes plugins enable cpa-status
```

Requires Hermes 0.21.7 or newer and `mgmt_key_env` on each instance you want
to see. Restart running gateways after enabling.

```
/cpa                    every instance: accounts per provider
/cpa <pool>             one instance, plus the last hour of requests
/cpa <pool> <provider>  one line per account
```

- One read-only `GET /v8/management/credentials` per instance, only when you
  type `/cpa`. No timers, no retries; repeats within 20 seconds use memory.
- Last-seen 5h and weekly quota for Claude and Codex accounts, as CPA recorded
  it on real traffic. `/cpa` never asks CPA to check quota upstream.
- Emails are masked; `email`, `account`, `id_token`, `project_id` and `path`
  are never read.
- Idle chats only: sent during an agent turn it goes to Hermes' busy handling.
- The management key is full admin on CPA, and Hermes does not strip it from
  agent shells. After CPA rejects a key, `/cpa` never sends it again (salted
  HMAC in `$HERMES_HOME/cache/cpa-status/rejected.json`); CPA bans an IP after
  five failures, so fix a rejected key before retrying from other profiles.

Full documentation: the repository [README](../README.md#cpa-pool-status-companion-plugin).

# Contributing

Thanks for helping. Issues and pull requests are welcome; questions go to
[Discussions](https://github.com/duu261/hermes-cliproxyapi-provider/discussions)
and security problems to [SECURITY.md](SECURITY.md).

## Safety rules

These plugins talk to a CPA management API that bans a client IP for 30 minutes
after five bad keys, and they handle keys and account tokens. Every change keeps
these rules; a pull request that breaks one is not merged:

- Management calls are read-only GETs on the documented list, plus the
  `api-call` route that `/cpa quota` uses for its fixed vendor usage URLs. No
  writes, no new endpoints without an issue first.
- Nothing calls the management API on a timer, at startup or from a model
  catalog refresh beyond what the provider already does. `/cpa` and
  `/cpa quota` run only when typed.
- A 401 from CPA management stops at once and is remembered; that key is never
  sent to that CPA again. Every management call holds the shared guard
  (`cpa.mgmt_guard`) and checks `cpa.key_rejected` first.
- Keys, tokens, account emails, `auth_index` and account ids are never printed,
  logged or cached. Errors show a status and a category, never bodies, URLs or
  headers.
- No redirects, no plain `http://` to a host that is not loopback or private.

[AGENTS.md](AGENTS.md) holds the full rules for coding agents and humans
alike. Only the maintainer edits it: pull requests that change it are closed
automatically. If a rule blocks a good change, open an issue.

## Setup and tests

Python 3.11 or newer. The core tests need nothing else:

```bash
python -m unittest discover -s tests -v
python -m unittest discover -s cpa-status/tests -v
cmp cpa.py cpa-status/cpa.py
ruff check . && ruff format --check .
```

`cpa-status/cpa.py` is a byte-for-byte copy of `cpa.py`, because Hermes loads
each plugin on its own. Change `cpa.py`, then copy it over; CI fails when they
differ. With a Hermes checkout, also run the plugin checks from the README's
Development section.

Write the test first. Tests use local fake servers only: never point a test at
a real CPA, vendor or key.

## Pull requests

- One topic per pull request, based on `main`.
- Title as a Conventional Commit of 50 characters or fewer, for example
  `fix(cpa-status): mask emails in errors`. Pull requests are squash-merged, so
  the title becomes the commit.
- Update `CHANGELOG.md` under `Unreleased` and the README when behavior
  changes. English is canonical; the translated READMEs are updated by the
  maintainer.
- No keys, emails, hostnames or IPs anywhere: code, tests, docs or the pull
  request text.

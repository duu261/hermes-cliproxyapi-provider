# Security Policy

## Reporting a vulnerability

Please report security issues privately through
[GitHub private vulnerability reporting](https://github.com/duu261/hermes-cliproxyapi-provider/security/advisories/new).
Do not open a public issue for a vulnerability.

Include the plugin version, your Hermes version, your CLIProxyAPI version, and steps to
reproduce. Never include real API keys, gateway URLs or account details; use placeholders such
as `gateway.example`.

## Scope

In scope:

- Leaking a CLIProxyAPI client or management key (logs, error messages, tracebacks, cache
  files).
- Plain-HTTP requests outside the documented private-network list.
- Requests that follow redirects or read unbounded responses.
- Any management request outside the documented allowlist (read-only GETs, plus the
  `api-call` route for `/cpa quota` with its two fixed vendor usage URLs), or any retry of
  a management key CPA rejected, or any other path that lets the plugins cause CPA's
  management IP ban (see "Never get locked out of CPA" in the README).
- Account identifiers (`auth_index`, account id, email) shown, logged or written to disk.

Out of scope:

- Behavior of CLIProxyAPI itself or of the upstream model providers.
- Inference requests, which Hermes sends with its own clients.

## Supported versions

Only the latest release receives fixes.

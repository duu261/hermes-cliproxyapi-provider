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

- Leaking a CLIProxyAPI client key (logs, error messages, tracebacks, cache files).
- Plain-HTTP catalog requests outside the documented private-network list.
- Catalog requests that follow redirects or read unbounded responses.
- Any call to the CLIProxyAPI management API.

Out of scope:

- Behavior of CLIProxyAPI itself or of the upstream model providers.
- Inference requests, which Hermes sends with its own clients.

## Supported versions

Only the latest release receives fixes.

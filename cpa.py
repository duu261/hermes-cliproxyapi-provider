"""CLIProxyAPI bridge core: config, transport policy, catalogs, grouping, cache.

Pure standard library. Nothing here imports Hermes, so it is unit-testable and
safe to import during Hermes provider discovery.

Source references are to CLIProxyAPI (CPA) v8.0.27 unless noted.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlsplit

VERSION = "0.3.2"
USER_AGENT = f"hermes-cliproxyapi-provider/{VERSION}"
# CPA serves its richest public list (context window, reasoning levels) only to
# Grok Shell clients: internal/client/grokbuild IsGrokShellUserAgent.
GROK_LIST_USER_AGENT = f"{USER_AGENT} grok-shell-compatible"
# Anthropic-format /v1/models (internal/api/server_routes.go isAnthropicModelsRequest)
# carries max_tokens for every model. CPA fills DefaultClaudeMaxOutputTokens when a
# model has none, and cloaks ids that do not start with "claude-" as this prefix
# plus the id reversed (internal/client/claude/models EnsureClaudeModelIDPrefix).
ANTHROPIC_LIST_HEADERS = {"Anthropic-Version": "2023-06-01"}
CPA_DEFAULT_MAX_TOKENS = 64000
CLOAKED_ID_PREFIX = "claude-fable-5-dd-"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
# The credentials list carries per-account request history; large pools need room.
MAX_MANAGEMENT_LIST_BYTES = 32 * 1024 * 1024
PROFILE_PREFIX = "cpa"

ANTHROPIC = "anthropic_messages"
RESPONSES = "codex_responses"
CHAT = "chat_completions"
VALID_API_MODES = (ANTHROPIC, RESPONSES, CHAT)
DEFAULT_WIRE = CHAT

# CPA provider -> the Hermes api_mode its executor accepts with the least translation.
PROVIDER_WIRE = {
    # claude executor posts Anthropic Messages upstream (claude_executor_execute.go).
    "claude": ANTHROPIC,
    # KimiExecutor.RequestToFormat returns the Claude source unchanged and posts it to
    # Kimi's /v1/messages (kimi_executor.go, helps/kimi_responses.go).
    "kimi": ANTHROPIC,
    # Kiro plugin declares claude input and builds its upstream payload from Claude
    # format (kiro-cpa-plugin cmd/kiro-plugin/main.go).
    "kiro": ANTHROPIC,
    # codex, xai and meta executors translate to CPA's Codex Responses format and
    # post /responses (codex_executor_*.go, xai_executor_request.go, meta_executor_execute.go).
    "codex": RESPONSES,
    "xai": RESPONSES,
    "meta": RESPONSES,
    # Gemini-family executors speak Gemini native or Interactions, which Hermes does
    # not send to non-Google hosts, so they take the default chat wire.
}

# owned_by labels of CPA's static catalogs (internal/registry/models/models.json).
OWNER_PROVIDER = {
    "anthropic": "claude",
    "openai": "codex",
    "xai": "xai",
    "meta": "meta",
    "moonshot": "kimi",
    "antigravity": "antigravity",
    "google": "gemini",
}

# CPA provider -> management model-definitions channel (registry/model_definitions.go
# GetStaticModelDefinitionsByChannel; provider strings from internal/constant).
# Any other provider is asked by its own name (plugin providers, CPA v8.0.34+).
DEFINITION_CHANNEL = {
    "claude": "claude",
    "gemini": "gemini",
    "gemini-interactions": "gemini-interactions",
    "vertex": "vertex",
    "aistudio": "aistudio",
    "codex": "codex",
    "kimi": "kimi",
    "antigravity": "antigravity",
    "xai": "xai",
    "devin": "devin",
    "meta": "meta",
}

# Hermes' effort ladder (agent/reasoning_effort.py EFFORT_LADDER).
EFFORT_LADDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

# Refresh pacing.
PUBLIC_TTL = 30.0  # several profiles of one instance share one public refresh
MANAGEMENT_TTL = 900.0  # management pass at most every 15 minutes per instance
MGMT_PER_PROVIDER = 3  # accounts sampled per provider
MGMT_MAX_CALLS = 32  # hard cap on management requests per pass
MGMT_BUDGET_SECONDS = 20.0  # wall-clock cap per pass

DEFAULT_INSTANCE = "local"
DEFAULT_ROOT = "http://127.0.0.1:8317"
DEFAULT_KEY_ENV = "CPA_LOCAL_API_KEY"
DEFAULT_URL_ENV = "CPA_LOCAL_BASE_URL"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

_PRIVATE_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",
    )
)

Resolver = Callable[[str], Iterable[str]]


class GatewayError(Exception):
    """A CPA request failed. Carries only a status and a category, never bodies or URLs."""

    def __init__(self, status: int, category: str):
        super().__init__(f"CLIProxyAPI request failed: {category} (HTTP {status})")
        self.status = status
        self.category = category


# ── Config ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GroupOverride:
    models: tuple[str, ...] = ()
    api_mode: str = ""
    display_name: str = ""
    route_key_env: str = ""


@dataclass
class InstanceSpec:
    name: str
    root: str
    key_env: str
    mgmt_key_env: str = ""
    url_env: str = ""
    display_name: str = ""
    overrides: dict[str, GroupOverride] = field(default_factory=dict)
    # Billing route: inference goes to an operator's gateway in front of this CPA.
    route_root: str = ""
    route_key_env: str = ""

    @property
    def wide_name(self) -> str:
        return f"{PROFILE_PREFIX}-{self.name}"

    def group_name(self, group: str) -> str:
        return f"{PROFILE_PREFIX}-{self.name}-{group}"

    @property
    def routed(self) -> bool:
        return bool(self.route_root)

    def route_key_for(self, group: str | None) -> str:
        """Gateway key env for one group (its own, else the instance's); "" when unrouted."""
        override = self.overrides.get(group) if group else None
        if override and override.route_key_env:
            return override.route_key_env
        return self.route_key_env

    def route_key_envs(self) -> list[str]:
        """Every gateway key env this instance uses, instance key first."""
        if not self.routed:
            return []
        names = [self.route_key_env]
        for override in self.overrides.values():
            if override.route_key_env and override.route_key_env not in names:
                names.append(override.route_key_env)
        return names


def default_instance() -> InstanceSpec:
    """Used when no instances are configured: one local CPA on the default port."""
    return InstanceSpec(
        name=DEFAULT_INSTANCE,
        root=DEFAULT_ROOT,
        key_env=DEFAULT_KEY_ENV,
        url_env=DEFAULT_URL_ENV,
    )


def wire_for(group: str, override: GroupOverride | None = None) -> str:
    if override and override.api_mode:
        return override.api_mode
    return PROVIDER_WIRE.get(group, DEFAULT_WIRE)


def base_url_for(root: str, api_mode: str) -> str:
    # The Anthropic SDK appends /v1/messages itself; OpenAI clients need /v1.
    return root if api_mode == ANTHROPIC else root + "/v1"


def root_url(raw: str) -> str:
    """Gateway root: strips a trailing slash and ``/v1``."""
    url = raw.strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    return url


def slug(label: str) -> str:
    """Provider key from a CPA label: lowercase, [a-z0-9-], at most 48 chars."""
    return re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")[:48].strip("-")


def provider_for_owner(owned_by: str) -> str:
    label = owned_by.strip().lower()
    return OWNER_PROVIDER.get(label) or slug(label)


def normalize_provider(raw: str) -> str:
    """Provider key from a CPA credential's provider field."""
    value = raw.strip().lower()
    # Aliases GetStaticModelDefinitionsByChannel accepts for one executor.
    if value in ("kimi", "kimi-ai", "kimi.ai", "kimi.com"):
        return "kimi"
    if value in ("xai", "x-ai", "grok"):
        return "xai"
    if value in ("meta", "muse"):
        return "meta"
    return slug(value)


def _url_shape_ok(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return (
        parts.scheme in ("http", "https")
        and bool(parts.hostname)
        and parts.username is None
        and parts.password is None
        and not parts.query
        and not parts.fragment
    )


def parse_config(
    config: Any, *, resolver: Resolver | None = None
) -> tuple[list[InstanceSpec], list[str]]:
    """Build instance specs from the ``cliproxyapi`` config section.

    Returns ``(instances, errors)``. With no instances configured, returns the
    default local instance. An instance with any error is skipped. Error strings
    name the instance and field only, never URLs or values.
    """
    section = config.get("cliproxyapi") if isinstance(config, dict) else None
    instances = section.get("instances") if isinstance(section, dict) else None
    if not isinstance(instances, dict) or not instances:
        return [default_instance()], []
    specs: list[InstanceSpec] = []
    errors: list[str] = []
    for name, body in instances.items():
        spec, problem = _parse_instance(name, body, resolver)
        if spec is None:
            errors.append(problem)
        else:
            specs.append(spec)
    return specs, errors


def _parse_instance(
    name: Any, body: Any, resolver: Resolver | None
) -> tuple[InstanceSpec | None, str]:
    label = f"instance {name!r}" if isinstance(name, str) else "instance"
    if not isinstance(name, str) or not _NAME_RE.match(name):
        return None, f"{label}: name must match {_NAME_RE.pattern}"
    if not isinstance(body, dict):
        return None, f"{label}: must be a mapping"
    raw_url = body.get("base_url")
    if not isinstance(raw_url, str) or not _url_shape_ok(root_url(raw_url)):
        return None, f"{label}: base_url must be an http(s) URL without credentials"
    root = root_url(raw_url)
    if not transport_allowed(root, resolver=resolver):
        return None, f"{label}: plain http is allowed only for private addresses"
    key_env = body.get("key_env")
    if not isinstance(key_env, str) or not _ENV_RE.match(key_env):
        return None, f"{label}: key_env must be an environment variable name"
    mgmt_key_env = body.get("mgmt_key_env", "")
    if mgmt_key_env and (not isinstance(mgmt_key_env, str) or not _ENV_RE.match(mgmt_key_env)):
        return None, f"{label}: mgmt_key_env must be an environment variable name"
    display = body.get("display_name", "")
    if not isinstance(display, str):
        return None, f"{label}: display_name must be a string"
    groups = body.get("groups", {})
    if groups is None:
        groups = {}
    if not isinstance(groups, dict):
        return None, f"{label}: groups must be a mapping"
    overrides: dict[str, GroupOverride] = {}
    for group, gbody in groups.items():
        glabel = f"{label} group {group!r}"
        if not isinstance(group, str) or not _SLUG_RE.match(group):
            return None, f"{glabel}: name must match {_SLUG_RE.pattern}"
        if not isinstance(gbody, dict):
            return None, f"{glabel}: must be a mapping"
        patterns = gbody.get("models", [])
        if patterns is None:
            patterns = []
        if not isinstance(patterns, list) or not all(
            isinstance(p, str) and p.strip() for p in patterns
        ):
            return None, f"{glabel}: models must be a list of id patterns"
        api_mode = gbody.get("api_mode", "")
        if api_mode and api_mode not in VALID_API_MODES:
            return None, f"{glabel}: api_mode must be one of {', '.join(VALID_API_MODES)}"
        gdisplay = gbody.get("display_name", "")
        if not isinstance(gdisplay, str):
            return None, f"{glabel}: display_name must be a string"
        groute = gbody.get("route_key_env", "")
        if groute and (not isinstance(groute, str) or not _ENV_RE.match(groute)):
            return None, f"{glabel}: route_key_env must be an environment variable name"
        overrides[group] = GroupOverride(
            models=tuple(p.strip() for p in patterns),
            api_mode=api_mode or "",
            display_name=gdisplay,
            route_key_env=groute or "",
        )
    route_root, route_key_env, problem = _parse_route(
        label, body, key_env, mgmt_key_env, overrides, resolver
    )
    if problem:
        return None, problem
    return (
        InstanceSpec(
            name=name,
            root=root,
            key_env=key_env,
            mgmt_key_env=mgmt_key_env or "",
            display_name=display,
            overrides=overrides,
            route_root=route_root,
            route_key_env=route_key_env,
        ),
        "",
    )


def _parse_route(
    label: str,
    body: dict,
    key_env: str,
    mgmt_key_env: str,
    overrides: dict[str, GroupOverride],
    resolver: Resolver | None,
) -> tuple[str, str, str]:
    """``(route_root, route_key_env, error)`` for the optional billing route."""
    raw = body.get("route_via", "")
    route_key_env = body.get("route_key_env", "")
    group_keys = [o.route_key_env for o in overrides.values() if o.route_key_env]
    if not raw:
        if route_key_env or group_keys:
            return "", "", f"{label}: route_key_env needs route_via"
        return "", "", ""
    if not isinstance(raw, str) or not _url_shape_ok(root_url(raw)):
        return "", "", f"{label}: route_via must be an http(s) URL without credentials"
    route_root = root_url(raw)
    if not transport_allowed(route_root, resolver=resolver):
        return "", "", f"{label}: route_via plain http is allowed only for private addresses"
    if not isinstance(route_key_env, str) or not _ENV_RE.match(route_key_env):
        return "", "", f"{label}: route_key_env must be an environment variable name"
    # One key per side: the CPA keys never go to the gateway, the gateway key never to CPA.
    cpa_keys = {key_env, mgmt_key_env} - {""}
    if cpa_keys & {route_key_env, *group_keys}:
        return "", "", f"{label}: route_key_env must differ from key_env and mgmt_key_env"
    return route_root, route_key_env, ""


# ── Transport policy ────────────────────────────────────────────────────────


def _ip_is_private(raw: str) -> bool:
    try:
        ip = ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _PRIVATE_NETS)


def _default_resolver(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


def transport_allowed(url: str, *, resolver: Resolver | None = None) -> bool:
    """HTTPS anywhere; plain HTTP only when every resolved address is private."""
    if not _url_shape_ok(url):
        return False
    parts = urlsplit(url)
    if parts.scheme == "https":
        return True
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host.split("%", 1)[0])
        return _ip_is_private(host)
    except ValueError:
        pass
    try:
        addresses = list((resolver or _default_resolver)(host))
    except (OSError, UnicodeError):
        return False
    return bool(addresses) and all(_ip_is_private(a) for a in addresses)


# ── HTTP ────────────────────────────────────────────────────────────────────


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise GatewayError(code, "redirect")


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _category(status: int) -> str:
    if status in (401, 403):
        return "auth"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "upstream"
    return "http"


def get_json(
    url: str,
    api_key: str,
    *,
    timeout: float,
    user_agent: str = USER_AGENT,
    max_bytes: int = MAX_RESPONSE_BYTES,
    headers: dict[str, str] | None = None,
) -> Any:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {api_key}")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", user_agent)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = response.read(max_bytes + 1)
    except GatewayError:
        raise
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise GatewayError(status, _category(status)) from None
    except TimeoutError:
        raise GatewayError(0, "timeout") from None
    except (urllib.error.URLError, OSError):
        raise GatewayError(0, "network") from None
    if len(body) > max_bytes:
        raise GatewayError(200, "response_too_large")
    try:
        return json.loads(body)
    except ValueError:
        raise GatewayError(200, "bad_response") from None


def _entries(payload: Any, key: str) -> list[dict[str, Any]]:
    items = payload.get(key) if isinstance(payload, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _efforts(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        if value in EFFORT_LADDER and value not in out:
            out.append(value)
    return out


# ── Catalog ─────────────────────────────────────────────────────────────────


@dataclass
class Catalog:
    """What one CPA instance serves, plus where each model comes from."""

    ids: list[str] = field(default_factory=list)
    meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)
    fetched_at: float = 0.0
    # Management results: provider -> model ids, and per-id definitions.
    mgmt_groups: dict[str, list[str]] = field(default_factory=dict)
    mgmt_defs: dict[str, dict[str, Any]] = field(default_factory=dict)
    mgmt_checked_at: float = 0.0
    # Fingerprint of a management key CPA rejected; never retried while unchanged.
    mgmt_auth_failed: str = ""
    # Outcome of the last management pass in this process (not persisted).
    mgmt_status: str = ""
    # Billing route: gateway key env name -> ids the gateway lists for that key.
    route_ids: dict[str, list[str]] = field(default_factory=dict)


def fetch_public_catalog(
    root: str, api_key: str, *, timeout: float = 8.0, resolver: Resolver | None = None
) -> Catalog:
    """Model ids and owners from ``/v1/models`` plus metadata from CPA's other public lists.

    Only the id list is required. The Grok Shell list adds context windows and
    reasoning levels; the Gemini list adds output limits and input modalities.
    Both cover every model, matched here by exact id. Missing values stay unknown.
    """
    if not transport_allowed(root, resolver=resolver):
        raise GatewayError(0, "transport_policy")
    ids: list[str] = []
    owners: dict[str, str] = {}
    for item in _entries(get_json(root + "/v1/models", api_key, timeout=timeout), "data"):
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id and model_id not in owners:
            ids.append(model_id)
            owner = item.get("owned_by")
            owners[model_id] = owner if isinstance(owner, str) else ""
    known = set(ids)
    meta: dict[str, dict[str, Any]] = {}

    try:
        grok = get_json(
            root + "/v1/models", api_key, timeout=timeout, user_agent=GROK_LIST_USER_AGENT
        )
    except GatewayError:
        grok = None
    for item in _entries(grok, "data"):
        model_id = item.get("id")
        if model_id not in known:
            continue
        entry = meta.setdefault(model_id, {})
        window = _positive_int(item.get("context_window"))
        if window:
            entry["context_window"] = window
        efforts = _efforts(
            e.get("value") for e in item.get("reasoning_efforts") or [] if isinstance(e, dict)
        )
        if efforts:
            entry["reasoning_efforts"] = efforts
            entry["supports_reasoning"] = True

    try:
        gemini = get_json(root + "/v1beta/models", api_key, timeout=timeout)
    except GatewayError:
        gemini = None
    for item in _entries(gemini, "models"):
        name = item.get("name")
        model_id = (
            name[len("models/") :] if isinstance(name, str) and name.startswith("models/") else name
        )
        if model_id not in known:
            continue
        entry = meta.setdefault(model_id, {})
        output = _positive_int(item.get("outputTokenLimit"))
        if output:
            entry["max_output_tokens"] = output
        window = _positive_int(item.get("inputTokenLimit"))
        if window and "context_window" not in entry:
            entry["context_window"] = window
        modalities = item.get("supportedInputModalities")
        if isinstance(modalities, list) and modalities:
            entry["supports_vision"] = any(
                isinstance(m, str) and m.lower() == "image" for m in modalities
            )

    try:
        anthropic = get_json(
            root + "/v1/models", api_key, timeout=timeout, headers=ANTHROPIC_LIST_HEADERS
        )
    except GatewayError:
        anthropic = None
    for item in _entries(anthropic, "data"):
        model_id = _uncloak(item.get("id"), known)
        output = _positive_int(item.get("max_tokens"))
        # CPA writes its default for a model with no limit; that is not a real value.
        if model_id is None or not output or output == CPA_DEFAULT_MAX_TOKENS:
            continue
        meta.setdefault(model_id, {})["max_output_tokens"] = output

    return Catalog(ids=ids, meta={k: v for k, v in meta.items() if v}, owners=owners)


def _uncloak(raw: Any, known: set[str]) -> str | None:
    """Served id for an Anthropic-format list id, or None when CPA does not serve it."""
    if not isinstance(raw, str):
        return None
    if raw in known:
        return raw
    if raw.startswith(CLOAKED_ID_PREFIX):
        decoded = raw[len(CLOAKED_ID_PREFIX) :][::-1]
        if decoded in known:
            return decoded
    return None


def definition_meta(item: dict[str, Any]) -> dict[str, Any]:
    """Metadata from one management ModelInfo (registry/model_registry.go json tags)."""
    entry: dict[str, Any] = {}
    window = _positive_int(item.get("context_length")) or _positive_int(item.get("inputTokenLimit"))
    if window:
        entry["context_window"] = window
    output = _positive_int(item.get("max_completion_tokens")) or _positive_int(
        item.get("outputTokenLimit")
    )
    if output:
        entry["max_output_tokens"] = output
    modalities = item.get("supportedInputModalities")
    if isinstance(modalities, list) and modalities:
        entry["supports_vision"] = any(
            isinstance(m, str) and m.lower() == "image" for m in modalities
        )
    thinking = item.get("thinking")
    if isinstance(thinking, dict):
        entry["supports_reasoning"] = True
        levels = thinking.get("levels")
        efforts = _efforts(levels) if isinstance(levels, list) else []
        if efforts:
            entry["reasoning_efforts"] = efforts
    return entry


# ── Management ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _ManagementPaths:
    credentials: str
    models: str
    definitions: str


_V8 = _ManagementPaths(
    "/v8/management/credentials",
    "/v8/management/credentials/models",
    "/v8/management/routing/model-definitions/",
)
_V0 = _ManagementPaths(
    "/v0/management/auth-files",
    "/v0/management/auth-files/models",
    "/v0/management/model-definitions/",
)


@dataclass
class ManagementResult:
    status: str  # "ok", "auth_failed" or "unavailable"
    groups: dict[str, list[str]] = field(default_factory=dict)
    definitions: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: int = 0


def management_fingerprint(key: str, salt: str = "") -> str:
    """Identifies a management key without storing it: ``<salt>:<hmac>``.

    A random per-record salt keeps a weak key from being brute-forced offline
    from the cache file.
    """
    salt = salt or os.urandom(16).hex()
    digest = hmac.new(bytes.fromhex(salt), key.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{salt}:{digest}"


def fingerprint_matches(key: str, stored: str) -> bool:
    salt, _, _digest = stored.partition(":")
    try:
        bytes.fromhex(salt)
        expected = management_fingerprint(key, salt).encode("ascii")
        return bool(salt) and hmac.compare_digest(expected, stored.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# Keys CPA rejected in this process, per gateway root. Another instance (or
# another refresh racing the cache write) never sends the same rejected key to
# the same CPA again. In memory only; the cache carries the latch across restarts.
_REJECTED: dict[str, list[str]] = {}
_REJECTED_LOCK = threading.Lock()


def _rejected_here(root: str, key: str) -> bool:
    with _REJECTED_LOCK:
        return any(fingerprint_matches(key, fp) for fp in _REJECTED.get(root, ()))


def _remember_rejected(root: str, key: str) -> None:
    with _REJECTED_LOCK:
        _REJECTED.setdefault(root, []).append(management_fingerprint(key))


def choose_accounts(listing: Any, per_provider: int) -> dict[str, list[str]]:
    """A few enabled accounts per provider from a credentials listing.

    Reads only provider, name/id, disabled and unavailable. Accounts that are
    currently available come first.
    """
    ready: dict[str, list[str]] = {}
    later: dict[str, list[str]] = {}
    for item in _entries(listing, "files"):
        raw_provider = item.get("provider") or item.get("type")
        name = item.get("name") or item.get("id")
        if not isinstance(raw_provider, str) or not isinstance(name, str) or not name:
            continue
        if item.get("disabled") is True:
            continue
        provider = normalize_provider(raw_provider)
        if not provider:
            continue
        bucket = later if item.get("unavailable") is True else ready
        bucket.setdefault(provider, []).append(name)
    chosen: dict[str, list[str]] = {}
    for provider in sorted(set(ready) | set(later)):
        names = (ready.get(provider, []) + later.get(provider, []))[:per_provider]
        if names:
            chosen[provider] = names
    return chosen


def fetch_management(
    root: str,
    mgmt_key: str,
    *,
    timeout: float = 8.0,
    resolver: Resolver | None = None,
    per_provider: int = MGMT_PER_PROVIDER,
    max_calls: int = MGMT_MAX_CALLS,
    budget: float = MGMT_BUDGET_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> ManagementResult:
    """Provider per model and model definitions from the CPA management API.

    Read-only GETs only: the credentials list, a few accounts' model lists per
    provider, and one model-definitions list per provider. A 401 ends the pass
    at once (CPA bans an IP after five failures); every other failure means
    "unavailable" and the caller falls back to public grouping. Nothing from a
    response is kept beyond provider names, model ids and model metadata.
    """
    if not transport_allowed(root, resolver=resolver):
        return ManagementResult("unavailable")
    deadline = clock() + budget
    calls = 0

    def call(path: str, max_bytes: int = MAX_RESPONSE_BYTES) -> Any:
        nonlocal calls
        remaining = deadline - clock()
        if calls >= max_calls or remaining <= 0:
            raise GatewayError(0, "budget")
        calls += 1
        return get_json(root + path, mgmt_key, timeout=min(timeout, remaining), max_bytes=max_bytes)

    def fatal(exc: GatewayError) -> bool:
        return exc.status in (401, 403)

    try:
        try:
            listing = call(_V8.credentials, MAX_MANAGEMENT_LIST_BYTES)
            paths = _V8
        except GatewayError as exc:
            if exc.status != 404:
                raise
            listing = call(_V0.credentials, MAX_MANAGEMENT_LIST_BYTES)
            paths = _V0
        chosen = choose_accounts(listing, per_provider)
        del listing
        groups: dict[str, list[str]] = {}
        out_of_budget = False
        for provider, names in chosen.items():
            for name in names:
                try:
                    payload = call(f"{paths.models}?name={quote(name, safe='')}")
                except GatewayError as exc:
                    if fatal(exc):
                        raise
                    out_of_budget = exc.category == "budget"
                    if out_of_budget:
                        break
                    continue
                bucket = groups.setdefault(provider, [])
                for item in _entries(payload, "models"):
                    model_id = item.get("id")
                    if isinstance(model_id, str) and model_id and model_id not in bucket:
                        bucket.append(model_id)
            if out_of_budget:
                break
        definitions: dict[str, dict[str, Any]] = {}
        # Static channels first, so plugin-provider lookups never use up the budget they need.
        for provider in sorted(groups, key=lambda p: (p not in DEFINITION_CHANNEL, p)):
            if out_of_budget:
                break
            # Static channels map by name; since CPA v8.0.34 the same route also
            # answers for plugin providers (Kiro and others) by provider name.
            # Older CPA answers 400 "unknown channel": that group stays unenriched.
            channel = DEFINITION_CHANNEL.get(provider, provider)
            try:
                payload = call(paths.definitions + quote(channel, safe=""))
            except GatewayError as exc:
                if fatal(exc):
                    raise
                out_of_budget = exc.category == "budget"
                continue
            for item in _entries(payload, "models"):
                model_id = item.get("id")
                if isinstance(model_id, str) and model_id and model_id not in definitions:
                    # First answer wins: built-in channels are read first, so a plugin
                    # provider serving the same id never replaces their metadata.
                    entry = definition_meta(item)
                    if entry:
                        definitions[model_id] = entry
        return ManagementResult("ok", {k: v for k, v in groups.items() if v}, definitions, calls)
    except GatewayError as exc:
        if exc.status == 401:
            return ManagementResult("auth_failed", calls=calls)
        return ManagementResult("unavailable", calls=calls)


# ── Grouping ────────────────────────────────────────────────────────────────


def match_models(ids: Iterable[str], patterns: Iterable[str]) -> list[str]:
    pats = tuple(patterns)
    return [i for i in ids if any(fnmatch.fnmatchcase(i, p) for p in pats)]


def fetch_route_ids(
    route_root: str, route_key: str, *, timeout: float = 8.0, resolver: Resolver | None = None
) -> list[str]:
    """Ids a billing gateway lists for one of its keys. Client ``/v1/models`` only."""
    if not transport_allowed(route_root, resolver=resolver):
        raise GatewayError(0, "transport_policy")
    ids: list[str] = []
    for item in _entries(get_json(route_root + "/v1/models", route_key, timeout=timeout), "data"):
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id and model_id not in ids:
            ids.append(model_id)
    return ids


def filter_routed(
    groups: dict[str, list[str]], spec: InstanceSpec, route_ids: dict[str, list[str]]
) -> dict[str, list[str]]:
    """Keep, per group, only the ids its gateway key sells; CPA order.

    A key whose gateway list was never read keeps CPA's ids, so routed groups
    register at import from CPA's cache. A known list, even empty, is strict.
    """
    if not spec.routed:
        return groups
    out: dict[str, list[str]] = {}
    for group, ids in groups.items():
        sold = route_ids.get(spec.route_key_for(group))
        if sold is None:
            out[group] = list(ids)
        else:
            keep = set(sold)
            out[group] = [i for i in ids if i in keep]
    return out


def compute_groups(
    catalog: Catalog, overrides: dict[str, GroupOverride] | None = None
) -> dict[str, list[str]]:
    """Provider -> served model ids.

    Management membership first (exact), then ``owned_by`` for ids management
    did not cover, then operator ``models`` patterns, which replace a group.
    An id can belong to several providers. Order follows ``catalog.ids``.
    """
    position = {model_id: index for index, model_id in enumerate(catalog.ids)}
    groups: dict[str, set[str]] = {}
    covered: set[str] = set()
    for provider, members in catalog.mgmt_groups.items():
        served = {m for m in members if m in position}
        if served:
            groups.setdefault(provider, set()).update(served)
            covered |= served
    for model_id in catalog.ids:
        if model_id in covered:
            continue
        provider = provider_for_owner(catalog.owners.get(model_id, ""))
        if provider:
            groups.setdefault(provider, set()).add(model_id)
    result = {p: sorted(ids, key=position.__getitem__) for p, ids in groups.items()}
    for group, override in (overrides or {}).items():
        if override.models:
            result[group] = match_models(catalog.ids, override.models)
    return result


def _definition_for(definitions: dict[str, dict[str, Any]], model_id: str) -> dict | None:
    if model_id in definitions:
        return definitions[model_id]
    if "/" in model_id:  # CPA prefixes ("pro/claude-x") reuse the base model's metadata
        return definitions.get(model_id.split("/", 1)[1])
    return None


def merge_meta(
    ids: Iterable[str],
    public: dict[str, dict[str, Any]],
    definitions: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Public list metadata, overridden key by key by management definitions."""
    merged: dict[str, dict[str, Any]] = {}
    for model_id in ids:
        entry = dict(public.get(model_id, {}))
        entry.update(_definition_for(definitions, model_id) or {})
        if entry:
            merged[model_id] = entry
    return merged


_CAPABILITY_KEYS = ("context_window", "supports_vision", "supports_reasoning")


def capabilities_for(catalog: Catalog, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Hermes ``model_capabilities`` for the given ids: only keys Hermes accepts."""
    out: dict[str, dict[str, Any]] = {}
    for model_id in ids:
        caps = {k: v for k, v in catalog.meta.get(model_id, {}).items() if k in _CAPABILITY_KEYS}
        if caps:
            out[model_id] = caps
    return out


# ── Refresh ─────────────────────────────────────────────────────────────────


def refresh_catalog(
    root: str,
    api_key: str,
    mgmt_key: str,
    previous: Catalog,
    *,
    mgmt_configured: bool | None = None,
    now: float | None = None,
    timeout: float = 8.0,
    resolver: Resolver | None = None,
    management: Callable[..., ManagementResult] = fetch_management,
) -> Catalog:
    """Fresh public catalog, plus management grouping when a key is set.

    The management pass runs at most once per ``MANAGEMENT_TTL`` and never again
    for a key CPA rejected, until the key changes. When management is configured
    but its key cannot be read right now, previous management results are kept.
    Raises ``GatewayError`` when the public model list fails.
    """
    now = time.time() if now is None else now
    if mgmt_configured is None:
        mgmt_configured = bool(mgmt_key)
    public = fetch_public_catalog(root, api_key, timeout=timeout, resolver=resolver)
    catalog = Catalog(
        ids=public.ids,
        owners=public.owners,
        fetched_at=now,
        mgmt_groups=previous.mgmt_groups,
        mgmt_defs=previous.mgmt_defs,
        mgmt_checked_at=previous.mgmt_checked_at,
        mgmt_auth_failed=previous.mgmt_auth_failed,
    )
    if not mgmt_configured:
        catalog.mgmt_groups, catalog.mgmt_defs = {}, {}
        catalog.mgmt_checked_at, catalog.mgmt_auth_failed = 0.0, ""
        catalog.mgmt_status = "off"
    elif not mgmt_key:
        catalog.mgmt_status = "missing_key"
    else:
        if (
            previous.mgmt_auth_failed and fingerprint_matches(mgmt_key, previous.mgmt_auth_failed)
        ) or _rejected_here(root, mgmt_key):
            catalog.mgmt_groups, catalog.mgmt_defs = {}, {}
            catalog.mgmt_auth_failed = previous.mgmt_auth_failed or management_fingerprint(mgmt_key)
            catalog.mgmt_status = "auth_failed"
        elif not previous.mgmt_auth_failed and 0 <= now - previous.mgmt_checked_at < MANAGEMENT_TTL:
            catalog.mgmt_status = "cached"
        else:
            result = management(root, mgmt_key, timeout=timeout, resolver=resolver)
            catalog.mgmt_checked_at = now
            catalog.mgmt_status = result.status
            if result.status == "ok":
                catalog.mgmt_groups = result.groups
                catalog.mgmt_defs = {
                    model_id: entry
                    for model_id in public.ids
                    if (entry := _definition_for(result.definitions, model_id))
                }
                catalog.mgmt_auth_failed = ""
            elif result.status == "auth_failed":
                _remember_rejected(root, mgmt_key)
                catalog.mgmt_groups, catalog.mgmt_defs = {}, {}
                catalog.mgmt_auth_failed = management_fingerprint(mgmt_key)
            # "unavailable": keep previous results; retry after the TTL.
    catalog.meta = merge_meta(public.ids, public.meta, catalog.mgmt_defs)
    return catalog


# ── Cache ───────────────────────────────────────────────────────────────────

CACHE_VERSION = 2


def cache_path(hermes_home: Path, instance: str) -> Path:
    return Path(hermes_home) / "cache" / "cliproxyapi" / f"{instance}.json"


def save_cache(hermes_home: Path, instance: str, catalog: Catalog) -> None:
    path = cache_path(hermes_home, instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(
        {
            "version": CACHE_VERSION,
            "ids": catalog.ids,
            "meta": catalog.meta,
            "owners": catalog.owners,
            "fetched_at": catalog.fetched_at,
            "mgmt_groups": catalog.mgmt_groups,
            "mgmt_defs": catalog.mgmt_defs,
            "mgmt_checked_at": catalog.mgmt_checked_at,
            "mgmt_auth_failed": catalog.mgmt_auth_failed,
            "route_ids": catalog.route_ids,
        },
        sort_keys=True,
    )
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{instance}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _str_dict(value: Any) -> dict[str, Any]:
    return {k: v for k, v in value.items() if isinstance(k, str)} if isinstance(value, dict) else {}


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def load_cache(hermes_home: Path, instance: str) -> Catalog:
    try:
        data = json.loads(cache_path(hermes_home, instance).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Catalog()
    if not isinstance(data, dict) or data.get("version") not in (1, CACHE_VERSION):
        return Catalog()
    ids = [i for i in data.get("ids") or [] if isinstance(i, str)]
    catalog = Catalog(
        ids=ids,
        meta={k: v for k, v in _str_dict(data.get("meta")).items() if isinstance(v, dict)},
    )
    if data.get("version") == 1:  # 0.1 caches: ids and metadata only
        return catalog
    catalog.owners = {k: v for k, v in _str_dict(data.get("owners")).items() if isinstance(v, str)}
    catalog.fetched_at = _number(data.get("fetched_at"))
    catalog.mgmt_groups = {
        k: [i for i in v if isinstance(i, str)]
        for k, v in _str_dict(data.get("mgmt_groups")).items()
        if isinstance(v, list)
    }
    catalog.mgmt_defs = {
        k: v for k, v in _str_dict(data.get("mgmt_defs")).items() if isinstance(v, dict)
    }
    catalog.mgmt_checked_at = _number(data.get("mgmt_checked_at"))
    failed = data.get("mgmt_auth_failed")
    catalog.mgmt_auth_failed = failed if isinstance(failed, str) else ""
    catalog.route_ids = {
        k: [i for i in v if isinstance(i, str)]
        for k, v in _str_dict(data.get("route_ids")).items()
        if isinstance(v, list)
    }
    return catalog

"""CLIProxyAPI bridge core: config parsing, transport policy, public catalog, cache.

Pure standard library. Nothing here imports Hermes, so it is unit-testable and
safe to import during Hermes provider discovery.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import json
import os
import re
import socket
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

VERSION = "0.1.1"
USER_AGENT = f"hermes-cliproxyapi-provider/{VERSION}"
# CPA serves its richest public list (context window, reasoning levels) only to
# Grok Shell clients: internal/client/grokbuild IsGrokShellUserAgent.
GROK_LIST_USER_AGENT = f"{USER_AGENT} grok-shell-compatible"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
PROFILE_PREFIX = "cpa"

VALID_API_MODES = ("anthropic_messages", "codex_responses", "chat_completions")
# CPA provider group -> Hermes api_mode. Each row is the wire the group's CPA
# executor speaks upstream, so CPA translates as little as possible.
GROUP_WIRE = {
    "claude": "anthropic_messages",
    "codex": "codex_responses",
}
DEFAULT_WIRE = "chat_completions"
# Hermes' effort ladder (agent/reasoning_effort.py EFFORT_LADDER).
EFFORT_LADDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
_GROUP_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
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


@dataclass(frozen=True)
class ProfileSpec:
    name: str
    instance: str
    group: str
    root: str
    base_url: str
    api_mode: str
    key_env: str
    patterns: tuple[str, ...]
    display_name: str = ""
    url_env: str = ""

    @property
    def cache_key(self) -> str:
        return self.instance


# Zero-config provider. Not named "cliproxyapi": users often already have a
# custom provider by that name, and a registered profile would shadow it.
DEFAULT_NAME = "cpa-local"
DEFAULT_ROOT = "http://127.0.0.1:8317"
DEFAULT_KEY_ENV = "CPA_LOCAL_API_KEY"
DEFAULT_URL_ENV = "CPA_LOCAL_BASE_URL"


def default_spec() -> ProfileSpec:
    """One local CPA, every model, chat wire (CPA translates to each upstream).

    Declared instances give each provider group its native wire instead.
    """
    return ProfileSpec(
        name=DEFAULT_NAME,
        instance="_local",
        group="all",
        root=DEFAULT_ROOT,
        base_url=DEFAULT_ROOT + "/v1",
        api_mode=DEFAULT_WIRE,
        key_env=DEFAULT_KEY_ENV,
        patterns=("*",),
        display_name="CLIProxyAPI (local)",
        url_env=DEFAULT_URL_ENV,
    )


@dataclass
class Catalog:
    ids: list[str] = field(default_factory=list)
    meta: dict[str, dict[str, Any]] = field(default_factory=dict)


# ── Config ──────────────────────────────────────────────────────────────────


def wire_for_group(group: str) -> str:
    return GROUP_WIRE.get(group, DEFAULT_WIRE)


def root_url(raw: str) -> str:
    """Gateway root: strips a trailing slash and ``/v1``."""
    url = raw.strip().rstrip("/")
    if url.endswith("/v1"):
        url = url[: -len("/v1")]
    return url


_root_url = root_url


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
) -> tuple[list[ProfileSpec], list[str]]:
    """Build profile specs from the ``cliproxyapi`` config section.

    Returns ``(specs, errors)``. An instance with any error registers nothing.
    Error strings name the instance and field only, never URLs or values.
    """
    section = config.get("cliproxyapi") if isinstance(config, dict) else None
    instances = section.get("instances") if isinstance(section, dict) else None
    if not isinstance(instances, dict):
        return [], []
    specs: list[ProfileSpec] = []
    errors: list[str] = []
    taken = {DEFAULT_NAME}
    for instance, body in instances.items():
        found, problems = _parse_instance(instance, body, resolver)
        clashes = sorted({s.name for s in found} & taken)
        if clashes:
            # Hyphens are legal in instance and group names, so "a"+"b-c" and
            # "a-b"+"c" both give cpa-a-b-c. Refuse rather than silently override.
            problems = [
                *problems,
                f"instance '{instance}': provider name already used: {clashes[0]}",
            ]
        if problems:
            errors.extend(problems)
        else:
            specs.extend(found)
            taken.update(s.name for s in found)
    return specs, errors


def _parse_instance(
    instance: Any, body: Any, resolver: Resolver | None
) -> tuple[list[ProfileSpec], list[str]]:
    label = f"instance {instance!r}" if isinstance(instance, str) else "instance"
    if not isinstance(instance, str) or not _NAME_RE.match(instance):
        return [], [f"{label}: name must match {_NAME_RE.pattern}"]
    if not isinstance(body, dict):
        return [], [f"{label}: must be a mapping"]
    raw_url = body.get("base_url")
    if not isinstance(raw_url, str) or not _url_shape_ok(_root_url(raw_url)):
        return [], [f"{label}: base_url must be an http(s) URL without credentials"]
    root = _root_url(raw_url)
    if not transport_allowed(root, resolver=resolver):
        return [], [f"{label}: plain http is allowed only for private addresses"]
    key_env = body.get("key_env")
    if not isinstance(key_env, str) or not _ENV_RE.match(key_env):
        return [], [f"{label}: key_env must be an environment variable name"]
    groups = body.get("groups")
    if not isinstance(groups, dict) or not groups:
        return [], [f"{label}: groups must be a non-empty mapping"]
    specs: list[ProfileSpec] = []
    for group, gbody in groups.items():
        glabel = f"{label} group {group!r}"
        if not isinstance(group, str) or not _GROUP_RE.match(group):
            return [], [f"{glabel}: name must match {_GROUP_RE.pattern}"]
        if not isinstance(gbody, dict):
            return [], [f"{glabel}: must be a mapping"]
        patterns = gbody.get("models")
        if (
            not isinstance(patterns, list)
            or not patterns
            or not all(isinstance(p, str) and p.strip() for p in patterns)
        ):
            return [], [f"{glabel}: models must be a non-empty list of id patterns"]
        api_mode = gbody.get("api_mode") or wire_for_group(group)
        if api_mode not in VALID_API_MODES:
            return [], [f"{glabel}: api_mode must be one of {', '.join(VALID_API_MODES)}"]
        display = gbody.get("display_name")
        specs.append(
            ProfileSpec(
                name=f"{PROFILE_PREFIX}-{instance}-{group}",
                instance=instance,
                group=group,
                root=root,
                # The Anthropic SDK appends /v1/messages itself; OpenAI clients need /v1.
                base_url=root if api_mode == "anthropic_messages" else root + "/v1",
                api_mode=api_mode,
                key_env=key_env,
                patterns=tuple(p.strip() for p in patterns),
                display_name=display if isinstance(display, str) else "",
            )
        )
    return specs, []


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


def get_json(url: str, api_key: str, *, timeout: float, user_agent: str = USER_AGENT) -> Any:
    request = urllib.request.Request(url, method="GET")
    request.add_header("Authorization", f"Bearer {api_key}")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", user_agent)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
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
    if len(body) > MAX_RESPONSE_BYTES:
        raise GatewayError(200, "response_too_large")
    try:
        return json.loads(body)
    except ValueError:
        raise GatewayError(200, "bad_response") from None


# ── Public catalog ──────────────────────────────────────────────────────────


def _entries(payload: Any, key: str) -> list[dict[str, Any]]:
    items = payload.get(key) if isinstance(payload, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def fetch_public_catalog(
    root: str, api_key: str, *, timeout: float = 8.0, resolver: Resolver | None = None
) -> Catalog:
    """Model ids from ``/v1/models`` plus metadata from CPA's other public lists.

    Only the id list is required. The Grok Shell list adds context windows and
    reasoning levels; the Gemini list adds output limits and input modalities.
    Both cover every model, matched here by exact id. Missing values stay unknown.
    """
    if not transport_allowed(root, resolver=resolver):
        raise GatewayError(0, "transport_policy")
    ids: list[str] = []
    for item in _entries(get_json(root + "/v1/models", api_key, timeout=timeout), "data"):
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id and model_id not in ids:
            ids.append(model_id)
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
        efforts = [
            e.get("value")
            for e in item.get("reasoning_efforts") or []
            if isinstance(e, dict) and e.get("value") in EFFORT_LADDER
        ]
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

    return Catalog(ids=ids, meta={k: v for k, v in meta.items() if v})


def match_models(ids: Iterable[str], patterns: Iterable[str]) -> list[str]:
    pats = tuple(patterns)
    return [i for i in ids if any(fnmatch.fnmatchcase(i, p) for p in pats)]


_CAPABILITY_KEYS = ("context_window", "supports_vision", "supports_reasoning")


def capabilities_for(catalog: Catalog, patterns: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Hermes ``model_capabilities`` for one group: only keys Hermes accepts."""
    out: dict[str, dict[str, Any]] = {}
    for model_id in match_models(catalog.ids, patterns):
        caps = {k: v for k, v in catalog.meta.get(model_id, {}).items() if k in _CAPABILITY_KEYS}
        if caps:
            out[model_id] = caps
    return out


# ── Cache ───────────────────────────────────────────────────────────────────


def cache_path(hermes_home: Path, instance: str) -> Path:
    return Path(hermes_home) / "cache" / "cliproxyapi" / f"{instance}.json"


def save_cache(hermes_home: Path, instance: str, catalog: Catalog) -> None:
    path = cache_path(hermes_home, instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"version": 1, "ids": catalog.ids, "meta": catalog.meta}, sort_keys=True)
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


def load_cache(hermes_home: Path, instance: str) -> Catalog:
    try:
        data = json.loads(cache_path(hermes_home, instance).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Catalog()
    if not isinstance(data, dict) or data.get("version") != 1:
        return Catalog()
    ids = [i for i in data.get("ids") or [] if isinstance(i, str)]
    raw_meta = data.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    return Catalog(ids=ids, meta={k: v for k, v in meta.items() if isinstance(v, dict)})

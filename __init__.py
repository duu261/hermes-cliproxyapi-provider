"""Hermes model-provider plugin: bridge CLIProxyAPI (CPA) providers into Hermes.

For each CPA instance in the ``cliproxyapi`` section of ``config.yaml`` this
registers one provider per CPA provider (``cpa-<instance>-<provider>``), each on
the wire that CPA's executor for it accepts with the least translation, plus one
instance-wide chat provider (``cpa-<instance>``). With no instances configured it
uses one local CPA (``cpa-local``).

Registration reads only config and the on-disk catalog cache. Network calls
happen in ``fetch_models``, which refreshes the catalog, writes the cache and
registers providers that appeared since startup.
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

try:  # loaded by Hermes as a package
    from . import cpa
except ImportError:  # pragma: no cover - direct import in tests
    import cpa  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:  # noqa: BLE001 - older Hermes or unusual embedding
        configured = os.environ.get("HERMES_HOME", "").strip()
        return Path(configured).expanduser() if configured else Path.home() / ".hermes"


def _load_yaml(text: str) -> Any:
    """Read YAML as Hermes does: ruamel.yaml (PyYAML is not guaranteed), YAML 1.1."""
    try:
        from ruamel.yaml import YAML
    except ImportError:  # pragma: no cover - not a Hermes interpreter
        import yaml  # type: ignore[import-untyped]

        return yaml.safe_load(text)
    reader = YAML(typ="safe", pure=True)
    reader.version = (1, 1)
    return reader.load(text)


def _read_config(home: Path) -> dict[str, Any]:
    """Parse ``config.yaml`` directly; importing hermes_cli.config during discovery can cycle."""
    try:
        data = _load_yaml((home / "config.yaml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 - any parser error means "no usable config"
        logger.warning("cliproxyapi: config.yaml unreadable (%s)", type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


def _read_secret(name: str) -> str:
    """Resolve a key by env var name the way Hermes does, at call time only."""
    if not name:
        return ""
    try:
        from agent.secret_scope import get_secret

        return get_secret(name) or ""
    except ImportError:  # pragma: no cover - older Hermes
        return os.environ.get(name, "")
    except Exception as exc:  # noqa: BLE001 - e.g. unscoped read under multiplexing
        logger.debug("cliproxyapi: secret %s unavailable (%s)", name, type(exc).__name__)
        return ""


class _Instance:
    """Shared state for every profile of one CPA instance."""

    def __init__(self, spec: cpa.InstanceSpec, home: Path, catalog: cpa.Catalog):
        self.spec = spec
        self.home = home
        self.catalog = catalog
        self.groups = self._groups(catalog)
        self.lock = threading.Lock()
        self.refreshed_at = 0.0  # monotonic; 0 means "not refreshed in this process"
        self.profiles: dict[str, CLIProxyAPIProfile] = {}

    def _groups(self, catalog: cpa.Catalog) -> dict[str, list[str]]:
        groups = cpa.compute_groups(catalog, self.spec.overrides)
        return cpa.filter_routed(groups, self.spec, catalog.route_ids)

    def _route_lists(self, timeout: float) -> dict[str, list[str]]:
        """Fresh gateway lists per route key; the last known list when one fails."""
        lists: dict[str, list[str]] = {}
        for env_name in self.spec.route_key_envs():
            previous = self.catalog.route_ids.get(env_name)
            key = _read_secret(env_name)
            try:
                if not key:
                    raise cpa.GatewayError(0, "missing_key")
                lists[env_name] = cpa.fetch_route_ids(self.spec.route_root, key, timeout=timeout)
            except cpa.GatewayError as exc:
                logger.warning(
                    "cliproxyapi %s: gateway model list for %s failed: %s; %s",
                    self.spec.name,
                    env_name,
                    exc,
                    "keeping the last known list" if previous is not None else "using CPA's list",
                )
                if previous is not None:
                    lists[env_name] = previous
        return lists

    def refresh(self, api_key: str, root: str, timeout: float) -> bool:
        """Refresh the catalog unless another profile just did. Returns success."""
        with self.lock:
            if self.refreshed_at and time.monotonic() - self.refreshed_at < cpa.PUBLIC_TTL:
                return True
            mgmt_key = _read_secret(self.spec.mgmt_key_env)
            try:
                catalog = cpa.refresh_catalog(
                    root,
                    api_key,
                    mgmt_key,
                    self.catalog,
                    mgmt_configured=bool(self.spec.mgmt_key_env),
                    timeout=timeout,
                )
            except cpa.GatewayError as exc:
                logger.warning("cliproxyapi %s: model list failed: %s", self.spec.name, exc)
                return False
            if catalog.mgmt_status in ("auth_failed", "unavailable", "missing_key"):
                logger.warning(
                    "cliproxyapi %s: management %s; using public model owners",
                    self.spec.name,
                    catalog.mgmt_status.replace("_", " "),
                )
            if self.spec.routed:
                catalog.route_ids = self._route_lists(timeout)
            try:
                cpa.save_cache(self.home, self.spec.name, catalog)
            except OSError as exc:
                if catalog.mgmt_status == "auth_failed":
                    # Without the cache the rejection is forgotten at restart, and CPA
                    # bans an address after five failed management logins.
                    logger.warning(
                        "cliproxyapi %s: cache write failed (%s); the rejected management key "
                        "will be tried again at next start. Fix the key or unset %s.",
                        self.spec.name,
                        type(exc).__name__,
                        self.spec.mgmt_key_env,
                    )
                else:
                    logger.warning(
                        "cliproxyapi %s: cache write failed (%s)",
                        self.spec.name,
                        type(exc).__name__,
                    )
            self.catalog = catalog
            self.groups = self._groups(catalog)
            self.refreshed_at = time.monotonic()
            for profile in self.profiles.values():
                profile._cpa_sync()
            _register_groups(self)
            return True

    def members(self, group: str | None) -> list[str]:
        if group is not None:
            return list(self.groups.get(group, []))
        if not self.spec.routed:
            return list(self.catalog.ids)
        sold = self.catalog.route_ids.get(self.spec.route_key_for(None))
        if sold is None:
            return list(self.catalog.ids)
        keep = set(sold)
        return [i for i in self.catalog.ids if i in keep]


class CLIProxyAPIProfile(ProviderProfile):
    """One CPA provider on one CPA instance, or the instance-wide chat provider.

    Private ``_cpa_*`` attributes are set by ``_build_profile``. Inference headers
    are left to Hermes: CPA reacts to client User-Agents, so the plugin sets its
    own User-Agent only on its catalog requests.
    """

    _cpa_instance: _Instance
    _cpa_group: str | None

    def _cpa_sync(self) -> None:
        ids = self._cpa_instance.members(self._cpa_group)
        self.fallback_models = tuple(ids)
        # Swap, never mutate: Hermes reads these without the instance lock.
        self.model_capabilities = cpa.capabilities_for(self._cpa_instance.catalog, ids)

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0
    ) -> list[str] | None:
        if not api_key:
            return None
        instance = self._cpa_instance
        spec = instance.spec
        if spec.routed:
            # Hermes hands over the gateway key and URL; discovery stays on CPA with
            # the CPA key, which is read here and never sent to the gateway.
            cpa_key = _read_secret(spec.key_env)
            if not cpa_key:
                logger.warning(
                    "cliproxyapi %s: %s is not set; cannot read CPA's model list",
                    spec.name,
                    spec.key_env,
                )
                return None
            api_key, root = cpa_key, spec.root
        else:
            # Callers pass the effective endpoint (a user *_BASE_URL override wins).
            root = cpa.root_url(base_url) if base_url else spec.root
        if not instance.refresh(api_key, root, timeout):
            return None
        return instance.members(self._cpa_group)

    def supported_reasoning_efforts(self, model: str | None) -> tuple[str, ...] | None:
        efforts = self._cpa_instance.catalog.meta.get(model or "", {}).get("reasoning_efforts")
        return tuple(efforts) if efforts else None

    def get_max_tokens(self, model: str | None) -> int | None:
        value = self._cpa_instance.catalog.meta.get(model or "", {}).get("max_output_tokens")
        return value if isinstance(value, int) and value > 0 else self.default_max_tokens


_INSTANCES: dict[str, _Instance] = {}
_TAKEN: dict[str, str] = {}  # registered profile name -> owning instance
_TAKEN_LOCK = threading.Lock()
_IMPORT_CONTEXT = contextvars.copy_context()


def _build_profile(instance: _Instance, group: str | None) -> CLIProxyAPIProfile:
    spec = instance.spec
    override = spec.overrides.get(group) if group else None
    api_mode = cpa.wire_for(group, override) if group else cpa.CHAT
    base = spec.display_name or f"CLIProxyAPI {spec.name}"
    if group is None:
        name, label = spec.wide_name, base
        description = f"CLIProxyAPI instance '{spec.name}', every model on the chat wire"
    else:
        name = spec.group_name(group)
        label = (override.display_name if override else "") or f"{base} ({group})"
        description = f"CLIProxyAPI instance '{spec.name}', provider '{group}'"
    if spec.routed:
        # Inference and the profile's key go to the billing gateway only.
        root, env_vars = spec.route_root, (spec.route_key_for(group),)
        description += ", billed through a gateway"
    else:
        root = spec.root
        env_vars = (spec.key_env, spec.url_env) if spec.url_env else (spec.key_env,)
    profile = CLIProxyAPIProfile(
        name=name,
        api_mode=api_mode,
        display_name=label,
        description=description,
        env_vars=env_vars,
        base_url=cpa.base_url_for(root, api_mode),
        models_url=root + "/v1/models",
        auth_type="api_key",
    )
    profile._cpa_instance = instance
    profile._cpa_group = group
    profile._cpa_sync()
    return profile


def _claim(name: str, owner: str) -> bool:
    with _TAKEN_LOCK:
        holder = _TAKEN.get(name)
        if holder is not None and holder != owner:
            return False
        _TAKEN[name] = owner
        return True


def _register(instance: _Instance, group: str | None) -> None:
    name = instance.spec.group_name(group) if group else instance.spec.wide_name
    if name in instance.profiles:
        return
    if not _claim(name, instance.spec.name):
        # Hyphens are legal in instance and provider names ("a"+"b-c" vs "a-b"+"c").
        logger.warning(
            "cliproxyapi %s: provider name %s is already used; skipped", instance.spec.name, name
        )
        return
    profile = _build_profile(instance, group)
    instance.profiles[name] = profile
    # Run in the import-time context: Hermes binds the registration target (this
    # profile home's layer, in multi-profile gateways) in a ContextVar during the
    # plugin import. Later registrations from fetch_models must land in the same
    # home, not in the process-wide registry. A fresh copy per call, because one
    # Context object cannot be entered by two threads at once.
    _IMPORT_CONTEXT.copy().run(register_provider, profile)


def _register_groups(instance: _Instance) -> None:
    for group in sorted(instance.groups):
        if instance.groups[group] and cpa.slug(group) == group:
            _register(instance, group)


def register_all() -> list[str]:
    home = _hermes_home()
    specs, errors = cpa.parse_config(_read_config(home))
    for error in errors:
        logger.warning("cliproxyapi config: %s", error)
    for spec in specs:
        instance = _Instance(spec, home, cpa.load_cache(home, spec.name))
        _INSTANCES[spec.name] = instance
        _register(instance, None)
        _register_groups(instance)
    return [name for instance in _INSTANCES.values() for name in instance.profiles]


register_all()

"""Hermes model-provider plugin: mirror CLIProxyAPI provider groups as Hermes providers.

Registers one ``ProviderProfile`` per (CPA instance x provider group) declared in
the ``cliproxyapi`` section of the Hermes ``config.yaml``. Registration reads only
config and the local catalog cache; network calls happen in ``fetch_models``.
"""

from __future__ import annotations

import logging
import os
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


def _read_config(home: Path) -> dict[str, Any]:
    """Parse ``config.yaml`` directly; importing hermes_cli.config during discovery can cycle.

    Hermes ships its YAML reader as ``hermes_yaml`` (ruamel); PyYAML is not guaranteed.
    """
    try:
        import hermes_yaml as yaml_reader
    except ImportError:  # pragma: no cover - older Hermes
        try:
            import yaml as yaml_reader  # type: ignore[no-redef]
        except ImportError:
            logger.warning("cliproxyapi: no YAML reader available; config ignored")
            return {}
    try:
        with open(home / "config.yaml", encoding="utf-8") as handle:
            data = yaml_reader.safe_load(handle)
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 - any parser error means "no usable config"
        logger.warning("cliproxyapi: config.yaml unreadable (%s)", type(exc).__name__)
        return {}
    return data if isinstance(data, dict) else {}


class CLIProxyAPIProfile(ProviderProfile):
    """One CPA provider group on one CPA instance.

    Private ``_cpa_*`` attributes are set by ``build_profile``. Inference headers
    are left to Hermes: CPA reacts to client User-Agents, so the plugin sets its
    own User-Agent only on its catalog requests.
    """

    _cpa_spec: cpa.ProfileSpec
    _cpa_home: Path
    _cpa_catalog: cpa.Catalog

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0
    ) -> list[str] | None:
        spec: cpa.ProfileSpec = self._cpa_spec
        if not api_key:
            return None
        # Callers pass the effective endpoint (a user *_BASE_URL override wins).
        root = cpa.root_url(base_url) if base_url else spec.root
        try:
            catalog = cpa.fetch_public_catalog(root, api_key, timeout=timeout)
        except cpa.GatewayError as exc:
            logger.warning("cliproxyapi %s: model list failed: %s", spec.name, exc)
            return None
        try:
            cpa.save_cache(self._cpa_home, spec.cache_key, catalog)
        except OSError as exc:
            logger.debug("cliproxyapi %s: cache write failed: %s", spec.name, exc)
        self._cpa_catalog = catalog
        self.model_capabilities.clear()
        self.model_capabilities.update(cpa.capabilities_for(catalog, spec.patterns))
        return cpa.match_models(catalog.ids, spec.patterns)

    def supported_reasoning_efforts(self, model: str | None) -> tuple[str, ...] | None:
        efforts = self._cpa_catalog.meta.get(model or "", {}).get("reasoning_efforts")
        return tuple(efforts) if efforts else None

    def get_max_tokens(self, model: str | None) -> int | None:
        value = self._cpa_catalog.meta.get(model or "", {}).get("max_output_tokens")
        return value if isinstance(value, int) and value > 0 else self.default_max_tokens


def build_profile(spec: cpa.ProfileSpec, home: Path, catalog: cpa.Catalog) -> CLIProxyAPIProfile:
    label = spec.display_name or f"CLIProxyAPI {spec.instance} ({spec.group})"
    env_vars = (spec.key_env, spec.url_env) if spec.url_env else (spec.key_env,)
    profile = CLIProxyAPIProfile(
        name=spec.name,
        api_mode=spec.api_mode,
        display_name=label,
        description=f"CLIProxyAPI instance '{spec.instance}', provider group '{spec.group}'",
        env_vars=env_vars,
        base_url=spec.base_url,
        models_url=spec.root + "/v1/models",
        auth_type="api_key",
        fallback_models=tuple(cpa.match_models(catalog.ids, spec.patterns)),
        model_capabilities=cpa.capabilities_for(catalog, spec.patterns),
    )
    profile._cpa_spec = spec
    profile._cpa_home = home
    profile._cpa_catalog = catalog
    return profile


def register_all() -> list[str]:
    home = _hermes_home()
    specs, errors = cpa.parse_config(_read_config(home))
    for error in errors:
        logger.warning("cliproxyapi config: %s", error)
    catalogs: dict[str, cpa.Catalog] = {}
    names: list[str] = []
    for spec in [cpa.default_spec(), *specs]:
        catalog = catalogs.setdefault(spec.cache_key, cpa.load_cache(home, spec.cache_key))
        register_provider(build_profile(spec, home, catalog))
        names.append(spec.name)
    return names


register_all()

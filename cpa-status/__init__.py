"""Hermes plugin: ``/cpa`` shows account health for every CLIProxyAPI pool.

Companion to the ``cliproxyapi-provider`` model-provider plugin (same repo).
It reads the same ``cliproxyapi`` section of ``config.yaml`` and, only when a
user runs ``/cpa``, sends one read-only management GET per pool. See
``status.py`` for the rules.

On Hermes 0.23.1 and later it also hides the keys that no provider declares
from the commands a chat session runs (see ``shell_hidden_names``). The
provider plugin cannot do this: a model-provider plugin has no ``register``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

try:  # loaded by Hermes as a package
    from . import status
except ImportError:  # pragma: no cover - direct import in tests
    import status  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# plugins.entries.cpa-status.settings.<SETTING>; hiding is on unless it says off.
SETTING = "hide_keys_from_shells"
_OFF_WORDS = frozenset({"false", "off", "no", "0"})
# The name rule of Hermes' session-env hook, and of key names in cpa.py.
_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")


def register(ctx) -> None:
    # No network here: doctor blocks sockets during register().
    ctx.register_command(
        "cpa",
        status.handle,
        description="CLIProxyAPI pool status: /cpa [pool] [provider] · /cpa quota <pool>",
    )
    try:
        _hide_keys(ctx)
    except Exception as exc:  # noqa: BLE001 - hiding a key must never cost the command
        logger.warning("cpa-status: keys are not hidden from agent shells (%s)", type(exc).__name__)


def shell_hidden_names(config: Any) -> list[str]:
    """Key variables of the ``cliproxyapi`` section that no provider declares.

    Hermes strips from agent shells only the key a provider profile declares as
    its inference key. That leaves every ``mgmt_key_env``, and the CPA
    ``key_env`` of an instance with a billing route, whose profiles declare the
    gateway key. Read straight from the config, with no address lookup, and
    also for an instance the provider skips: its keys are in the environment
    all the same.
    """
    section = config.get("cliproxyapi") if isinstance(config, dict) else None
    instances = section.get("instances") if isinstance(section, dict) else None
    names: list[str] = []
    for body in instances.values() if isinstance(instances, dict) else ():
        if not isinstance(body, dict):
            continue
        wanted = [body.get("mgmt_key_env")]
        if body.get("route_via"):
            wanted.append(body.get("key_env"))
        for name in wanted:
            if isinstance(name, str) and _NAME_RE.fullmatch(name) and name not in names:
                names.append(name)
    return names


def _is_off(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in _OFF_WORDS
    return isinstance(value, (bool, int)) and not value


def _unset(name: str):
    def resolver(_session_id: str) -> dict[str, None]:
        return {name: None}  # None: Hermes removes the variable from the child

    return resolver


def _hide_keys(ctx) -> None:
    hide = getattr(ctx, "register_session_env", None)
    if hide is None:  # Hermes before 0.23.1
        return
    if _is_off(ctx.get_config(SETTING, True)):
        return
    for name in shell_hidden_names(status._read_config(status._hermes_home())):
        if name.upper().startswith("HERMES_"):
            logger.warning(
                "cpa-status: %s stays visible to agent shells: Hermes keeps that prefix for itself",
                name,
            )
            continue
        try:
            # One registration per name, so one refused name leaves the others hidden.
            hide([name], _unset(name))
        except ValueError:
            logger.warning(
                "cpa-status: %s stays visible to agent shells: Hermes refused the name "
                "(another plugin may hold it)",
                name,
            )

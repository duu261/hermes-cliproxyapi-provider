"""Hermes plugin: ``/cpa`` shows account health for every CLIProxyAPI pool.

Companion to the ``cliproxyapi-provider`` model-provider plugin (same repo).
It reads the same ``cliproxyapi`` section of ``config.yaml`` and, only when a
user runs ``/cpa``, sends one read-only management GET per pool. See
``status.py`` for the rules.
"""

from __future__ import annotations

try:  # loaded by Hermes as a package
    from . import status
except ImportError:  # pragma: no cover - direct import in tests
    import status  # type: ignore[no-redef]


def register(ctx) -> None:
    # No I/O here: doctor blocks sockets during register().
    ctx.register_command(
        "cpa",
        status.handle,
        description="CLIProxyAPI pool status: /cpa [pool] [provider] · /cpa quota <pool>",
    )

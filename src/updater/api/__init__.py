"""The HTTP API the approved dashboard prototype runs on.

BACKEND_PLAN.md is the build order and the prototype in
``docs/frontend/asil-dashboard-DRAFT-FINAL.html`` is the contract. In phase 0
every endpoint is backed by fixtures extracted from that prototype; later phases
replace them with the real pipeline one endpoint at a time.

Needs the ``api`` extra: ``uv sync --extra api``. Pydantic is confined to this
package, at the boundary; nothing outside it imports from here.
"""

from __future__ import annotations

from updater.api.app import create_app

__all__ = ["create_app"]

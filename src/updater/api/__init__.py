"""The HTTP API the approved dashboard runs on.

The prototype in ``docs/frontend/asil-dashboard-DRAFT-FINAL.html`` is the
contract. Its content is served from fixtures extracted from it, and what people
do with that content is kept in :mod:`updater.state`; later phases replace the
fixtures with the real pipeline.

Needs the ``api`` extra: ``uv sync --extra api``. Pydantic is confined to this
package, at the boundary; nothing outside it imports from here.
"""

from __future__ import annotations

from updater.api.app import create_app

__all__ = ["create_app"]

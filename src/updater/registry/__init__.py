"""The approved-source registry (BUILD_SPEC.md section 5, Phase 1).

Loads the YAML list of sources ASIL has approved and hands back
:class:`updater.models.Source` records. Design rule 3 is enforced here: a file
path goes through :meth:`Registry.source_for_path` and an id through
:meth:`Registry.get`, and both refuse anything not on the list. Nothing else in
the pipeline may mint a ``Source``.

Usage::

    from updater.registry import load_registry

    # ./registry.yaml, or wherever $UPDATER_REGISTRY_PATH points
    registry = load_registry()
    source = registry.source_for_path(path)  # raises SourceNotRegistered
"""

from __future__ import annotations

from updater.registry.loader import (
    DEFAULT_REGISTRY_PATH,
    REGISTRY_PATH_ENV_VAR,
    MalformedRegistry,
    Registry,
    RegistryError,
    RegistryNotFound,
    SourceNotRegistered,
    load_registry,
    registry_path,
)

__all__ = [
    "DEFAULT_REGISTRY_PATH",
    "REGISTRY_PATH_ENV_VAR",
    "MalformedRegistry",
    "Registry",
    "RegistryError",
    "RegistryNotFound",
    "SourceNotRegistered",
    "load_registry",
    "registry_path",
]

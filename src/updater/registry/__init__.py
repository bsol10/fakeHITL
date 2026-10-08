"""The approved-source registry (BUILD_SPEC.md section 5, Phase 1).

Loads the YAML list of sources ASIL has approved and hands back
:class:`updater.models.Source` records.

This package is where design rule 3 is enforced: if a document is not in the
registry, it is not evidence. No open web search, no fetching a URL because it
looked relevant. The registry is the boundary of what the system may cite.

The boundary is a function, not a convention. Anything that starts from a file
path calls :meth:`Registry.source_for_path`, which refuses an unregistered file;
anything that starts from an id calls :meth:`Registry.get`, which refuses an
unknown one. Neither can be satisfied by inventing a ``Source``, and without a
``Source`` there is nothing to anchor a passage to.

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

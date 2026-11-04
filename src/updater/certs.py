"""Make HTTPS clients trust the certificates this machine already trusts.

The build network runs a TLS-intercepting proxy (Norton Web/Mail Shield) that
reissues every certificate under its own root. That root is in the Windows
certificate store but not in ``certifi``, so clients verifying against
``certifi`` fail: ``httpx``, and so ``huggingface_hub``, which reports it
misleadingly as *"We couldn't connect to https://huggingface.co"*. Same cause as
``system-certs = true`` under ``[tool.uv]`` in pyproject.toml.

:func:`ensure_system_certs` exports the OS trust store to a PEM once and points
the variables in :data:`CA_BUNDLE_ENV_VARS` at it. It is deliberately
conservative:

* it never overrides a bundle the operator configured;
* it does nothing off Windows (``ssl.enum_certificates`` exists only there);
* it only *adds* roots the OS already trusts, so it cannot make verification
  more permissive than the machine's own browsers.

If another client starts failing on certificates, this is the knob. Never use
``verify=False``.
"""

from __future__ import annotations

import logging
import os
import ssl
from pathlib import Path

__all__ = ["CA_BUNDLE_ENV_VARS", "ensure_system_certs", "export_system_certs"]

LOGGER = logging.getLogger(__name__)

CA_BUNDLE_ENV_VARS = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
"""The variables the HTTPS clients in this project read, in the order they win."""

_WINDOWS_CERT_STORES = ("ROOT", "CA")
"""Machine and user roots, then intermediates. An intercepting proxy's root
lands in ROOT; some corporate images put the signing CA in CA instead."""


def export_system_certs(destination: Path) -> int:
    """Write the OS trust store to ``destination`` as PEM. Returns the count.

    Every certificate is exported, not only those flagged for server
    authentication: filtering on that purpose drops the proxy's root, which is
    the certificate this export exists to capture.
    """
    enumerate_certs = getattr(ssl, "enum_certificates", None)
    if enumerate_certs is None:
        return 0

    seen: set[bytes] = set()
    blocks: list[str] = []
    for store in _WINDOWS_CERT_STORES:
        try:
            certificates = enumerate_certs(store)
        except OSError as error:  # pragma: no cover - depends on the machine
            LOGGER.debug("could not read certificate store %s: %s", store, error)
            continue
        for der, encoding, _trust in certificates:
            if encoding != "x509_asn" or der in seen:
                continue
            seen.add(der)
            blocks.append(ssl.DER_cert_to_PEM_cert(der))

    if not blocks:
        return 0

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("".join(blocks), encoding="ascii")
    return len(blocks)


def ensure_system_certs(cache_dir: Path | None = None) -> Path | None:
    """Point the HTTPS clients at the OS trust store, if they are not already.

    Returns the bundle path when one is now in effect, or ``None`` when nothing
    was needed (a bundle is already configured, or this platform's default trust
    store is the OS one).

    Call it before anything that downloads. Idempotent, and cheap after the
    first call.
    """
    for variable in CA_BUNDLE_ENV_VARS:
        configured = os.environ.get(variable)
        if configured and Path(configured).is_file():
            return Path(configured)

    if not hasattr(ssl, "enum_certificates"):
        return None

    cache = cache_dir or Path.home() / ".cache" / "asil-updater"
    bundle = cache / "system-ca-bundle.pem"
    if not bundle.is_file():
        count = export_system_certs(bundle)
        if not count:
            LOGGER.warning(
                "could not export the OS trust store; HTTPS downloads may fail "
                "certificate verification behind an intercepting proxy"
            )
            return None
        LOGGER.info("exported %d OS certificates to %s", count, bundle)

    for variable in CA_BUNDLE_ENV_VARS:
        os.environ.setdefault(variable, str(bundle))
    return bundle

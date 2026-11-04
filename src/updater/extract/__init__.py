"""Evidence extraction (BUILD_SPEC.md section 4, ``extract_evidence``).

One structured-output model call per claim over its retrieved passages, then a
string match of every quoted span against the passage it names. A quote that is
not there is discarded, logged and counted. That check is the hallucination
guard, and it is not optional.

Modules, in the order a call passes through them:

``evidence``  the stage: prompt, schema, the call, and what is kept or discarded
``verify``    finding a quote in a passage; pure, and imports no provider
``model``     the provider interface and its two errors
``provider``  ``UPDATER_MODEL_PROVIDER`` to a model
``claude``    Claude; the only module that imports ``anthropic``
``gemini``    Gemini; the only module that imports ``google-genai``

Importing this package imports neither SDK. Usage::

    from updater.extract import extract_evidence, model_from_env

    result = extract_evidence(claim.text, passages, model=model_from_env())
    for found in result.evidence:
        print(found.evidence.finding, found.quote, found.evidence.anchor)
    print(result.discarded_count, "discarded")
"""

from __future__ import annotations

from updater.extract.evidence import (
    EXACT_QUOTE_CONFIDENCE,
    WHITESPACE_QUOTE_CONFIDENCE,
    DiscardedFinding,
    DiscardReason,
    ExtractedEvidence,
    ExtractionResult,
    FindingType,
    evidence_schema,
    extract_evidence,
)
from updater.extract.model import (
    EvidenceModel,
    ModelCallFailed,
    ModelError,
    ModelReply,
    ModelUnavailable,
)
from updater.extract.provider import (
    DEFAULT_PROVIDER,
    PROVIDER_ENV_VAR,
    PROVIDERS,
    model_from_env,
)
from updater.extract.verify import VerifiedSpan, locate_quote, verify_quote

__all__ = [
    "DEFAULT_PROVIDER",
    "EXACT_QUOTE_CONFIDENCE",
    "PROVIDERS",
    "PROVIDER_ENV_VAR",
    "WHITESPACE_QUOTE_CONFIDENCE",
    "DiscardReason",
    "DiscardedFinding",
    "EvidenceModel",
    "ExtractedEvidence",
    "ExtractionResult",
    "FindingType",
    "ModelCallFailed",
    "ModelError",
    "ModelReply",
    "ModelUnavailable",
    "VerifiedSpan",
    "evidence_schema",
    "extract_evidence",
    "locate_quote",
    "model_from_env",
    "verify_quote",
]

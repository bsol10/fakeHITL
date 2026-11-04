"""Four-way claim comparison (BUILD_SPEC.md section 4, ``compare``).

Classifies each prior-year claim against this year's evidence:

``updated``
    Evidence contradicts or supersedes the prior claim.
``unchanged``
    Evidence corroborates it.
``new``
    A finding with no prior-year counterpart.
``cannot-verify``
    The default, whenever retrieval came back thin.

Every status carries a written rationale, ``unchanged`` included: the rationale
is as much the deliverable as the label.
"""

from __future__ import annotations

__all__: list[str] = []

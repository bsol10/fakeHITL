"""Token counting, in the embedding model's own units.

The chunking stage is the first stage with a budget, so per DESIGN.md it is the
stage that owns real token counting and replaces the four-characters-per-token
estimate ingest uses to sort by.

**The unit is a word piece of the embedding model's tokenizer, not a
general-purpose "token".** That is a deliberate choice: the only budget that can
actually be violated is the embedding model's input window, so the budget is
measured in the units that window is expressed in. Counting in some other
tokenizer's units would produce a number that is nobody's constraint.

Two measurements from the real corpus, both arguing for counting rather than
estimating:

* prose runs about 4.3 characters per word piece — close to ingest's estimate;
* a serialised table row runs about 3.5, because digits and punctuation
  fragment. Tabular sources are most of ASIL's material, so the estimate is
  optimistic by roughly 20% exactly where the corpus is densest.

One trap, found the hard way and worth stating because it is invisible: the
``tokenizer.json`` published with all-MiniLM-L6-v2 carries **truncation and
padding at 128 tokens**. Sentence-transformers overrides both at load time, but
a bare ``tokenizers.Tokenizer`` does not — so counting with it untouched silently
reports 128 for every long passage, and every chunk would come out at the wrong
size with nothing to show for it. :func:`load_tokenizer` disables both.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Protocol

from updater.certs import ensure_system_certs

if TYPE_CHECKING:
    from tokenizers import Tokenizer

__all__ = [
    "EMBEDDING_MODEL_ID",
    "MODEL_WINDOW_TOKENS",
    "SPECIAL_TOKEN_ALLOWANCE",
    "USABLE_WINDOW_TOKENS",
    "TokenCounter",
    "TokenizerUnavailable",
    "WordPieceCounter",
    "default_token_counter",
    "load_tokenizer",
]

EMBEDDING_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
"""The model BUILD_SPEC.md section 4 names for embedding."""

MODEL_WINDOW_TOKENS = 256
"""``max_seq_length`` of the embedding model, in word pieces.

Text beyond this is **silently truncated** by sentence-transformers — no
warning, no error, just a vector computed from a prefix. That is the
silent-loss failure this project keeps ruling out, so nothing here relies on the
model to handle overlong input; see :mod:`updater.retrieve.embed`, which windows
instead.
"""

SPECIAL_TOKEN_ALLOWANCE = 2
"""``[CLS]`` and ``[SEP]``, which the model spends from the same 256."""

USABLE_WINDOW_TOKENS = MODEL_WINDOW_TOKENS - SPECIAL_TOKEN_ALLOWANCE
"""Word pieces of real text that fit in one forward pass."""


class TokenizerUnavailable(RuntimeError):
    """The embedding model's tokenizer could not be loaded.

    Chunk boundaries depend on the counter, so falling back to an estimate would
    quietly produce a different index from the one every other environment
    builds. Better to stop.
    """


class TokenCounter(Protocol):
    """Anything that can say how long a piece of text is, in tokens.

    A protocol rather than a concrete class so the chunking tests can inject a
    deterministic counter and assert boundary behaviour without loading a model
    — and so a future evaluation can measure chunk sizes in a second tokenizer's
    units without touching the chunker.
    """

    def count(self, text: str) -> int: ...


@lru_cache(maxsize=1)
def load_tokenizer() -> Tokenizer:
    """The embedding model's tokenizer, with padding and truncation disabled.

    Uses ``tokenizers`` directly rather than ``sentence_transformers``: chunking
    needs to count, not to embed, and the light path avoids importing torch for
    a stage that has no use for it.
    """
    ensure_system_certs()
    try:
        from tokenizers import Tokenizer as HfTokenizer
    except ImportError as error:  # pragma: no cover - depends on the install
        raise TokenizerUnavailable(
            "the 'tokenizers' package is not installed. Chunking needs the "
            "embedding model's tokenizer to count tokens in the units the "
            "model's input window is expressed in. Install the extra: "
            "uv sync --extra retrieve"
        ) from error

    try:
        tokenizer = HfTokenizer.from_pretrained(EMBEDDING_MODEL_ID)
    except Exception as error:  # pragma: no cover - network or cache state
        raise TokenizerUnavailable(
            f"could not load the tokenizer for {EMBEDDING_MODEL_ID}: {error}. "
            f"The first run downloads it from the Hugging Face hub; behind a "
            f"TLS-intercepting proxy see updater.certs."
        ) from error

    # See the module docstring: the published tokenizer.json truncates and pads
    # at 128, which would cap every count at 128 without saying so.
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return tokenizer


@dataclass(frozen=True, slots=True)
class WordPieceCounter:
    """Counts word pieces of the embedding model's vocabulary.

    Special tokens are excluded from the count and accounted for separately by
    :data:`USABLE_WINDOW_TOKENS`, so a caller comparing a count against a budget
    is comparing like with like.
    """

    tokenizer: Tokenizer

    def count(self, text: str) -> int:
        if not text:
            return 0
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)


def default_token_counter() -> TokenCounter:
    """The counter every stage should use unless a test says otherwise."""
    return WordPieceCounter(load_tokenizer())

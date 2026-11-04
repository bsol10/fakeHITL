"""Token counting, in the embedding model's own word pieces.

Chunking is the first stage with a budget, so it owns real counting; ingest's
four-characters-per-token estimate is optimistic on table rows, where digits
and punctuation fragment. The unit is a word piece of the embedding model's
tokenizer because the only budget that can be violated is that model's input
window.
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
"""``max_seq_length`` of the embedding model, in word pieces. sentence-transformers
silently truncates beyond it, so :mod:`updater.retrieve.embed` windows instead."""

SPECIAL_TOKEN_ALLOWANCE = 2
"""``[CLS]`` and ``[SEP]``, which the model spends from the same 256."""

USABLE_WINDOW_TOKENS = MODEL_WINDOW_TOKENS - SPECIAL_TOKEN_ALLOWANCE
"""Word pieces of real text that fit in one forward pass."""


class TokenizerUnavailable(RuntimeError):
    """The embedding model's tokenizer could not be loaded.

    There is no fallback to an estimate: chunk boundaries depend on the counter,
    so it would quietly build a different index from every other environment's.
    """


class TokenCounter(Protocol):
    """Anything that can say how long a piece of text is, in tokens.

    A protocol so tests can inject a deterministic counter without a model.
    """

    def count(self, text: str) -> int: ...


@lru_cache(maxsize=1)
def load_tokenizer() -> Tokenizer:
    """The embedding model's tokenizer, with padding and truncation disabled.

    Uses ``tokenizers`` directly rather than ``sentence_transformers``, so
    counting does not import torch.
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

    # Trap: the tokenizer.json published with the model truncates and pads at
    # 128 tokens. sentence-transformers overrides both at load; a bare Tokenizer
    # does not, so without these two lines every long count silently reads 128.
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

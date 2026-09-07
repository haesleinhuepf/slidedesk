"""Optional embedding-based semantic search via the KIARA OpenAI-compatible API.

Caches one embedding vector per slide (see `slide_embeddings` table in db.py) so
that only new/changed slide text needs to be sent to the embedding endpoint;
searching just embeds the query and compares it against the cached vectors.
"""
from __future__ import annotations

import array
import math
import os
from typing import List, Sequence

DEFAULT_BASE_URL = "https://kiara.sc.uni-leipzig.de/api/"
DEFAULT_MODEL = "vllm-multilingual-e5-large-instruct"


class EmbeddingError(RuntimeError):
    """Raised when an embedding could not be computed (missing deps/key, API error, ...)."""


def _get_client():
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise EmbeddingError("The 'openai' package is required for semantic search") from exc

    api_key = os.environ.get("KIARA_API_KEY")
    if not api_key:
        raise EmbeddingError("KIARA_API_KEY environment variable is not set")

    base_url = os.environ.get("KIARA_BASE_URL", DEFAULT_BASE_URL)
    return OpenAI(base_url=base_url, api_key=api_key)


def is_available() -> bool:
    """Whether semantic search can plausibly be used (deps + API key present)."""
    try:
        _get_client()
    except EmbeddingError:
        return False
    return True


def embed_kiara(text: str, embedding_model: str = DEFAULT_MODEL, client=None) -> List[float]:
    """Embed `text` using the KIARA OpenAI-compatible embeddings endpoint."""
    client = client or _get_client()
    response = client.embeddings.create(model=embedding_model, input=text)
    return response.data[0].embedding


def pack_vector(vector: Sequence[float]) -> bytes:
    """Serialize an embedding vector to bytes for storage in the DB."""
    return array.array("f", vector).tobytes()


def unpack_vector(blob: bytes) -> array.array:
    """Deserialize an embedding vector previously stored with `pack_vector`."""
    arr = array.array("f")
    arr.frombytes(blob)
    return arr


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

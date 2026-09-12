"""Optional embedding-based semantic search via a local Hugging Face model.

Caches one embedding vector per slide (see `slide_embeddings` table in db.py) so
that only new/changed slide text needs to be sent to the embedding endpoint;
searching just embeds the query and compares it against the cached vectors.
"""
from __future__ import annotations

import array
import math
import threading
from typing import List, Sequence

DEFAULT_MODEL = "intfloat/multilingual-e5-large-instruct"

_local_model_cache = {}
_local_model_lock = threading.Lock()


class EmbeddingError(RuntimeError):
    """Raised when an embedding could not be computed (missing deps/key, API error, ...)."""


def is_available() -> bool:
    """Check whether the local embedding dependencies are installed."""
    try:
        import torch
        import transformers
    except ImportError:
        return False
    return True


def embed_local(text: str, embedding_model: str = DEFAULT_MODEL) -> List[float]:
    """Embed `text` using a locally downloaded Hugging Face E5 model."""
    try:
        import torch
        from transformers import AutoModel, AutoTokenizer
    except ImportError as exc:
        raise EmbeddingError("The 'torch' and 'transformers' packages are required for embeddings") from exc

    with _local_model_lock:
        if embedding_model not in _local_model_cache:
            tokenizer = AutoTokenizer.from_pretrained(embedding_model)
            model = AutoModel.from_pretrained(embedding_model)
            model.eval()
            _local_model_cache[embedding_model] = (tokenizer, model)
        tokenizer, model = _local_model_cache[embedding_model]

    inputs = tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
    with torch.inference_mode():
        outputs = model(**inputs)
        token_embeddings = outputs.last_hidden_state
        attention_mask = inputs["attention_mask"].unsqueeze(-1).expand(token_embeddings.size()).float()
        pooled = (token_embeddings * attention_mask).sum(dim=1) / attention_mask.sum(dim=1).clamp(min=1e-9)
        normalized = torch.nn.functional.normalize(pooled, p=2, dim=1)
    return normalized[0].cpu().tolist()


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

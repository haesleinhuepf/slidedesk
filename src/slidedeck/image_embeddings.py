"""Lazy, process-local CLIP inference; persistent vectors live in the project DB."""
from __future__ import annotations

import threading
from importlib.util import find_spec

from .embeddings import EmbeddingError

MODEL = "openai/clip-vit-base-patch32"
_lock = threading.Lock()
_models = {}


def is_available() -> bool:
    """Check dependencies without loading or downloading the model."""
    return all(find_spec(name) is not None for name in ("torch", "transformers"))


def embed_image(image, model: str = MODEL):
    """Load CLIP once and embed an RGB slide without tracking gradients."""
    with _lock:
        try:
            import torch
            from transformers import CLIPModel, CLIPProcessor

            if model not in _models:
                encoder = CLIPModel.from_pretrained(model)
                encoder.eval()
                processor = CLIPProcessor.from_pretrained(model)
                _models[model] = encoder, processor
            encoder, processor = _models[model]
            inputs = processor(images=image.convert("RGB"), return_tensors="pt")
            with torch.no_grad():
                features = encoder.get_image_features(**inputs)
                # Transformers 5 returns a model output; 4 returns a tensor.
                if hasattr(features, "pooler_output"):
                    features = features.pooler_output
                return features.squeeze(0).cpu().tolist()
        except Exception as exc:
            raise EmbeddingError(f"CLIP image embedding failed: {exc}") from exc

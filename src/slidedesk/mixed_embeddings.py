"""Mixed slide embeddings: a local VLM describes the strip-layout slide image,
then the description is embedded with the local text-embedding model.
"""
from __future__ import annotations

import threading
from importlib.util import find_spec
from typing import List, Tuple

from . import embeddings
from .embeddings import EmbeddingError, get_device

VLM_MODEL = "Qwen/Qwen3-VL-2B-Thinking" # "google/gemma-3-4b-it"
PROMPT = (
    "Given this image of a powerpoint slide, please write a short abstract what is "
    "shown in this slide. Ignore layout and logos. Focus on the conceptual content of "
    "the slide. If the slide contains nothing, or just acknowledgements or just a "
    'headline, answer with "No content".'
)
NO_CONTENT = "no content"

_lock = threading.Lock()
_models = {}


def is_available() -> bool:
    """Check dependencies without loading or downloading the models."""
    return all(find_spec(name) is not None for name in ("torch", "transformers"))


def is_no_content(description: str) -> bool:
    """True if the VLM answered that the slide has no conceptual content."""
    return description.strip().strip("\"'.!* \n").lower() == NO_CONTENT


def describe_image(image, model: str = VLM_MODEL, prompt: str = PROMPT) -> str:
    """Ask the VLM (loaded once per process) to summarize a slide image."""
    import time
    with _lock:
        start_time = time.time()
        try:
            import torch
            from transformers import AutoModelForImageTextToText, AutoProcessor

            device = get_device()
            cache_key = (model, str(device))
            if cache_key not in _models:
                dtype = torch.bfloat16 if str(device) == "cuda" else torch.float32
                # low_cpu_mem_usage would place weights on the "meta" device; disable it
                # so the model is materialized directly and can be moved with .to(device).
                vlm = AutoModelForImageTextToText.from_pretrained(
                    model, dtype=dtype, low_cpu_mem_usage=False
                )
                vlm.to(device)
                vlm.eval()
                processor = AutoProcessor.from_pretrained(model, use_fast=True)
                _models[cache_key] = vlm, processor
            vlm, processor = _models[cache_key]
            messages = [{"role": "user", "content": [
                {"type": "image", "image": image.convert("RGB")},
                {"type": "text", "text": prompt},
            ]}]
            inputs = processor.apply_chat_template(
                messages, add_generation_prompt=True, tokenize=True,
                return_dict=True, return_tensors="pt",
            )
            # Moves all tensors to the device; casts only floating ones (pixel values).
            inputs = inputs.to(device, dtype=vlm.dtype)
            with torch.inference_mode():
                output = vlm.generate(**inputs, max_new_tokens=256, do_sample=False)
            generated = output[0, inputs["input_ids"].shape[-1]:]
            result = processor.decode(generated, skip_special_tokens=True).strip()
            print("Image description:", result, "computation time: ", time.time() - start_time, "device:", device)
            return result
        except Exception as exc:
            print("Error occurred during image description.")
            raise EmbeddingError(f"VLM slide description failed: {exc}") from exc


def embed_mixed(image, vlm_model: str = VLM_MODEL,
                embedding_model: str = embeddings.DEFAULT_MODEL) -> Tuple[str, List[float]]:
    """Describe a (strip-layout) slide image with the VLM and embed the description.

    Returns `(description, vector)`. The vector is empty when the VLM reports
    "No content", so such slides are cached but never matched.
    """
    description = describe_image(image, vlm_model)
    if not description or is_no_content(description):
        return description, []
    return description, embeddings.embed_local(description, embedding_model=embedding_model)

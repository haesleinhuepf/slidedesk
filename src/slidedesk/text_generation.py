"""Local Hugging Face text generation for advanced slide-search outlines."""
from __future__ import annotations

import threading

MODEL = "ibm-granite/granite-4.1-3b"
SYSTEM_PROMPT = (
    "Generate a bullet point list of slides in a given context. Tell a story going "
    "from the basics to advanced details such as underlying methods, technology, "
    "usage, risks and limitations unless requested differently. Answer ONLY with "
    "the bullet point list, no further explanation required."
)

_model_lock = threading.Lock()
_model_cache = None


class TextGenerationError(RuntimeError):
    """Raised when the local text-generation model cannot produce an answer."""


def generate_list(prompt: str) -> str:
    """Generate a slide outline from a user prompt using the cached local model."""
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise TextGenerationError(
            "The 'torch' and 'transformers' packages are required for text generation"
        ) from exc

    global _model_cache
    try:
        with _model_lock:
            if _model_cache is None:
                tokenizer = AutoTokenizer.from_pretrained(MODEL)
                model = AutoModelForCausalLM.from_pretrained(MODEL, dtype=torch.float32)
                model.eval()
                _model_cache = (tokenizer, model)
            tokenizer, model = _model_cache

            conversation = tokenizer.apply_chat_template(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                tokenize=False,
                add_generation_prompt=True,
            )
            inputs = tokenizer(conversation, return_tensors="pt")
            with torch.inference_mode():
                output = model.generate(
                    **inputs,
                    max_new_tokens=512,
                    do_sample=False,
                    **({"pad_token_id": tokenizer.eos_token_id}
                       if tokenizer.eos_token_id is not None else {}),
                )
            generated = output[0, inputs["input_ids"].shape[-1]:]
            return tokenizer.decode(generated, skip_special_tokens=True).strip()
    except TextGenerationError:
        raise
    except Exception as exc:
        raise TextGenerationError(f"Text generation failed: {exc}") from exc
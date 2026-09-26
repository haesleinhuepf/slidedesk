"""Local Hugging Face text generation for advanced slide-search outlines."""
from __future__ import annotations

import threading

from .embeddings import get_device

MODEL = "Qwen/Qwen3.5-0.8B"
SYSTEM_PROMPT = (
    "Generate a bullet point list of slides in a given context. Tell a story going "
    "from the basics to advanced details such as underlying methods, technology, "
    "usage, risks and limitations unless requested differently."
    "The bullet point list  should be single-level, one bullet point per slide."
    "Answer ONLY with the bullet point list, no further explanation required."
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

    print("Starting text generation for prompt:", prompt)

    global _model_cache
    try:
        device = get_device()
        with _model_lock:
            if _model_cache is None:
                tokenizer = AutoTokenizer.from_pretrained(MODEL)
                # low_cpu_mem_usage would place weights on the "meta" device; disable it
                # so the model is materialized directly and can be moved with .to(device).
                model = AutoModelForCausalLM.from_pretrained(
                    MODEL, dtype=torch.float32, low_cpu_mem_usage=False
                )
                print("Loaded model and tokenizer into cache.")
                model.to(device)
                print("Moved model to device:", device)
                model.eval()
                print("Set model to evaluation mode.")

                _model_cache = (tokenizer, model)
            tokenizer, model = _model_cache
            print("Using cached model and tokenizer.")
            conversation = tokenizer.apply_chat_template(
                [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                tokenize=False,
                add_generation_prompt=True,
            )
            print("Applied chat template to conversation.")
            inputs = tokenizer(conversation, return_tensors="pt")
            print("Tokenized conversation into model inputs.")
            inputs = {
                key: value.to(device) if hasattr(value, "to") else value
                for key, value in inputs.items()
            }
            print("Moved model inputs to device:", device)
            with torch.inference_mode():
                print("Starting model inference.")
                output = model.generate(
                    **inputs,
                    max_new_tokens=512,
                    do_sample=False,
                    **({"pad_token_id": tokenizer.eos_token_id}
                       if tokenizer.eos_token_id is not None else {}),
                )
                print("Model inference completed.")
            generated = output[0, inputs["input_ids"].shape[-1]:]
            print("Extracted generated tokens")
            decoded = tokenizer.decode(generated, skip_special_tokens=True).strip()
            print("Decoded generated text:", decoded)
            return decoded
    except TextGenerationError:
        print("Text generation failed with a TextGenerationError.")
        raise
    except Exception as exc:
        print("Text generation failed with an unexpected error:", exc)
        raise TextGenerationError(f"Text generation failed: {exc}") from exc
"""Local Hugging Face text generation for advanced slide-search outlines."""
from __future__ import annotations

import threading
from queue import Empty

from .embeddings import get_device

MODEL = "Qwen/Qwen3.5-0.8B"
SYSTEM_PROMPT = (
    "Generate a bullet point list of slides in a given context. Tell a story going "
    "from the basics to advanced details such as underlying methods, technology, "
    "usage, risks and limitations unless requested differently."
    "The bullet point list should be single-level (NO sub-bullets!), one bullet point starting with '* ' per slide."
    "Example prompt: 'list 5 slides about easter egg painting'."
    "Example response:"
    "* History of easter egg painting"
    "* Color theory: why it matters"
    "* Tools required: basic brushes and additional materials"
    "* Painting Techniques: how to apply them"
    "* Cultural significance of easter egg painting"
    ""
    "Answer ONLY with the SINGLE-level bullet point list, no further explanation required."
)

_model_lock = threading.Lock()
_model_cache = None


class TextGenerationError(RuntimeError):
    """Raised when the local text-generation model cannot produce an answer."""


def generate_list_stream(prompt: str, cancel_event: threading.Event):
    """Yield generated text chunks until generation completes or is cancelled."""
    try:
        import torch
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            StoppingCriteria,
            StoppingCriteriaList,
            TextIteratorStreamer,
        )
    except ImportError as exc:
        print("Error:", exc)
        import traceback
        traceback.print_exc()
        raise TextGenerationError(
            "The 'torch' and 'transformers' packages are required for text generation"
        ) from exc

    global _model_cache
    try:
        device = get_device()
        with _model_lock:
            if cancel_event.is_set():
                return
            if _model_cache is None:
                tokenizer = AutoTokenizer.from_pretrained(MODEL)
                model = AutoModelForCausalLM.from_pretrained(
                    MODEL, dtype=torch.float32, low_cpu_mem_usage=False
                )
                model.to(device)
                model.eval()
                _model_cache = (tokenizer, model)
            if cancel_event.is_set():
                return
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
            inputs = {
                key: value.to(device) if hasattr(value, "to") else value
                for key, value in inputs.items()
            }
            streamer = TextIteratorStreamer(
                tokenizer, skip_prompt=True, skip_special_tokens=True, timeout=0.1
            )

            class StopOnCancel(StoppingCriteria):
                def __call__(self, input_ids, scores, **kwargs):
                    return cancel_event.is_set()

            errors = []

            def run_generation():
                try:
                    with torch.inference_mode():
                        model.generate(
                            **inputs,
                            streamer=streamer,
                            stopping_criteria=StoppingCriteriaList([StopOnCancel()]),
                            max_new_tokens=512,
                            do_sample=False,
                            **({"pad_token_id": tokenizer.eos_token_id}
                               if tokenizer.eos_token_id is not None else {}),
                        )
                except Exception as exc:
                    errors.append(exc)
                    streamer.end()

            worker = threading.Thread(target=run_generation, daemon=True)
            worker.start()
            try:
                while True:
                    try:
                        chunk = next(streamer)
                    except Empty:
                        if not worker.is_alive():
                            break
                        continue
                    if chunk and not cancel_event.is_set():
                        yield chunk
            finally:
                worker.join()
            if errors:
                raise errors[0]
    except TextGenerationError:
        raise
    except Exception as exc:
        raise TextGenerationError(f"Text generation failed: {exc}") from exc


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
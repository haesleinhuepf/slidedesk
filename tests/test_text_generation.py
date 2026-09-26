from types import SimpleNamespace
from unittest.mock import Mock

import torch

from slidedesk import text_generation


def test_generate_list_uses_granite_chat_prompt_and_caches_model(monkeypatch):
    tokenizer = Mock()
    tokenizer.apply_chat_template.return_value = "formatted conversation"
    tokenizer.return_value = {
        "input_ids": torch.tensor([[1, 2]]),
        "attention_mask": torch.tensor([[1, 1]]),
    }
    tokenizer.eos_token_id = 2
    tokenizer.decode.return_value = "- Basics\n- Advanced methods"
    model = Mock()
    model.generate.return_value = torch.tensor([[1, 2, 3, 4]])
    tokenizer_factory = Mock(from_pretrained=Mock(return_value=tokenizer))
    model_factory = Mock(from_pretrained=Mock(return_value=model))
    monkeypatch.setattr("transformers.AutoTokenizer", tokenizer_factory)
    monkeypatch.setattr("transformers.AutoModelForCausalLM", model_factory)
    monkeypatch.setattr(text_generation, "_model_cache", None)

    assert text_generation.generate_list("List 20 slides about AI") == "- Basics\n- Advanced methods"
    assert text_generation.generate_list("A second prompt") == "- Basics\n- Advanced methods"

    tokenizer_factory.from_pretrained.assert_called_once_with(text_generation.MODEL)
    model_factory.from_pretrained.assert_called_once_with(
        text_generation.MODEL, dtype=torch.float32, low_cpu_mem_usage=False
    )
    messages = tokenizer.apply_chat_template.call_args_list[0].args[0]
    assert messages[0] == {"role": "system", "content": text_generation.SYSTEM_PROMPT}
    assert messages[1] == {"role": "user", "content": "List 20 slides about AI"}
    assert tokenizer.decode.call_args_list[0].args[0].tolist() == [3, 4]
    model.eval.assert_called_once()
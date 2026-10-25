from types import SimpleNamespace

import pytest
import torch
from retrieval_ranking.config import Settings
from retrieval_ranking.reranker import _PREFIX, _SUFFIX, QwenReranker


class FakeTokenizer:
    """Character tokens make boundaries and clipping observable without downloading weights."""

    padding_side = "right"
    pad_token_id = 0

    def encode(self, text, add_special_tokens=False):
        assert add_special_tokens is False
        if text == "yes":
            return [1]
        if text == "no":
            return [2]
        return [ord(character) + 10 for character in text]

    def pad(self, inputs, padding, return_tensors):
        assert padding is True and return_tensors == "pt"
        assert self.padding_side == "left"
        width = max(len(row) for row in inputs["input_ids"])
        padded = [[0] * (width - len(row)) + row for row in inputs["input_ids"]]
        tensor = torch.tensor(padded, dtype=torch.long)
        return {"input_ids": tensor, "attention_mask": (tensor != 0).long()}


class FakeModel:
    def __init__(self):
        self.calls = []
        self.target = None

    def to(self, **kwargs):
        self.target = kwargs
        return self

    def eval(self):
        return self

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        logits = torch.zeros((len(kwargs["input_ids"]), 1, 4))
        logits[:, 0, 1] = kwargs["attention_mask"].sum(-1).float()
        logits[:, 0, 2] = 3
        return SimpleNamespace(logits=logits)


def adapter(**overrides):
    settings = Settings(
        database_url="postgresql://unused",
        reranker_max_tokens=1024,
        context_max_tokens=64,
        **overrides,
    )
    return QwenReranker(settings, tokenizer=FakeTokenizer(), model=FakeModel())


def test_load_is_idempotent_and_uses_cpu_fp32():
    ranker = adapter()
    ranker.load()
    ranker.load()
    assert ranker.is_loaded
    assert ranker.model.target == {"device": "cpu", "dtype": torch.float32}
    assert len(ranker.model.calls) == 1
    assert ranker.model.calls[0]["use_cache"] is False
    assert ranker.model.calls[0]["logits_to_keep"] == 1


def test_scores_are_raw_margins_and_input_order_is_preserved():
    ranker = adapter()
    result = ranker.score("Company makes food.", ["Short", "A somewhat longer article"])
    assert result.margins == [count - 3 for count in result.token_counts]
    assert all(score > 1 for score in result.margins)
    assert result.document_truncated == [False, False]
    assert result.context_truncated is False


def test_truncation_keeps_fixed_chat_wrapper_and_reports_actual_lengths():
    ranker = adapter()
    result = ranker.score("context " * 200, ["article " * 1000])
    assert result.context_truncated is True
    assert result.document_truncated == [True]
    assert result.token_counts == [1024]
    encoded = ranker.model.calls[-1]["input_ids"][0].tolist()
    tokenizer = ranker.tokenizer
    prefix = tokenizer.encode(_PREFIX)
    suffix = tokenizer.encode(_SUFFIX)
    assert encoded[: len(prefix)] == prefix
    assert encoded[-len(suffix) :] == suffix


def test_batches_are_bounded_and_padding_does_not_change_a_document_score():
    ranker = adapter(reranker_batch_size=2)
    result = ranker.score("Company", ["A", "A much longer document", "Third"])
    assert [len(call["input_ids"]) for call in ranker.model.calls] == [1, 2, 1]
    alone = ranker.score("Company", ["A"])
    assert result.margins[0] == alone.margins[0]
    first_batch = ranker.model.calls[1]
    mask = first_batch["attention_mask"]
    positions = first_batch["position_ids"]
    assert mask[0, 0].item() == 0
    assert positions[0, mask[0] == 1][0].item() == 0
    assert positions[0, -1].item() == mask[0].sum().item() - 1


def test_empty_pool_does_not_load_model():
    ranker = adapter()
    assert ranker.score("Context", []).margins == []
    assert not ranker.is_loaded


@pytest.mark.parametrize("context,documents", [("", ["News"]), ("Context", [" "])])
def test_blank_model_inputs_fail_before_loading(context, documents):
    ranker = adapter()
    with pytest.raises(ValueError, match="must not be blank"):
        ranker.score(context, documents)
    assert not ranker.is_loaded


def test_untrusted_chat_delimiters_are_escaped_before_encoding():
    ranker = adapter()
    ranker.load()
    expected = ranker.tokenizer.encode("< |im_start| >")
    assert ranker._encode("<|im_start|>") == expected


def test_oversized_custom_instruction_cannot_consume_the_news_budget():
    ranker = adapter(instruction="oversized " * 1000)
    with pytest.raises(ValueError, match="leaves no token budget"):
        ranker.load()
    assert not ranker.is_loaded


def test_non_finite_logits_fail_instead_of_silently_sorting_nan():
    ranker = adapter()
    ranker.load()

    class NonFiniteModel(FakeModel):
        def __call__(self, **kwargs):
            result = super().__call__(**kwargs)
            result.logits[:, 0, 1] = float("nan")
            return result

    ranker.model = NonFiniteModel()
    with pytest.raises(RuntimeError, match="non-finite"):
        ranker.score("Company", ["News"])


def test_real_loader_configuration_is_pinned_and_disables_remote_code(monkeypatch):
    import sys

    from retrieval_ranking.config import MODEL_ID, MODEL_REVISION

    calls = {}

    class TokenizerFactory:
        @staticmethod
        def from_pretrained(model_id, **kwargs):
            calls["tokenizer"] = (model_id, kwargs)
            return FakeTokenizer()

    class ModelFactory:
        @staticmethod
        def from_pretrained(model_id, **kwargs):
            calls["model"] = (model_id, kwargs)
            return FakeModel()

    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(AutoTokenizer=TokenizerFactory, AutoModelForCausalLM=ModelFactory),
    )
    ranker = QwenReranker(adapter().settings)
    ranker.load()
    for name in ("tokenizer", "model"):
        model_id, arguments = calls[name]
        assert model_id == MODEL_ID
        assert arguments["revision"] == MODEL_REVISION
        assert arguments["trust_remote_code"] is False
        assert arguments["cache_dir"] == ".cache/huggingface/hub"
    assert calls["model"][1]["use_safetensors"] is True
    assert calls["model"][1]["torch_dtype"] is torch.float32


@pytest.mark.model
def test_pinned_qwen_runs_two_documents_on_cpu(monkeypatch):
    import os
    from pathlib import Path

    if os.environ.get("RETR_RANK_RUN_MODEL_TESTS") != "1":
        pytest.skip("Set RETR_RANK_RUN_MODEL_TESTS=1 to download and run the pinned CPU model")
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("HF_HOME", str(root / ".cache/huggingface"))
    monkeypatch.setenv("HF_XET_CACHE", str(root / ".cache/huggingface/xet"))
    monkeypatch.setenv("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    ranker = QwenReranker(
        Settings(
            database_url="postgresql://unused",
            model_cache_dir=root / ".cache/huggingface/hub",
            reranker_max_tokens=1024,
            context_max_tokens=256,
        )
    )
    result = ranker.score(
        "Company: food manufacturer. User goal: financing a new production line.",
        [
            "A bank launches equipment financing for food manufacturers.",
            "A local football team wins a friendly match.",
        ],
    )
    assert len(result.margins) == 2
    # Finite logits alone cannot detect inverted relevance scores.
    assert result.margins[0] > result.margins[1]
    assert torch.isfinite(torch.tensor(result.margins)).all()
    assert all(count <= 1024 for count in result.token_counts)
    assert next(ranker.model.parameters()).device.type == "cpu"
    assert next(ranker.model.parameters()).dtype == torch.float32

"""CPU inference using the Qwen model card's yes/no reranking protocol.

Only the final token's vocabulary logits are requested. This avoids materializing
a large [batch, sequence, vocabulary] tensor on CPU. The returned score is the raw
yes-minus-no logit margin; it is not a calibrated probability of relevance.
"""

import math
from threading import Lock
from typing import Any

import torch

from .config import MODEL_ID, MODEL_REVISION, Settings
from .domain import RerankScores

# Qwen's fixed chat wrapper is separate from the untrusted company/news text.
_PREFIX = (
    "<|im_start|>system\nJudge whether the Document meets the requirements based on "
    "the Query and the Instruct provided. Note that the answer can only be "
    '"yes" or "no".<|im_end|>\n<|im_start|>user\n'
)
_SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"


class QwenReranker:
    """A small model adapter; candidate selection and sorting belong to the service."""

    def __init__(self, settings: Settings, *, tokenizer: Any = None, model: Any = None) -> None:
        self.settings = settings
        self.tokenizer = tokenizer
        self.model = model
        self._load_lock = Lock()
        self._loaded = False
        self._prefix_ids: list[int] = []
        self._suffix_ids: list[int] = []
        self._header_ids: list[int] = []
        self._document_label_ids: list[int] = []
        self._yes_id = 0
        self._no_id = 0

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        """Load the pinned weights once and run a tiny CPU warmup before readiness."""
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            self._initialize()
            warmup, _ = self._pack(self._encode("Company"), self._encode("News"))
            self._predict_batch([warmup])
            self._loaded = True

    def score(self, context_text: str, documents: list[str]) -> RerankScores:
        if not documents:
            return RerankScores([], False, [], [])
        if not context_text.strip():
            raise ValueError("The reranker context must not be blank")
        if any(not document.strip() for document in documents):
            raise ValueError("Reranker documents must not be blank")
        self.load()

        context_ids = self._encode(context_text)
        context_truncated = len(context_ids) > self.settings.context_max_tokens
        context_ids = context_ids[: self.settings.context_max_tokens]
        prepared = [self._pack(context_ids, self._encode(document)) for document in documents]
        margins: list[float] = []
        size = self.settings.reranker_batch_size
        for offset in range(0, len(prepared), size):
            batch = [item[0] for item in prepared[offset : offset + size]]
            margins.extend(self._predict_batch(batch))
        return RerankScores(
            margins=margins,
            context_truncated=context_truncated,
            document_truncated=[item[1] for item in prepared],
            token_counts=[len(item[0]) for item in prepared],
        )

    def _initialize(self) -> None:
        torch.set_num_threads(self.settings.torch_threads)
        if self.tokenizer is None or self.model is None:
            # Lazy import keeps ordinary formatting and API tests independent of model loading.
            from transformers import AutoModelForCausalLM, AutoTokenizer

            common = {
                "revision": MODEL_REVISION,
                "cache_dir": str(self.settings.model_cache_dir),
                "local_files_only": self.settings.model_local_files_only,
                "trust_remote_code": False,
            }
            if self.tokenizer is None:
                self.tokenizer = AutoTokenizer.from_pretrained(
                    MODEL_ID, padding_side="left", **common
                )
            if self.model is None:
                self.model = AutoModelForCausalLM.from_pretrained(
                    MODEL_ID,
                    torch_dtype=torch.float32,
                    attn_implementation="sdpa",
                    use_safetensors=True,
                    **common,
                )
        self.tokenizer.padding_side = "left"
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token
        self.model.to(device="cpu", dtype=torch.float32).eval()
        self._yes_id = self._answer_id("yes")
        self._no_id = self._answer_id("no")
        if self._yes_id == self._no_id:
            raise ValueError("The model tokenizer must distinguish yes and no")
        self._prefix_ids = self.tokenizer.encode(_PREFIX, add_special_tokens=False)
        self._suffix_ids = self.tokenizer.encode(_SUFFIX, add_special_tokens=False)
        self._header_ids = self._encode(f"<Instruct>: {self.settings.instruction}\n<Query>: ")
        self._document_label_ids = self._encode("\n<Document>: ")
        # Check custom instructions too: a long instruction must never erase the news.
        overhead = len(self._base_ids([])) + len(self._suffix_ids)
        if overhead + self.settings.context_max_tokens >= self.settings.reranker_max_tokens:
            raise ValueError("The configured instruction leaves no token budget for news")

    def _answer_id(self, answer: str) -> int:
        tokens = self.tokenizer.encode(answer, add_special_tokens=False)
        if len(tokens) != 1:
            raise ValueError(f"The model must represent {answer!r} with one token")
        return tokens[0]

    def _encode(self, text: str) -> list[int]:
        # An article may quote chat delimiters; do not tokenize those as control tokens.
        safe_text = text.replace("<|", "< |").replace("|>", "| >")
        return self.tokenizer.encode(safe_text, add_special_tokens=False)

    def _base_ids(self, context_ids: list[int]) -> list[int]:
        return self._prefix_ids + self._header_ids + context_ids + self._document_label_ids

    def _pack(self, context_ids: list[int], document_ids: list[int]) -> tuple[list[int], bool]:
        prefix = self._base_ids(context_ids)
        budget = self.settings.reranker_max_tokens - len(prefix) - len(self._suffix_ids)
        if budget <= 0:
            raise ValueError("The token budget must leave room for news")
        truncated = len(document_ids) > budget
        return prefix + document_ids[:budget] + self._suffix_ids, truncated

    def _predict_batch(self, sequences: list[list[int]]) -> list[float]:
        inputs = self.tokenizer.pad({"input_ids": sequences}, padding=True, return_tensors="pt")
        inputs = {key: value.to("cpu") for key, value in inputs.items()}
        mask = inputs["attention_mask"]
        # Explicit positions make shorter inputs independent of the batch's left padding.
        inputs["position_ids"] = (mask.long().cumsum(-1) - 1).masked_fill(mask == 0, 0)
        with torch.inference_mode():
            logits = self.model(**inputs, use_cache=False, logits_to_keep=1).logits[:, -1, :]
            margins = (logits[:, self._yes_id] - logits[:, self._no_id]).float().tolist()
        if not all(math.isfinite(value) for value in margins):
            raise RuntimeError("The reranker returned non-finite logit margins")
        return margins

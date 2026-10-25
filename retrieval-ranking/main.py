import os

# Keep auxiliary Hugging Face/Xet caches in this checkout before importing Transformers.
os.environ.setdefault("HF_HOME", ".cache/huggingface")
os.environ.setdefault("HF_XET_CACHE", ".cache/huggingface/xet")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

from retrieval_ranking.api import create_app  # noqa: E402

app = create_app()

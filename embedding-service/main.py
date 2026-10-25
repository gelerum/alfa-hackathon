"""CPU-only Qwen embeddings. This service does not access the database."""

import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"
MODEL_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
DIMENSIONS = 1024
QUERY_INSTRUCTION = (
    "Retrieve business news relevant to the company's activities, markets, "
    "risks, and opportunities."
)
logger = logging.getLogger("uvicorn.error")
MAX_BODY_BYTES = 2 * 1024 * 1024


class RequestSizeLimit:
    """Bound bytes before FastAPI buffers or JSON-decodes the request body."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for name, value in scope.get("headers", []):
            if name.lower() == b"content-length":
                try:
                    length = int(value)
                    if length < 0:
                        raise ValueError
                except ValueError:
                    return await JSONResponse({"detail": "Invalid Content-Length"}, 400)(
                        scope, receive, send
                    )
                if length > MAX_BODY_BYTES:
                    return await JSONResponse({"detail": "Request body exceeds 2 MiB"}, 413)(
                        scope, receive, send
                    )
        # Also check actual bytes: Content-Length may be absent (chunked) or incorrect.
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                return await JSONResponse({"detail": "Request body exceeds 2 MiB"}, 413)(
                    scope, receive, send
                )
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, bounded_receive, send)


class EmbedRequest(BaseModel):
    # Explicit role prevents accidentally embedding company queries as documents.
    role: Literal["query", "document"]
    inputs: list[str] = Field(min_length=1, max_length=8)
    instruction: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_texts(self):
        if any(not text.strip() or len(text) > 32000 for text in self.inputs):
            raise ValueError("Each input must be nonblank and at most 32000 characters")
        if self.instruction is not None:
            if self.role != "query" or not self.instruction.strip():
                raise ValueError("A nonblank instruction is only allowed for query role")
        return self


class EmbedResponse(BaseModel):
    model: str
    revision: str
    dimensions: int
    normalized: bool
    role: Literal["query", "document"]
    instruction: str | None
    embeddings: list[list[float]]


def load_model():
    # Lazy imports let API contract tests run without downloading model weights.
    import torch
    from sentence_transformers import SentenceTransformer

    threads = int(os.getenv("TORCH_NUM_THREADS", "8"))
    max_tokens = int(os.getenv("MAX_INPUT_TOKENS", "2048"))
    if threads < 1 or not 1 <= max_tokens <= 32768:
        raise ValueError("TORCH_NUM_THREADS must be positive; MAX_INPUT_TOKENS must be 1..32768")
    torch.set_num_threads(threads)
    torch.set_num_interop_threads(1)
    logger.info("Loading %s revision %s on CPU (%s threads)", MODEL_ID, MODEL_REVISION, threads)
    model = SentenceTransformer(
        MODEL_ID,
        revision=MODEL_REVISION,
        device="cpu",
        trust_remote_code=False,
        model_kwargs={"dtype": torch.float32},
        processor_kwargs={"padding_side": "left"},
    )
    model.max_seq_length = max_tokens
    if model.get_embedding_dimension() != DIMENSIONS:
        raise RuntimeError("Unexpected embedding dimensions")
    # Warm up before readiness is reported.
    model.encode(["warmup"], prompt="", normalize_embeddings=True, show_progress_bar=False)
    return model


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.model = load_model()
    app.state.inference_lock = threading.Lock()
    logger.info("Embedding service ready")
    yield
    del app.state.model


app = FastAPI(title="Qwen CPU embedding service", lifespan=lifespan)
app.add_middleware(RequestSizeLimit)


@app.get("/health")
def health(request: Request):
    if getattr(request.app.state, "model", None) is None:
        raise HTTPException(503, "Model not loaded")
    return {
        "status": "ready",
        "model": MODEL_ID,
        "revision": MODEL_REVISION,
        "dimensions": DIMENSIONS,
        "device": "cpu",
        "max_input_tokens": request.app.state.model.max_seq_length,
    }


@app.post("/embed", response_model=EmbedResponse)
def embed(body: EmbedRequest, request: Request):
    model = request.app.state.model
    # Do not queue an unbounded number of CPU-heavy jobs in the web thread pool.
    if not request.app.state.inference_lock.acquire(blocking=False):
        raise HTTPException(429, "Model is busy; retry later", headers={"Retry-After": "1"})
    try:
        instruction = (body.instruction or QUERY_INSTRUCTION) if body.role == "query" else None
        texts = [
            f"Instruct: {instruction}\nQuery: {text}" if instruction is not None else text
            for text in body.inputs
        ]
        # Reject long texts instead of silently truncating the end of a news article.
        tokens = model.tokenizer(texts, padding=False, truncation=False)["input_ids"]
        too_long = [i for i, ids in enumerate(tokens) if len(ids) > model.max_seq_length]
        if too_long:
            raise HTTPException(
                422,
                f"Inputs {too_long} exceed {model.max_seq_length} tokens including instruction; "
                "split the text or supply a shorter version",
            )
        vectors = model.encode(
            texts,
            batch_size=2,
            prompt="",
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return EmbedResponse(
            model=MODEL_ID,
            revision=MODEL_REVISION,
            dimensions=DIMENSIONS,
            normalized=True,
            role=body.role,
            instruction=instruction,
            embeddings=vectors.tolist(),
        )
    finally:
        request.app.state.inference_lock.release()

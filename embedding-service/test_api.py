import asyncio
from unittest.mock import patch

import numpy as np
import pytest
from fastapi.testclient import TestClient

import main


class FakeModel:
    max_seq_length = 32

    def __init__(self):
        self.calls = []

    def tokenizer(self, texts, **kwargs):
        return {"input_ids": [[1] * len(text.split()) for text in texts]}

    def encode(self, texts, **kwargs):
        self.calls.append((texts, kwargs))
        vectors = np.zeros((len(texts), main.DIMENSIONS))
        vectors[:, 0] = 1
        return vectors


@pytest.fixture
def client():
    with patch.object(main, "load_model", return_value=FakeModel()):
        with TestClient(main.app) as c:
            yield c


def test_health(client):
    result = client.get("/health")
    assert result.status_code == 200
    assert result.json()["device"] == "cpu"
    assert result.json()["dimensions"] == 1024


def test_documents_are_not_prefixed(client):
    inputs = ["Заголовок\nТекст новости", "Другая новость"]
    result = client.post("/embed", json={"role": "document", "inputs": inputs})
    assert result.status_code == 200
    body = result.json()
    assert len(body["embeddings"]) == 2
    assert len(body["embeddings"][0]) == 1024
    assert body["revision"] == main.MODEL_REVISION
    assert body["instruction"] is None
    texts, kwargs = main.app.state.model.calls[-1]
    assert texts == inputs
    assert kwargs["normalize_embeddings"] is True
    assert kwargs["prompt"] == ""


def test_queries_get_instruction(client):
    result = client.post("/embed", json={"role": "query", "inputs": ["Импорт оборудования"]})
    assert result.status_code == 200
    assert result.json()["instruction"] == main.QUERY_INSTRUCTION
    assert main.app.state.model.calls[-1][0] == [
        f"Instruct: {main.QUERY_INSTRUCTION}\nQuery: Импорт оборудования"
    ]


def test_custom_instruction(client):
    result = client.post("/embed", json={
        "role": "query", "inputs": ["Текст"], "instruction": "Retrieve relevant documents."
    })
    assert result.status_code == 200
    assert result.json()["instruction"] == "Retrieve relevant documents."


@pytest.mark.parametrize("body", [
    {"inputs": ["text"]},
    {"role": "invalid", "inputs": ["text"]},
    {"role": "document", "inputs": []},
    {"role": "document", "inputs": [" "]},
    {"role": "document", "inputs": ["x" * 32001]},
    {"role": "document", "inputs": ["x"] * 9},
    {"role": "document", "inputs": ["x"], "instruction": "wrong"},
    {"role": "query", "inputs": ["x"], "instruction": "  "},
])
def test_validation(client, body):
    assert client.post("/embed", json=body).status_code == 422


def test_long_input_rejected_and_lock_released(client):
    result = client.post("/embed", json={"role": "document", "inputs": ["word " * 33]})
    assert result.status_code == 422
    assert not main.app.state.inference_lock.locked()
    assert not main.app.state.model.calls


def test_busy_model(client):
    lock = main.app.state.inference_lock
    lock.acquire()
    try:
        result = client.post("/embed", json={"role": "document", "inputs": ["text"]})
        assert result.status_code == 429
        assert result.headers["Retry-After"] == "1"
        assert client.get("/health").status_code == 200
    finally:
        lock.release()


@pytest.mark.parametrize("headers,chunks,expected_reads", [
    ([(b"content-length", str(main.MAX_BODY_BYTES + 1).encode())], [], 0),
    ([], [b"x" * main.MAX_BODY_BYTES, b"x", b"not consumed"], 2),
    ([(b"content-length", b"1")], [b"x" * (main.MAX_BODY_BYTES + 1)], 1),
])
def test_body_limit_before_parsing(headers, chunks, expected_reads):
    async def check():
        reads = 0
        messages = []

        async def downstream(scope, receive, send):
            pytest.fail("Oversized body reached FastAPI")

        async def receive():
            nonlocal reads
            chunk = chunks[reads]
            reads += 1
            return {"type": "http.request", "body": chunk, "more_body": reads < len(chunks)}

        async def send(message):
            messages.append(message)

        await main.RequestSizeLimit(downstream)(
            {"type": "http", "headers": headers}, receive, send
        )
        assert messages[0]["status"] == 413
        assert reads == expected_reads

    asyncio.run(check())


def test_oversized_extra_field(client):
    result = client.post("/embed", json={
        "role": "document", "inputs": ["valid"], "extra": "x" * main.MAX_BODY_BYTES
    })
    assert result.status_code == 413
    assert not main.app.state.model.calls


def test_failure_releases_lock(client):
    with patch.object(main.app.state.model, "encode", side_effect=RuntimeError("failed")):
        with pytest.raises(RuntimeError, match="failed"):
            client.post("/embed", json={"role": "document", "inputs": ["text"]})
    assert not main.app.state.inference_lock.locked()


def test_query_waits_for_running_batch_and_documents_yield(client):
    import threading
    import time

    state = main.app.state
    state.inference_lock.acquire()
    results = {}
    waiter = threading.Thread(
        target=lambda: results.update(query=client.post("/embed", json={"role": "query", "inputs": ["q"]}))
    )
    waiter.start()
    for _ in range(100):
        if state.queries_waiting:
            break
        time.sleep(0.01)
    assert state.queries_waiting == 1
    # A bulk document request must not jump ahead of a waiting query.
    assert client.post("/embed", json={"role": "document", "inputs": ["d"]}).status_code == 429
    state.inference_lock.release()
    waiter.join(timeout=5)
    assert results["query"].status_code == 200
    assert state.queries_waiting == 0 and not state.inference_lock.locked()

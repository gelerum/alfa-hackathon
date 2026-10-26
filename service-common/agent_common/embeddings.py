import math
import time

import httpx

from agent_common.core import db, digest, j, settings

QUERY_INSTRUCTION = (
    "Retrieve business news relevant to the company's activities, markets, risks, and opportunities."
)
QUERY_TEMPLATE = "Instruct: {instruction}\nQuery: {text}"


def vector_literal(values):
    if len(values) != 1024 or not all(math.isfinite(v) for v in values):
        raise ValueError("Expected 1024 finite embedding values")
    norm = math.hypot(*values)
    if not math.isclose(norm, 1.0, rel_tol=1e-3, abs_tol=1e-3):
        raise ValueError("Expected an L2-normalized embedding")
    return "[" + ",".join(str(float(v)) for v in values) + "]"


def config(chunk=False):
    health = httpx.get(settings.embedding_base_url + "/health", timeout=15).raise_for_status().json()
    key = settings.embedding_config_key + (":chunks-v1" if chunk else "")
    if health["dimensions"] != 1024:
        raise ValueError("Unsupported embedding dimensions")
    with db() as conn:
        conn.execute(
            "INSERT INTO embedding_configs(config_key,model_id,model_revision,dimensions,"
            "query_template,document_template,normalize,extra) VALUES(%s,%s,%s,1024,%s,%s,true,%s) "
            "ON CONFLICT(config_key) DO NOTHING",
            (
                key,
                health["model"],
                health["revision"],
                QUERY_TEMPLATE,
                "{text}" if chunk else "{title}\n\n{body}",
                j(
                    {
                        "query_instruction": QUERY_INSTRUCTION,
                        "document_text_prep": "chunks-1200c-v1" if chunk else "news-title-body-4000c-v1",
                    }
                ),
            ),
        )
        row = conn.execute("SELECT * FROM embedding_configs WHERE config_key=%s", (key,)).fetchone()
    expected = (
        health["model"],
        health["revision"],
        QUERY_TEMPLATE,
        "{text}" if chunk else "{title}\n\n{body}",
    )
    actual = (row["model_id"], row["model_revision"], row["query_template"], row["document_template"])
    if (
        actual != expected
        or row["extra"].get("query_instruction") != QUERY_INSTRUCTION
        or not row["normalize"]
    ):
        raise ValueError("Embedding configuration mismatch; create a new config key")
    return row


def embed(texts, role, cfg):
    body = {"role": role, "inputs": texts}
    if role == "query":
        body["instruction"] = QUERY_INSTRUCTION
    # The CPU model is shared with bulk news indexing: wait for it instead of failing the profile.
    deadline = time.monotonic() + settings.embedding_wait_seconds
    attempt = 0
    while True:
        attempt += 1
        try:
            response = httpx.post(settings.embedding_base_url + "/embed", json=body, timeout=180)
        except httpx.TransportError:
            if time.monotonic() >= deadline:
                raise RuntimeError("Embedding service unreachable") from None
            time.sleep(min(2**attempt, 10))
            continue
        if response.status_code in (429, 503):
            if time.monotonic() >= deadline:
                break
            time.sleep(min(2**attempt, 5))
            continue
        response.raise_for_status()
        result = response.json()
        if (
            result["model"],
            result["revision"],
            result["dimensions"],
            result["normalized"],
            result["role"],
        ) != (cfg["model_id"], cfg["model_revision"], 1024, True, role):
            raise ValueError("Embedding response does not match requested configuration")
        if role == "query" and result.get("instruction") != QUERY_INSTRUCTION:
            raise ValueError("Embedding query instruction mismatch")
        if len(result["embeddings"]) != len(texts):
            raise ValueError("Embedding response count mismatch")
        for vector in result["embeddings"]:
            vector_literal(vector)
        return result["embeddings"]
    raise RuntimeError("Embedding service remained busy or unavailable")


def query_embedding(text, cfg):
    values = embed([text], "query", cfg)[0]
    with db() as conn:
        conn.execute(
            "INSERT INTO search_embeddings(embedding_config_id,encoding_role,text_sha256,embedding) "
            "VALUES(%s,'query',%s,%s::vector) ON CONFLICT DO NOTHING",
            (cfg["embedding_config_id"], digest(text.encode()), vector_literal(values)),
        )
        row = conn.execute(
            "SELECT embedding_id FROM search_embeddings WHERE embedding_config_id=%s "
            "AND encoding_role='query' AND text_sha256=%s",
            (cfg["embedding_config_id"], digest(text.encode())),
        ).fetchone()
    return row["embedding_id"]

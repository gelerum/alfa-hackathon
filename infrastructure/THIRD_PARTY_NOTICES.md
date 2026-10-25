# Third-party components and modeled integrations

This is an inventory, not a replacement for upstream license texts. Verify upstream licenses
and distribution conditions before publishing images or redistributing source documents.
Python versions are pinned in `uv.lock`; existing Parser/Embedding services retain their locks.

| Component | Use | Upstream / license family |
|---|---|---|
| Python 3.12 | Application runtime | https://www.python.org/ — PSF |
| FastAPI / Starlette / Uvicorn | HTTP APIs | https://fastapi.tiangolo.com/ , https://www.starlette.io/ , https://www.uvicorn.org/ — MIT / BSD |
| Pydantic / pydantic-settings | Contracts and configuration | https://docs.pydantic.dev/ — MIT |
| HTTPX / httpcore | Internal HTTP | https://www.python-httpx.org/ — BSD |
| OpenAI Python SDK | Approved OpenAI-compatible model endpoint | https://github.com/openai/openai-python — Apache-2.0 |
| psycopg 3 | PostgreSQL adapter | https://www.psycopg.org/ — LGPL-3.0; binary distribution also contains upstream libraries |
| PostgreSQL / pgvector | Data, durable queue, lexical/vector retrieval | https://www.postgresql.org/ , https://github.com/pgvector/pgvector — PostgreSQL license |
| PyMuPDF / MuPDF | PDF rendering and extraction | https://pymupdf.readthedocs.io/ — AGPL-3.0 or commercial; **review deployment/distribution obligations** |
| Tesseract / traineddata | Russian/English OCR, no generative model | https://github.com/tesseract-ocr/tesseract — Apache-2.0 |
| pytesseract | OCR wrapper | https://github.com/madmaze/pytesseract — Apache-2.0 |
| Pillow | Image handling | https://python-pillow.org/ — HPND |
| python-docx / lxml | DOCX | https://python-docx.readthedocs.io/ , https://lxml.de/ — MIT / BSD |
| openpyxl | XLSX, cached values only | https://openpyxl.readthedocs.io/ — MIT |
| BeautifulSoup | Web text extraction | https://www.crummy.com/software/BeautifulSoup/ — MIT |
| SearXNG | External search, upstream engines may reject traffic | https://github.com/searxng/searxng — AGPL-3.0 |
| SentenceTransformers / Transformers / PyTorch | Existing embedding-service | https://sbert.net/ , https://huggingface.co/docs/transformers/ , https://pytorch.org/ — Apache-2.0 / BSD |
| Qwen3-Embedding-0.6B | 1024-dimensional embedding model; pinned revision in embedding-service | https://huggingface.co/Qwen/Qwen3-Embedding-0.6B — Apache-2.0 |
| Qwen3-Reranker-0.6B | Non-generative yes/no logit scoring; revision `e61197ed45024b0ed8a2d74b80b4d909f1255473` | https://huggingface.co/Qwen/Qwen3-Reranker-0.6B — Apache-2.0 |
| Hugging Face Hub / Tokenizers / Safetensors | Model loading and weights | https://huggingface.co/docs — Apache-2.0 |
| Playwright | Browser regression tests | https://playwright.dev/ — Apache-2.0 |
| uv / pytest / Ruff | Locking and tests | https://docs.astral.sh/uv/ , https://pytest.org/ , https://docs.astral.sh/ruff/ — MIT/Apache-2.0 / MIT |
| SQLite | Independent mock source catalog | https://www.sqlite.org/ — public domain |

## Models and data

- Generative calls use only the case-approved model allowlist in `../service-common/agent_common/runtime.py`.
  Default: Qwen3.6 35B A3B via the team's configured endpoint. No external autonomous AI service
  or ready-made multiagent framework is used as the solution's main component.
- Qwen3 embedding is an existing non-generative retrieval component; confirm organizer policy
  if the approved-model restriction is interpreted to cover embedding models too.
- Bank CSVs and the nine supplied bank PDF materials are case-provided data, not datasets
  newly licensed by this implementation. Do not redistribute outside the authorized case context
  without checking permission. The synthetic IDs do not identify real people or organizations.
- The source case CSV/PDF are included for an authorized private team/jury checkout.
  Do not publish this repository or its data publicly without checking case permissions.
- News/web content retains its original source and applicable publisher conditions. Storage
  and extraction do not transfer copyright. Respect terms, robots policies and access restrictions.

## Explicitly modeled / incomplete integrations

1. `bank-materials-source` is a simulated publication API, seeded from case PDFs. It is not an
   official Alfa-Bank endpoint. New materials can be published live to the simulation.
2. `/admin/demo` creates a fictional company/user and explicitly associates it with a synthetic
   bank record. This is not real identity resolution or production SSO.
3. `retrieval-ranking` is included in this monorepo. Backend calls synchronous `/rank`, stores
   the ranking run ID and submits that ID to Insight Agent `/runs`. Internal ranker has no
   public authentication endpoint and is not published on a host port.
4. Unit/integration tests fake generative responses and embedding vectors. Those tests establish
   contracts and persistence, not real model accuracy. Mock OCR unit tests do not evaluate real OCR.
5. Docker images and OS packages need deployment-specific digest/version pinning for strict
   bit-for-bit reproducibility; language/application dependencies are locked.

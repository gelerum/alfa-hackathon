"""FastAPI boundary with injectable adapters for isolated tests."""

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .config import MODEL_ID, MODEL_REVISION, SCORE_TYPE, Settings
from .errors import RankingError
from .ports import Repository, Reranker
from .schemas import NewsResponse, RankRequest, RankResponse
from .service import RankingResult, RankingService

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    repository: Repository | None = None,
    reranker: Reranker | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        config = settings if settings is not None else Settings()
        repo = repository
        model = reranker
        if repo is None:
            from .db import PostgresRepository

            repo = PostgresRepository(config)
        if model is None:
            from .reranker import QwenReranker

            model = QwenReranker(config)
            await run_in_threadpool(model.load)
        app.state.repository = repo
        app.state.service = RankingService(config, repo, model)
        yield

    app = FastAPI(title="Retrieval and ranking", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(RankingError)
    async def ranking_error_handler(_request, exc: RankingError):
        headers = {"Retry-After": "1"} if exc.status_code == 429 else None
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
            headers=headers,
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "retrieval-ranking"}

    @app.get("/ready")
    def ready():
        try:
            app.state.repository.check_ready()
        except Exception:
            logger.exception("Database readiness check failed")
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return {
            "status": "ready",
            "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION,
            "device": "cpu",
        }

    @app.post("/rank", response_model=RankResponse)
    def rank(request: RankRequest):
        try:
            result = app.state.service.rank(request.to_query())
        except RankingError:
            raise
        except Exception:
            logger.exception("Ranking request failed")
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "code": "ranking_unavailable",
                        "message": "Ranking could not be completed",
                    },
                },
            )
        return _response(result)

    return app


def _response(result: RankingResult) -> RankResponse:
    context = result.context
    preferences = context.preferences
    items = [
        NewsResponse(
            article_id=item.candidate.article_id,
            content_version=item.candidate.content_version,
            title=item.candidate.title,
            text=item.candidate.text,
            description=item.candidate.description,
            published_at=item.candidate.published_at,
            source_url=item.candidate.source_url,
            source_name=item.candidate.source_name,
            event_id=item.candidate.event_id,
            rank=item.rank,
            score=item.score,
            score_type=SCORE_TYPE,
            retrieval_hits=[asdict(hit) for hit in item.candidate.hits],
            document_truncated=item.document_truncated,
            token_count=item.token_count,
        )
        for item in result.items
    ]
    return RankResponse(
        ranking_run_id=result.run_id,
        company_id=context.company_id,
        user_id=result.query.user_id,
        profile_id=context.profile_id,
        user_context_id=preferences.user_context_id if preferences else None,
        embedding_config_id=context.embedding_config_id,
        as_of=result.query.as_of,
        candidate_count=result.candidate_count,
        context_truncated=result.context_truncated,
        model_id=MODEL_ID,
        model_revision=MODEL_REVISION,
        score_type=SCORE_TYPE,
        items=items,
    )

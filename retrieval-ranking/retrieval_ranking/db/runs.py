"""Persist complete candidate traces before finalizing the immutable ranking run."""

import json
import math
from dataclasses import asdict
from datetime import date, datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from ..config import FORMAT_VERSION, MODEL_ID, MODEL_REVISION, SCORE_TYPE, Settings
from ..domain import CompanyContext, RankingQuery, ScoredCandidate
from ..errors import RankingError


class RunStore:
    def __init__(self, settings: Settings):
        self.settings = settings

    def start(
        self,
        connection,
        run_id: UUID,
        query: RankingQuery,
        context: CompanyContext,
        snapshot: dict[str, Any],
    ) -> None:
        preferences = context.preferences
        if (preferences.user_id if preferences else None) != query.user_id:
            raise RankingError("context_owner_mismatch", "Query and context user differ", 500)
        connection.execute(
            """
            INSERT INTO ranking_runs (
                ranking_run_id, company_id, user_id, profile_id, user_context_id, as_of,
                published_after, top_k, retrieval_config_version, ranking_config_version,
                ranker1_version, ranking_mode, embedding_config_id, feature_pipeline_version,
                result_unit, context_snapshot, policy_id, status
            ) VALUES (
                %(run)s, %(company)s, %(user)s, %(profile)s, %(context)s, %(as_of)s,
                %(after)s, %(top_k)s, %(retrieval)s, %(ranking)s, %(model)s, 'context',
                %(config)s, %(format)s, 'article', %(snapshot)s, %(policy)s, 'running'
            )
            """,
            {
                "run": run_id,
                "company": context.company_id,
                "user": query.user_id,
                "profile": context.profile_id,
                "context": preferences.user_context_id if preferences else None,
                "as_of": query.as_of,
                "after": query.published_after,
                "top_k": query.top_k,
                "retrieval": self.settings.retrieval_version(),
                "ranking": self.settings.ranking_version(),
                "model": f"{MODEL_ID}@{MODEL_REVISION}",
                "config": context.embedding_config_id,
                "format": FORMAT_VERSION,
                "snapshot": Jsonb(json_safe(snapshot)),
                "policy": self.settings.ranking_version(),
            },
        )

    def complete(
        self,
        connection,
        run_id: UUID,
        items: list[ScoredCandidate],
        metadata: dict[str, Any],
    ) -> None:
        run = connection.execute(
            "SELECT status, top_k FROM ranking_runs WHERE ranking_run_id = %s FOR UPDATE",
            (run_id,),
        ).fetchone()
        if run is None or run["status"] != "running":
            raise RankingError("run_not_running", "Only a running ranking can be completed", 409)
        self._validate_items(items, run["top_k"])
        rows = [self._item_parameters(run_id, item) for item in items]
        if rows:
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO ranking_items (
                        ranking_run_id, article_id, content_version, event_id, retrieval_hits,
                        features_snapshot, context_score, score, score_type, rank, reason_codes
                    ) VALUES (
                        %(run)s, %(article)s, %(version)s, %(event)s, %(hits)s, %(features)s,
                        %(score)s, %(score)s, %(score_type)s, %(rank)s, %(reasons)s
                    )
                    """,
                    rows,
                )
        connection.execute(
            """
            UPDATE ranking_runs
            SET context_snapshot = context_snapshot || %(result)s,
                status = 'completed', completed_at = clock_timestamp()
            WHERE ranking_run_id = %(run)s
            """,
            {"run": run_id, "result": Jsonb({"ranking_result": json_safe(metadata)})},
        )

    @staticmethod
    def fail(connection, run_id: UUID) -> None:
        connection.execute(
            """
            UPDATE ranking_runs SET status = 'failed', completed_at = clock_timestamp()
            WHERE ranking_run_id = %s AND status = 'running'
            """,
            (run_id,),
        )

    @staticmethod
    def _validate_items(items: list[ScoredCandidate], top_k: int) -> None:
        if len({item.candidate.article_id for item in items}) != len(items):
            raise RankingError("duplicate_candidate", "Candidate articles must be unique", 500)
        if any(not math.isfinite(item.score) for item in items):
            raise RankingError("invalid_ranking_score", "Ranking scores must be finite", 500)
        ranks = sorted(item.rank for item in items if item.rank is not None)
        expected = list(range(1, min(top_k, len(items)) + 1))
        if ranks != expected:
            raise RankingError(
                "invalid_ranking_order", "Visible ranks must be contiguous Top K", 500
            )

    @staticmethod
    def _item_parameters(run_id: UUID, item: ScoredCandidate) -> dict[str, Any]:
        candidate = item.candidate
        # These codes describe retrieval provenance; they are not generated business explanations.
        reasons = sorted(
            {
                f"retrieved:{hit.method}:{hit.facet_type}"
                if hit.facet_type
                else f"retrieved:{hit.method}"
                for hit in candidate.hits
            }
        )
        return {
            "run": run_id,
            "article": candidate.article_id,
            "version": candidate.content_version,
            "event": candidate.event_id,
            "hits": Jsonb(json_safe([asdict(h) for h in candidate.hits])),
            "features": Jsonb(
                {
                    "document_truncated": item.document_truncated,
                    "token_count": item.token_count,
                }
            ),
            "score": item.score,
            "score_type": SCORE_TYPE,
            "rank": item.rank,
            "reasons": reasons,
        }


def json_safe(value: Any) -> Any:
    def default(item: Any):
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, (datetime, date)):
            return item.isoformat()
        raise TypeError(f"Unsupported JSON snapshot value: {type(item).__name__}")

    return json.loads(json.dumps(value, default=default, allow_nan=False))

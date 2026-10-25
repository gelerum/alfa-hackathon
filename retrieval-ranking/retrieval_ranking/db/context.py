"""Resolve immutable company/user snapshots before selecting compatible query vectors."""

import json
import math
from typing import Any
from uuid import UUID

import psycopg

from ..config import Settings
from ..domain import CompanyContext, Facet, Fact, Goal, Preferences, RankingQuery
from ..errors import ContextNotReady, RankingError

EMBEDDING_MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"


class ContextReader:
    def __init__(self, settings: Settings):
        self.settings = settings

    def read(
        self,
        connection: psycopg.Connection[dict[str, Any]],
        query: RankingQuery,
    ) -> CompanyContext:
        if query.user_context_id is not None and query.user_id is None:
            raise RankingError("invalid_user_context", "user_context_id requires user_id", 422)
        preferences = self._preferences(connection, query)
        profile = self._profile(connection, query)
        config = self._configuration(connection, profile["profile_id"], query)
        facets = self._facets(
            connection,
            profile["profile_id"],
            preferences,
            config["embedding_config_id"],
            query,
        )
        return CompanyContext(
            company_id=query.company_id,
            profile_id=profile["profile_id"],
            profile_version=profile["profile_version"],
            embedding_config_id=config["embedding_config_id"],
            embedding_metadata=config,
            company_name=profile["legal_name"],
            missing_data=profile["missing_data"],
            conflicts=profile["conflicts"],
            facets=facets,
            facts=self._facts(connection, profile["profile_id"]),
            goals=self._goals(connection, profile["profile_id"], query),
            preferences=preferences,
        )

    def _profile(self, connection, query: RankingQuery) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT p.profile_id, p.profile_version, p.missing_data, p.conflicts, c.legal_name
            FROM company_profiles p JOIN companies c USING (company_id)
            WHERE p.company_id = %(company)s AND p.status = 'ready'
              AND p.published_at <= %(as_of)s
              AND (%(profile)s::uuid IS NULL OR p.profile_id = %(profile)s::uuid)
            ORDER BY p.profile_version DESC
            LIMIT 1
            """,
            {"company": query.company_id, "as_of": query.as_of, "profile": query.profile_id},
        ).fetchone()
        if row is None:
            raise ContextNotReady("No requested ready company profile exists at as_of")
        return row

    def _preferences(self, connection, query: RankingQuery) -> Preferences | None:
        if query.user_id is None:
            return None
        member = connection.execute(
            """
            SELECT 1 FROM company_memberships
            WHERE company_id = %s AND user_id = %s AND is_active AND joined_at <= %s
            """,
            (query.company_id, query.user_id, query.as_of),
        ).fetchone()
        if member is None:
            raise RankingError(
                "company_membership_required",
                "An active company membership is required",
                403,
            )
        row = connection.execute(
            """
            SELECT user_context_id, user_id, context_version, business_role, responsibility,
                   focus_topics, interest_markets, watchlist
            FROM user_preferences
            WHERE company_id = %(company)s AND user_id = %(user)s AND status = 'ready'
              AND published_at <= %(as_of)s
              AND (%(context)s::uuid IS NULL OR user_context_id = %(context)s::uuid)
            ORDER BY context_version DESC
            LIMIT 1
            """,
            {
                "company": query.company_id,
                "user": query.user_id,
                "as_of": query.as_of,
                "context": query.user_context_id,
            },
        ).fetchone()
        if row is None:
            raise ContextNotReady("No requested ready user preferences exist at as_of")
        goals = connection.execute(
            """
            SELECT goal_id, text FROM user_preference_goals
            WHERE user_context_id = %s AND (expires_at IS NULL OR expires_at > %s)
            ORDER BY goal_id
            """,
            (row["user_context_id"], query.as_of),
        ).fetchall()
        return Preferences(**row, goals=[Goal(**goal) for goal in goals])

    def _configuration(self, connection, profile_id: UUID, query: RankingQuery) -> dict:
        rows = connection.execute(
            """
            SELECT DISTINCT c.*
            FROM company_context_facets f
            JOIN embedding_configs c USING (embedding_config_id)
            WHERE f.profile_id = %(profile)s AND f.scope = 'company' AND f.status = 'ready'
              AND f.published_at <= %(as_of)s
              AND (%(config)s::uuid IS NULL OR c.embedding_config_id = %(config)s::uuid)
            """,
            {"profile": profile_id, "as_of": query.as_of, "config": query.embedding_config_id},
        ).fetchall()
        if len(rows) != 1:
            raise ContextNotReady(
                "Exactly one compatible corporate embedding configuration is required"
            )
        config = rows[0]
        if (
            config["model_id"] != EMBEDDING_MODEL_ID
            or config["dimensions"] != 1024
            or config["distance_metric"] != "cosine"
            or not config["normalize"]
        ):
            raise ContextNotReady(
                "The profile does not use the agreed Qwen cosine embedding contract"
            )
        return config

    def _facets(
        self,
        connection,
        profile_id: UUID,
        preferences: Preferences | None,
        config_id: UUID,
        query: RankingQuery,
    ) -> list[Facet]:
        rows = connection.execute(
            """
            SELECT f.facet_id, f.facet_type, f.text, f.keywords, f.entities, f.scope,
                   e.embedding::text AS embedding, f.text_hash, e.text_sha256
            FROM company_context_facets f
            JOIN search_embeddings e
              ON e.embedding_id = f.embedding_id
             AND e.embedding_config_id = f.embedding_config_id
             AND e.encoding_role = 'query'
            WHERE f.profile_id = %(profile)s AND f.status = 'ready'
              AND f.embedding_config_id = %(config)s AND f.published_at <= %(as_of)s
              AND e.created_at <= %(as_of)s
              AND (f.scope = 'company' OR
                   (f.scope = 'user_company' AND f.user_context_id = %(context)s::uuid))
            ORDER BY f.scope, f.facet_type, f.facet_id
            """,
            {
                "profile": profile_id,
                "config": config_id,
                "as_of": query.as_of,
                "context": preferences.user_context_id if preferences else None,
            },
        ).fetchall()
        if not rows or not any(row["scope"] == "company" for row in rows):
            raise ContextNotReady("No compatible ready corporate query vectors are available")
        if len(rows) > self.settings.max_facets:
            raise RankingError(
                "facet_budget_exceeded",
                "The ready context exceeds max_facets; increase the explicit candidate budget",
            )
        facets = []
        for row in rows:
            if row.pop("text_hash") != row.pop("text_sha256"):
                raise ContextNotReady("A ready facet text hash does not match its query embedding")
            vector = parse_vector(row.pop("embedding"))
            facets.append(Facet(**row, embedding=vector))
        return facets

    @staticmethod
    def _facts(connection, profile_id: UUID) -> list[Fact]:
        rows = connection.execute(
            """
            SELECT f.fact_id, f.field, f.value, f.unit, f.assertion_type, f.source_type
            FROM company_profile_facts p JOIN company_facts f USING (fact_id)
            WHERE p.profile_id = %s AND f.status = 'usable'
              AND f.assertion_type IN ('reported', 'calculated')
            ORDER BY f.field, f.fact_id
            """,
            (profile_id,),
        ).fetchall()
        # Hypotheses stay out of factual context; the profiler must expose limitations separately.
        return [Fact(**row) for row in rows]

    @staticmethod
    def _goals(connection, profile_id: UUID, query: RankingQuery) -> list[Goal]:
        rows = connection.execute(
            """
            SELECT g.goal_id, g.text
            FROM company_profile_goals p JOIN company_goals g USING (goal_id)
            WHERE p.profile_id = %s AND g.lifecycle_status = 'active'
              AND (g.expires_at IS NULL OR g.expires_at > %s)
            ORDER BY g.goal_id
            """,
            (profile_id, query.as_of),
        ).fetchall()
        return [Goal(**row) for row in rows]


def parse_vector(value: str) -> list[float]:
    """Reject malformed query vectors instead of producing arbitrary cosine neighbors."""
    try:
        vector = json.loads(value)
        valid = (
            isinstance(vector, list)
            and len(vector) == 1024
            and all(type(x) in (int, float) and math.isfinite(x) for x in vector)
            and any(x != 0 for x in vector)
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ContextNotReady("A ready facet has an invalid, non-finite or zero embedding")
    return [float(x) for x in vector]

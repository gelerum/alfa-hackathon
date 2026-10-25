"""Synthetic integration fixtures: basis vectors test mechanics, not recommendation quality."""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID

from psycopg.types.json import Jsonb

COMPANY = "U_TEST"
OTHER_COMPANY = "U_OTHER"
PROFILE_ID = UUID(int=101)
FUTURE_PROFILE_ID = UUID(int=102)
USER_ID = UUID(int=201)
INACTIVE_USER_ID = UUID(int=202)
USER_CONTEXT_ID = UUID(int=301)
CONFIG_ID = UUID(int=401)
OTHER_CONFIG_ID = UUID(int=402)
ACTIVITY_FACET_ID = UUID(int=501)
COUNTERPARTY_FACET_ID = UUID(int=502)
PERSONAL_FACET_ID = UUID(int=503)
FACT_ID = UUID(int=601)
GOAL_ID = UUID(int=701)
PERSONAL_GOAL_ID = UUID(int=702)
AS_OF = datetime(2026, 1, 10, tzinfo=UTC)
KNOWN = datetime(2026, 1, 1, tzinfo=UTC)
FUTURE = datetime(2026, 1, 20, tzinfo=UTC)


def vector(*values: float) -> str:
    return "[" + ",".join(str(value) for value in (*values, *([0] * (1024 - len(values))))) + "]"


def _embedding(connection, number, values, text):
    embedding_id = UUID(int=number)
    digest = sha256(text.encode()).hexdigest()
    connection.execute(
        """INSERT INTO search_embeddings
        (embedding_id,embedding_config_id,encoding_role,text_sha256,embedding,created_at)
        VALUES (%s,%s,'query',%s,%s::vector,%s)""",
        (embedding_id, CONFIG_ID, digest, vector(*values), KNOWN),
    )
    return embedding_id, digest


def _facet(
    connection,
    facet_id,
    profile_id,
    kind,
    text,
    values,
    number,
    *,
    scope="company",
    keywords=None,
    entities=None,
    published=KNOWN,
):
    embedding_id, digest = _embedding(connection, number, values, text)
    connection.execute(
        """INSERT INTO company_context_facets
        (facet_id,company_id,profile_id,scope,user_context_id,facet_type,text,keywords,entities,
         embedding_config_id,embedding_id,text_hash,generation_version,status,created_at,published_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'synthetic-v1','ready',%s,%s)""",
        (
            facet_id,
            COMPANY,
            profile_id,
            scope,
            USER_CONTEXT_ID if scope == "user_company" else None,
            kind,
            text,
            keywords or [],
            Jsonb(entities or []),
            CONFIG_ID,
            embedding_id,
            digest,
            KNOWN,
            published,
        ),
    )


def add_news(
    connection,
    article_id,
    title,
    values=None,
    *,
    config=CONFIG_ID,
    published=KNOWN,
    fetched=KNOWN,
    recorded=KNOWN,
    embedded=KNOWN,
    duplicate=None,
    current_version="v1",
    revision_version="v1",
    embedding_version=None,
):
    """Represent archived versions separately from the mutable current news row."""
    connection.execute(
        """INSERT INTO news
        (article_id,canonical_url,source_name,title,body,description,language,published_at,
         fetched_at,content_version,duplicate_of_id)
        VALUES (%s,%s,'Synthetic Source',%s,%s,'Synthetic fixture','ru',%s,%s,%s,%s)""",
        (
            article_id,
            f"https://example.test/news/{article_id}",
            title,
            title + " body",
            published,
            fetched,
            current_version,
            duplicate,
        ),
    )
    connection.execute(
        """INSERT INTO news_revisions
        (article_id,content_version,title,body,description,recorded_at)
        VALUES (%s,%s,%s,%s,'Synthetic fixture',%s)""",
        (article_id, revision_version, title, title + " archived body", recorded),
    )
    if values is not None:
        connection.execute(
            """INSERT INTO news_embeddings
            (article_id,content_version,embedding_config_id,embedding,created_at)
            VALUES (%s,%s,%s,%s::vector,%s)""",
            (article_id, embedding_version or revision_version, config, vector(*values), embedded),
        )


def _seed_identity(connection):
    connection.execute(
        "INSERT INTO companies(company_id,legal_name,created_at) VALUES (%s,%s,%s),(%s,%s,%s)",
        (COMPANY, "Synthetic Metals", KNOWN, OTHER_COMPANY, "Other synthetic company", KNOWN),
    )
    for user_id, active in ((USER_ID, True), (INACTIVE_USER_ID, False)):
        connection.execute(
            "INSERT INTO app_users(user_id,auth_subject,created_at) VALUES (%s,%s,%s)",
            (user_id, f"synthetic:{user_id}", KNOWN),
        )
        connection.execute(
            """INSERT INTO company_memberships
            (company_id,user_id,is_active,joined_at) VALUES (%s,%s,%s,%s)""",
            (COMPANY, user_id, active, KNOWN),
        )


def _seed_embedding_configs(connection):
    for config_id, key in ((CONFIG_ID, "synthetic-basis-v1"), (OTHER_CONFIG_ID, "other-basis-v1")):
        connection.execute(
            """INSERT INTO embedding_configs
            (embedding_config_id,config_key,model_id,model_revision,dimensions,
             distance_metric,normalize,extra,created_at)
            VALUES (%s,%s,'Qwen/Qwen3-Embedding-0.6B','synthetic-fixture',1024,
                    'cosine',true,%s,%s)""",
            (config_id, key, Jsonb({"synthetic": True}), KNOWN),
        )


def _seed_profiles(connection):
    connection.execute(
        """INSERT INTO company_profiles
        (profile_id,company_id,profile_version,payload,generator_version,created_at)
        VALUES (%s,%s,1,%s,'synthetic-v1',%s),(%s,%s,2,%s,'synthetic-v1',%s)""",
        (
            PROFILE_ID,
            COMPANY,
            Jsonb({"description": "Synthetic metals"}),
            KNOWN,
            FUTURE_PROFILE_ID,
            COMPANY,
            Jsonb({"description": "Future profile"}),
            KNOWN,
        ),
    )
    connection.execute(
        """INSERT INTO company_facts
        (fact_id,company_id,field,value,source_type,assertion_type,status,created_at,observed_at)
        VALUES (%s,%s,'activity',%s,'bank_import','reported','usable',%s,%s)""",
        (FACT_ID, COMPANY, Jsonb("Metals production"), KNOWN, KNOWN),
    )
    connection.execute(
        "INSERT INTO company_profile_facts(profile_id,fact_id,company_id) VALUES (%s,%s,%s)",
        (PROFILE_ID, FACT_ID, COMPANY),
    )
    connection.execute(
        """INSERT INTO company_goals
        (goal_id,company_id,text,lifecycle_status,source_type,created_at)
        VALUES (%s,%s,'Reduce energy costs','active','user_answer',%s)""",
        (GOAL_ID, COMPANY, KNOWN),
    )
    connection.execute(
        "INSERT INTO company_profile_goals(profile_id,goal_id,company_id) VALUES (%s,%s,%s)",
        (PROFILE_ID, GOAL_ID, COMPANY),
    )
    _facet(
        connection,
        ACTIVITY_FACET_ID,
        PROFILE_ID,
        "activity",
        "Metal production",
        (1, 0, 0),
        801,
        keywords=["вольфрам"],
    )
    _facet(
        connection,
        COUNTERPARTY_FACET_ID,
        PROFILE_ID,
        "counterparty_network",
        "Known synthetic counterparty",
        (0, 1, 0),
        802,
    )
    _facet(
        connection,
        UUID(int=504),
        FUTURE_PROFILE_ID,
        "activity",
        "Future business activity",
        (0, 0, 0, 1),
        804,
        published=FUTURE,
    )
    for profile_id, published in ((PROFILE_ID, KNOWN), (FUTURE_PROFILE_ID, FUTURE)):
        connection.execute(
            "UPDATE company_profiles SET status='ready',published_at=%s WHERE profile_id=%s",
            (published, profile_id),
        )


def _seed_preferences(connection):
    connection.execute(
        """INSERT INTO user_preferences
        (user_context_id,company_id,user_id,context_version,business_role,watchlist,created_at)
        VALUES (%s,%s,%s,1,'Procurement manager',%s,%s)""",
        (USER_CONTEXT_ID, COMPANY, USER_ID, ["АрктическийПорт"], KNOWN),
    )
    connection.execute(
        """INSERT INTO user_preference_goals
        (goal_id,user_context_id,company_id,text,created_at)
        VALUES (%s,%s,%s,'Monitor port logistics',%s)""",
        (PERSONAL_GOAL_ID, USER_CONTEXT_ID, COMPANY, KNOWN),
    )
    connection.execute(
        "UPDATE user_preferences SET status='ready',published_at=%s WHERE user_context_id=%s",
        (KNOWN, USER_CONTEXT_ID),
    )
    _facet(
        connection,
        PERSONAL_FACET_ID,
        PROFILE_ID,
        "watchlist",
        "Arctic port logistics",
        (0, 0, 1),
        803,
        scope="user_company",
        entities=[{"name": "АрктическийПорт"}],
    )


def _seed_news(connection):
    add_news(connection, "activity", "Вольфрам: завод расширяет производство", (1, 0, 0))
    add_news(connection, "counterparty", "Поставщик меняет стоимость поставок", (0, 1, 0))
    add_news(connection, "lexical", "Вольфрам дорожает на рынке", (-1, 0, 0))
    add_news(connection, "personal", "АрктическийПорт ограничивает перевозки", (0, 0, 1))
    add_news(connection, "future", "Future article", (1, 0, 0), published=FUTURE)
    add_news(connection, "late-fetched", "Late fetched article", (1, 0, 0), fetched=FUTURE)
    add_news(connection, "late-revision", "Late revision article", (1, 0, 0), recorded=FUTURE)
    add_news(connection, "late-embedding", "Late embedded article", (1, 0, 0), embedded=FUTURE)
    add_news(connection, "undated", "Undated article", (1, 0, 0), published=None)
    add_news(connection, "duplicate", "Reprinted article", (1, 0, 0), duplicate="activity")
    add_news(connection, "wrong-config", "Wrong model article", (1, 0, 0), config=OTHER_CONFIG_ID)
    add_news(
        connection, "missing-current-revision", "Missing revision", (1, 0, 0), current_version="v2"
    )
    add_news(connection, "stale-embedding", "Stale embedding", (1, 0, 0))
    connection.execute(
        """INSERT INTO news_revisions(article_id,content_version,title,body,recorded_at)
        VALUES ('stale-embedding','v2','Stale embedding updated','Updated body',%s)""",
        (KNOWN,),
    )
    connection.execute(
        "UPDATE news SET content_version='v2',title='Stale embedding updated' "
        "WHERE article_id='stale-embedding'"
    )
    add_news(connection, "lexical-without-vector", "Вольфрам без вектора", None)


def seed_database(connection):
    """Publish draft snapshots in the same order required of upstream writers."""
    _seed_identity(connection)
    _seed_embedding_configs(connection)
    _seed_profiles(connection)
    _seed_preferences(connection)
    _seed_news(connection)

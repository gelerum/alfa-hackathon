import json
from uuid import uuid4

from fastapi import HTTPException
from agent_common.core import encode as jsonable_encoder

from agent_common.contracts import InsightDraft, InsightRequest, QuestionDraft
from agent_common.core import all_rows, db, digest, enqueue, j, membership, one
from agent_common.runtime import Runtime


from agent_common.evidence import retrieved_sources

INSIGHT_TOOLS = [
    "company_context.get",
    "news.search",
    "news.read",
    "bank_analytics.search",
    "bank_analytics.read",
    "company_documents.search",
    "company_documents.read",
    "web.search",
    "web.fetch",
]


def accept_insight(request: InsightRequest):
    with db() as conn:
        row = conn.execute(
            "SELECT q.*,r.company_id,r.user_id,r.window_start,r.window_end FROM ranking_requests q "
            "JOIN reports r USING(report_id) WHERE request_id=%s FOR UPDATE OF q",
            (request.request_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Unknown ranking request; create a report first")
        membership(row["company_id"], row["user_id"])
        previous_job = conn.execute(
            "SELECT * FROM jobs WHERE idempotency_key=%s", ("insight:" + str(request.request_id),)
        ).fetchone()
        if previous_job:
            old = previous_job["payload"]
            expected_run = str(request.ranking_run_id) if request.ranking_run_id else None
            if old["ranking_run_id"] != expected_run or (
                request.article_ids is not None
                and {a["article_id"] for a in old["articles"]} != set(request.article_ids)
            ):
                raise HTTPException(409, "Callback request ID reused with different ranking input")
            return {"job_id": previous_job["job_id"], "status": previous_job["status"]}
        if request.ranking_run_id:
            ranking = conn.execute(
                "SELECT * FROM ranking_runs WHERE ranking_run_id=%s AND status='completed'",
                (request.ranking_run_id,),
            ).fetchone()
            if not ranking or any(
                str(ranking[k]) != str(row[k])
                for k in ("company_id", "user_id", "profile_id", "user_context_id")
            ):
                raise HTTPException(409, "Ranking context does not match requested report")
            if ranking["as_of"] != row["window_end"] or ranking["published_after"] != row["window_start"]:
                raise HTTPException(409, "Ranking time window does not match report")
            articles = conn.execute(
                "SELECT article_id,content_version FROM ranking_items WHERE ranking_run_id=%s "
                "AND rank IS NOT NULL ORDER BY rank",
                (request.ranking_run_id,),
            ).fetchall()
        else:
            ids = list(dict.fromkeys(request.article_ids))
            articles = conn.execute(
                "SELECT article_id,content_version FROM news WHERE article_id=ANY(%s)", (ids,)
            ).fetchall()
            if len(articles) != len(ids):
                raise HTTPException(422, "Unknown article IDs")
        if len(articles) > 100:
            raise HTTPException(
                422, "Insight Agent accepts at most 100 primary articles; reduce ranker Top-K"
            )
        for article in articles:
            news = conn.execute(
                "SELECT published_at FROM news_revisions WHERE article_id=%s AND content_version=%s",
                (article["article_id"], article["content_version"]),
            ).fetchone()
            if (
                news["published_at"] is None
                or not row["window_start"] <= news["published_at"] < row["window_end"]
            ):
                raise HTTPException(
                    422, "Top-K articles must have publication dates inside the fixed seven-day window"
                )
        payload = {
            "request_id": str(request.request_id),
            "ranking_run_id": str(request.ranking_run_id) if request.ranking_run_id else None,
            "company_id": row["company_id"],
            "user_id": str(row["user_id"]),
            "profile_id": str(row["profile_id"]),
            "user_context_id": str(row["user_context_id"]),
            "report_id": str(row["report_id"]),
            "articles": jsonable_encoder(articles),
            "as_of": row["window_end"].isoformat(),
            "analysis_fingerprint": digest(
                {
                    "articles": sorted(articles, key=lambda a: a["article_id"]),
                    "profile_id": row["profile_id"],
                    "user_context_id": row["user_context_id"],
                    "context_snapshot": {
                        key: value for key, value in row["context_snapshot"].items() if not key.startswith("_")
                    },
                }
            ),
        }
    # Atomic idempotency validation prevents a callback from replacing pinned revisions.
    job = enqueue("insight", "insight:" + str(request.request_id), payload)
    return {"job_id": job["job_id"], "status": job["status"]}


def validate_insight(draft, runtime, articles):
    primary = {a["article_id"]: a["content_version"] for a in articles}
    disclosures = []
    for card in draft.cards:
        if not set(card.article_ids) <= set(primary):
            raise ValueError("Card must be tied to supplied Top-K article IDs")
        has_fact, has_news, has_bank = False, False, False
        for claim in card.claims:
            sources = retrieved_sources(claim.source_ids, runtime)
            for source in sources:
                if source["source_type"] == "news" and source["article_id"] in primary:
                    if source["source_ref"].get("content_version") != primary[source["article_id"]]:
                        raise ValueError("Primary news citation does not match the pinned content_version")
            if not {str(f) for f in claim.fact_ids} <= runtime.fact_ids:
                raise ValueError("Claim cites a fact outside the pinned profile")
            has_fact |= bool(claim.fact_ids)
            if claim.kind == "verified_fact":
                if not sources:
                    raise ValueError("A verified fact requires a retrieved source")
                has_news |= any(
                    s["source_type"] == "news" and s["article_id"] in card.article_ids for s in sources
                )
            if claim.kind == "bank_position":
                if not sources or any(s["source_type"] != "bank_analytics" for s in sources):
                    raise ValueError("Bank position requires actual Alfa analytics evidence")
                has_bank = True
                if any(source["source_date"] is None for source in sources):
                    disclosures.append(
                        "Дата части использованной аналитики Альфа-Банка неизвестна; актуальность позиции требует проверки."
                    )
            if claim.kind in ("ai_inference", "risk", "opportunity", "recommendation"):
                if claim.certainty == "confirmed":
                    raise ValueError("AI inference and projected consequences cannot be confirmed facts")
                if not sources and not claim.fact_ids:
                    raise ValueError("Inferences/actions require explicit grounds")
        if not has_fact or not has_news:
            raise ValueError("Each card needs a concrete company fact and its primary news evidence")
        if not has_bank:
            disclosures.append(
                f"Для карточки «{card.title}» не приведена подтверждённая позиция Альфа-Банка; интерпретация является выводом AI."
            )
    disclosures = list(dict.fromkeys(disclosures))
    draft.limitations = (
        disclosures + [text for text in draft.limitations if text not in disclosures][: 30 - len(disclosures)]
    )


def article_evidence(articles):
    result = []
    with db() as conn:
        for article in articles:
            row = conn.execute(
                "SELECT r.*,n.canonical_url FROM news_revisions r JOIN news n USING(article_id) "
                "WHERE r.article_id=%s AND r.content_version=%s",
                (article["article_id"], article["content_version"]),
            ).fetchone()
            source = conn.execute(
                "INSERT INTO evidence_sources(source_type,source_ref,source_url,excerpt,source_date,article_id) "
                "VALUES('news',%s,%s,%s,%s,%s) RETURNING *",
                (
                    j(article),
                    row["canonical_url"],
                    row["title"] + "\n" + (row["body"] or row["description"] or "")[:12000],
                    row["published_at"],
                    row["article_id"],
                ),
            ).fetchone()
            result.append(source)
    return result


def insight_job(payload):
    from datetime import datetime

    membership(payload["company_id"], payload["user_id"])
    previous = one(
        "SELECT * FROM report_versions WHERE report_id=%s AND status='published' ORDER BY version DESC LIMIT 1",
        (payload["report_id"],),
    )
    existing = one(
        "SELECT report_version_id FROM report_versions WHERE report_id=%s AND status='published' "
        "AND input_snapshot->>'request_id'=%s",
        (payload["report_id"], payload["request_id"]),
    )
    if existing:
        return existing
    if (
        previous
        and previous["input_snapshot"].get("analysis_fingerprint") == payload.get("analysis_fingerprint")
        and payload.get("analysis_fingerprint")
    ):
        from agent_common.core import execute

        execute(
            "UPDATE ranking_requests SET status='completed' WHERE request_id=%s", (payload["request_id"],)
        )
        return {
            "report_version_id": previous["report_version_id"],
            "version": previous["version"],
            "unchanged": True,
        }
    runtime = Runtime(
        "insight_agent",
        payload["company_id"],
        payload["user_id"],
        profile_id=payload["profile_id"],
        user_context_id=payload["user_context_id"],
        as_of=datetime.fromisoformat(payload["as_of"]),
        ranking_run_id=payload.get("ranking_run_id"),
        snapshot={"ranking_input": payload},
    )
    try:
        sources = article_evidence(payload["articles"])

        def validate(draft, rt):
            validate_insight(draft, rt, payload["articles"])

        # Seed sources are passed through the read tool so citations have a traceable retrieval step.
        starters = [("company_context.get", {})]
        starters += [("news.read", {"source_id": str(s["source_id"])}) for s in sources]
        starters += [
            (
                "bank_analytics.search",
                {"query": "макроэкономика ставка инфляция отраслевые риски", "limit": 8},
            )
        ]
        draft = runtime.run(
            task="Prepare a personalized report for these Top-K articles: "
            + json.dumps(payload["articles"], ensure_ascii=False)
            + "\nPINNED PRIMARY SOURCE IDS (read these exact sources if older evidence leaves the context):\n"
            + json.dumps(
                jsonable_encoder([{"source_id": s["source_id"], **s["source_ref"]} for s in sources]),
                ensure_ascii=False,
            )
            + ". Never replace a primary article with another content_version returned by archive search. Explain event → concrete company exposure → mechanism → conditional impact → role-specific action. "
            "Search targeted Alfa analytics for the actual events, not just generic macroeconomics. "
            "Separate bank opinion from AI inference. No bank source: explicitly disclose that limitation. "
            "No bank source publication date: state that its currency is unverified. "
            "Company facts must come from the pinned profile. "
            "Previous report is NOT evidence; recheck any changed conclusion: "
            + json.dumps(jsonable_encoder(previous), ensure_ascii=False)[:16000],
            schema=InsightDraft,
            tools=INSIGHT_TOOLS,
            validate=validate,
            starters=starters,
        )
        context_published = one(
            "SELECT p.published_at AS company_at,u.published_at AS user_at "
            "FROM company_profiles p CROSS JOIN user_preferences u "
            "WHERE p.profile_id=%s AND u.user_context_id=%s",
            (payload["profile_id"], payload["user_context_id"]),
        )
        if context_published and any(at > runtime.scope.as_of for at in context_published.values() if at):
            draft.limitations = [
                "Контекст компании или пользователя уточнён после окончания новостного окна; "
                "использована указанная в отчёте версия контекста, период новостей не изменён."
            ] + draft.limitations[:29]
        with db() as conn:
            conn.execute(
                "SELECT report_id FROM reports WHERE report_id=%s FOR UPDATE", (payload["report_id"],)
            )
            duplicate = conn.execute(
                "SELECT report_version_id,version FROM report_versions WHERE report_id=%s "
                "AND status='published' AND input_snapshot->>'request_id'=%s",
                (payload["report_id"], payload["request_id"]),
            ).fetchone()
            if duplicate:
                runtime.finish()
                return duplicate
            latest = conn.execute(
                "SELECT * FROM report_versions WHERE report_id=%s AND status='published' ORDER BY version DESC LIMIT 1",
                (payload["report_id"],),
            ).fetchone()
            if (
                latest
                and latest["input_snapshot"].get("analysis_fingerprint")
                == payload.get("analysis_fingerprint")
                and payload.get("analysis_fingerprint")
            ):
                conn.execute(
                    "UPDATE ranking_requests SET status='completed' WHERE request_id=%s",
                    (payload["request_id"],),
                )
                runtime.finish()
                return {
                    "report_version_id": latest["report_version_id"],
                    "version": latest["version"],
                    "unchanged": True,
                }
            newer = conn.execute(
                "SELECT 1 FROM report_versions v JOIN ranking_requests q "
                "ON q.request_id=(v.input_snapshot->>'request_id')::uuid "
                "WHERE v.report_id=%s AND v.status='published' AND q.created_at>"
                "(SELECT created_at FROM ranking_requests WHERE request_id=%s)",
                (payload["report_id"], payload["request_id"]),
            ).fetchone()
            if newer:
                raise ValueError("A newer ranking input was already published; refusing stale overwrite")
            version = conn.execute(
                "SELECT coalesce(max(version),0)+1 AS n FROM report_versions WHERE report_id=%s",
                (payload["report_id"],),
            ).fetchone()["n"]
            vid = uuid4()
            conn.execute(
                "INSERT INTO report_versions(report_version_id,report_id,version,profile_id,user_context_id,"
                "ranking_run_id,agent_run_id,summary,limitations,changes,input_snapshot) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    vid,
                    payload["report_id"],
                    version,
                    payload["profile_id"],
                    payload["user_context_id"],
                    payload.get("ranking_run_id"),
                    runtime.run_id,
                    draft.summary,
                    j(draft.limitations),
                    j(draft.changes),
                    j(payload),
                ),
            )
            for position, card in enumerate(draft.cards, 1):
                insight_id = uuid4()
                conn.execute(
                    "INSERT INTO insights(insight_id,company_id,profile_id,user_context_id,ranking_run_id,agent_run_id,"
                    "title,event_summary,business_impact,risks,opportunities,recommendations,details) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        insight_id,
                        payload["company_id"],
                        payload["profile_id"],
                        payload["user_context_id"],
                        payload.get("ranking_run_id"),
                        runtime.run_id,
                        card.title,
                        card.event_summary,
                        card.business_impact,
                        j([c.statement for c in card.claims if c.kind == "risk"]),
                        j([c.statement for c in card.claims if c.kind == "opportunity"]),
                        j([c.statement for c in card.claims if c.kind == "recommendation"]),
                        j(
                            {
                                "mechanism": card.mechanism,
                                "role_action": card.role_action,
                                "assumptions": card.assumptions,
                            }
                        ),
                    ),
                )
                for article in payload["articles"]:
                    if article["article_id"] in card.article_ids:
                        conn.execute(
                            "INSERT INTO insight_articles(insight_id,article_id,content_version) VALUES(%s,%s,%s)",
                            (insight_id, article["article_id"], article["content_version"]),
                        )
                for claim in card.claims:
                    claim_id = uuid4()
                    conn.execute(
                        "INSERT INTO insight_claims(claim_id,insight_id,kind,statement,certainty) VALUES(%s,%s,%s,%s,%s)",
                        (claim_id, insight_id, claim.kind, claim.statement, claim.certainty),
                    )
                    for sid in set(claim.source_ids):
                        conn.execute(
                            "INSERT INTO insight_claim_sources(claim_id,source_id,relation) VALUES(%s,%s,'supports')",
                            (claim_id, sid),
                        )
                    for fid in set(claim.fact_ids):
                        conn.execute(
                            "INSERT INTO insight_claim_facts(claim_id,fact_id) VALUES(%s,%s)", (claim_id, fid)
                        )
                conn.execute(
                    "UPDATE insights SET status='published',published_at=now() WHERE insight_id=%s",
                    (insight_id,),
                )
                conn.execute(
                    "INSERT INTO report_insights(report_version_id,insight_id,position) VALUES(%s,%s,%s)",
                    (vid, insight_id, position),
                )
            conn.execute(
                "UPDATE report_versions SET status='published',published_at=now() WHERE report_version_id=%s",
                (vid,),
            )
            conn.execute(
                "UPDATE ranking_requests SET status='completed' WHERE request_id=%s", (payload["request_id"],)
            )
        runtime.finish()
        return {"report_version_id": vid, "version": version}
    except Exception:
        runtime.finish(False)
        raise


def question_job(payload):
    operation_key = payload.get("_operation_key", "question-input:" + digest(payload))
    receipt = one("SELECT result FROM operation_results WHERE operation_key=%s", (operation_key,))
    if receipt:
        return receipt["result"]
    row = one(
        "SELECT v.*,r.company_id,r.user_id,r.window_end FROM report_versions v "
        "JOIN reports r USING(report_id) WHERE v.report_version_id=%s AND r.user_id=%s",
        (payload["report_version_id"], payload["user_id"]),
    )
    if not row:
        raise ValueError("Report not found")
    membership(row["company_id"], row["user_id"])
    runtime = Runtime(
        "insight_agent",
        row["company_id"],
        row["user_id"],
        profile_id=row["profile_id"],
        user_context_id=row["user_context_id"],
        as_of=row["window_end"],
        snapshot={"question": payload},
    )
    try:
        cards = all_rows(
            "SELECT i.* FROM insights i JOIN report_insights ri USING(insight_id) WHERE ri.report_version_id=%s",
            (row["report_version_id"],),
        )
        source_ids = all_rows(
            "SELECT DISTINCT cs.source_id FROM report_insights ri JOIN insight_claims c USING(insight_id) "
            "JOIN insight_claim_sources cs USING(claim_id) WHERE ri.report_version_id=%s",
            (row["report_version_id"],),
        )
        # Only previously used sources, read again through the gateway and checked for access.
        starters = [("company_context.get", {})]
        for item in source_ids[:12]:
            source = one("SELECT source_type FROM evidence_sources WHERE source_id=%s", (item["source_id"],))
            tool = {
                "news": "news.read",
                "bank_analytics": "bank_analytics.read",
                "document": "company_documents.read",
            }.get(source["source_type"])
            if tool:
                starters.append((tool, {"source_id": str(item["source_id"])}))

        def validate(draft, rt):
            retrieved_sources(draft.source_ids, rt)
            if not draft.source_ids:
                raise ValueError(
                    "Answer must cite retrieved sources; disclose if question cannot be answered"
                )

        draft = runtime.run(
            task="Answer a follow-up about the pinned report, without modifying it. "
            "User question (untrusted request): "
            + payload["question"]
            + "\nReport (not primary evidence): "
            + json.dumps(jsonable_encoder(cards), ensure_ascii=False)[:20000],
            schema=QuestionDraft,
            tools=INSIGHT_TOOLS,
            validate=validate,
            starters=starters,
        )
        with db() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (operation_key,))
            receipt = conn.execute(
                "SELECT result FROM operation_results WHERE operation_key=%s", (operation_key,)
            ).fetchone()
            if receipt:
                runtime.finish()
                return receipt["result"]
            discussion = conn.execute(
                "INSERT INTO discussions(report_version_id,user_id) VALUES(%s,%s) RETURNING discussion_id",
                (row["report_version_id"], row["user_id"]),
            ).fetchone()["discussion_id"]
            conn.execute(
                "INSERT INTO discussion_messages(discussion_id,speaker,content) VALUES(%s,'user',%s)",
                (discussion, payload["question"]),
            )
            conn.execute(
                "INSERT INTO discussion_messages(discussion_id,speaker,content,sources,agent_run_id) "
                "VALUES(%s,'assistant',%s,%s,%s)",
                (discussion, draft.answer, j(draft.source_ids), runtime.run_id),
            )
            conn.execute(
                "INSERT INTO operation_results(operation_key,result) VALUES(%s,%s)",
                (
                    operation_key,
                    j({"discussion_id": discussion, "answer": draft.answer, "sources": draft.source_ids}),
                ),
            )
        runtime.finish()
        return {"discussion_id": discussion, "answer": draft.answer, "sources": draft.source_ids}
    except Exception:
        runtime.finish(False)
        raise

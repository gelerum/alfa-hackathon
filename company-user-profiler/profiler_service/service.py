import json
from uuid import uuid4

from agent_common.core import encode as jsonable_encoder

from agent_common.contracts import ProfileDraft
from agent_common.core import db, digest, j, membership, one
from agent_common.embeddings import config, query_embedding
from agent_common.runtime import Runtime


from agent_common.evidence import retrieved_sources

PROFILE_TOOLS = [
    "company_bank_data.get",
    "company_documents.search",
    "company_documents.read",
    "web.search",
    "web.fetch",
]


def validate_profile(draft, runtime):
    for fact in draft.facts:
        sources = retrieved_sources(fact.source_ids, runtime)
        # Web research is generic in the fictional-company demo, never company identity/facts.
        if any(
            s["source_type"] not in ("bank_import", "document", "derived", "user_answer") for s in sources
        ):
            raise ValueError(
                "Fictional-company facts require company documents, bank data or explicit company clarifications"
            )
        if not any(fact.evidence_quote in (s["excerpt"] or "") for s in sources):
            raise ValueError("Fact evidence quote must be an exact retrieved excerpt")
    for facet in draft.facets:
        if any(i < 0 or i >= len(draft.facts) for i in facet.fact_indexes):
            raise ValueError("Facet references an unknown fact index")


def company_inputs(conn, company_id):
    return {
        "bank": conn.execute(
            "SELECT b.bank_record_id,b.source_snapshot_id,s.source_as_of,s.currency,s.revenue_year "
            "FROM bank_company_data b JOIN bank_import_batches s USING(source_snapshot_id) "
            "WHERE b.company_id=%s AND s.status='ready' ORDER BY b.bank_record_id",
            (company_id,),
        ).fetchall(),
        "payments": conn.execute(
            "SELECT p.source_snapshot_id,count(*) AS records,max(p.payment_edge_id) AS last_edge "
            "FROM bank_payment_edges p JOIN bank_import_batches s USING(source_snapshot_id) "
            "WHERE (p.payer=%s OR p.beneficiary=%s) AND s.status='ready' "
            "GROUP BY p.source_snapshot_id ORDER BY p.source_snapshot_id",
            (company_id, company_id),
        ).fetchall(),
        "people": conn.execute(
            "SELECT p.source_snapshot_id,count(*) AS records,max(p.person_link_id) AS last_link "
            "FROM bank_person_links p JOIN bank_import_batches s USING(source_snapshot_id) "
            "WHERE s.status='ready' AND p.synthetic_f IN (SELECT l.synthetic_f FROM bank_person_links l "
            "JOIN bank_import_batches own ON own.source_snapshot_id=l.source_snapshot_id "
            "WHERE l.synthetic_u=%s AND own.status='ready') GROUP BY p.source_snapshot_id ORDER BY p.source_snapshot_id",
            (company_id,),
        ).fetchall(),
        "groups": conn.execute(
            "SELECT b.source_snapshot_id,count(*) AS records,max(b.bank_record_id) AS last_record "
            "FROM bank_company_data b JOIN bank_import_batches s USING(source_snapshot_id) "
            "WHERE s.status='ready' AND b.group_id IN (SELECT l.group_id FROM bank_company_data l "
            "JOIN bank_import_batches own ON own.source_snapshot_id=l.source_snapshot_id "
            "WHERE l.company_id=%s AND own.status='ready') GROUP BY b.source_snapshot_id ORDER BY b.source_snapshot_id",
            (company_id,),
        ).fetchall(),
        "documents": conn.execute(
            "SELECT document_id,content_sha256,processing_status FROM documents "
            "WHERE company_id=%s ORDER BY document_id",
            (company_id,),
        ).fetchall(),
        "clarifications": conn.execute(
            "SELECT source_id FROM evidence_sources WHERE company_id=%s AND source_type='user_answer' "
            "AND source_ref->>'kind'='company_clarification' ORDER BY source_id",
            (company_id,),
        ).fetchall(),
    }


def profile_job(payload):
    company_id, user_id = payload["company_id"], payload["user_id"]
    membership(company_id, user_id, write=True)
    operation_key = payload.get("_operation_key", "profile-input:" + digest(payload))
    receipt = one("SELECT result FROM operation_results WHERE operation_key=%s", (operation_key,))
    if receipt:
        return receipt["result"]
    answer = one(
        "SELECT * FROM onboarding_answers WHERE company_id=%s AND user_id=%s "
        "AND (%s::uuid IS NULL OR answer_id=%s) ORDER BY created_at DESC LIMIT 1",
        (company_id, user_id, payload.get("answer_id"), payload.get("answer_id")),
    )
    latest_answer = one(
        "SELECT answer_id FROM onboarding_answers WHERE company_id=%s AND user_id=%s "
        "ORDER BY created_at DESC LIMIT 1",
        (company_id, user_id),
    )
    if payload.get("answer_id") and latest_answer and str(latest_answer["answer_id"]) != payload["answer_id"]:
        return {"status": "superseded", "reason": "A newer onboarding answer exists"}
    answers = (
        answer["answers"]
        if answer
        else {"role": "Руководитель", "responsibilities": [], "goals": [], "note": ""}
    )
    with db() as conn:
        inputs = company_inputs(conn, company_id)
    if any(d["processing_status"] in ("pending", "processing") for d in inputs["documents"]):
        raise RuntimeError("Waiting for document extraction")
    fingerprint = digest(inputs)
    existing = one(
        "SELECT * FROM company_profiles WHERE company_id=%s AND status='ready' "
        "AND payload->>'input_hash'=%s ORDER BY profile_version DESC LIMIT 1",
        (company_id, fingerprint),
    )
    runtime = None
    try:
        if not existing:
            runtime = Runtime("company_profiler", company_id, user_id, snapshot={"company_inputs": inputs})
            draft = runtime.run(
                task="Build a shared COMPANY profile only, never personal goals or user-specific assumptions. "
                "Use bank evidence, uploaded company documents and explicit shared company clarifications. "
                "User clarifications are reported information, not independent verification. Preserve missing data and conflicts. "
                "Facts need exact evidence quotes and source IDs. Facet fact_indexes are zero-based. "
                "Web is optional generic industry research, never a source of company identity. "
                "One concise clarification question is allowed only for a major unresolved gap. "
                "Failed documents: " + json.dumps(jsonable_encoder(inputs["documents"]), ensure_ascii=False),
                schema=ProfileDraft,
                tools=PROFILE_TOOLS,
                validate=validate_profile,
                starters=[
                    ("company_bank_data.get", {}),
                    (
                        "company_documents.search",
                        {"query": "деятельность компании продукция рынки финансы", "limit": 10},
                    ),
                ],
            )
            cfg = config()
            facet_vectors = [(facet, query_embedding(facet.text, cfg)) for facet in draft.facets]
        else:
            draft = None
            cfg = config()
            facet_vectors = []
        profile_summary = draft.summary if draft else existing["payload"]["summary"]
        personal_text = (
            f"{profile_summary[:500]}\nРоль: {answers['role']}. Ответственность: "
            + ", ".join(answers.get("responsibilities", []))
            + ". Цели: "
            + ", ".join(answers.get("goals", []))
            + ". Фокус пользователя: "
            + answers.get("note", "")
        )[:1800]
        personal_embedding = query_embedding(personal_text, cfg)
        with db() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("profile:" + company_id,))
            receipt = conn.execute(
                "SELECT result FROM operation_results WHERE operation_key=%s", (operation_key,)
            ).fetchone()
            if receipt:
                if runtime:
                    runtime.finish()
                return receipt["result"]
            newest_answer = conn.execute(
                "SELECT answer_id FROM onboarding_answers WHERE company_id=%s AND user_id=%s "
                "ORDER BY created_at DESC LIMIT 1",
                (company_id, user_id),
            ).fetchone()
            if (str(newest_answer["answer_id"]) if newest_answer else None) != (
                str(answer["answer_id"]) if answer else None
            ):
                raise RuntimeError("User answers changed during profiling; retry required")
            # Check again after external calls: concurrent publication or changed input must not misbind.
            current_inputs = company_inputs(conn, company_id)
            if digest(current_inputs) != fingerprint:
                raise RuntimeError("Company inputs changed during profiling; retry required")
            if draft:
                concurrent = conn.execute(
                    "SELECT * FROM company_profiles WHERE company_id=%s AND status='ready' "
                    "AND payload->>'input_hash'=%s ORDER BY profile_version DESC LIMIT 1",
                    (company_id, fingerprint),
                ).fetchone()
                if concurrent:
                    existing = concurrent
                else:
                    version = conn.execute(
                        "SELECT coalesce(max(profile_version),0)+1 AS n FROM company_profiles "
                        "WHERE company_id=%s",
                        (company_id,),
                    ).fetchone()["n"]
                    profile_id = uuid4()
                    conn.execute(
                        "INSERT INTO company_profiles(profile_id,company_id,profile_version,payload,missing_data,"
                        "conflicts,generator_version) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                        (
                            profile_id,
                            company_id,
                            version,
                            j(
                                {
                                    "schema_version": 1,
                                    "summary": draft.summary,
                                    "input_hash": fingerprint,
                                    "clarification": draft.clarification,
                                    "is_fictional": True,
                                    "agent_run_id": runtime.run_id,
                                    "document_ids": [
                                        str(d["document_id"])
                                        for d in inputs["documents"]
                                        if d["processing_status"] == "ready"
                                    ],
                                }
                            ),
                            j(draft.missing_data),
                            j(draft.conflicts),
                            "profiler-v1",
                        ),
                    )
                    fact_ids = []
                    for fact in draft.facts:
                        sources = retrieved_sources(fact.source_ids, runtime)
                        source = sources[0]
                        source_type = (
                            source["source_type"] if source["source_type"] != "bank_analytics" else "document"
                        )
                        fact_id = uuid4()
                        conn.execute(
                            "INSERT INTO company_facts(fact_id,company_id,field,value,source_type,source_ref,"
                            "evidence_text,assertion_type,status,validation_reason) "
                            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'usable',%s)",
                            (
                                fact_id,
                                company_id,
                                fact.field,
                                j(fact.value),
                                source_type,
                                j(source["source_ref"]),
                                fact.evidence_quote,
                                "calculated" if source_type == "derived" else "reported",
                                "Grounded extraction and model self-review",
                            ),
                        )
                        for sid in fact.source_ids:
                            conn.execute(
                                "INSERT INTO fact_sources(fact_id,source_id) VALUES(%s,%s)", (fact_id, sid)
                            )
                        conn.execute(
                            "INSERT INTO company_profile_facts(profile_id,fact_id,company_id) VALUES(%s,%s,%s)",
                            (profile_id, fact_id, company_id),
                        )
                        fact_ids.append(fact_id)
                    for bank in inputs["bank"] + inputs["payments"] + inputs["people"] + inputs["groups"]:
                        conn.execute(
                            "INSERT INTO company_profile_bank_sources(profile_id,source_snapshot_id) VALUES(%s,%s) "
                            "ON CONFLICT DO NOTHING",
                            (profile_id, bank["source_snapshot_id"]),
                        )
                    for facet, embedding_id in facet_vectors:
                        facet_id = uuid4()
                        conn.execute(
                            "INSERT INTO company_context_facets(facet_id,company_id,profile_id,scope,facet_type,text,"
                            "keywords,embedding_config_id,embedding_id,text_hash,generation_version) "
                            "VALUES(%s,%s,%s,'company',%s,%s,%s,%s,%s,%s,'profiler-v1')",
                            (
                                facet_id,
                                company_id,
                                profile_id,
                                facet.facet_type,
                                facet.text,
                                facet.keywords,
                                cfg["embedding_config_id"],
                                embedding_id,
                                digest(facet.text.encode()),
                            ),
                        )
                        for index in set(facet.fact_indexes):
                            conn.execute(
                                "INSERT INTO facet_facts(facet_id,profile_id,fact_id) VALUES(%s,%s,%s)",
                                (facet_id, profile_id, fact_ids[index]),
                            )
                        conn.execute(
                            "UPDATE company_context_facets SET status='ready',published_at=now() WHERE facet_id=%s",
                            (facet_id,),
                        )
                    conn.execute(
                        "UPDATE company_profiles SET status='ready',published_at=now() WHERE profile_id=%s",
                        (profile_id,),
                    )
                    existing = conn.execute(
                        "SELECT * FROM company_profiles WHERE profile_id=%s", (profile_id,)
                    ).fetchone()
            pref_version = conn.execute(
                "SELECT coalesce(max(context_version),0)+1 AS n FROM user_preferences "
                "WHERE company_id=%s AND user_id=%s",
                (company_id, user_id),
            ).fetchone()["n"]
            context_id = uuid4()
            conn.execute(
                "INSERT INTO user_preferences(user_context_id,company_id,user_id,context_version,business_role,"
                "responsibility,focus_topics,raw_answers,answer_depth) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    context_id,
                    company_id,
                    user_id,
                    pref_version,
                    answers["role"],
                    ", ".join(answers.get("responsibilities", [])),
                    answers.get("responsibilities", []),
                    j(
                        {
                            **answers,
                            "schema_version": 1,
                            "source_answer_id": answer["answer_id"] if answer else None,
                        }
                    ),
                    "expandable",
                ),
            )
            goal_ids = []
            for text in answers.get("goals", []):
                gid = uuid4()
                conn.execute(
                    "INSERT INTO user_preference_goals(goal_id,user_context_id,company_id,text) VALUES(%s,%s,%s,%s)",
                    (gid, context_id, company_id, text),
                )
                goal_ids.append(gid)
            conn.execute(
                "UPDATE user_preferences SET status='ready',published_at=now() WHERE user_context_id=%s",
                (context_id,),
            )
            facet_id = uuid4()
            conn.execute(
                "INSERT INTO company_context_facets(facet_id,company_id,profile_id,scope,user_context_id,facet_type,"
                "text,embedding_config_id,embedding_id,text_hash,generation_version) "
                "VALUES(%s,%s,%s,'user_company',%s,'user_focus',%s,%s,%s,%s,'profiler-v1')",
                (
                    facet_id,
                    company_id,
                    existing["profile_id"],
                    context_id,
                    personal_text,
                    cfg["embedding_config_id"],
                    personal_embedding,
                    digest(personal_text.encode()),
                ),
            )
            for gid in goal_ids:
                conn.execute(
                    "INSERT INTO facet_preference_goals(facet_id,user_context_id,goal_id) VALUES(%s,%s,%s)",
                    (facet_id, context_id, gid),
                )
            conn.execute(
                "UPDATE company_context_facets SET status='ready',published_at=now() WHERE facet_id=%s",
                (facet_id,),
            )
            conn.execute(
                "INSERT INTO operation_results(operation_key,result) VALUES(%s,%s)",
                (operation_key, j({"profile_id": existing["profile_id"], "user_context_id": context_id})),
            )
        if runtime:
            runtime.finish()
        return {"profile_id": existing["profile_id"], "user_context_id": context_id}
    except Exception:
        if runtime:
            runtime.finish(False)
        raise

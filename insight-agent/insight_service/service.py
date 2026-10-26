import json
import re
from uuid import UUID, uuid4
from urllib.parse import urlsplit

from fastapi import HTTPException
from agent_common.core import encode as jsonable_encoder

from agent_common.contracts import EvidenceInsightDraft, InsightDraft, InsightRequest, QuestionDraft
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
                        key: value
                        for key, value in row["context_snapshot"].items()
                        if not key.startswith("_")
                    },
                }
            ),
        }
    # Atomic idempotency validation prevents a callback from replacing pinned revisions.
    job = enqueue("insight", "insight:" + str(request.request_id), payload)
    return {"job_id": job["job_id"], "status": job["status"]}


MAX_PRIMARY_ARTICLES = 6
FACT_LABELS = {
    "main_okved_name": "вид деятельности",
    "main_okved_code": "код ОКВЭД",
    "registration_city": "город регистрации",
    "registration_region": "регион регистрации",
    "revenue_value": "выручка",
    "employees_fns_band": "численность сотрудников",
    "company_age_band": "возраст компании, лет",
    "eco_mark_label": "стадия развития",
    "okfs_name": "форма собственности",
    "cl_type": "статус клиента банка",
    "loan_mark": "есть кредитный продукт",
    "leasing_mark": "есть лизинг",
    "factoring_mark": "есть факторинг",
    "fts_flag": "есть таможенные операции",
    "fts_country_name": "страны внешней торговли",
    "group_id": "идентификатор группы",
    "payments_incoming": "входящие платежи",
    "payments_outgoing": "исходящие платежи",
    "corporate_group": "группа компаний",
}
ARTICLE_PREVIEW_CHARS = 1800
BANK_PREVIEW_CHARS = 900


def _sentences(text, limit):
    parts = re.split(r"(?<=[.!?…])\s+|\n+", text or "")
    return [p.strip() for p in parts if 30 <= len(p.strip()) <= 450][:limit]


def evidence_pack(results, article_count):
    """Short refs (F/A/B/S) so the model picks evidence instead of retyping UUIDs and quotes."""
    refs, lines = {}, []
    context = results[0] if results else {}
    lines.append("COMPANY FACTS (pinned profile):")
    for i, fact in enumerate((context.get("facts") or [])[:30], 1):
        refs[f"F{i}"] = ("fact", str(fact["fact_id"]))
        lines.append(f"F{i} {FACT_LABELS.get(fact['field'], fact['field'])}: {str(fact['value'])[:220]}")
    sentence_no = 0

    def quotes(source, limit, chars):
        nonlocal sentence_no
        for text in _sentences((source.get("excerpt") or "")[:chars], limit):
            sentence_no += 1
            refs[f"S{sentence_no}"] = ("sentence", source, text)
            lines.append(f"  S{sentence_no}: {text}")

    lines.append("PRIMARY NEWS (Top-K for this week):")
    for i, result in enumerate(results[1 : 1 + article_count], 1):
        for source in (result or {}).get("sources", [])[:1]:
            refs[f"A{i}"] = ("source", source)
            date = str(source.get("source_date") or "дата неизвестна")[:10]
            title = (source.get("excerpt") or "").split("\n", 1)[0][:200]
            lines.append(f"A{i} [{date}] {title}")
            quotes(source, 6, ARTICLE_PREVIEW_CHARS)
    bank = results[1 + article_count] if len(results) > 1 + article_count else {}
    lines.append("ALFA-BANK ANALYTICS:")
    for i, source in enumerate((bank or {}).get("sources", [])[:5], 1):
        refs[f"B{i}"] = ("source", source)
        date = str(source.get("source_date") or "дата неизвестна")[:10]
        lines.append(f"B{i} [{date}] {(source.get('excerpt') or '')[:120]}")
        quotes(source, 4, BANK_PREVIEW_CHARS)
    lines.append(
        "HOW TO ANSWER: synthesize EVENTS, not one card per post. Every impact conclusion must connect "
        "a company F-ref with at least TWO independent sources (e.g. A1+A2 from different publishers, "
        "or A1+B1). Cite both in the SAME ai_inference/risk/opportunity claim. Read the excerpts and "
        "explain what each contributes: confirmation, a complementary factor, or a contradiction. "
        "Do not count reposts as corroboration. Do not attach unrelated citations to satisfy a quota. "
        "If no independent support exists, omit the card and disclose the gap. "
        "Return action=finish with 1-4 cards in Russian. In claims use refs instead of ids: "
        "verified_fact = one S-ref from that card's article with EMPTY statement (the system copies the exact quote); "
        "bank_position = one S-ref from a B source with EMPTY statement; ai_inference/risk/opportunity/recommendation = "
        "your own statement plus refs to at least one F and the card's A. card.article_ids may use A-refs. "
        "Each card needs: verified_fact, at least one risk/opportunity/ai_inference with F+A refs, and a role-specific "
        "recommendation. Never put UUIDs in claims: refs only. Write for a business reader: call the company "
        "«компания», never print company IDs, field names (loan_mark, fts_country_name) or refs inside texts. Keep it short: at most 3 cards, 3-5 claims per card, "
        "each text field under 350 characters. Never invent debt terms, currency exposure or supplier contracts. "
        "Say 'если кредиты имеют плавающую ставку' and 'если закупки оплачиваются в юанях', not that they do. "
        "Absence of factoring is not a constraint. Company payment totals cover an unknown period, not annual revenue. "
        "There is no need for web research. If bank evidence is absent, "
        "disclose that limitation and finish; do not repeatedly search for it. "
        'Output shape example (use actual refs): {"action":"finish","result":{"summary":"...",'
        '"cards":[{"title":"...","event_summary":"...","business_impact":"...","mechanism":"...",'
        '"role_action":"...","article_ids":["A1"],"claims":['
        '{"kind":"verified_fact","refs":["S1"]},'
        '{"kind":"risk","statement":"...","refs":["F1","A1"]},'
        '{"kind":"recommendation","statement":"...","refs":["F1","A1"]}]}],"limitations":[]}}'
    )
    preferences = context.get("preferences") or {}
    answers = preferences.get("raw_answers") or {}
    lines.append(
        "USER PRIORITIES: "
        + json.dumps(
            {
                "role": preferences.get("business_role"),
                "responsibilities": answers.get("responsibilities", []),
                "goals": context.get("user_goals", []),
                "kpis": answers.get("kpis", []),
                "note": answers.get("note", ""),
            },
            ensure_ascii=False,
            default=str,
        )[:2500]
    )
    return refs, "\n".join(lines)


def resolve_refs(draft, refs, allowed_facts=None):
    """Replace short refs with exact source IDs, fact IDs and verbatim quotes."""
    for card in draft.cards:
        card.article_ids = [
            refs[a.strip().upper()][1]["article_id"]
            if refs.get(a.strip().upper(), ("",))[0] == "source"
            and refs[a.strip().upper()][1].get("article_id")
            else a
            for a in card.article_ids
        ]
        for claim in card.claims:
            for ref in claim.refs:
                entry = refs.get(ref.strip().upper())
                if not entry:
                    # Accept an exact known UUID for backwards compatibility, never a guessed ID.
                    entry = next(
                        (
                            item
                            for item in refs.values()
                            if (item[0] == "fact" and item[1] == ref)
                            or (item[0] in ("source", "sentence") and str(item[1]["source_id"]) == ref)
                        ),
                        None,
                    )
                if not entry:
                    raise ValueError(f"Unknown evidence ref {ref}; use only F/A/B/S refs from the list")
                if entry[0] == "fact":
                    claim.fact_ids.append(UUID(entry[1]))
                    continue
                source = entry[1]
                claim.source_ids.append(UUID(str(source["source_id"])))
                if source.get("article_id") and source["article_id"] not in card.article_ids:
                    card.article_ids.append(source["article_id"])
                if entry[0] == "sentence" and (
                    claim.kind in ("verified_fact", "bank_position") or not claim.statement.strip()
                ):
                    claim.statement = entry[2]
            claim.refs = []
            claim.source_ids = list(dict.fromkeys(claim.source_ids))
            claim.fact_ids = list(dict.fromkeys(claim.fact_ids))
            if allowed_facts is not None and any(str(fid) not in allowed_facts for fid in claim.fact_ids):
                raise ValueError(
                    "Claim cites a fact outside the pinned profile; use F-refs from the evidence pack"
                )
            if not claim.statement.strip() and claim.kind in ("verified_fact", "bank_position"):
                expected_type = "news" if claim.kind == "verified_fact" else "bank_analytics"
                for entry in refs.values():
                    if (
                        entry[0] == "sentence"
                        and entry[1].get("source_type") == expected_type
                        and UUID(str(entry[1]["source_id"])) in claim.source_ids
                    ):
                        claim.statement = entry[2]
                        break
            if not claim.statement.strip():
                raise ValueError(
                    f"Claim kind={claim.kind} needs non-empty statement, or a cited news/bank sentence"
                )
        # News quotations are mechanical, not model reasoning. If the model omitted the
        # verified_fact claim, copy a sentence from a news source it actually cited.
        if not any(claim.kind == "verified_fact" for claim in card.claims):
            cited = {str(sid) for claim in card.claims for sid in claim.source_ids}
            sentence = next(
                (
                    entry
                    for entry in refs.values()
                    if entry[0] == "sentence"
                    and entry[1].get("source_type") == "news"
                    and entry[1].get("article_id") in card.article_ids
                    and str(entry[1]["source_id"]) in cited
                ),
                None,
            )
            if sentence and len(card.claims) < 30:
                from agent_common.contracts import ClaimDraft

                card.claims.insert(
                    0,
                    ClaimDraft(
                        kind="verified_fact",
                        statement=sentence[2],
                        certainty="confirmed",
                        source_ids=[UUID(str(sentence[1]["source_id"]))],
                    ),
                )


def validate_insight(draft, runtime, articles):
    primary = {a["article_id"]: a["content_version"] for a in articles}
    disclosures = []
    for card in draft.cards:
        if not set(card.article_ids) <= set(primary):
            raise ValueError("Card must be tied to supplied Top-K article IDs")
        has_fact, has_news, has_bank, has_causal_link = False, False, False, False
        for claim in card.claims:
            sources = retrieved_sources(claim.source_ids, runtime)
            for source in sources:
                if source["source_type"] == "news" and source["article_id"] in primary:
                    if source["source_ref"].get("content_version") != primary[source["article_id"]]:
                        raise ValueError("Primary news citation does not match the pinned content_version")
            if not {str(f) for f in claim.fact_ids} <= runtime.fact_ids:
                raise ValueError("Claim cites a fact outside the pinned profile")
            has_fact |= bool(claim.fact_ids)
            if claim.kind in ("verified_fact", "bank_position") and not any(
                " ".join(claim.statement.split()) in " ".join((s["excerpt"] or "").split()) for s in sources
            ):
                raise ValueError("Factual and bank-position statements must be exact supporting excerpts")
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
                has_causal_link |= bool(claim.fact_ids) and any(
                    s["source_type"] == "news" and s["article_id"] in card.article_ids for s in sources
                )
        if not has_causal_link:
            raise ValueError("A business-impact claim must link a company fact and primary event evidence")
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


def source_publisher(source):
    """Conservative publisher identity: chunks and reposts on one site aren't independent."""
    url = urlsplit(source.get("source_url") or "")
    host = (url.hostname or "").lower().removeprefix("www.")
    if host in ("t.me", "telegram.me"):
        return "telegram:" + url.path.strip("/").split("/")[0].lower()
    if source.get("source_type") == "bank_analytics":
        return "alfa-bank"
    return host or "unknown"


def retain_supported_cards(draft, runtime):
    """An impact conclusion must itself cite news and a second independent publisher.

    References elsewhere in the card do not substantiate that conclusion. Single-source
    cards are omitted, not padded with unrelated citations.
    """
    retained = []
    for card in draft.cards:
        supported = False
        grounded_claims = []
        for claim in card.claims:
            if claim.kind not in ("ai_inference", "risk", "opportunity", "recommendation"):
                grounded_claims.append(claim)
                continue
            if not claim.fact_ids:
                continue
            sources = retrieved_sources(claim.source_ids, runtime)
            public = [s for s in sources if s["source_type"] in ("news", "bank_analytics", "public_web")]
            publishers, contents = set(), set()
            for source in public:
                excerpt = (source.get("excerpt") or "").strip()
                # A different headline/domain does not turn an identical repost into confirmation.
                body = excerpt.split("\n", 1)[-1]
                signature = " ".join(body.lower().split())
                if not signature or signature in contents:
                    continue
                contents.add(signature)
                publishers.add(source_publisher(source))
            publishers.discard("unknown")
            primary_news = any(
                s["source_type"] == "news" and s.get("article_id") in card.article_ids for s in public
            )
            if primary_news and len(publishers) >= 2:
                grounded_claims.append(claim)
                if claim.kind in ("ai_inference", "risk", "opportunity"):
                    supported = True
        if supported and len(grounded_claims) >= 2:
            card.claims = grounded_claims
            retained.append(card)
        else:
            notice = f"Вывод «{card.title}» не опубликован: нет обоснования из двух независимых источников."
            if notice not in draft.limitations:
                draft.limitations = (draft.limitations + [notice])[:30]
    if len(retained) != len(draft.cards) and retained:
        draft.summary = "Подтверждённые темы обзора: " + "; ".join(card.title for card in retained) + "."
    draft.cards = retained
    if not retained:
        draft.summary = "Недостаточно независимых источников для подтверждённого персонального обзора."
    return draft


def diverse_primary(articles):
    """Prefer publisher variety within the ranked candidates, without inventing new Top-K articles."""
    ordered, deferred, seen = [], [], set()
    for article in articles:
        news = one(
            "SELECT canonical_url,duplicate_of_id FROM news WHERE article_id=%s", (article["article_id"],)
        )
        if news and news["duplicate_of_id"]:
            continue
        publisher = source_publisher({"source_url": news["canonical_url"]}) if news else "unknown"
        if publisher in seen:
            deferred.append(article)
        else:
            ordered.append(article)
            seen.add(publisher)
    return (ordered + deferred)[:MAX_PRIMARY_ARTICLES]


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
        primary = diverse_primary(payload["articles"])
        sources = article_evidence(primary)
        refs = {}

        def prepare(rt, results):
            # Search returns chunk previews, not complete evidence_sources. Read and register
            # the actual publications; several chunks from one document count only once.
            bank_hits = list((results[-1] or {}).get("sources", []))
            for source in sources[:2]:
                query = source["excerpt"].split("\n", 1)[0][:200]
                found = rt.tool("bank_analytics.search", {"query": query, "limit": 3}, BANK_PREVIEW_CHARS)
                bank_hits.extend(found.get("sources", []))
            publications, seen_docs = [], set()
            for hit in bank_hits:
                sid = hit.get("source_id")
                doc = str(hit.get("document_id") or sid)
                if not sid or doc in seen_docs:
                    continue
                read = rt.tool("bank_analytics.read", {"source_id": str(sid)}, BANK_PREVIEW_CHARS)
                evidence = read.get("sources", [])
                if evidence:
                    seen_docs.add(doc)
                    publications.extend(evidence)
                if len(publications) >= 4:
                    break
            results[-1] = {"sources": publications}
            pack_refs, text = evidence_pack(results, len(sources))
            refs.update(pack_refs)
            # Replace duplicated starter tool bodies with the compact evidence pack.
            # Full excerpts remain in evidence_sources and are checked by validate_insight.
            rt.messages = rt.messages[:2]
            rt.messages[1]["content"] = rt.messages[1]["content"].split("\nPINNED CONTEXT DATA:\n", 1)[0]
            return text

        def normalized(candidate):
            result = InsightDraft.model_validate(candidate.model_dump())
            resolve_refs(result, refs, runtime.fact_ids)
            return result

        def validate(candidate, rt):
            validate_insight(normalized(candidate), rt, primary)

        # Seed sources are passed through the read tool so citations have a traceable retrieval step.
        starters = [("company_context.get", {})]
        starters += [
            ("news.read", {"source_id": str(s["source_id"])}, ARTICLE_PREVIEW_CHARS) for s in sources
        ]
        starters += [
            (
                "bank_analytics.search",
                {"query": "макроэкономика ставка инфляция отраслевые риски", "limit": 5},
                BANK_PREVIEW_CHARS,
            )
        ]
        draft = runtime.run(
            task="Prepare a personalized weekly report for the user's company from the evidence pack below. "
            "Explain event → concrete company exposure → mechanism → conditional impact → role-specific action. "
            "Separate bank opinion from AI inference. No bank source: explicitly disclose that limitation. "
            "Company facts must come from the pinned profile. Never replace a primary article with another "
            "content_version. Previous report is NOT evidence; recheck any changed conclusion: "
            + json.dumps(jsonable_encoder(previous), ensure_ascii=False)[:4000],
            schema=EvidenceInsightDraft,
            # Search results are already supplied. Only targeted reading can add useful evidence;
            # repeating broad searches burns the local model's budget without adding context.
            tools=["news.read", "bank_analytics.read", "company_documents.read"],
            validate=validate,
            starters=starters,
            prepare=prepare,
        )
        draft = normalized(draft)
        validate_insight(draft, runtime, primary)
        retain_supported_cards(draft, runtime)
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
    target = payload.get("insight_id")
    cards = all_rows(
        "SELECT i.* FROM insights i JOIN report_insights ri USING(insight_id) "
        "WHERE ri.report_version_id=%s AND (%s::uuid IS NULL OR i.insight_id=%s)",
        (row["report_version_id"], target, target),
    )
    if target and not cards:
        raise ValueError("Card is not in the pinned report")
    source_ids = all_rows(
        "SELECT DISTINCT cs.source_id FROM report_insights ri JOIN insight_claims c USING(insight_id) "
        "JOIN insight_claim_sources cs USING(claim_id) WHERE ri.report_version_id=%s "
        "AND (%s::uuid IS NULL OR c.insight_id=%s)",
        (row["report_version_id"], target, target),
    )
    primary = all_rows(
        "SELECT DISTINCT ia.article_id,ia.content_version FROM report_insights ri "
        "JOIN insight_articles ia USING(insight_id) WHERE ri.report_version_id=%s",
        (row["report_version_id"],),
    )
    runtime = Runtime(
        "insight_agent",
        row["company_id"],
        row["user_id"],
        profile_id=row["profile_id"],
        user_context_id=row["user_context_id"],
        as_of=row["window_end"],
        snapshot={
            "question": payload,
            "primary_articles": primary,
            "pinned_source_ids": [str(item["source_id"]) for item in source_ids],
        },
    )
    try:
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
        source_details = jsonable_encoder(retrieved_sources(draft.source_ids, runtime))
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
                    j(
                        {
                            "discussion_id": discussion,
                            "answer": draft.answer,
                            "sources": draft.source_ids,
                            "source_details": source_details,
                        }
                    ),
                ),
            )
        runtime.finish()
        return {
            "discussion_id": discussion,
            "answer": draft.answer,
            "sources": draft.source_ids,
            "source_details": source_details,
        }
    except Exception:
        runtime.finish(False)
        raise

from __future__ import annotations

import http.client
import ipaddress
import json
import multiprocessing as mp
import socket
import ssl
from datetime import timedelta
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from fastapi import HTTPException
from agent_common.core import encode as jsonable_encoder

from agent_common.bank_semantics import normalize_company_record
from agent_common.contracts import AgentScope, FetchArgs, ReadArgs, SearchArgs, WebArgs
from agent_common.core import all_rows, db, digest, j, membership, now, one, settings
from agent_common.embeddings import config, embed, vector_literal
from agent_common.evidence import assert_source_eligible

TOOLS = {
    "company_context.get",
    "company_bank_data.get",
    "company_documents.search",
    "company_documents.read",
    "news.search",
    "news.read",
    "bank_analytics.search",
    "bank_analytics.read",
    "web.search",
    "web.fetch",
}


def validate_scope(scope: AgentScope):
    membership(scope.company_id, scope.user_id)
    run = one(
        "SELECT * FROM agent_runs WHERE agent_run_id=%s AND company_id=%s AND user_id=%s AND status=%s",
        (scope.agent_run_id, scope.company_id, scope.user_id, "running"),
    )
    if not run or run["input_snapshot"].get("scope") != jsonable_encoder(scope):
        raise HTTPException(403, "Scope must match the immutable agent input")
    if run["started_at"] < now() - timedelta(seconds=settings.agent_timeout_seconds):
        raise HTTPException(403, "Agent run tool capability has expired")
    with db() as conn:
        reserved = conn.execute(
            "UPDATE agent_runs SET tool_calls=tool_calls+1 WHERE agent_run_id=%s AND status='running' "
            "AND tool_calls<%s RETURNING agent_run_id",
            (scope.agent_run_id, settings.agent_max_tool_calls),
        ).fetchone()
    if not reserved:
        raise HTTPException(429, "Agent tool-call budget exhausted or run finalized")
    return run


def context(scope):
    profile = (
        one(
            "SELECT * FROM company_profiles WHERE profile_id=%s AND company_id=%s AND status=%s",
            (scope.profile_id, scope.company_id, "ready"),
        )
        if scope.profile_id
        else None
    )
    preferences = (
        one(
            "SELECT * FROM user_preferences WHERE user_context_id=%s AND company_id=%s "
            "AND user_id=%s AND status=%s",
            (scope.user_context_id, scope.company_id, scope.user_id, "ready"),
        )
        if scope.user_context_id
        else None
    )
    facts = (
        all_rows(
            "SELECT f.* FROM company_facts f JOIN company_profile_facts pf USING(fact_id) "
            "WHERE pf.profile_id=%s ORDER BY f.field",
            (scope.profile_id,),
        )
        if profile
        else []
    )
    goals = (
        all_rows("SELECT * FROM user_preference_goals WHERE user_context_id=%s", (scope.user_context_id,))
        if preferences
        else []
    )
    sources = (
        all_rows(
            "SELECT DISTINCT s.* FROM evidence_sources s JOIN fact_sources fs USING(source_id) "
            "JOIN company_profile_facts pf USING(fact_id) WHERE pf.profile_id=%s",
            (scope.profile_id,),
        )
        if profile
        else []
    )
    return {
        "profile": profile,
        "preferences": preferences,
        "facts": facts,
        "user_goals": goals,
        "sources": sources,
    }


def bank_data(scope):
    rows = all_rows(
        "SELECT b.*,s.source_as_of,s.currency,s.revenue_year,s.source_filename FROM bank_company_data b "
        "JOIN bank_import_batches s USING(source_snapshot_id) "
        "WHERE b.company_id=%s AND s.status='ready' ORDER BY s.imported_at DESC,b.bank_record_id LIMIT 30",
        (scope.company_id,),
    )
    sources = []
    with db() as conn:
        for raw_row in rows:
            row = normalize_company_record(raw_row)
            ref = {"bank_record_id": row["bank_record_id"], "snapshot_id": str(row["source_snapshot_id"])}
            if row.get("interpretation_version"):
                ref["interpretation_version"] = row["interpretation_version"]
            existing = conn.execute(
                "SELECT * FROM evidence_sources WHERE company_id=%s AND source_type='bank_import' "
                "AND source_ref=%s LIMIT 1",
                (scope.company_id, j(ref)),
            ).fetchone()
            excerpt = json.dumps(jsonable_encoder(row), ensure_ascii=False)
            if not existing:
                existing = conn.execute(
                    "INSERT INTO evidence_sources(company_id,source_type,source_ref,excerpt) "
                    "VALUES(%s,'bank_import',%s,%s) RETURNING *",
                    (scope.company_id, j(ref), excerpt),
                ).fetchone()
            sources.append(existing)
    payment_rows = (
        "normalized_payments AS (SELECT p.source_snapshot_id,p.payer,p.beneficiary,p.pay_amt,p.pay_cnt,"
        "p.aggregation_from,p.aggregation_to,CASE WHEN b.source_filename='df_Uid_payment_hack.csv' "
        "THEN 'RUB' ELSE p.currency END AS currency FROM bank_payment_edges p "
        "JOIN bank_import_batches b USING(source_snapshot_id) WHERE b.status='ready') "
    )
    payments = all_rows(
        "WITH " + payment_rows +
        "SELECT source_snapshot_id,currency,aggregation_from,aggregation_to,"
        "CASE WHEN payer=%s THEN 'outgoing' ELSE 'incoming' END AS direction,"
        "count(*) AS edges,sum(pay_amt) AS amount,sum(pay_cnt) AS payments "
        "FROM normalized_payments WHERE (payer=%s OR beneficiary=%s) AND payer<>beneficiary "
        "AND source_snapshot_id IN(SELECT source_snapshot_id FROM bank_import_batches WHERE status='ready') "
        "GROUP BY source_snapshot_id,currency,aggregation_from,aggregation_to,direction",
        (scope.company_id, scope.company_id, scope.company_id),
    )
    counterparties = all_rows(
        "WITH " + payment_rows + ", grouped AS (SELECT source_snapshot_id,currency,aggregation_from,aggregation_to,"
        "CASE WHEN payer=%s THEN 'outgoing' ELSE 'incoming' END AS direction,"
        "CASE WHEN payer=%s THEN beneficiary ELSE payer END AS counterparty,sum(pay_amt) AS amount "
        "FROM normalized_payments WHERE (payer=%s OR beneficiary=%s) AND payer<>beneficiary "
        "AND source_snapshot_id IN(SELECT source_snapshot_id FROM bank_import_batches WHERE status='ready') "
        "GROUP BY 1,2,3,4,5,6), scored AS (SELECT *,amount/nullif(sum(amount) OVER "
        "(PARTITION BY source_snapshot_id,currency,aggregation_from,aggregation_to,direction),0) AS share,"
        "row_number() OVER(PARTITION BY source_snapshot_id,currency,aggregation_from,aggregation_to,direction "
        "ORDER BY amount DESC NULLS LAST,counterparty) AS position FROM grouped) "
        "SELECT * FROM scored WHERE position<=5 ORDER BY source_snapshot_id,direction,position",
        (scope.company_id, scope.company_id, scope.company_id, scope.company_id),
    )
    person_network = one(
        "SELECT count(DISTINCT own.synthetic_f) AS linked_people,"
        "count(DISTINCT peer.synthetic_u) AS linked_companies "
        "FROM bank_person_links own JOIN bank_import_batches b ON b.source_snapshot_id=own.source_snapshot_id "
        "LEFT JOIN bank_person_links peer ON peer.synthetic_f=own.synthetic_f AND peer.synthetic_u<>own.synthetic_u "
        "AND peer.source_snapshot_id IN(SELECT source_snapshot_id FROM bank_import_batches WHERE status='ready') "
        "WHERE own.synthetic_u=%s AND b.status='ready'",
        (scope.company_id,),
    )
    corporate_groups = all_rows(
        "SELECT source_snapshot_id,group_id,count(DISTINCT company_id) AS companies "
        "FROM bank_company_data WHERE source_snapshot_id IN "
        "(SELECT source_snapshot_id FROM bank_import_batches WHERE status='ready') "
        "AND group_id IN(SELECT group_id FROM bank_company_data WHERE company_id=%s AND source_snapshot_id IN "
        "(SELECT source_snapshot_id FROM bank_import_batches WHERE status='ready')) "
        "GROUP BY source_snapshot_id,group_id ORDER BY source_snapshot_id,group_id",
        (scope.company_id,),
    )
    if payments or person_network["linked_people"] or corporate_groups:
        analytics = {
            "method": "company-exposure-v1",
            "person_network": person_network,
            "corporate_groups": corporate_groups,
            "aggregates": payments,
            "top_counterparties": counterparties,
            "limitations": "Shares cover only observed amounts; self transfers excluded. Unknown currency/period stays unknown. "
            "Shared synthetic person links do not establish ownership, control, job role or real identity. "
            "Payment edges do not establish supplier/customer roles; unknown counterparty attributes stay unknown.",
        }
        ref = {"method": "company-exposure-v1", "input_hash": digest(analytics)}
        with db() as conn:
            source = conn.execute(
                "SELECT * FROM evidence_sources WHERE company_id=%s AND source_type='derived' "
                "AND source_ref=%s LIMIT 1",
                (scope.company_id, j(ref)),
            ).fetchone()
            if not source:
                source = conn.execute(
                    "INSERT INTO evidence_sources(company_id,source_type,source_ref,excerpt) "
                    "VALUES(%s,'derived',%s,%s) RETURNING *",
                    (scope.company_id, j(ref), json.dumps(jsonable_encoder(analytics), ensure_ascii=False)),
                ).fetchone()
            sources.append(source)
    sources += all_rows(
        "SELECT * FROM evidence_sources WHERE company_id=%s AND source_type='user_answer' "
        "AND source_ref->>'kind'='company_clarification' ORDER BY observed_at DESC LIMIT 30",
        (scope.company_id,),
    )
    return {
        "sources": sources,
        "payment_aggregates": payments,
        "top_counterparties": counterparties,
        "person_network": person_network,
        "corporate_groups": corporate_groups,
        "limitations": [
            "Shared synthetic person links do not establish ownership, control, job role or real identity.",
            "At most 30 bank company records are shown; repeated IDs may contain conflicting attributes.",
            "The supplied company CSV reports 2025 revenue in RUB; 1000 means unavailable, not actual revenue.",
            "The supplied payment CSV amounts are RUB, accumulated since January 1; the year/end date are not supplied.",
            "Other payment sources need their own currency and period metadata.",
            "Customs countries do not establish import/export direction, products or routes.",
            "Payment links do not prove supplier/customer roles. Missing counterparties have unknown attributes.",
            "Synthetic identities cannot be mapped to real organizations.",
            "Loan product presence does not establish debt balance or floating interest rate.",
        ],
    }


def search(scope: AgentScope, name, args: SearchArgs):
    kind = {
        "news.search": "news",
        "bank_analytics.search": "bank_analytics",
        "company_documents.search": "company_document",
    }[name]
    # Only latest news revision known at the analysis cutoff; historical context is allowed.
    where = "c.kind=%s AND (c.company_id IS NULL OR c.company_id=%s)"
    params = [kind, scope.company_id]
    if kind == "company_document" and scope.profile_id:
        profile = one(
            "SELECT payload FROM company_profiles WHERE profile_id=%s AND company_id=%s",
            (scope.profile_id, scope.company_id),
        )
        ids = (profile or {}).get("payload", {}).get("document_ids", [])
        where += " AND c.document_id=ANY(%s::uuid[])"
        params.append(ids)
    if kind != "company_document":
        where += " AND (c.published_at IS NULL OR c.published_at<%s)"
        params.append(scope.as_of)
    if args.published_after:
        where += " AND c.published_at>=%s"
        params.append(args.published_after)
    if kind == "bank_analytics":
        run = one("SELECT started_at FROM agent_runs WHERE agent_run_id=%s", (scope.agent_run_id,))
        known_at = run["started_at"] if run else scope.as_of
        where += (
            " AND c.observed_at<=%s AND EXISTS(SELECT 1 FROM documents d WHERE d.document_id=c.document_id "
            "AND d.processing_status='ready' AND d.fetched_at<=%s) "
            "AND NOT EXISTS(SELECT 1 FROM documents old JOIN documents newer "
            "ON newer.external_id=old.external_id WHERE old.document_id=c.document_id "
            "AND newer.processing_status='ready' AND newer.fetched_at>old.fetched_at AND newer.fetched_at<=%s "
            "AND EXISTS(SELECT 1 FROM rag_chunks nc WHERE nc.document_id=newer.document_id AND nc.observed_at<=%s) "
            "AND (newer.published_at IS NULL OR newer.published_at<%s))"
        )
        params += [known_at, known_at, known_at, known_at, scope.as_of]
    if kind == "news":
        where += (
            " AND EXISTS(SELECT 1 FROM news_revisions r WHERE r.article_id=c.article_id "
            "AND r.content_version=c.content_version AND r.recorded_at<=%s) "
            "AND NOT EXISTS(SELECT 1 FROM news_revisions newer WHERE newer.article_id=c.article_id "
            "AND newer.recorded_at<=%s AND newer.recorded_at>"
            "(SELECT recorded_at FROM news_revisions r WHERE r.article_id=c.article_id "
            "AND r.content_version=c.content_version))"
        )
        # as_of controls event publication, not ingestion; late-fetched items are available now.
        # The input cutoff for revision visibility is frozen at agent start, not weekly event cutoff.
        run = one("SELECT started_at FROM agent_runs WHERE agent_run_id=%s", (scope.agent_run_id,))
        params += [run["started_at"], run["started_at"]]
    lexical = all_rows(
        "SELECT c.*,ts_rank_cd(c.search_vector,websearch_to_tsquery('russian',%s)) AS score "
        "FROM rag_chunks c WHERE "
        + where
        + " AND c.search_vector @@ websearch_to_tsquery('russian',%s) ORDER BY score DESC,c.chunk_id LIMIT 60",
        [args.query] + params + [args.query],
    )
    semantic = []
    degraded = None
    try:
        cfg = config(chunk=True)
        vector = embed([args.query], "query", cfg)[0]
        semantic = all_rows(
            "SELECT c.* FROM rag_chunks c WHERE "
            + where
            + " AND c.embedding_config_id=%s ORDER BY c.embedding <=> %s::vector LIMIT 60",
            params + [cfg["embedding_config_id"], vector_literal(vector)],
        )
    except (httpx.HTTPError, ValueError, RuntimeError) as exc:
        degraded = f"Vector search unavailable ({type(exc).__name__}); lexical search used"
    scores, candidates = {}, {}
    for rows in (lexical, semantic):
        for rank, row in enumerate(rows, 1):
            key = str(row["chunk_id"])
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
            candidates[key] = row
    result, per_doc = [], {}
    for key in sorted(scores, key=lambda k: (-scores[k], k)):
        row = candidates[key]
        doc = str(row["document_id"] or row["article_id"])
        if per_doc.get(doc, 0) >= 3:
            continue
        source = one("SELECT * FROM evidence_sources WHERE source_id=%s", (row["source_id"],))
        try:
            assert_source_eligible(source, scope)
        except ValueError:
            continue
        per_doc[doc] = per_doc.get(doc, 0) + 1
        result.append(
            {k: v for k, v in row.items() if k not in ("embedding", "search_vector", "score")}
            | {"rrf_score": scores[key]}
        )
        if len(result) == args.limit:
            break
    return {
        "sources": result,
        "limitation": degraded,
        "retrieval": {
            "lexical_candidates": len(lexical),
            "semantic_candidates": len(semantic),
            "returned": len(result),
            "empty": not result,
            "publication_cutoff": scope.as_of,
        },
    }


def read(scope, name, args):
    source = one(
        "SELECT * FROM evidence_sources WHERE source_id=%s AND (company_id IS NULL OR company_id=%s) "
        "AND (source_type NOT IN ('news','bank_analytics') OR source_date IS NULL OR source_date<%s)",
        (args.source_id, scope.company_id, scope.as_of),
    )
    if not source:
        raise HTTPException(404, "Source not available")
    try:
        assert_source_eligible(source, scope)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    expected = {
        "news.read": "news",
        "bank_analytics.read": "bank_analytics",
        "company_documents.read": "document",
    }[name]
    if source["source_type"] != expected:
        raise HTTPException(404, "Source type mismatch")
    if name == "company_documents.read" and scope.profile_id:
        profile = one(
            "SELECT payload FROM company_profiles WHERE profile_id=%s AND company_id=%s",
            (scope.profile_id, scope.company_id),
        )
        if str(source["document_id"]) not in (profile or {}).get("payload", {}).get("document_ids", []):
            raise HTTPException(404, "Document is not in the pinned company profile")
    chunk = one("SELECT * FROM rag_chunks WHERE source_id=%s", (args.source_id,))
    if not chunk:
        return {"sources": [source]}
    neighbors = all_rows(
        "SELECT source_id,title,content,metadata,ordinal,kind,published_at,observed_at,"
        "article_id,content_version,document_id FROM rag_chunks "
        "WHERE kind=%s AND (company_id IS NULL OR company_id=%s) AND ordinal BETWEEN %s AND %s "
        "AND ((document_id=%s) OR (article_id=%s AND content_version=%s)) ORDER BY ordinal",
        (
            chunk["kind"],
            scope.company_id,
            chunk["ordinal"] - 1,
            chunk["ordinal"] + 1,
            chunk["document_id"],
            chunk["article_id"],
            chunk["content_version"],
        ),
    )
    return {"sources": neighbors}


def public_addresses(host):
    if not host or host.lower() in ("localhost", "metadata.google.internal"):
        raise ValueError("Public hostname required")
    ips = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError("Private or non-public addresses are forbidden")
    return ips


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, ip):
        super().__init__(host, timeout=15, context=ssl.create_default_context())
        self.ip = ip

    def connect(self):
        sock = socket.create_connection((self.ip, 443), timeout=self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _fetch_public(url):
    """DNS is validated and pinned; each redirect is revalidated. HTTPS/443 only."""
    for _ in range(4):
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.password:
            raise ValueError("Only public HTTPS URLs on port 443 are allowed")
        ips = public_addresses(parts.hostname)
        conn = PinnedHTTPS(parts.hostname, ips[0])
        try:
            conn.request(
                "GET",
                (parts.path or "/") + ("?" + parts.query if parts.query else ""),
                headers={"User-Agent": "AlphaResearchPrototype/1.0", "Accept-Encoding": "identity"},
            )
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                url = urljoin(url, response.getheader("Location", ""))
                continue
            if response.status != 200:
                raise ValueError(f"Upstream HTTP {response.status}")
            content_type = response.getheader("Content-Type", "")
            if not any(t in content_type for t in ("text/html", "text/plain")):
                raise ValueError("Only HTML/text pages supported")
            if response.getheader("Content-Encoding", "identity") != "identity":
                raise ValueError("Compressed upstream responses are not accepted")
            data = response.read(1_000_001)
            if len(data) > 1_000_000:
                raise ValueError("Web page exceeds limit")
        finally:
            conn.close()
        soup = BeautifulSoup(data, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        main = soup.find("main") or soup.find("article") or soup
        return url, main.get_text(" ", strip=True)[:16000]
    raise ValueError("Too many redirects")


def _fetch_child(url, pipe):
    try:
        pipe.send((True, _fetch_public(url)))
    except Exception as exc:
        pipe.send((False, str(exc)))
    finally:
        pipe.close()


def fetch_public(url, timeout=20):
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.port not in (None, 443) or parts.username or parts.password:
        raise ValueError("Only public HTTPS URLs on port 443 are allowed")
    if timeout <= 0:
        raise TimeoutError("Public fetch deadline expired")
    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_fetch_child, args=(url, child), daemon=True)
    try:
        process.start()
        child.close()
        if not parent.poll(timeout):
            raise TimeoutError("Public fetch wall-time limit exceeded")
        ok, result = parent.recv()
        if not ok:
            raise ValueError(result)
        return result
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join()
        parent.close()
        child.close()


def web_fetch(scope, args):
    discovered = one(
        "SELECT 1 FROM evidence_sources WHERE source_type='public_web' AND source_url=%s "
        "AND source_ref->>'discovery_run_id'=%s AND source_ref->>'discovery_only'='true'",
        (args.url, str(scope.agent_run_id)),
    )
    if not discovered:
        raise ValueError("Fetch URL must be an unchanged result of web.search in this run")
    run = one("SELECT started_at FROM agent_runs WHERE agent_run_id=%s", (scope.agent_run_id,))
    remaining = settings.agent_timeout_seconds - (now() - run["started_at"]).total_seconds()
    url, text = fetch_public(args.url, timeout=min(20, remaining))
    # Public web cannot establish company identity in the fictional-company demo.
    with db() as conn:
        source = conn.execute(
            "INSERT INTO evidence_sources(source_type,source_ref,source_url,excerpt) "
            "VALUES('public_web',%s,%s,%s) RETURNING *",
            (j({"content_sha256": digest(text.encode()), "publication_date": "unknown"}), url, text),
        ).fetchone()
    return {
        "sources": [source],
        "limitation": "Publication date unknown; verify temporal applicability. Not bank opinion.",
    }


def web_search(scope, args):
    response = httpx.get(
        settings.searxng_base_url + "/search",
        params={"q": args.query, "format": "json", "language": "ru"},
        timeout=20,
    )
    response.raise_for_status()
    results = response.json().get("results", [])[: args.limit]
    with db() as conn:
        for result in results:
            url = result.get("url") or ""
            parts = urlsplit(url)
            if parts.scheme == "https" and not parts.username and not parts.password:
                conn.execute(
                    "INSERT INTO evidence_sources(source_type,source_ref,source_url) VALUES('public_web',%s,%s)",
                    (j({"discovery_run_id": str(scope.agent_run_id), "discovery_only": True}), url),
                )
    return {
        "results": [
            {"title": r.get("title"), "url": r.get("url"), "snippet": r.get("content", "")[:1000]}
            for r in results
        ],
        "limitation": "Search snippets are discovery only, not citable evidence.",
    }


def dispatch(name, scope, arguments):
    run = validate_scope(scope)
    try:
        if name not in TOOLS:
            raise HTTPException(404, "Unknown tool")
        if run["agent_name"] == "company_profiler" and name.startswith(("news.", "bank_analytics.")):
            raise HTTPException(403, "Profiler does not have this capability")
        if name == "company_context.get":
            if arguments:
                raise ValueError("No arguments expected")
            result = context(scope)
        elif name == "company_bank_data.get":
            if arguments:
                raise ValueError("No arguments expected")
            result = bank_data(scope)
        elif name.endswith(".search") and name != "web.search":
            result = search(scope, name, SearchArgs.model_validate(arguments))
        elif name.endswith(".read"):
            result = read(scope, name, ReadArgs.model_validate(arguments))
        elif name == "web.search":
            result = web_search(scope, WebArgs.model_validate(arguments))
        else:
            result = web_fetch(scope, FetchArgs.model_validate(arguments))
    except Exception:
        with db() as conn:
            conn.execute(
                "INSERT INTO tool_audit(agent_run_id,company_id,user_id,tool_name,arguments,status) "
                "VALUES(%s,%s,%s,%s,%s,'error')",
                (scope.agent_run_id, scope.company_id, scope.user_id, name, j(arguments)),
            )
        raise
    with db() as conn:
        conn.execute(
            "INSERT INTO tool_audit(agent_run_id,company_id,user_id,tool_name,arguments,status,result_count) "
            "VALUES(%s,%s,%s,%s,%s,'ok',%s)",
            (
                scope.agent_run_id,
                scope.company_id,
                scope.user_id,
                name,
                j(arguments),
                len(result.get("sources", [])),
            ),
        )
    return jsonable_encoder(result)

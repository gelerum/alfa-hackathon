from agent_common.core import all_rows, one


def assert_source_eligible(source, scope):
    run_id = getattr(scope, "agent_run_id", None)
    run = (
        one("SELECT started_at,input_snapshot FROM agent_runs WHERE agent_run_id=%s", (run_id,))
        if run_id
        else None
    )
    if not run:
        return
    snapshot = run["input_snapshot"]
    pinned_ids = snapshot.get("pinned_source_ids", [])
    if str(source["source_id"]) in pinned_ids:
        return
    if source["source_type"] == "news":
        articles = snapshot.get("ranking_input", {}).get("articles", snapshot.get("primary_articles", []))
        pinned = {a["article_id"]: a["content_version"] for a in articles}
        version = source["source_ref"].get("content_version")
        if source["article_id"] in pinned:
            if version != pinned[source["article_id"]]:
                raise ValueError("Primary source content_version differs from pinned input")
            return
        revision = one(
            "SELECT recorded_at FROM news_revisions WHERE article_id=%s AND content_version=%s",
            (source["article_id"], version),
        )
        if not revision or revision["recorded_at"] > run["started_at"]:
            raise ValueError("News revision was not available at agent start")
    if source["source_type"] == "bank_analytics":
        document = one("SELECT * FROM documents WHERE document_id=%s", (source["document_id"],))
        if (
            not document
            or document["processing_status"] != "ready"
            or document["fetched_at"] > run["started_at"]
            or source["observed_at"] > run["started_at"]
        ):
            raise ValueError("Bank source was not available at agent start")
        if document["external_id"] and one(
            "SELECT 1 FROM documents d WHERE external_id=%s AND processing_status='ready' "
            "AND fetched_at>%s AND fetched_at<=%s AND (published_at IS NULL OR published_at<%s) "
            "AND EXISTS(SELECT 1 FROM rag_chunks c WHERE c.document_id=d.document_id AND c.observed_at<=%s)",
            (
                document["external_id"],
                document["fetched_at"],
                run["started_at"],
                scope.as_of,
                run["started_at"],
            ),
        ):
            raise ValueError("Bank source has a newer revision in this snapshot")


def retrieved_sources(ids, runtime):
    ids = {str(v) for v in ids}
    if not ids <= runtime.sources:
        raise ValueError("Citation was not retrieved in this run")
    sources = (
        all_rows("SELECT * FROM evidence_sources WHERE source_id=ANY(%s::uuid[])", (list(ids),))
        if ids
        else []
    )
    if len(sources) != len(ids):
        raise ValueError("Citation does not exist")
    for source in sources:
        assert_source_eligible(source, runtime.scope)
        if source["company_id"] not in (None, runtime.scope.company_id):
            raise ValueError("Cross-company citation")
        if (
            source["source_type"] in ("news", "bank_analytics")
            and source["source_date"]
            and source["source_date"] >= runtime.scope.as_of
        ):
            raise ValueError("Event or bank analysis citation is later than the report event cutoff")
    return sources

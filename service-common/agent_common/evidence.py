from agent_common.core import all_rows


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
        if source["company_id"] not in (None, runtime.scope.company_id):
            raise ValueError("Cross-company citation")
        if (
            source["source_type"] in ("news", "bank_analytics")
            and source["source_date"]
            and source["source_date"] >= runtime.scope.as_of
        ):
            raise ValueError("Event or bank analysis citation is later than the report event cutoff")
    return sources

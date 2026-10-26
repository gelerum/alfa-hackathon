from datetime import timedelta

import httpx
from fastapi import HTTPException

from agent_common.core import ServiceBusy, all_rows, db, digest, execute, j, monday_window, now, one, settings


def create_report(company_id, user_id, trigger_type, key, end=None):
    end = end or now()
    start = end - timedelta(days=7)
    with db() as conn:
        conn.execute(
            "INSERT INTO reports(company_id,user_id,trigger_type,window_start,window_end,deadline,idempotency_key) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(idempotency_key) DO NOTHING",
            (company_id, user_id, trigger_type, start, end, end, key),
        )
        row = conn.execute("SELECT * FROM reports WHERE idempotency_key=%s", (key,)).fetchone()
        if (
            str(row["user_id"]) != str(user_id)
            or row["company_id"] != company_id
            or row["trigger_type"] != trigger_type
        ):
            raise HTTPException(409, "Idempotency key belongs to another report")
        return row


def request_ranking(report):
    if report["window_end"] > now():
        return None
    active = one(
        "SELECT 1 FROM company_memberships WHERE company_id=%s AND user_id=%s AND is_active",
        (report["company_id"], report["user_id"]),
    )
    if not active:
        return None
    profile = one("SELECT * FROM current_company_profiles WHERE company_id=%s", (report["company_id"],))
    pref = one(
        "SELECT * FROM current_user_preferences WHERE company_id=%s AND user_id=%s",
        (report["company_id"], report["user_id"]),
    )
    if not profile or not pref:
        return None
    articles = all_rows(
        "SELECT n.article_id,n.content_version FROM news n JOIN news_revisions r "
        "ON r.article_id=n.article_id AND r.content_version=n.content_version "
        "WHERE r.published_at>=%s AND r.published_at<%s "
        "AND n.duplicate_of_id IS NULL ORDER BY n.article_id",
        (report["window_start"], report["window_end"]),
    )
    vector_index = all_rows(
        "SELECT e.article_id,e.content_version,e.embedding_config_id,e.created_at "
        "FROM news_embeddings e JOIN news_revisions r USING(article_id,content_version) "
        "JOIN news n ON n.article_id=e.article_id AND n.content_version=e.content_version "
        "WHERE n.duplicate_of_id IS NULL AND r.published_at>=%s AND r.published_at<%s "
        "AND e.embedding_config_id IN (SELECT embedding_config_id FROM company_context_facets "
        "WHERE profile_id=%s AND scope='company' AND status='ready') "
        "ORDER BY e.article_id,e.content_version,e.embedding_config_id",
        (report["window_start"], report["window_end"], profile["profile_id"]),
    )
    bank_docs = all_rows(
        "SELECT document_id FROM documents WHERE source_type='bank_analytics' "
        "AND processing_status='ready' AND (published_at IS NULL OR published_at<%s) ORDER BY document_id",
        (report["window_end"],),
    )
    fingerprint = digest(
        {
            "profile": profile["profile_id"],
            "user": pref["user_context_id"],
            "articles": articles,
            "vector_index": vector_index,
            "bank_documents": bank_docs,
        }
    )
    with db() as conn:
        conn.execute(
            "INSERT INTO ranking_requests(report_id,reason,input_fingerprint,profile_id,user_context_id,context_snapshot) "
            "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                report["report_id"],
                "initial_or_late_data",
                fingerprint,
                profile["profile_id"],
                pref["user_context_id"],
                j({"bank_documents": bank_docs}),
            ),
        )
        request = conn.execute(
            "SELECT * FROM ranking_requests WHERE report_id=%s AND input_fingerprint=%s",
            (report["report_id"], fingerprint),
        ).fetchone()
    schedule_dispatch(request["request_id"])
    return request


def schedule_dispatch(request_id):
    """Keep per-batch attempts bounded, but recover a terminal batch after a cooldown.

    Each recovery gets a new durable job (the same external request_id). Locking the
    request serializes schedulers; a missing ranker configuration produces only one
    waiting job, and changing the destination creates an independent dispatch key.
    """
    base = "rank:" + str(request_id) + ":" + digest(settings.ranker_url)[:12]
    with db() as conn:
        request = conn.execute(
            "SELECT * FROM ranking_requests WHERE request_id=%s FOR UPDATE", (request_id,)
        ).fetchone()
        if not request or request["status"] == "completed":
            return
        if request["status"] == "sent":
            insight = conn.execute(
                "SELECT status,last_error FROM jobs WHERE idempotency_key=%s",
                ("insight:" + str(request_id),),
            ).fetchone()
            if insight:
                if insight["status"] == "failed":
                    conn.execute(
                        "UPDATE ranking_requests SET last_error=%s WHERE request_id=%s",
                        (insight["last_error"], request_id),
                    )
                return
            conn.execute("UPDATE ranking_requests SET status='pending' WHERE request_id=%s", (request_id,))
        latest = conn.execute(
            "SELECT * FROM jobs WHERE kind='dispatch_ranking' AND "
            "(idempotency_key=%s OR idempotency_key LIKE %s) ORDER BY created_at DESC,job_id DESC LIMIT 1",
            (base, base + ":retry:%"),
        ).fetchone()
        key = base
        if latest:
            if latest["status"] == "failed":
                conn.execute(
                    "UPDATE ranking_requests SET status='failed',last_error=%s WHERE request_id=%s",
                    (latest["last_error"], request_id),
                )
                ended = latest["finished_at"] or latest["available_at"]
                if ended > now() - timedelta(minutes=15):
                    return
            elif not (request["status"] == "sent" and latest["status"] == "completed"):
                return
            key = base + ":retry:" + str(latest["job_id"])
        conn.execute(
            "INSERT INTO jobs(kind,idempotency_key,payload) VALUES('dispatch_ranking',%s,%s) "
            "ON CONFLICT(idempotency_key) DO NOTHING",
            (key, j({"request_id": request_id})),
        )
        conn.execute("UPDATE ranking_requests SET status='pending' WHERE request_id=%s", (request_id,))


def dispatch_ranking(payload):
    request = one(
        "SELECT q.*,r.company_id,r.user_id,r.window_start,r.window_end FROM ranking_requests q "
        "JOIN reports r USING(report_id) WHERE request_id=%s",
        (payload["request_id"],),
    )
    from agent_common.core import membership

    membership(request["company_id"], request["user_id"])
    if request["status"] in ("sent", "completed"):
        return {"request_id": request["request_id"]}
    if not settings.ranker_url:
        # Deliberately no fake ranking: a developer can submit an explicit callback.
        return {
            "request_id": request["request_id"],
            "status": "awaiting_ranker_configuration_or_manual_callback",
        }
    from agent_common.core import encode as jsonable_encoder

    run_id = request["context_snapshot"].get("_ranking_run_id")
    if not run_id:
        recovered = one(
            "SELECT ranking_run_id FROM ranking_runs WHERE company_id=%s AND status='completed' "
            "AND context_snapshot->>'request_id'=%s ORDER BY completed_at DESC LIMIT 1",
            (request["company_id"], str(request["request_id"])),
        )
        if recovered:
            run_id = str(recovered["ranking_run_id"])
        else:
            response = httpx.post(
                settings.ranker_url,
                json=jsonable_encoder(
                    {
                        "request_id": request["request_id"],
                        "company_id": request["company_id"],
                        "user_id": request["user_id"],
                        "profile_id": request["profile_id"],
                        "user_context_id": request["user_context_id"],
                        "published_after": request["window_start"],
                        "as_of": request["window_end"],
                        "knowledge_as_of": request["created_at"],
                        "top_k": 10,
                    }
                ),
                headers={"Authorization": f"Bearer {settings.internal_token}"},
                timeout=settings.ranker_timeout_seconds,
            )
            if response.status_code == 429:
                raise ServiceBusy("Ranker is busy with another report")
            response.raise_for_status()
            run_id = str(response.json()["ranking_run_id"])
        execute(
            "UPDATE ranking_requests SET context_snapshot=context_snapshot || %s WHERE request_id=%s",
            (j({"_ranking_run_id": run_id}), request["request_id"]),
        )
    response = httpx.post(
        settings.insight_callback_url,
        json={"request_id": str(request["request_id"]), "ranking_run_id": run_id},
        headers={"Authorization": f"Bearer {settings.internal_token}"},
        timeout=30,
    )
    response.raise_for_status()
    execute(
        "UPDATE ranking_requests SET status='sent',last_error=NULL WHERE request_id=%s AND status<>'completed'",
        (request["request_id"],),
    )
    return {"request_id": request["request_id"], "status": "sent"}


def publish_deadline_fallback(report):
    """An honest availability bulletin, not fabricated analytical content."""
    with db() as conn:
        conn.execute("SELECT report_id FROM reports WHERE report_id=%s FOR UPDATE", (report["report_id"],))
        if conn.execute(
            "SELECT 1 FROM report_versions WHERE report_id=%s AND status='published'", (report["report_id"],)
        ).fetchone():
            return
        version = conn.execute(
            "SELECT coalesce(max(version),0)+1 AS n FROM report_versions WHERE report_id=%s",
            (report["report_id"],),
        ).fetchone()["n"]
        limitations = [
            "К сроку подготовки персонализированный анализ не завершён. "
            "Это уведомление о неполноте, а не готовый аналитический вывод. "
            "Проверенная версия будет опубликована после обработки данных."
        ]
        conn.execute(
            "INSERT INTO report_versions(report_id,version,status,summary,limitations,published_at,input_snapshot) "
            "VALUES(%s,%s,'published',%s,%s,now(),%s)",
            (
                report["report_id"],
                version,
                "Аналитика уточняется: доступность данных ограничена.",
                j(limitations),
                j({"deadline_fallback": True, "window_end": report["window_end"]}),
            ),
        )


def scheduler_tick():
    at = now()
    # Recover the latest missed deadline after restart and prepare the upcoming one early.
    _, previous_end = monday_window(at)
    _, upcoming_end = monday_window(at, upcoming=True)
    members = all_rows("SELECT company_id,user_id,joined_at FROM company_memberships WHERE is_active")
    for member in members:
        ends = []
        if member["joined_at"] <= previous_end:
            ends.append(previous_end)
        if upcoming_end - at <= timedelta(hours=3):
            ends.append(upcoming_end)
        for end in ends:
            key = f"weekly:{member['company_id']}:{member['user_id']}:{end.isoformat()}"
            create_report(member["company_id"], member["user_id"], "scheduled", key, end)
    # Late arrivals refine a fixed window; no sliding dates on existing reports.
    reports = all_rows("SELECT * FROM reports WHERE window_end>=now()-interval '14 days' ORDER BY deadline")
    for report in reports:
        if report["trigger_type"] == "scheduled" and report["deadline"] <= at:
            publish_deadline_fallback(report)
        request_ranking(report)

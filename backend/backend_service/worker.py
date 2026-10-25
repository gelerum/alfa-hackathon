import logging
import multiprocessing as mp
import os
import signal
import time
from datetime import timedelta
from uuid import uuid4

from agent_common.core import db, execute, j, now, settings

log = logging.getLogger("backend_service.worker")
KINDS = {
    "backend": ["dispatch_ranking", "bank_poll", "news_index", "chunk_embed"],
    "documents": ["extract_document"],
    "profiler": ["profile"],
    "insight": ["insight", "question"],
}


def claim(kinds):
    token = uuid4()
    with db() as conn:
        # Exhausted leases are terminal, not permanently stuck in running.
        conn.execute(
            "UPDATE jobs SET status='failed',last_error='Worker lease expired after final attempt',finished_at=now() "
            "WHERE status='running' AND lease_until<now() AND attempts>=max_attempts AND kind=ANY(%s)",
            (kinds,),
        )
        return conn.execute(
            "WITH candidate AS (SELECT job_id FROM jobs WHERE kind=ANY(%s) AND attempts<max_attempts "
            "AND ((status='pending' AND available_at<=now()) OR (status='running' AND lease_until<now())) "
            "ORDER BY available_at FOR UPDATE SKIP LOCKED LIMIT 1) "
            "UPDATE jobs SET status='running',attempts=attempts+1,lease_token=%s,"
            "lease_until=now()+interval '2 minutes' FROM candidate c WHERE jobs.job_id=c.job_id RETURNING jobs.*",
            (kinds, token),
        ).fetchone()


def handle(kind, payload):
    if kind == "extract_document":
        from backend_service.documents import process_document
        from backend_service.ingestion import refresh_profiles_for_document

        result = process_document(payload)
        refresh_profiles_for_document(payload)
        return result
    if kind == "profile":
        from profiler_service.service import profile_job

        return profile_job(payload)
    if kind == "insight":
        from insight_service.service import insight_job

        return insight_job(payload)
    if kind == "question":
        from insight_service.service import question_job

        return question_job(payload)
    if kind == "dispatch_ranking":
        from backend_service.reports import dispatch_ranking

        return dispatch_ranking(payload)
    if kind == "bank_poll":
        from backend_service.ingestion import poll_bank_source

        return poll_bank_source()
    if kind == "news_index":
        from backend_service.documents import index_news_batch

        return index_news_batch()
    if kind == "chunk_embed":
        from backend_service.documents import embed_chunks_batch

        return embed_chunks_batch()
    raise ValueError("Unknown job kind")


def child(kind, payload, pipe):
    # CPU/memory and wall-time limits constrain hostile documents. Deployment is non-root.
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (3 * 1024**3, 3 * 1024**3))
        resource.setrlimit(resource.RLIMIT_CPU, (900, 900))
        pipe.send({"ok": True, "result": handle(kind, payload)})
    except Exception as exc:
        log.exception("Job failed: %s", kind)
        pipe.send({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:2000]})
    finally:
        pipe.close()


def finish(job, outcome):
    with db() as conn:
        if outcome["ok"]:
            completed = conn.execute(
                "UPDATE jobs SET status='completed',result=%s,finished_at=now(),lease_until=NULL "
                "WHERE job_id=%s AND lease_token=%s AND status='running'",
                (j(outcome.get("result")), job["job_id"], job["lease_token"]),
            ).rowcount
            if (
                completed
                and job["kind"] == "chunk_embed"
                and conn.execute("SELECT 1 FROM rag_chunks WHERE embedding IS NULL LIMIT 1").fetchone()
            ):
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("maintenance:chunk_embed",)
                )
                active = conn.execute(
                    "SELECT 1 FROM jobs WHERE kind='chunk_embed' AND status IN ('pending','running')"
                ).fetchone()
                if not active:
                    conn.execute(
                        "INSERT INTO jobs(kind,idempotency_key,payload,max_attempts) VALUES('chunk_embed',%s,%s,2) "
                        "ON CONFLICT DO NOTHING",
                        ("embed-next:" + str(job["job_id"]), j({})),
                    )
        else:
            conn.execute(
                "UPDATE jobs SET status=CASE WHEN attempts>=max_attempts THEN 'failed' ELSE 'pending' END,"
                "last_error=%s,available_at=%s,lease_until=NULL,finished_at=CASE WHEN attempts>=max_attempts THEN now() ELSE NULL END "
                "WHERE job_id=%s AND lease_token=%s AND status='running'",
                (
                    outcome["error"],
                    now() + timedelta(seconds=min(30 * 2 ** job["attempts"], 900)),
                    job["job_id"],
                    job["lease_token"],
                ),
            )


def maintenance():
    from backend_service.reports import scheduler_tick

    scheduler_tick()
    execute(
        "UPDATE agent_runs a SET status=CASE WHEN "
        "EXISTS(SELECT 1 FROM report_versions v WHERE v.agent_run_id=a.agent_run_id AND v.status='published') OR "
        "EXISTS(SELECT 1 FROM company_profiles p WHERE p.payload->>'agent_run_id'=a.agent_run_id::text AND p.status='ready') OR "
        "EXISTS(SELECT 1 FROM discussion_messages m WHERE m.agent_run_id=a.agent_run_id) "
        "THEN 'completed' ELSE 'failed' END,finished_at=now() "
        "WHERE a.status='running' AND a.started_at<now()-interval '25 minutes'"
    )
    bucket = int(time.time() // 60)
    for kind in ("bank_poll", "news_index", "chunk_embed"):
        # At most one active maintenance task of each kind, even with multiple workers.
        with db() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("maintenance:" + kind,))
            pending = conn.execute(
                "SELECT 1 FROM jobs WHERE kind=%s AND status IN ('pending','running')", (kind,)
            ).fetchone()
            if not pending:
                conn.execute(
                    "INSERT INTO jobs(kind,idempotency_key,payload,max_attempts) VALUES(%s,%s,%s,2) "
                    "ON CONFLICT DO NOTHING",
                    (kind, f"{kind}:{bucket}", j({})),
                )


def release_on_shutdown(job):
    # A graceful interruption does not consume the final attempt. The token/status
    # predicates prevent an old worker from releasing a recovered or completed job.
    return execute(
        "UPDATE jobs SET status='pending',attempts=greatest(attempts-1,0),lease_until=NULL "
        "WHERE job_id=%s AND lease_token=%s AND status='running'",
        (job["job_id"], job["lease_token"]),
    )


def main():
    logging.basicConfig(level=logging.INFO)
    role = os.getenv("WORKER_ROLE", "backend")
    kinds = KINDS[role]
    context = mp.get_context("spawn")
    running = []
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_maintenance = 0
    concurrency = int(os.getenv("WORKER_CONCURRENCY", "2"))
    while not stopping:
        try:
            if role == "backend" and time.monotonic() - last_maintenance > 30:
                maintenance()
                last_maintenance = time.monotonic()
            for item in list(running):
                process, pipe, job, started = item
                if pipe.poll():
                    try:
                        outcome = pipe.recv()
                    except EOFError:
                        outcome = {"ok": False, "error": "Worker subprocess exited without a result"}
                    process.join(timeout=2)
                    if process.is_alive():
                        process.terminate()
                    finish(job, outcome)
                    pipe.close()
                    running.remove(item)
                elif not process.is_alive() or time.monotonic() - started > 1200:
                    if process.is_alive():
                        process.kill()
                    process.join()
                    finish(job, {"ok": False, "error": "Worker subprocess died or exceeded wall-time limit"})
                    pipe.close()
                    running.remove(item)
                else:
                    owned = execute(
                        "UPDATE jobs SET lease_until=now()+interval '2 minutes' WHERE job_id=%s "
                        "AND lease_token=%s AND status='running'",
                        (job["job_id"], job["lease_token"]),
                    )
                    if not owned:
                        process.kill()
                        process.join()
                        pipe.close()
                        running.remove(item)
            while len(running) < concurrency:
                job = claim(kinds)
                if not job:
                    break
                parent, child_pipe = context.Pipe(duplex=False)
                process = context.Process(
                    target=child,
                    args=(
                        job["kind"],
                        {**job["payload"], "_operation_key": job["idempotency_key"]},
                        child_pipe,
                    ),
                )
                process.start()
                child_pipe.close()
                running.append((process, parent, job, time.monotonic()))
        except Exception:
            log.exception("Worker tick failed; will retry")
        time.sleep(settings.worker_poll_seconds)
    for process, pipe, job, _ in running:
        if process.is_alive():
            process.kill()
        process.join()
        pipe.close()
        # Release on graceful shutdown; ungraceful exit is recovered by the lease.
        release_on_shutdown(job)


if __name__ == "__main__":
    main()

"""Developer observability: service health, workers, queues, agents, tools and data freshness."""

import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import httpx

from agent_common.core import all_rows, now, one, settings
from agent_common.runtime import ALLOWED_MODELS

WORKER_ALIVE_SECONDS = 60
EXPECTED_WORKERS = ("backend", "documents", "profiler", "insight")


def _base(url):
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme else ""


def _probe(name, url):
    if not url:
        return {"name": name, "status": "not_configured", "url": url}
    started = time.monotonic()
    try:
        response = httpx.get(url, timeout=2.5)
        status = "ok" if response.status_code < 400 else "error"
        detail = "" if status == "ok" else f"HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        status, detail = "down", type(exc).__name__
    return {
        "name": name,
        "url": url,
        "status": status,
        "detail": detail,
        "latency_ms": round((time.monotonic() - started) * 1000),
    }


def services():
    ranker = _base(settings.ranker_url)
    targets = [
        ("tool-gateway", _base(settings.gateway_url) + "/health"),
        ("embeddings", _base(settings.embedding_base_url) + "/health"),
        ("retrieval-ranking", ranker + "/ready" if ranker else ""),
        ("bank-materials-source", _base(settings.bank_source_url) + "/health"),
        ("searxng", _base(settings.searxng_base_url) + "/healthz"),
    ]
    with ThreadPoolExecutor(len(targets)) as pool:
        result = list(pool.map(lambda target: _probe(*target), targets))
    try:
        one("SELECT 1 AS ok")
        result.insert(0, {"name": "postgres", "status": "ok", "url": "", "detail": ""})
    except Exception as exc:  # pragma: no cover - backend is unusable then anyway
        result.insert(0, {"name": "postgres", "status": "down", "url": "", "detail": type(exc).__name__})
    llm_ready = bool(settings.openai_base_url and settings.openai_api_key)
    allowed = settings.openai_model.lower() in ALLOWED_MODELS
    result.append(
        {
            "name": "llm",
            "url": _base(settings.openai_base_url),
            "status": "ok" if llm_ready and allowed else "not_configured",
            "detail": f"model={settings.openai_model}"
            + ("" if allowed else " (не в списке разрешённых)")
            + ("" if llm_ready else " · нет OPENAI_BASE_URL/OPENAI_API_KEY"),
        }
    )
    return result


def workers():
    rows = all_rows(
        "SELECT *,extract(epoch FROM now()-last_seen)::int AS age_s,"
        "extract(epoch FROM now()-started_at)::int AS uptime_s FROM worker_heartbeats "
        "WHERE last_seen>now()-interval '10 minutes' ORDER BY role,worker_id"
    )
    for row in rows:
        row["alive"] = row["age_s"] <= WORKER_ALIVE_SECONDS
    alive_roles = {row["role"] for row in rows if row["alive"]}
    return {"items": rows, "missing_roles": [role for role in EXPECTED_WORKERS if role not in alive_roles]}


def queues():
    return all_rows(
        "SELECT kind,"
        "count(*) FILTER (WHERE status='pending' AND available_at<=now()) AS ready,"
        "count(*) FILTER (WHERE status='pending' AND available_at>now()) AS retry_scheduled,"
        "count(*) FILTER (WHERE status='running') AS running,"
        "count(*) FILTER (WHERE status='failed' AND coalesce(finished_at,created_at)>now()-interval '24 hours') "
        "AS failed_24h,"
        "count(*) FILTER (WHERE status='completed' AND finished_at>now()-interval '1 hour') AS completed_1h,"
        "count(*) FILTER (WHERE status='completed' AND finished_at>now()-interval '24 hours') AS completed_24h,"
        "extract(epoch FROM now()-min(created_at) FILTER (WHERE status='pending'))::int AS oldest_pending_s,"
        "round(avg(extract(epoch FROM started_at-created_at)) FILTER (WHERE started_at IS NOT NULL "
        "AND created_at>now()-interval '24 hours')::numeric,1) AS avg_wait_s,"
        "round(avg(extract(epoch FROM finished_at-started_at)) FILTER (WHERE status='completed' "
        "AND finished_at>now()-interval '24 hours')::numeric,1) AS avg_run_s,"
        "round((percentile_cont(0.95) WITHIN GROUP (ORDER BY extract(epoch FROM finished_at-started_at)) "
        "FILTER (WHERE status='completed' AND finished_at>now()-interval '24 hours'))::numeric,1) AS p95_run_s "
        "FROM jobs GROUP BY kind ORDER BY kind"
    )


def failures(limit=25):
    return all_rows(
        "SELECT job_id,kind,status,attempts,max_attempts,last_error,created_at,available_at,finished_at,"
        "payload->>'company_id' AS company_id FROM jobs WHERE last_error IS NOT NULL "
        "AND (status='failed' OR status='pending') ORDER BY coalesce(finished_at,available_at,created_at) DESC "
        "LIMIT %s",
        (limit,),
    )


def agents():
    return all_rows(
        "WITH runs AS (SELECT a.*,extract(epoch FROM coalesce(a.finished_at,now())-a.started_at) AS duration_s,"
        "(SELECT count(*) FROM agent_steps s WHERE s.agent_run_id=a.agent_run_id AND s.step_kind='validation' "
        "AND s.status='error') AS validation_errors,"
        "(SELECT count(*) FROM agent_steps s WHERE s.agent_run_id=a.agent_run_id AND s.step_kind='tool_result' "
        "AND s.status='error') AS tool_errors "
        "FROM agent_runs a WHERE a.started_at>now()-interval '24 hours') "
        "SELECT agent_name,count(*) AS runs,"
        "count(*) FILTER (WHERE status='completed') AS completed,"
        "count(*) FILTER (WHERE status='failed') AS failed,"
        "count(*) FILTER (WHERE status='running') AS running,"
        "round(avg(duration_s) FILTER (WHERE status='completed')::numeric,1) AS avg_duration_s,"
        "round((percentile_cont(0.95) WITHIN GROUP (ORDER BY duration_s) "
        "FILTER (WHERE status='completed'))::numeric,1) AS p95_duration_s,"
        "coalesce(sum(llm_tokens),0) AS tokens,round(avg(llm_tokens)::numeric) AS avg_tokens,"
        "round(avg(tool_calls)::numeric,1) AS avg_tool_calls,"
        "coalesce(sum(validation_errors),0) AS validation_errors,coalesce(sum(tool_errors),0) AS tool_errors "
        "FROM runs GROUP BY agent_name ORDER BY agent_name"
    )


def tools():
    return all_rows(
        "SELECT tool_name,count(*) AS calls,count(*) FILTER (WHERE status<>'ok') AS errors,"
        "round(avg(result_count)::numeric,1) AS avg_results,max(created_at) AS last_call "
        "FROM tool_audit WHERE created_at>now()-interval '24 hours' GROUP BY tool_name ORDER BY calls DESC"
    )


def data():
    news = one(
        "SELECT count(*) AS total,max(fetched_at) AS last_fetch,"
        "count(*) FILTER (WHERE fetched_at>now()-interval '24 hours') AS fetched_24h,"
        "count(*) FILTER (WHERE published_at>now()-interval '7 days') AS published_7d,"
        "count(*) FILTER (WHERE duplicate_of_id IS NOT NULL) AS duplicates,"
        "(SELECT count(*) FROM news_revisions) AS revisions,"
        "(SELECT count(DISTINCT (article_id,content_version)) FROM news_embeddings) AS embedded_revisions FROM news"
    )
    news["sources"] = all_rows(
        "SELECT source_name,count(*) AS articles,max(fetched_at) AS last_fetch,max(published_at) AS last_published,"
        "count(*) FILTER (WHERE fetched_at>now()-interval '24 hours') AS fetched_24h "
        "FROM news GROUP BY source_name ORDER BY max(fetched_at) DESC NULLS LAST"
    )
    return {
        "news": news,
        "documents": all_rows(
            "SELECT source_type,processing_status,count(*) AS count FROM documents GROUP BY 1,2 ORDER BY 1,2"
        ),
        "rag_chunks": one(
            "SELECT count(*) AS total,count(*) FILTER (WHERE embedding IS NULL) AS without_embedding FROM rag_chunks"
        ),
        "profiles": one(
            "SELECT (SELECT count(*) FROM current_company_profiles) AS company_profiles,"
            "(SELECT count(*) FROM current_user_preferences) AS user_contexts,"
            "(SELECT count(*) FROM company_profiles WHERE status='failed') AS failed_profiles"
        ),
        "ranking": all_rows(
            "SELECT status,count(*) AS count FROM ranking_requests WHERE created_at>now()-interval '7 days' "
            "GROUP BY status ORDER BY status"
        ),
        "reports": one(
            "SELECT count(*) AS reports_7d,"
            "(SELECT count(*) FROM report_versions WHERE status='published' AND published_at>now()-interval '7 days') "
            "AS published_7d,"
            "(SELECT count(*) FROM report_versions WHERE status='published' "
            "AND input_snapshot @> '{\"deadline_fallback\":true}'::jsonb) AS deadline_bulletins "
            "FROM reports WHERE created_at>now()-interval '7 days'"
        ),
    }


def snapshot(probe_services=True):
    return {
        "generated_at": now(),
        "services": services() if probe_services else [],
        "workers": workers(),
        "queues": queues(),
        "failures": failures(),
        "agents": agents(),
        "tools": tools(),
        "data": data(),
    }



def public_snapshot():
    """Read-only demo health; no identities, source contents, traces or exception text."""
    worker_state = workers()
    roles = []
    for role in EXPECTED_WORKERS:
        items = [worker for worker in worker_state["items"] if worker["role"] == role]
        roles.append({
            "role": role,
            "alive": any(worker["alive"] for worker in items),
            "running": sum(len(worker.get("running_jobs") or []) for worker in items),
            "processed": sum(worker.get("processed", 0) for worker in items),
            "failed": sum(worker.get("failed", 0) for worker in items),
        })
    return {
        "generated_at": now(),
        "mode": "demo",
        "services": [
            {
                **{key: service[key] for key in ("name", "status", "latency_ms") if key in service},
                "status": "configured" if service["name"] == "llm" and service["status"] == "ok"
                else service["status"],
            }
            for service in services()
        ],
        "workers": roles,
        "queues": [
            {key: queue.get(key) for key in (
                "kind", "ready", "retry_scheduled", "running", "failed_24h", "completed_1h",
                "oldest_pending_s", "avg_run_s",
            )}
            for queue in queues()
        ],
    }


def agent_runs(limit=50, agent=None, status=None, company_id=None):
    return all_rows(
        "SELECT a.agent_run_id,a.agent_name,a.company_id,a.user_id,a.status,a.llm_model,a.llm_tokens,a.tool_calls,"
        "a.started_at,a.finished_at,round(extract(epoch FROM coalesce(a.finished_at,now())-a.started_at)::numeric,1) "
        "AS duration_s,(SELECT count(*) FROM agent_steps s WHERE s.agent_run_id=a.agent_run_id) AS steps,"
        "(SELECT count(*) FROM agent_steps s WHERE s.agent_run_id=a.agent_run_id AND s.status='error') AS errors "
        "FROM agent_runs a WHERE (%s::text IS NULL OR a.agent_name=%s) AND (%s::text IS NULL OR a.status=%s) "
        "AND (%s::text IS NULL OR a.company_id=%s) ORDER BY a.started_at DESC LIMIT %s",
        (agent, agent, status, status, company_id, company_id, limit),
    )


def agent_run(agent_run_id):
    run = one("SELECT * FROM agent_runs WHERE agent_run_id=%s", (agent_run_id,))
    if not run:
        return None
    return {
        "run": run,
        "steps": all_rows(
            "SELECT step_no,step_kind,tool_name,arguments,left(result_summary::text,4000) AS result_summary,status,"
            "started_at,finished_at FROM agent_steps WHERE agent_run_id=%s ORDER BY step_no",
            (agent_run_id,),
        ),
        "tool_audit": all_rows(
            "SELECT tool_name,arguments,status,result_count,created_at FROM tool_audit WHERE agent_run_id=%s "
            "ORDER BY created_at",
            (agent_run_id,),
        ),
    }


def _labels(**labels):
    inner = ",".join(f'{key}="{str(value).replace(chr(34), "")}"' for key, value in labels.items())
    return "{" + inner + "}" if inner else ""


def prometheus():
    snap = snapshot(probe_services=True)
    lines = []

    def metric(name, kind, help_text, samples):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            lines.append(f"{name}{_labels(**labels)} {float(value or 0)}")

    metric(
        "alpha_service_up",
        "gauge",
        "Dependency health probe (1=ok)",
        [({"service": s["name"]}, s["status"] == "ok") for s in snap["services"]],
    )
    metric(
        "alpha_worker_alive",
        "gauge",
        "Worker heartbeat within 60s",
        [({"role": w["role"], "worker": w["worker_id"]}, w["alive"]) for w in snap["workers"]["items"]],
    )
    metric(
        "alpha_worker_jobs_processed_total",
        "counter",
        "Jobs finished by a worker since its start",
        [({"role": w["role"], "worker": w["worker_id"]}, w["processed"]) for w in snap["workers"]["items"]],
    )
    for field, help_text in (
        ("ready", "Jobs ready to run"),
        ("retry_scheduled", "Jobs waiting for a retry"),
        ("running", "Jobs running"),
        ("failed_24h", "Jobs failed in 24h"),
        ("completed_24h", "Jobs completed in 24h"),
        ("oldest_pending_s", "Age of the oldest pending job"),
        ("avg_wait_s", "Average queue wait, 24h"),
        ("p95_run_s", "p95 job run time, 24h"),
    ):
        metric(
            f"alpha_jobs_{field}",
            "gauge",
            help_text,
            [({"kind": q["kind"]}, q[field]) for q in snap["queues"]],
        )
    for field, help_text in (
        ("runs", "Agent runs in 24h"),
        ("failed", "Failed agent runs in 24h"),
        ("running", "Agent runs in progress"),
        ("p95_duration_s", "p95 agent run duration, 24h"),
        ("tokens", "LLM tokens in 24h"),
        ("avg_tool_calls", "Average tool calls per run"),
        ("validation_errors", "Validation repair loops in 24h"),
    ):
        metric(
            f"alpha_agent_{field}",
            "gauge",
            help_text,
            [({"agent": a["agent_name"]}, a[field]) for a in snap["agents"]],
        )
    metric(
        "alpha_tool_calls_24h",
        "gauge",
        "Tool calls in 24h",
        [({"tool": t["tool_name"]}, t["calls"]) for t in snap["tools"]],
    )
    metric(
        "alpha_tool_errors_24h",
        "gauge",
        "Tool errors in 24h",
        [({"tool": t["tool_name"]}, t["errors"]) for t in snap["tools"]],
    )
    news = snap["data"]["news"]
    metric("alpha_news_total", "gauge", "Articles stored", [({}, news["total"])])
    metric("alpha_news_fetched_24h", "gauge", "Articles fetched in 24h", [({}, news["fetched_24h"])])
    metric(
        "alpha_news_embedded_revisions",
        "gauge",
        "Embedded news revisions",
        [({}, news["embedded_revisions"])],
    )
    metric("alpha_news_revisions", "gauge", "News revisions", [({}, news["revisions"])])
    last = news["last_fetch"]
    metric(
        "alpha_news_last_fetch_age_seconds",
        "gauge",
        "Seconds since the last stored article",
        [({}, (now() - last).total_seconds() if last else -1)],
    )
    return "\n".join(lines) + "\n"

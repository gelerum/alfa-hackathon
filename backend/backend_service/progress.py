"""Human-readable pipeline state for one company and user: what is happening right now."""

import re
from datetime import timedelta

from agent_common.core import all_rows, now, one, settings

TOOL_LABELS = {
    "company_bank_data.get": "Читает банковские данные компании",
    "company_context.get": "Загружает зафиксированный профиль и ваш фокус",
    "company_documents.search": "Ищет в документах компании",
    "company_documents.read": "Читает документ компании",
    "news.search": "Ищет новости",
    "news.read": "Читает новость",
    "bank_analytics.search": "Ищет в аналитике Альфа-Банка",
    "bank_analytics.read": "Читает материал Альфа-Банка",
    "web.search": "Ищет отраслевой контекст в интернете",
    "web.fetch": "Открывает веб-страницу",
}
ERROR_HINTS = (
    (
        "Configure approved LLM",
        "Не настроен LLM: заполните OPENAI_BASE_URL, OPENAI_API_KEY и OPENAI_MODEL в .env",
    ),
    ("allowlist", "Модель в OPENAI_MODEL не входит в список разрешённых кейсом"),
    ("Waiting for document extraction", "Ждём, пока из загруженных документов будет извлечён текст"),
    ("Agent budget exhausted", "Агент не уложился в лимит времени, будет повтор"),
    ("lease expired", "Обработчик остановился во время выполнения"),
    ("Ranker is busy", "Отбор новостей занят другим обзором — ждём очереди"),
    ("Agent token budget exhausted", "Агент израсходовал лимит токенов на анализ"),
    (
        "Embedding service remained",
        "Сервис эмбеддингов занят (идёт векторизация новостей) или ещё загружает модель",
    ),
    ("Embedding service unreachable", "Сервис эмбеддингов недоступен — проверьте контейнер embeddings"),
)
STUCK_AFTER = timedelta(minutes=2)
WORKER_NAMES = {"profiler": "подготовки профиля", "insight": "анализа новостей", "documents": "документов"}
VALIDATION_HINTS = (
    ("Self-review issues", "самопроверка нашла неподтверждённые утверждения"),
    ("evidence quote must be an exact", "цитата к факту не совпадает дословно с источником"),
    ("Fact value must be extractive", "значение факта не совпадает с банковскими данными или документом"),
    (
        "Fictional-company facts require",
        "факт о компании взят не из банковских данных, документов или ваших ответов",
    ),
    ("Facet references an unknown fact", "признак для поиска ссылается на несуществующий факт"),
    ("Tool not in capability allowlist", "агент попытался вызвать недоступный инструмент"),
    ("Unknown action", "модель вернула ответ неизвестного вида"),
    ("validation error", "ответ не соответствует схеме профиля"),
    ("Expecting", "модель вернула некорректный JSON"),
    ("Unterminated string", "модель вернула обрезанный JSON"),
    ("Expected a JSON object", "модель вернула не JSON-объект"),
)


def _error(text):
    if not text:
        return ""
    for needle, hint in ERROR_HINTS:
        if needle.lower() in text.lower():
            return hint
    return re.sub(r"^[A-Za-z_.]*(?:Error|Exception|Timeout):\s*", "", text)[:300]


def _validation_reason(row):
    summary = row.get("result_summary")
    error = summary.get("error", "") if isinstance(summary, dict) else ""
    for needle, hint in VALIDATION_HINTS:
        if needle.lower() in error.lower():
            return hint
    return error[:160]


def _step(row):
    if row["step_kind"] == "tool_call":
        label = TOOL_LABELS.get(row["tool_name"], row["tool_name"] or "Вызывает инструмент")
        query = (row["arguments"] or {}).get("query") if isinstance(row["arguments"], dict) else None
        return label + (f": «{query}»" if query else "")
    if row["step_kind"] == "plan":
        return "Планирует шаги анализа"
    if row["step_kind"] == "validation":
        if row["status"] != "error":
            summary = row.get("result_summary")
            review = isinstance(summary, dict) and "self_review" in summary
            return "Самопроверка пройдена" if review else "Проверка фактов и источников пройдена"
        reason = _validation_reason(row)
        return "Проверка не пройдена" + (f": {reason}" if reason else "") + " — агент исправляет результат"
    if row["step_kind"] == "synthesis":
        return "Формирует итоговый результат"
    return ""


def _worker_alive(role):
    return bool(
        one(
            "SELECT 1 FROM worker_heartbeats WHERE role=%s AND last_seen>now()-interval '60 seconds' LIMIT 1",
            (role,),
        )
    )


def _run(agent, company_id, user_id, since):
    run = one(
        "SELECT agent_run_id,status,started_at,finished_at FROM agent_runs WHERE agent_name=%s AND company_id=%s "
        "AND (user_id=%s OR user_id IS NULL) AND started_at>=%s ORDER BY started_at DESC LIMIT 1",
        (agent, company_id, user_id, since),
    )
    if not run:
        return None, []
    steps = all_rows(
        "SELECT step_no,step_kind,tool_name,arguments,result_summary,status,started_at FROM agent_steps "
        "WHERE agent_run_id=%s AND step_kind<>'tool_result' ORDER BY step_no DESC LIMIT 8",
        (run["agent_run_id"],),
    )
    failed = one(
        "SELECT count(*) AS n FROM agent_steps WHERE agent_run_id=%s AND step_kind='validation' AND status='error'",
        (run["agent_run_id"],),
    )["n"]
    items = [
        {"text": _step(s), "at": s["started_at"], "status": s["status"], "kind": s["step_kind"]}
        for s in reversed(steps)
        if _step(s)
    ]
    # Number repair attempts so repeated failures are not mistaken for duplicated lines.
    number = failed - sum(1 for item in items if item["kind"] == "validation" and item["status"] == "error")
    for item in items:
        if item.pop("kind") == "validation" and item["status"] == "error":
            number += 1
            item["text"] = item["text"].replace(
                "Проверка не пройдена",
                f"Проверка не пройдена (попытка {number} из {settings.agent_max_steps})",
                1,
            )
    return run, items


def _job_stage(stage, job, run_label, role):
    if job["status"] == "running":
        stage.update(state="active", detail=run_label, at=job["created_at"])
    elif job["status"] == "failed":
        stage.update(state="error", detail="Не удалось: " + _error(job["last_error"]), at=job["finished_at"])
    elif job["attempts"]:
        stage["retrying"] = True
        stage.update(
            state="error",
            detail=f"Повторная попытка {job['attempts'] + 1} из {job['max_attempts']}. Причина: {_error(job['last_error'])}",
            at=job["available_at"],
        )
    else:
        stage.update(state="waiting", detail="В очереди", at=job["created_at"])
    if job["status"] == "pending" and not _worker_alive(role):
        offline = f"обработчик {WORKER_NAMES.get(role, role)} не запущен"
        stage["detail"] = f"{stage['detail']} · {offline}" if job["attempts"] else "В очереди, но " + offline
    elif job["status"] == "pending" and not job["attempts"] and now() - job["created_at"] > STUCK_AFTER:
        stage["detail"] = f"В очереди дольше 2 минут: проверьте обработчик {WORKER_NAMES.get(role, role)}"
    return stage


def _profile_stage(company_id, user_id, focus, profile):
    stage = {"key": "profile", "title": "Профиль компании и ваш фокус", "state": "idle", "steps": []}
    job = one(
        "SELECT * FROM jobs WHERE kind='profile' AND payload->>'company_id'=%s AND payload->>'user_id'=%s "
        "ORDER BY created_at DESC LIMIT 1",
        (company_id, str(user_id)),
    )
    if not job:
        if profile:
            return {
                **stage,
                "state": "done",
                "detail": "Профиль собран — по нему подбираются новости",
                "at": profile["published_at"],
            }
        return {
            **stage,
            "detail": "Начнётся после сохранения фокуса" if not focus else "Ожидает запуска агента",
        }
    if job["status"] == "completed":
        return {
            **stage,
            "state": "done",
            "detail": "Профиль собран — по нему подбираются новости",
            "at": job["finished_at"],
        }
    _job_stage(stage, job, "Агент собирает профиль компании", "profiler")
    run, steps = _run("company_profiler", company_id, user_id, job["created_at"])
    stage["steps"] = steps
    if run and job["status"] == "running" and not steps:
        stage["detail"] = "Агент запущен, ждём первого шага"
    return stage


def _report_stage(company_id, user_id, profile_ready):
    stage = {"key": "report", "title": "Обзор: отбор новостей и анализ агентом", "state": "idle", "steps": []}
    report = one(
        "SELECT r.report_id,r.created_at,q.request_id,q.status AS ranking_status,q.last_error AS ranking_error,"
        "v.version,v.published_at FROM reports r "
        "LEFT JOIN LATERAL (SELECT * FROM ranking_requests q WHERE q.report_id=r.report_id "
        "ORDER BY created_at DESC LIMIT 1) q ON true "
        "LEFT JOIN LATERAL (SELECT * FROM report_versions v WHERE v.report_id=r.report_id AND v.status='published' "
        "ORDER BY version DESC LIMIT 1) v ON true "
        "WHERE r.company_id=%s AND r.user_id=%s ORDER BY r.created_at DESC LIMIT 1",
        (company_id, user_id),
    )
    if not report:
        detail = (
            "Нажмите «Подготовить обзор»"
            if profile_ready
            else "Станет доступен, когда агент соберёт профиль компании (этап выше)"
        )
        return {**stage, "detail": detail}
    if report["published_at"]:
        return {
            **stage,
            "state": "done",
            "detail": "Обзор опубликован",
            "at": report["published_at"],
        }
    job = (
        one("SELECT * FROM jobs WHERE idempotency_key=%s", ("insight:" + str(report["request_id"]),))
        if report["request_id"]
        else None
    )
    if job:
        _job_stage(stage, job, "Агент анализирует отобранные новости и аналитику банка", "insight")
        _, stage["steps"] = _run("insight_agent", company_id, user_id, job["created_at"])
    elif report["ranking_status"] == "failed":
        stage.update(state="error", detail="Отбор новостей не удался: " + _error(report["ranking_error"]))
    elif report["ranking_status"]:
        stage.update(
            state="active", detail="Отбираем и ранжируем новости под профиль", at=report["created_at"]
        )
    else:
        stage.update(state="waiting", detail="Ждёт готового профиля компании", at=report["created_at"])
    return stage


def pipeline(company_id, user_id):
    focus = one(
        "SELECT answers,created_at FROM onboarding_answers WHERE company_id=%s AND user_id=%s "
        "ORDER BY created_at DESC LIMIT 1",
        (company_id, user_id),
    )
    profile = one("SELECT * FROM current_company_profiles WHERE company_id=%s", (company_id,))
    preferences = one(
        "SELECT * FROM current_user_preferences WHERE company_id=%s AND user_id=%s", (company_id, user_id)
    )
    bank = one(
        "SELECT d.main_okved_name,d.registration_city FROM bank_company_data d "
        "JOIN bank_import_batches s USING(source_snapshot_id) WHERE d.company_id=%s AND s.status='ready' "
        "ORDER BY d.bank_record_id DESC LIMIT 1",
        (company_id,),
    )
    payments = one(
        "SELECT count(DISTINCT CASE WHEN payer=%s THEN beneficiary ELSE payer END) AS n FROM bank_payment_edges "
        "WHERE (payer=%s OR beneficiary=%s) AND payer<>beneficiary",
        (company_id, company_id, company_id),
    )["n"]
    documents = all_rows(
        "SELECT processing_status,count(*) AS n FROM documents WHERE company_id=%s AND source_type='user_upload' "
        "GROUP BY 1",
        (company_id,),
    )
    news = one(
        "SELECT count(*) AS total,max(fetched_at) AS last_fetch,"
        "count(*) FILTER (WHERE published_at>=now()-interval '7 days') AS week,"
        "(SELECT count(DISTINCT article_id) FROM news_embeddings) AS embedded FROM news"
    )
    stages = [
        {
            "key": "focus",
            "title": "Ваш фокус",
            "state": "done" if focus else "idle",
            "detail": f"Сохранён: {focus['answers'].get('role', '')}" if focus else "Не заполнен",
            "at": focus["created_at"] if focus else None,
        },
        {
            "key": "bank",
            "title": "Банковские данные",
            "state": "done" if bank else "error",
            "detail": f"{bank['main_okved_name']} · {bank['registration_city'] or 'город не указан'} · "
            f"контрагентов в платежах: {payments}"
            if bank
            else "Строка компании в банковских данных не найдена",
        },
    ]
    counts = {row["processing_status"]: row["n"] for row in documents}
    if not counts:
        stages.append(
            {
                "key": "documents",
                "title": "Документы компании",
                "state": "skipped",
                "detail": "Не загружены — профиль строится по банковским данным и вашему фокусу",
            }
        )
    else:
        busy = counts.get("pending", 0) + counts.get("processing", 0)
        parts = [f"готово {counts.get('ready', 0)}"]
        if busy:
            parts.append(f"обрабатывается {busy}")
        if counts.get("failed"):
            parts.append(f"с ошибкой {counts['failed']}")
        state = "active" if busy else "error" if counts.get("failed") else "done"
        stages.append(
            {
                "key": "documents",
                "title": "Документы компании",
                "state": state,
                "detail": "Извлечение текста: " + ", ".join(parts),
            }
        )
    stages.append(_profile_stage(company_id, user_id, focus, profile))
    if not news["total"]:
        news_stage = {"state": "error", "detail": "Новостей в базе пока нет — проверьте сервис parser"}
    else:
        stale = news["last_fetch"] < now() - timedelta(hours=2)
        news_stage = {
            "state": "waiting" if stale else "done",
            "detail": f"В базе {news['total']} новостей, за 7 дней {news['week']}, "
            f"готово к смысловому поиску {news['embedded']}"
            + (" · давно не было новых поступлений, проверьте parser" if stale else ""),
            "at": news["last_fetch"],
        }
    stages.append({"key": "news", "title": "Сбор новостей", **news_stage})
    stages.append(_report_stage(company_id, user_id, bool(profile and preferences)))
    return {
        "stages": stages,
        "busy": any(
            (s["state"] in ("active", "waiting") or s.get("retrying")) and s["key"] != "news" for s in stages
        ),
        "profile": _fixed_profile(profile),
        "user_context": _fixed_focus(preferences),
    }


def _fixed_profile(profile):
    if not profile:
        return None
    facts = all_rows(
        "SELECT f.field,f.value,f.unit,f.source_type,f.evidence_text FROM company_facts f "
        "JOIN company_profile_facts pf USING(fact_id) WHERE pf.profile_id=%s ORDER BY f.source_type,f.field",
        (profile["profile_id"],),
    )
    facets = all_rows(
        "SELECT facet_type,text,keywords FROM company_context_facets WHERE profile_id=%s AND scope='company' "
        "AND status='ready' ORDER BY facet_type",
        (profile["profile_id"],),
    )
    return {
        "version": profile["profile_version"],
        "published_at": profile["published_at"],
        "summary": profile["payload"].get("summary", ""),
        "missing_data": profile["missing_data"],
        "conflicts": profile["conflicts"],
        "facts": facts,
        "facets": facets,
    }


def _fixed_focus(preferences):
    if not preferences:
        return None
    answers = preferences["raw_answers"] if isinstance(preferences["raw_answers"], dict) else {}
    return {
        "version": preferences["context_version"],
        "published_at": preferences["published_at"],
        **{key: answers.get(key) for key in ("role", "responsibilities", "goals", "kpis", "note")},
    }

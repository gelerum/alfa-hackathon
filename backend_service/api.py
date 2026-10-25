import os
import secrets
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from agent_common.contracts import Answers, StrictModel
from agent_common.core import (
    admin,
    all_rows,
    current_user,
    db,
    digest,
    enqueue,
    execute,
    j,
    membership,
    one,
    settings,
)
from backend_service.demo import create_demo_session
from backend_service.documents import SUPPORTED
from backend_service.reports import create_report, request_ranking

from agent_common.http_limits import BodyLimit

app = FastAPI(title="Alpha Business Backend", version="0.1.0", docs_url="/admin/docs")
api_router = APIRouter()
app.add_middleware(BodyLimit, limit=settings.max_upload_bytes + 1024 * 1024)

# Mount API at /api
app.include_router(api_router, prefix="/api")

# Serve frontend (static assets under /frontend/)
_FRONTEND_DIR = Path(os.environ.get("FRONTEND_DIR", Path(__file__).resolve().parents[2] / "frontend"))
if _FRONTEND_DIR.exists():
    app.mount("/frontend", StaticFiles(directory=str(_FRONTEND_DIR)), name="frontend")


class DemoCompany(StrictModel):
    synthetic_company_id: str = Field(min_length=1, max_length=128)
    fictional_name: str = Field(min_length=1, max_length=200)
    display_name: str = Field(default="Демо-пользователь", max_length=200)


@api_router.get("/health")
def health():
    return {"status": "ok", "database": bool(one("SELECT 1 AS ok"))}


_FRONT_INDEX = _FRONTEND_DIR / "index.html"


@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    if _FRONT_INDEX.exists():
        return _FRONT_INDEX.read_text(encoding="utf-8")
    return HTMLResponse("<h1>Backend only — no frontend directory found</h1>", status_code=404)


@api_router.get("/admin/metrics", dependencies=[Depends(admin)])
def metrics():
    return {
        "jobs": all_rows("SELECT kind,status,count(*) AS count FROM jobs GROUP BY kind,status"),
        "agents": all_rows(
            "SELECT agent_name,status,count(*) AS count,sum(llm_tokens) AS tokens,sum(tool_calls) AS tool_calls "
            "FROM agent_runs GROUP BY agent_name,status"
        ),
        "documents": all_rows(
            "SELECT processing_status,count(*) AS count FROM documents GROUP BY processing_status"
        ),
        "deadline_bulletins": one(
            "SELECT count(*) AS count FROM report_versions WHERE status='published' "
            "AND input_snapshot @> '{\"deadline_fallback\":true}'::jsonb"
        ),
    }


@api_router.post("/admin/demo", dependencies=[Depends(admin)])
def bootstrap(request: DemoCompany):
    """Explicit demo-only identity provisioning, never an automatic synthetic-to-real match."""
    token = secrets.token_urlsafe(32)
    with db() as conn:
        company = conn.execute(
            "SELECT company_id FROM companies WHERE company_id=%s", (request.synthetic_company_id,)
        ).fetchone()
        if not company:
            raise HTTPException(404, "Select an existing synthetic company from CSV import")
        conn.execute(
            "UPDATE companies SET legal_name=%s,is_fictional=true WHERE company_id=%s",
            (request.fictional_name, request.synthetic_company_id),
        )
        user = conn.execute(
            "INSERT INTO app_users(auth_subject,display_name) VALUES(%s,%s) RETURNING *",
            ("demo:" + str(uuid4()), request.display_name),
        ).fetchone()
        conn.execute(
            "INSERT INTO company_memberships(company_id,user_id,access_role) VALUES(%s,%s,'admin')",
            (request.synthetic_company_id, user["user_id"]),
        )
        conn.execute(
            "INSERT INTO api_tokens(token_hash,user_id) VALUES(%s,%s)", (digest(token), user["user_id"])
        )
    return {
        "user_id": user["user_id"],
        "company_id": request.synthetic_company_id,
        "token": token,
        "fictional": True,
        "warning": "Demo mapping is not a real company identification.",
    }


@api_router.post("/demo/session")
def demo_session():
    """Unauthenticated, shared demo account; only available with explicit DEMO_MODE."""
    return create_demo_session()


@api_router.get("/companies")
def companies(user: UUID = Depends(current_user)):
    return all_rows(
        "SELECT c.*,m.business_role,m.access_role FROM companies c JOIN company_memberships m USING(company_id) "
        "WHERE m.user_id=%s AND m.is_active",
        (user,),
    )


@api_router.post("/companies/{company_id}/onboarding", status_code=202)
def onboarding(company_id: str, answers: Answers, user: UUID = Depends(current_user)):
    membership(company_id, user, write=True)
    with db() as conn:
        row = conn.execute(
            "INSERT INTO onboarding_answers(company_id,user_id,answers) VALUES(%s,%s,%s) RETURNING answer_id",
            (company_id, user, j(answers.model_dump())),
        ).fetchone()
        job = conn.execute(
            "INSERT INTO jobs(kind,idempotency_key,payload) VALUES('profile',%s,%s) RETURNING job_id,status",
            (
                "answers:" + str(row["answer_id"]),
                j({"company_id": company_id, "user_id": user, "answer_id": row["answer_id"]}),
            ),
        ).fetchone()
    return {"answer_id": row["answer_id"], "job_id": job["job_id"], "status": job["status"]}


class ClarificationAnswer(StrictModel):
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=2000)


@api_router.post("/companies/{company_id}/clarifications", status_code=202)
def clarify(company_id: str, request: ClarificationAnswer, user: UUID = Depends(current_user)):
    membership(company_id, user, write=True)
    with db() as conn:
        source = conn.execute(
            "INSERT INTO evidence_sources(company_id,source_type,source_ref,excerpt,source_date) "
            "VALUES(%s,'user_answer',%s,%s,now()) RETURNING source_id",
            (
                company_id,
                j({"kind": "company_clarification", "question": request.question, "author_user_id": user}),
                request.answer,
            ),
        ).fetchone()
        key = "clarification:" + str(source["source_id"])
        job = conn.execute(
            "INSERT INTO jobs(kind,idempotency_key,payload) VALUES('profile',%s,%s) RETURNING job_id",
            (key, j({"company_id": company_id, "user_id": user})),
        ).fetchone()
    return {
        "job_id": job["job_id"],
        "source_id": source["source_id"],
        "status": "pending",
        "notice": "Ответ сохраняется как сообщённая вами информация о компании, не независимый проверенный факт.",
    }


@api_router.post("/companies/{company_id}/documents", status_code=202)
async def upload(
    company_id: str,
    file: UploadFile = File(),
    title: str = Form(default=""),
    user: UUID = Depends(current_user),
):
    membership(company_id, user, write=True)
    filename = Path((file.filename or "").replace("\\", "/")).name
    if len(filename) > 255:
        raise HTTPException(422, "Filename exceeds 255 characters")
    if Path(filename).suffix.lower() not in SUPPORTED:
        raise HTTPException(415, "Supported formats: PDF, DOCX, XLSX, PNG, JPEG, TIFF, WEBP")
    if len(title) > 300:
        raise HTTPException(422, "Title too long")
    data = await file.read(settings.max_upload_bytes + 1)
    if not data or len(data) > settings.max_upload_bytes:
        raise HTTPException(413, "Empty file or upload exceeds limit")
    document_id = uuid4()
    key = str(document_id)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    path = settings.storage_dir / key
    path.write_bytes(data)
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO documents(document_id,company_id,source_type,title,storage_key,content_sha256,"
                "original_filename,media_type,metadata) VALUES(%s,%s,'user_upload',%s,%s,%s,%s,%s,%s)",
                (
                    document_id,
                    company_id,
                    title or filename,
                    key,
                    digest(data),
                    filename,
                    file.content_type,
                    j({"uploaded_by": user}),
                ),
            )
            extraction_job = conn.execute(
                "INSERT INTO jobs(kind,idempotency_key,payload) VALUES('extract_document',%s,%s) RETURNING job_id",
                (
                    "extract:" + key,
                    j({"document_id": document_id, "company_id": company_id, "user_id": user}),
                ),
            ).fetchone()
    except Exception:
        # A lost commit acknowledgement is ambiguous: never delete a possibly committed original.
        try:
            persisted = one("SELECT 1 FROM documents WHERE document_id=%s", (document_id,))
            if not persisted:
                path.unlink(missing_ok=True)
        except Exception:
            pass  # Retain an orphan for operator cleanup rather than lose a referenced original.
        raise
    return {"document_id": document_id, "job_id": extraction_job["job_id"], "status": "pending"}


@api_router.get("/companies/{company_id}/documents")
def list_documents(company_id: str, user: UUID = Depends(current_user)):
    membership(company_id, user)
    return all_rows(
        "SELECT document_id,title,original_filename,processing_status,processing_error,created_at "
        "FROM (SELECT *,fetched_at AS created_at FROM documents) d WHERE company_id=%s ORDER BY fetched_at DESC",
        (company_id,),
    )


@api_router.get("/documents/{document_id}/content")
def document_content(document_id: UUID, user: UUID = Depends(current_user)):
    row = one("SELECT * FROM documents WHERE document_id=%s", (document_id,))
    if not row:
        raise HTTPException(404)
    if row["company_id"]:
        membership(row["company_id"], user)
    elif row["source_type"] != "bank_analytics":
        raise HTTPException(403)
    return FileResponse(
        settings.storage_dir / row["storage_key"],
        filename=row["original_filename"],
        media_type="application/octet-stream",
        headers={"X-Content-Type-Options": "nosniff"},
    )


@api_router.get("/companies/{company_id}/context")
def get_context(company_id: str, user: UUID = Depends(current_user)):
    membership(company_id, user)
    profile = one("SELECT * FROM current_company_profiles WHERE company_id=%s", (company_id,))
    preferences = one(
        "SELECT * FROM current_user_preferences WHERE company_id=%s AND user_id=%s", (company_id, user)
    )
    return {
        "profile": profile,
        "user_context": preferences,
        "status": "ready" if profile and preferences else "preparing",
    }


class ReportRequest(StrictModel):
    request_id: UUID


@api_router.post("/companies/{company_id}/reports", status_code=202)
def generate_report(company_id: str, request: ReportRequest, user: UUID = Depends(current_user)):
    membership(company_id, user)
    report = create_report(company_id, user, "on_demand", f"on-demand:{user}:{request.request_id}")
    ranking = request_ranking(report)
    return {
        "report": report,
        "ranking_request": ranking,
        "status": "awaiting_ranker" if ranking else "awaiting_profile",
    }


@api_router.get("/companies/{company_id}/reports")
def reports(company_id: str, user: UUID = Depends(current_user)):
    membership(company_id, user)
    return all_rows(
        "SELECT r.*,v.version,v.report_version_id,v.summary,v.limitations,v.published_at, "
        "coalesce(v.input_snapshot @> '{\"deadline_fallback\":true}'::jsonb,false) AS availability_notice "
        "FROM reports r LEFT JOIN LATERAL (SELECT * FROM report_versions v WHERE v.report_id=r.report_id "
        "AND v.status='published' ORDER BY version DESC LIMIT 1) v ON true "
        "WHERE r.company_id=%s AND r.user_id=%s ORDER BY r.window_end DESC LIMIT 100",
        (company_id, user),
    )


def owned_report(report_id, user):
    row = one("SELECT * FROM reports WHERE report_id=%s AND user_id=%s", (report_id, user))
    if not row:
        raise HTTPException(404)
    membership(row["company_id"], user)
    return row


@api_router.get("/reports/{report_id}/ranking-requests")
def ranking_requests(report_id: UUID, user: UUID = Depends(current_user)):
    owned_report(report_id, user)
    return all_rows(
        "SELECT * FROM ranking_requests WHERE report_id=%s ORDER BY created_at DESC", (report_id,)
    )


@api_router.get("/reports/{report_id}/versions")
def versions(report_id: UUID, user: UUID = Depends(current_user)):
    owned_report(report_id, user)
    return all_rows(
        "SELECT * FROM report_versions WHERE report_id=%s AND status='published' ORDER BY version DESC",
        (report_id,),
    )


@api_router.get("/reports/{report_id}")
def report_detail(report_id: UUID, version: int | None = None, user: UUID = Depends(current_user)):
    report = owned_report(report_id, user)
    row = one(
        "SELECT * FROM report_versions WHERE report_id=%s AND status='published' "
        "AND (%s::integer IS NULL OR version=%s) ORDER BY version DESC LIMIT 1",
        (report_id, version, version),
    )
    if not row:
        return {"report": report, "status": "preparing", "cards": []}
    cards = all_rows(
        "SELECT i.* FROM insights i JOIN report_insights ri USING(insight_id) "
        "WHERE ri.report_version_id=%s ORDER BY ri.position",
        (row["report_version_id"],),
    )
    for card in cards:
        claims = all_rows(
            "SELECT * FROM insight_claims WHERE insight_id=%s ORDER BY created_at,claim_id",
            (card["insight_id"],),
        )
        for claim in claims:
            claim["sources"] = all_rows(
                "SELECT s.* FROM evidence_sources s JOIN insight_claim_sources cs USING(source_id) "
                "WHERE cs.claim_id=%s",
                (claim["claim_id"],),
            )
            claim["company_facts"] = all_rows(
                "SELECT f.* FROM company_facts f JOIN insight_claim_facts cf USING(fact_id) "
                "WHERE cf.claim_id=%s",
                (claim["claim_id"],),
            )
            for fact in claim["company_facts"]:
                fact["sources"] = all_rows(
                    "SELECT s.* FROM evidence_sources s JOIN fact_sources fs USING(source_id) "
                    "WHERE fs.fact_id=%s",
                    (fact["fact_id"],),
                )
        card["claims"] = claims
    context_metadata = one(
        "SELECT profile_version,published_at FROM company_profiles WHERE profile_id=%s", (row["profile_id"],)
    )
    return {
        "report": report,
        "version": row,
        "cards": cards,
        "company_context_metadata": context_metadata,
        "status": "availability_notice" if row["input_snapshot"].get("deadline_fallback") else "ready",
    }


class Question(StrictModel):
    request_id: UUID
    question: str = Field(min_length=1, max_length=2000)


@api_router.post("/reports/{report_id}/questions", status_code=202)
def ask(report_id: UUID, request: Question, user: UUID = Depends(current_user)):
    owned_report(report_id, user)
    version = one(
        "SELECT * FROM report_versions WHERE report_id=%s AND status='published' ORDER BY version DESC LIMIT 1",
        (report_id,),
    )
    if not version or not version["profile_id"]:
        raise HTTPException(409, "An analytical report is not ready yet")
    payload = {
        "report_version_id": str(version["report_version_id"]),
        "user_id": str(user),
        "question": request.question,
    }
    job = enqueue("question", f"question:{user}:{request.request_id}", payload)
    return {"job_id": job["job_id"], "status": job["status"]}


@api_router.get("/reports/{report_id}/discussions")
def discussions(report_id: UUID, user: UUID = Depends(current_user)):
    owned_report(report_id, user)
    return all_rows(
        "SELECT d.discussion_id,d.report_version_id,m.* FROM discussions d "
        "JOIN report_versions v USING(report_version_id) JOIN discussion_messages m USING(discussion_id) "
        "WHERE v.report_id=%s AND d.user_id=%s ORDER BY m.created_at",
        (report_id, user),
    )


@api_router.get("/jobs/{job_id}")
def job_status(job_id: UUID, user: UUID = Depends(current_user)):
    row = one("SELECT * FROM jobs WHERE job_id=%s", (job_id,))
    if not row:
        raise HTTPException(404)
    payload = row["payload"]
    if str(payload.get("user_id")) != str(user):
        raise HTTPException(404)
    if payload.get("company_id"):
        membership(payload["company_id"], user)
    elif payload.get("report_version_id"):
        report = one(
            "SELECT report_id FROM report_versions WHERE report_version_id=%s",
            (payload["report_version_id"],),
        )
        owned_report(report["report_id"], user)
    return {k: row[k] for k in ("job_id", "kind", "status", "attempts", "last_error", "result")}


@api_router.post("/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: UUID, user: UUID = Depends(current_user)):
    row = job_status(job_id, user)
    if row["status"] != "failed":
        raise HTTPException(409, "Only failed jobs can be retried")
    execute(
        "UPDATE jobs SET status='pending',attempts=0,available_at=now(),finished_at=NULL "
        "WHERE job_id=%s AND status='failed'",
        (job_id,),
    )
    return {"job_id": job_id, "status": "pending"}

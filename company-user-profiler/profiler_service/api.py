from fastapi import Depends, FastAPI

from agent_common.contracts import ProfileRequest
from agent_common.core import enqueue, internal, membership, one
from agent_common.http_limits import BodyLimit


def create_profiler():
    app = FastAPI(title="Company-user profiler")
    app.add_middleware(BodyLimit, limit=128 * 1024)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/runs", status_code=202, dependencies=[Depends(internal)])
    def run(request: ProfileRequest):
        membership(request.company_id, request.user_id, write=True)
        answer = one(
            "SELECT answer_id FROM onboarding_answers WHERE company_id=%s AND user_id=%s "
            "ORDER BY created_at DESC LIMIT 1",
            (request.company_id, request.user_id),
        )
        payload = {
            "company_id": request.company_id,
            "user_id": str(request.user_id),
            "answer_id": str(answer["answer_id"]) if answer else None,
        }
        job = enqueue("profile", "profile-request:" + str(request.request_id), payload)
        return {"job_id": job["job_id"], "status": job["status"]}

    return app

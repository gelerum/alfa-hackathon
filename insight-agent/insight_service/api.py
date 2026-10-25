from fastapi import Depends, FastAPI

from insight_service.service import accept_insight
from agent_common.contracts import InsightRequest
from agent_common.core import internal
from agent_common.http_limits import BodyLimit


def create_insight():
    app = FastAPI(title="Insight Agent")
    app.add_middleware(BodyLimit, limit=128 * 1024)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/runs", status_code=202, dependencies=[Depends(internal)])
    def run(request: InsightRequest):
        return accept_insight(request)

    return app

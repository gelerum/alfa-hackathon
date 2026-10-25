import logging

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import ValidationError

from agent_common.contracts import ToolCall
from agent_common.core import internal
from agent_common.http_limits import BodyLimit
from tool_gateway_service.tools import dispatch

app = FastAPI(title="Alpha Tool Gateway", version="0.1.0")
app.add_middleware(BodyLimit, limit=128 * 1024)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/tools/{name}", dependencies=[Depends(internal)])
def call(name: str, request: ToolCall):
    try:
        return dispatch(name, request.scope, request.arguments)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except httpx.HTTPError as exc:
        logging.exception("Tool upstream unavailable: %s", name)
        raise HTTPException(502, "Tool upstream unavailable") from exc

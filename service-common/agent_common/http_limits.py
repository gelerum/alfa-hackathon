from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse


class BodyLimitExceeded(HTTPException):
    def __init__(self):
        super().__init__(413, "Request body exceeds limit")


class BodyLimit:
    """Limit actual streamed request bytes, not only the client-supplied Content-Length."""

    def __init__(self, app, limit=1024 * 1024):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        length = dict(scope.get("headers", [])).get(b"content-length")
        if length:
            try:
                invalid = int(length) < 0
                oversized = int(length) > self.limit
            except ValueError:
                invalid, oversized = True, False
            if invalid or oversized:
                return await JSONResponse(
                    {"detail": "Invalid or oversized request body"}, status_code=400 if invalid else 413
                )(scope, receive, send)
        used = 0
        started = False

        async def limited_receive():
            nonlocal used
            message = await receive()
            used += len(message.get("body", b""))
            if used > self.limit:
                raise BodyLimitExceeded()
            return message

        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except BodyLimitExceeded:
            if not started:
                await JSONResponse({"detail": "Request body exceeds limit"}, status_code=413)(
                    scope, receive, send
                )

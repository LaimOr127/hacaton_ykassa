import re

from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from .payment_documents import PDF_LIMIT


class LimitPostBody:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)

        path = scope["path"]
        limit = 20_000
        if path == "/merchant/catalog":
            limit = 80_000
        elif re.fullmatch(r"/merchant/orders/[a-f0-9]{32}/receipts/[a-f0-9]{32}", path):
            limit = PDF_LIMIT + 8192
        size = Headers(scope=scope).get("content-length")
        rejected = JSONResponse(
            {"code": "request_too_large", "message": "Запрос слишком длинный."},
            status_code=413,
        )
        if size is not None and (
            not size.isascii() or not size.isdecimal() or len(size) > len(str(limit)) or int(size) > limit
        ):
            return await rejected(scope, receive, send)
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            chunk = event.get("body", b"")
            if len(body) + len(chunk) > limit:
                return await rejected(scope, receive, send)
            body.extend(chunk)
            if not event.get("more_body"):
                break

        async def replay():
            if body is not None:
                return pop_body()
            return await receive()

        def pop_body():
            nonlocal body
            payload = bytes(body)
            body = None
            return {"type": "http.request", "body": payload, "more_body": False}

        return await self.app(scope, replay, send)

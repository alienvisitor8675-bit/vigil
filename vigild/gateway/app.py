"""The gateway's HTTP face: a separate listener from the REST API, so API
tokens never work here and session tokens never work there. Workers reach
it at http://host.docker.internal:8788 through their egress proxy, with
ANTHROPIC_BASE_URL pointing here and the session token as the auth token."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from vigild.gateway.service import Gateway, GatewayError


def _token(request: Request) -> str | None:
    key = request.headers.get("x-api-key")
    if key:
        return key
    auth = request.headers.get("authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else None


def create_gateway_app(gw: Gateway) -> FastAPI:
    app = FastAPI(title="vigil gateway", docs_url=None, redoc_url=None, openapi_url=None)

    async def _proxy(request: Request, path: str) -> Response:
        try:
            gs = gw.authorize(_token(request))
            raw = await request.body()
            headers = {k.lower(): v for k, v in request.headers.items()}
            status, out_headers, body = await gw.forward(gs, path, headers, raw)
        except GatewayError as e:
            return JSONResponse(e.body(), status_code=e.status)
        if isinstance(body, bytes):
            return Response(body, status_code=status, headers=out_headers)
        stream: AsyncIterator[bytes] = body
        return StreamingResponse(stream, status_code=status, headers=out_headers)

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        return await _proxy(request, "/v1/messages")

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request) -> Response:
        return await _proxy(request, "/v1/messages/count_tokens")

    return app

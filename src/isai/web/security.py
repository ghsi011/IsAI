"""Web security for the localhost single-user GUI.

Defenses (right-sized per the spec): loopback-only binding (enforced at server
start), a random per-run access token required on every request (the primary
CSRF/DNS-rebinding defense), Host-header validation, restrictive security
headers, and no-store on everything dynamic. All assets are local; the CSP
allows no external origins and no inline script.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

TOKEN_QUERY_PARAM = "token"  # noqa: S105 - parameter name, not a secret
TOKEN_HEADER = "x-isai-token"  # noqa: S105 - header name, not a secret

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

_STATIC_CACHE = "private, max-age=300"


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def _allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({"127.0.0.1", "localhost", f"127.0.0.1:{port}", f"localhost:{port}"})


class SecurityMiddleware(BaseHTTPMiddleware):
    """Token + Host validation and security headers on every response."""

    def __init__(self, app: ASGIApp, *, token: str, port: int) -> None:
        super().__init__(app)
        self._token = token
        self._hosts = _allowed_hosts(port)

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host = request.headers.get("host", "")
        if host not in self._hosts:
            return self._deny("invalid Host header")
        presented = request.query_params.get(TOKEN_QUERY_PARAM) or request.headers.get(
            TOKEN_HEADER, ""
        )
        if not presented or not hmac.compare_digest(presented, self._token):
            return self._deny("missing or invalid access token")
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers[name] = value
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = _STATIC_CACHE
        return response

    @staticmethod
    def _deny(reason: str) -> JSONResponse:
        response = JSONResponse({"error": "web_security", "detail": reason}, status_code=403)
        for name, value in _SECURITY_HEADERS.items():
            response.headers[name] = value
        return response

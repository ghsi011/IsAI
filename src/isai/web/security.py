"""Web security for the localhost single-user GUI.

Defenses (right-sized per the spec): loopback-only binding (enforced at server
start), a random per-run access token required on every request (the primary
CSRF/DNS-rebinding defense), Host-header validation, restrictive security
headers, and no-store on everything dynamic. All assets are local; the CSP
allows no external origins and no inline script.

Implemented as a pure ASGI middleware — Starlette's BaseHTTPMiddleware buffers
response bodies, which would stall the SSE stream.
"""

from __future__ import annotations

import hmac
import json
import secrets
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Message, Receive, Scope, Send

TOKEN_QUERY_PARAM = "token"  # noqa: S105 - parameter name, not a secret
TOKEN_HEADER = b"x-isai-token"

_SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
    (
        b"content-security-policy",
        b"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        b"connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'self'; "
        b"frame-ancestors 'none'",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
]

_NO_STORE = (b"cache-control", b"no-store")
_STATIC_CACHE = (b"cache-control", b"private, max-age=300")


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def _allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({"127.0.0.1", "localhost", f"127.0.0.1:{port}", f"localhost:{port}"})


class SecurityMiddleware:
    """Token + Host validation and security headers on every response."""

    def __init__(self, app: ASGIApp, *, token: str, port: int) -> None:
        self.app = app
        self._token = token
        self._hosts = _allowed_hosts(port)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        host = headers.get(b"host", b"").decode("latin-1")
        if host not in self._hosts:
            await self._deny(send, "invalid Host header")
            return

        presented = headers.get(TOKEN_HEADER, b"").decode("latin-1")
        if not presented:
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            values = query.get(TOKEN_QUERY_PARAM, [])
            presented = values[0] if values else ""
        if not presented or not hmac.compare_digest(presented, self._token):
            await self._deny(send, "missing or invalid access token")
            return

        is_static = scope.get("path", "").startswith("/static/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                extra = list(_SECURITY_HEADERS)
                extra.append(_STATIC_CACHE if is_static else _NO_STORE)
                message["headers"] = list(message.get("headers") or []) + extra
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    async def _deny(send: Send, reason: str) -> None:
        body = json.dumps({"error": "web_security", "detail": reason}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    *_SECURITY_HEADERS,
                    _NO_STORE,
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

"""Response headers that tell browsers and proxies how to treat what the API sends (v1.17.5).

Added to EVERY response, including errors from the other middleware (413 body too large, 429 rate limit):

* X-Content-Type-Options: nosniff        a browser must not guess a different type for the body
* Cache-Control: no-store                 a proxy / CDN / browser must not keep a copy of business data (invoices, PDFs,
                                          customer lists, tokens). Only when the route did not choose its own value: the
                                          ticket photos and the shop logo / signature keep theirs.
* Referrer-Policy: no-referrer
* X-Frame-Options: DENY and a Content-Security-Policy of `default-src 'none'; frame-ancestors 'none'`
                                          the API serves data, never a page: nothing may be framed, loaded or run from
                                          it. Left off the interactive docs (/docs, /redoc), which need scripts, when
                                          ENABLE_DOCS is on.
* Strict-Transport-Security               only when HSTS_MAX_AGE is set (seconds). Off by default because it is only right
                                          once the API is reachable over HTTPS alone; then use 31536000.

None of this changes a status code or a body, and the Android app ignores all of it.
"""
from app.config import settings

_DOC_PATHS = ("/docs", "/redoc", "/openapi.json")


class SecurityHeadersMiddleware:
    def __init__(self, app, hsts_max_age: int | None = None):
        self.app = app
        self.hsts_max_age = settings.hsts_max_age if hsts_max_age is None else hsts_max_age

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        is_docs = path in _DOC_PATHS or path.endswith("/openapi.json")

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                present = {name.lower() for name, _ in message.get("headers", [])}
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                ]
                if b"cache-control" not in present:
                    extra.append((b"cache-control", b"no-store"))
                if not is_docs:
                    extra.append((b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"))
                if self.hsts_max_age > 0:
                    extra.append((b"strict-transport-security", f"max-age={self.hsts_max_age}".encode()))
                message = {**message, "headers": [*message.get("headers", []),
                                                   *((n, v) for n, v in extra if n not in present)]}
            await send(message)

        await self.app(scope, receive, send_with_headers)

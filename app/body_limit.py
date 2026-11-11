"""Refuses oversized request bodies BEFORE they are read (v1.13.2).

Starlette parses a whole multipart upload (spooling every file to disk) before an endpoint can look at it, so a size
check inside the endpoint comes too late. This middleware looks at the declared Content-Length first (an oversized
request is answered with 413 without reading a byte) and counts the bytes of a request that doesn't declare one, so a
chunked upload is cut off as soon as it passes its limit.

Limits: ordinary JSON requests MAX_BODY_KB (default 2048 KB); photo uploads on a ticket (attend / addendum; up to MAX_PHOTOS photos of
MAX_UPLOAD_MB each, plus 1 MB for the form); the shop logo MAX_LOGO_MB plus 1 MB; a purchase attachment MAX_ATTACHMENT_MB plus 1 MB; a profile image MAX_PROFILE_IMAGE_MB plus 1 MB; the base64 signature MAX_SIGNATURE_MB x 4/3 plus 1 MB; a database restore MAX_RESTORE_MB plus 1 MB.
"""
import re

from app.config import settings

_MB = 1024 * 1024
_ATTEND = re.compile(r"^/api(?:/v1)?/service-tickets/[^/]+/(?:attend|addendum)/?$")
_RESTORE = re.compile(r"^/api(?:/v1)?/admin/restore/?$")
_LOGO = re.compile(r"^/api(?:/v1)?/store-profile/logo/?$")
_ATTACHMENT = re.compile(r"^/api(?:/v1)?/purchases/[^/]+/attachment/?$")
_PROFILE_IMAGE = re.compile(r"^/api(?:/v1)?/profile/image/?$")
_SIGNATURE = re.compile(r"^/api(?:/v1)?/store-profile/signature/?$")


def limit_for(path: str) -> int:
    """Largest request body, in bytes, accepted for this path."""
    if _RESTORE.match(path):
        return (settings.max_restore_mb + 1) * _MB
    if _LOGO.match(path):
        return (settings.max_logo_mb + 1) * _MB
    if _ATTACHMENT.match(path):
        return (settings.max_attachment_mb + 1) * _MB
    if _PROFILE_IMAGE.match(path):
        return (settings.max_profile_image_mb + 1) * _MB
    if _SIGNATURE.match(path):
        return (settings.max_signature_mb * 4 // 3 + 1) * _MB  # base64 is a third bigger than the image it carries
    if _ATTEND.match(path):
        from app.routers.tickets import MAX_PHOTOS  # imported late: the routers import this package's settings

        return (MAX_PHOTOS * settings.max_upload_mb + 1) * _MB
    return settings.max_body_kb * 1024


def _describe(limit: int) -> str:
    return f"{limit // _MB} MB" if limit >= _MB else f"{limit // 1024} KB"


async def _reject(send, limit: int) -> None:
    body = ('{"error": "The request is too large (the limit here is %s)"}' % _describe(limit)).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})


class BodyLimitMiddleware:
    """Pure ASGI middleware (no BaseHTTPMiddleware: it must see the raw receive channel)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)

        limit = limit_for(scope["path"])
        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    break
                if declared > limit:
                    return await _reject(send, limit)
                break

        received = 0
        started = False   # the application has begun its own response
        rejected = False  # we answered 413 ourselves; whatever the application does next is discarded

        async def counting_receive():
            nonlocal received, rejected
            if rejected:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit and not started:
                    # Answer right here instead of raising: FastAPI turns an exception raised while it reads the
                    # body into its own 400. The application then sees a disconnect and stops reading.
                    rejected = True
                    await _reject(send, limit)
                    return {"type": "http.disconnect"}
            return message

        async def tracking_send(message):
            nonlocal started
            if rejected:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counting_receive, tracking_send)
        except Exception:
            if not rejected:
                raise

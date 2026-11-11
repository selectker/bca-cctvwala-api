"""The real client address, even behind a reverse proxy (v1.17.1).

Rate limits, sign-in lock-outs and the audit log all use `request.client.host`. This middleware sets it:

* A request that does NOT come from a trusted proxy (TRUSTED_PROXIES) keeps the address it really came from, and any
  X-Forwarded-For / CLIENT_IP_HEADER it carries is ignored. A client cannot pick its own address.
* A request from a trusted proxy gets the address the proxy saw: CLIENT_IP_HEADER when that is configured, otherwise
  the X-Forwarded-For chain read from the RIGHT, skipping trusted proxies; the first address that is not one is the
  client. (The left-hand entries are whatever the client wrote and are never trusted.)

Pure ASGI, so it runs before the rate limiter. It replaces uvicorn's `--proxy-headers` for this purpose: with
`--forwarded-allow-ips '*'` uvicorn believes the client's own header, which is exactly what this avoids (start uvicorn
with `--no-proxy-headers`, as in the README; if it is left on with the default 127.0.0.1 it does no harm).
"""
import ipaddress

from app.config import settings


def _parse(text: str):
    try:
        ip = ipaddress.ip_address(text.strip())
    except ValueError:
        return None
    return ip.ipv4_mapped if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped else ip


class ClientIPMiddleware:
    def __init__(self, app, networks=None, header: str | None = None):
        self.app = app
        self.networks = list(settings.trusted_proxy_networks if networks is None else networks)
        configured = settings.client_ip_header if header is None else header
        self.header = configured.lower().encode() if configured else None

    def _trusted(self, ip) -> bool:
        return any(ip in network for network in self.networks)

    def _from_headers(self, headers):
        if self.header:
            for name, value in headers:
                if name == self.header:
                    return _parse(value.decode("latin-1"))
            return None
        chain: list[str] = []
        for name, value in headers:
            if name == b"x-forwarded-for":
                chain.extend(part for part in value.decode("latin-1").split(",") if part.strip())
        for part in reversed(chain):
            ip = _parse(part)
            if ip is None:
                return None  # garbage can only be client-supplied: don't guess, keep the proxy's own address
            if not self._trusted(ip):
                return ip
        return None

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and scope.get("client"):
            peer = _parse(scope["client"][0])
            if peer is not None and self._trusted(peer):
                real = self._from_headers(scope["headers"])
                if real is not None:
                    scope = dict(scope)
                    scope["client"] = (str(real), 0)
        await self.app(scope, receive, send)

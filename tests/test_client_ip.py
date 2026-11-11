"""v1.17.1: the real client address behind a proxy, per-worker rate limits, literal search text, legacy default."""
import asyncio
import ipaddress

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.client_ip import ClientIPMiddleware
from app.config import Settings
from app.limiter import per_worker
from app.utils.search import SearchTerm, like_escape

PROXY = [ipaddress.ip_network("10.0.0.0/24")]


def seen_client(peer: str, headers: dict[str, str], *, networks=PROXY, header: str | None = None):
    """The `client` the application underneath ends up seeing."""
    captured = {}

    async def inner(scope, receive, send):
        captured["client"] = scope["client"]

    scope = {
        "type": "http", "client": (peer, 5000),
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    asyncio.run(ClientIPMiddleware(inner, networks=networks, header=header)(scope, None, None))
    return captured["client"][0]


def test_untrusted_peer_cannot_choose_its_address():
    # A client talking to the API directly (or through anything not listed) keeps the address it really has.
    assert seen_client("203.0.113.9", {"X-Forwarded-For": "1.2.3.4"}) == "203.0.113.9"
    assert seen_client("203.0.113.9", {"X-Forwarded-For": "1.2.3.4"}, header="X-Real-IP") == "203.0.113.9"


def test_trusted_proxy_gives_the_client_address():
    assert seen_client("10.0.0.5", {"X-Forwarded-For": "198.51.100.7"}) == "198.51.100.7"


def test_forged_left_hand_entries_are_ignored():
    # The client wrote "9.9.9.9"; the proxy appended the address it actually saw.
    assert seen_client("10.0.0.5", {"X-Forwarded-For": "9.9.9.9, 198.51.100.7"}) == "198.51.100.7"


def test_chain_of_trusted_proxies_is_skipped():
    assert seen_client("10.0.0.5", {"X-Forwarded-For": "198.51.100.7, 10.0.0.9"}) == "198.51.100.7"
    assert seen_client("10.0.0.5", {"X-Forwarded-For": "198.51.100.7", "x-forwarded-for": "10.0.0.9"}) == "198.51.100.7"


def test_missing_or_garbage_header_keeps_the_proxy_address():
    assert seen_client("10.0.0.5", {}) == "10.0.0.5"
    assert seen_client("10.0.0.5", {"X-Forwarded-For": "not-an-ip"}) == "10.0.0.5"
    assert seen_client("10.0.0.5", {"X-Forwarded-For": "10.0.0.8"}) == "10.0.0.5"  # only proxies in the chain


def test_single_address_header_when_configured():
    headers = {"CF-Connecting-IP": "198.51.100.7", "X-Forwarded-For": "9.9.9.9"}
    assert seen_client("10.0.0.5", headers, header="CF-Connecting-IP") == "198.51.100.7"
    assert seen_client("10.0.0.5", {"X-Forwarded-For": "9.9.9.9"}, header="CF-Connecting-IP") == "10.0.0.5"


def test_ipv6_and_mapped_addresses():
    nets = [ipaddress.ip_network("::1/128"), ipaddress.ip_network("127.0.0.1/32")]
    assert seen_client("::1", {"X-Forwarded-For": "2001:db8::7"}, networks=nets) == "2001:db8::7"
    assert seen_client("::ffff:127.0.0.1", {"X-Forwarded-For": "198.51.100.7"}, networks=nets) == "198.51.100.7"


def _settings(**env):
    base = dict(db_host="h", db_name="d", db_user="u", db_password="p", jwt_secret="x" * 40,
                r2_account_id="a", r2_access_key_id="k", r2_secret_access_key="s", r2_bucket="b")
    return Settings(_env_file=None, **{**base, **env})


@pytest.mark.parametrize("bad", ["*", "0.0.0.0/0", "::/0", "nonsense", "10.0.0.1, *"])
def test_config_refuses_trust_everything(bad):
    with pytest.raises(ValidationError):
        _settings(trusted_proxies=bad)


def test_config_accepts_proxy_lists_and_defaults():
    assert _settings().trusted_proxies == "127.0.0.1,::1"
    s = _settings(trusted_proxies="10.0.0.0/24, 192.168.1.5", client_ip_header="CF-Connecting-IP")
    assert len(s.trusted_proxy_networks) == 2 and s.client_ip_header == "CF-Connecting-IP"
    with pytest.raises(ValidationError):
        _settings(client_ip_header="Bad Header!")
    with pytest.raises(ValidationError):
        _settings(api_workers=0)


def test_legacy_api_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("LEGACY_API_ENABLED", raising=False)
    assert _settings().legacy_api_enabled is False
    assert _settings(legacy_api_enabled=True).legacy_api_enabled is True


def test_rate_limits_are_split_across_workers(monkeypatch):
    from app import limiter

    monkeypatch.setattr(limiter.settings, "api_workers", 1)
    assert per_worker("10/minute") == "10/minute"
    monkeypatch.setattr(limiter.settings, "api_workers", 2)
    assert per_worker("10/minute") == "5/minute"
    assert per_worker("500/15minutes") == "250/15minutes"
    assert per_worker("5/minute") == "3/minute"  # rounded up: the total stays at about the configured value
    monkeypatch.setattr(limiter.settings, "api_workers", 4)
    assert per_worker("2/minute") == "1/minute"  # never below one
    assert per_worker("unlimited") == "unlimited"


def test_search_text_is_matched_literally():
    assert like_escape("100%_off\\now") == "100\\%\\_off\\\\now"
    assert like_escape("plain") == "plain" and like_escape(None) is None

    app = FastAPI()

    @app.get("/s")
    def s(search: SearchTerm = None):
        return {"search": search}

    client = TestClient(app)
    assert client.get("/s", params={"search": "50%_x"}).json() == {"search": "50\\%\\_x"}
    assert client.get("/s").json() == {"search": None}
    assert client.get("/s", params={"search": "a" * 101}).status_code == 422
    assert client.get("/s", params={"search": "a" * 100}).status_code == 200


def test_every_ilike_in_the_api_declares_its_escape_character():
    # A new search added without ESCAPE would treat % and _ as wildcards again.
    import pathlib
    import re

    pattern = re.compile(r"ILIKE '%'\|\|\$(?:\{0\}|\d+)\|\|'%'(?! ESCAPE '\\\\')")
    offenders = [str(p) for p in pathlib.Path("app").rglob("*.py") if pattern.search(p.read_text())]
    assert not offenders, f"ILIKE without ESCAPE in: {offenders}"


def test_escaped_search_matches_literally_in_postgres():
    from tests.helpers import raw_connect

    async def run():
        conn = await raw_connect()
        try:
            names = ["50% Off Traders", "500 Traders", "A_B Stores", "AxB Stores", "back\\slash Co"]
            rows = {}
            for term in ("50%", "_", "A_B", "\\", "traders"):
                rows[term] = {
                    r[0] for r in await conn.fetch(
                        "SELECT n FROM unnest($2::text[]) AS n WHERE n ILIKE '%'||$1||'%' ESCAPE '\\'",
                        like_escape(term), names,
                    )
                }
            return rows
        finally:
            await conn.close()

    rows = asyncio.run(run())
    assert rows["50%"] == {"50% Off Traders"}                      # % is not "anything"
    assert rows["_"] == {"A_B Stores"}                             # _ is not "any one character"
    assert rows["A_B"] == {"A_B Stores"}                           # so AxB no longer matches A_B
    assert rows["\\"] == {"back\\slash Co"}                        # a typed backslash is just a backslash
    assert rows["traders"] == {"50% Off Traders", "500 Traders"}   # ordinary, case-insensitive search unchanged

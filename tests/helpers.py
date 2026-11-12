"""Small helpers shared by the end-to-end tests."""


def first_login(client, user, new_pin="482916"):
    """Sign a freshly created (or PIN-reset) staff account in the way the app must: with the one-time PIN, which only
    allows changing it. Returns a token that can be used for everything else."""
    resp = client.post("/api/v1/auth/login-pin", json={"account_id": user["id"], "pin": user["pin"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["must_change_pin"] is True
    token = resp.json()["token"]
    if new_pin == user["pin"]:
        new_pin = "135790"
    changed = client.post("/api/v1/auth/change-pin", headers={"Authorization": f"Bearer {token}"},
                          json={"current_pin": user["pin"], "new_pin": new_pin})
    assert changed.status_code == 200, changed.text
    return changed.json()["token"]


async def raw_connect():
    """A plain asyncpg connection to the test database (for tests that talk to PostgreSQL directly)."""
    import asyncpg

    from app.config import settings
    from app.db import build_ssl_context

    return await asyncpg.connect(
        host=settings.db_host, port=settings.db_port, database=settings.db_name,
        user=settings.db_user, password=settings.db_password, ssl=build_ssl_context(),
    )

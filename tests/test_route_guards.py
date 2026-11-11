"""Every /api/v1 route needs a sign-in (and most an OWNER role) unless it is on this short, deliberate list.

Written after a new router shipped in development with its GET routes unguarded: a missing router-level dependency is
easy to do and the compiler can't see it. No database needed.
"""
from app.main import app

PUBLIC = {
    ("POST", "/auth/bootstrap-admin"), ("POST", "/auth/bootstrap-owner"), ("GET", "/auth/bootstrap-status"),
    ("POST", "/auth/login"), ("POST", "/auth/login-pin"),
}


def _guard(route):
    deps = []

    def walk(dependant):
        for sub in dependant.dependencies:
            deps.append(sub.call)
            walk(sub)

    walk(route.dependant)
    names = [getattr(c, "__qualname__", "") for c in deps]
    if any("require_role" in n for n in names):
        return "role"
    return "signed-in" if any(n.endswith("get_current_user") for n in names) else "public"


def _v1_routes():
    for entry in app.routes:
        ctx = getattr(entry, "include_context", None)
        if ctx is not None and ctx.prefix == "/api/v1":
            yield from ctx.included_router.routes


def test_only_the_intended_routes_are_public():
    public = {(sorted(r.methods)[0], r.path) for r in _v1_routes() if _guard(r) == "public"}
    assert public == PUBLIC, f"unguarded routes: {sorted(public - PUBLIC)}; no longer public: {sorted(PUBLIC - public)}"


def test_the_money_routers_are_owner_only():
    for r in _v1_routes():
        if r.path.startswith(("/expenses", "/reports", "/customers", "/suppliers", "/invoices", "/purchases", "/quotations")):
            assert _guard(r) == "role", f"{sorted(r.methods)} {r.path} is not role-guarded"

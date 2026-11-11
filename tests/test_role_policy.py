"""Role policy: every business route is OWNER-only unless it is explicitly listed below.

Needs no database -- it inspects the dependency tree of every registered route.
"""
import inspect
import os

for _k, _v in {"DB_HOST": "x", "DB_NAME": "x", "DB_USER": "x", "DB_PASSWORD": "x", "JWT_SECRET": "x" * 32}.items():
    os.environ.setdefault(_k, _v)

from fastapi.routing import APIRoute  # noqa: E402

from app.main import app  # noqa: E402

FIELD = {"OWNER", "STAFF", "TECHNICIAN"}

# (METHOD, path) -> roles allowed. Anything not listed here must be exactly {"OWNER"}.
ALLOWED = {
    # public
    ("GET", "/health"): None,
    ("GET", "/api/v1/version"): None,
    ("GET", "/api/v1/auth/bootstrap-status"): None,
    ("POST", "/api/v1/auth/bootstrap-admin"): None,
    ("POST", "/api/v1/auth/bootstrap-owner"): None,
    ("POST", "/api/v1/auth/login"): None,
    ("POST", "/api/v1/auth/login-pin"): None,
    # any signed-in account (own profile / pickers / read-only store header)
    ("GET", "/api/v1/auth/me"): "ANY",
    ("GET", "/api/v1/auth/team"): "ANY",
    ("POST", "/api/v1/auth/change-pin"): "ANY",
    ("GET", "/api/v1/store-profile"): "ANY",
    ("GET", "/api/v1/profile"): "ANY",
    ("PUT", "/api/v1/profile"): "ANY",
    ("POST", "/api/v1/profile/change-password"): "ANY",
    ("GET", "/api/v1/profile/activity"): "ANY",
    ("GET", "/api/v1/profile/image"): "ANY",
    ("POST", "/api/v1/profile/image"): "ANY",
    ("DELETE", "/api/v1/profile/image"): "ANY",
    ("GET", "/api/v1/auth/team/{user_id}/image"): "ANY",
    # field work
    ("GET", "/api/v1/amc/my-visits"): FIELD,
    ("PUT", "/api/v1/amc/visits/{visit_id}"): FIELD,
}
# Whole routers with their own policy (checked elsewhere: tickets by role per route, admin by platform admin).
OWN_POLICY_PREFIXES = ("/api/v1/service-tickets", "/api/v1/admin")


def _guards(dependant):
    """Yield (kind, roles) for every auth dependency in the tree."""
    for dep in dependant.dependencies:
        fn = dep.call
        name = getattr(fn, "__qualname__", "")
        if name.startswith("require_role.<locals>"):
            yield "roles", set(inspect.getclosurevars(fn).nonlocals["roles"])
        elif name == "get_current_user":
            yield "any", None
        elif name == "require_platform_admin":
            yield "platform_admin", None
        yield from _guards(dep)


def _policy(route):
    kinds = list(_guards(route.dependant))
    roles = [r for k, r in kinds if k == "roles"]
    if roles:
        return set.intersection(*roles)  # all guards must pass
    if any(k == "any" for k, _ in kinds):
        return "ANY"
    return None


def test_every_business_route_is_owner_only_unless_listed():
    problems = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith(("/api/v1/", "/health")):
            continue
        if route.path.startswith(OWN_POLICY_PREFIXES):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            actual = _policy(route)
            expected = ALLOWED.get((method, route.path), {"OWNER"})
            if actual != expected:
                problems.append(f"{method} {route.path}: expected {expected}, got {actual}")
    assert not problems, "\n".join(problems)

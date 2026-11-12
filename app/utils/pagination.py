from fastapi import Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse


class Page:
    """limit/offset query params shared by every list endpoint.

    Lists still return a plain JSON array (so existing clients keep working); the total
    number of matching rows is sent in the X-Total-Count header.
    Queries must select `COUNT(*) OVER() AS _total` and use a deterministic ORDER BY.
    """

    def __init__(self, limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0, le=100_000_000)):
        self.limit = limit
        self.offset = offset


def paged_response(rows, extra_drop: tuple[str, ...] = (), extra_headers: dict[str, str] | None = None) -> JSONResponse:
    total = int(rows[0]["_total"]) if rows else 0
    drop = {"_total", *extra_drop}
    items = [{k: v for k, v in dict(r).items() if k not in drop} for r in rows]
    headers = {"X-Total-Count": str(total), **(extra_headers or {})}
    return JSONResponse(content=jsonable_encoder(items), headers=headers)

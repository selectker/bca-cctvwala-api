"""Search boxes: the text a user types is matched literally (v1.17.1).

The list endpoints search with `column ILIKE '%' || $1 || '%'`. In a LIKE pattern `%` and `_` are wildcards, so typing
`%` matched every row and `_` any single character; a pattern such as `%_%_%_%_%` also made PostgreSQL do needless
work. SearchTerm escapes `\\`, `%` and `_` once, when the query parameter is read, and every ILIKE in the API says
`ESCAPE '\\'`, so what is typed is what is looked for.
"""
from typing import Annotated

from fastapi import Query
from pydantic import AfterValidator

MAX_SEARCH_LENGTH = 100


def _clean_search(value: str | None) -> str | None:
    """Reject a NUL byte (PostgreSQL can't store or compare it: it used to surface as a 500) and escape for LIKE."""
    if value is not None and "\x00" in value:
        raise ValueError("can't contain a null character")
    return like_escape(value)


def like_escape(value: str | None) -> str | None:
    """`value` with LIKE's special characters made literal (for use with ESCAPE '\\')."""
    if value is None:
        return None
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# A `search` query parameter: at most 100 characters, already escaped for ILIKE ... ESCAPE '\\'.
SearchTerm = Annotated[str | None, Query(max_length=MAX_SEARCH_LENGTH), AfterValidator(_clean_search)]

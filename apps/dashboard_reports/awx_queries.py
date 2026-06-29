"""
AWX database query helpers for dashboard reports filter dropdowns.

Provides low-level SQL helpers and higher-level fetch functions for retrieving
organizations, job templates, projects, and labels directly from the AWX database.
"""

import enum
import logging
from typing import Any

logger = logging.getLogger(__name__)


class AWXQuery(enum.Enum):
    """Enumeration of allowed AWX read-only SELECT queries."""

    ORGANIZATIONS = "SELECT id, name FROM main_organization"
    TEMPLATES = (
        "SELECT ujt.id, ujt.name "
        "FROM main_unifiedjobtemplate ujt "
        "JOIN main_jobtemplate jt on jt.unifiedjobtemplate_ptr_id = ujt.id"
    )
    PROJECTS = (
        "SELECT ujt.id, ujt.name "
        "FROM main_unifiedjobtemplate ujt "
        "JOIN main_project pj on pj.unifiedjobtemplate_ptr_id = ujt.id"
    )
    LABELS = "SELECT id, name FROM main_label"


def _build_where_clause(join_alias: str, search_str: str | None, pk: Any, ids: list[Any] | None = None) -> tuple[str, list[Any]]:
    """Build WHERE clause and parameters for SQL query."""
    where_clauses = []
    params = []
    if search_str:
        # Escape backslash first, then ILIKE wildcards, so user-supplied % and _ are treated literally.
        escaped = search_str.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where_clauses.append(f"{join_alias}name ilike %s ESCAPE E'\\\\'")
        params.append("%" + escaped + "%")
    if pk is not None:
        where_clauses.append(f"{join_alias}id = %s")
        params.append(pk)
    if ids:
        placeholders = ", ".join(["%s"] * len(ids))
        where_clauses.append(f"{join_alias}id IN ({placeholders})")
        params.extend(ids)
    clause = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""
    return clause, params


def _execute_db_query(db_connection, query: str, params: list[Any]) -> tuple[list[str], list[Any]]:
    """Execute SQL query and return columns and data."""
    with db_connection.cursor() as cursor:
        cursor.execute(query, params)
        columns = [col[0] for col in cursor.description]
        data = cursor.fetchall()
    return columns, data


def _execute_count_query(db_connection, count_query: str, params: list[Any]) -> int:
    """Execute a COUNT query and return the integer result."""
    with db_connection.cursor() as cursor:
        cursor.execute(count_query, params)
        return cursor.fetchone()[0]


def fetch_data_from_db(awx_query: AWXQuery, join_alias: str = "", **kwargs: Any) -> tuple[list[Any], int]:
    """
    Execute a parameterized SQL query against the AWX database with optional search, pk, limit, and offset filters.

    When ``limit`` is provided, runs a COUNT(*) subquery first to obtain the total matching row count,
    then fetches only the requested page via LIMIT/OFFSET — keeping full table scans out of Python memory.
    When ``limit`` is omitted (e.g. single-row retrieve by pk), returns all matching rows and derives
    the total from the result length.

    Returns ``(rows, total_count)``.
    """
    db_connection = kwargs.get("db_connection")
    search_str = kwargs.get("search_str")
    pk = kwargs.get("pk")
    ids = kwargs.get("ids")
    limit = kwargs.get("limit")
    offset = kwargs.get("offset", 0)

    base_query = awx_query.value
    where_clause, params = _build_where_clause(join_alias, search_str, pk, ids)
    order_clause = f" ORDER BY {join_alias}name"

    if limit is not None:
        count_query = f"SELECT COUNT(*) FROM ({base_query}{where_clause}) AS _count_subq"  # noqa: S608 base_query is an AWXQuery enum value (hardcoded literal); where_clause uses %s placeholders
        total = _execute_count_query(db_connection, count_query, params)
        query = base_query + where_clause + order_clause + " LIMIT %s OFFSET %s"
        _, data = _execute_db_query(db_connection, query, params + [limit, offset])
    else:
        query = base_query + where_clause + order_clause
        _, data = _execute_db_query(db_connection, query, params)
        total = len(data)

    return data, total


def format_id_name_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """Format rows as list of dicts with 'id' and 'name' keys."""
    return [{"id": row[0], "name": row[1]} for row in rows]


def fetch_id_name(
    awx_query: AWXQuery, join_alias: str = "", error_msg: str = "", **kwargs
) -> tuple[list[dict[str, Any]], int]:
    """
    Fetch id/name pairs from the AWX database and return ``(items, total_count)``.

    ``total_count`` is the DB-level COUNT when pagination params (``limit``/``offset``) are supplied,
    or the length of the result set otherwise.  Raises the underlying exception after logging on failure.
    """
    try:
        rows, total = fetch_data_from_db(awx_query, join_alias=join_alias, **kwargs)
    except Exception:
        logger.exception(error_msg)
        raise
    return format_id_name_rows(rows), total


def fetch_organizations(**kwargs) -> tuple[list[dict[str, Any]], int]:
    """Fetch organizations from DB, returning ``(items, total_count)``."""
    return fetch_id_name(AWXQuery.ORGANIZATIONS, error_msg="Error fetching organizations from AWX database", **kwargs)


def fetch_templates(**kwargs) -> tuple[list[dict[str, Any]], int]:
    """Fetch job templates from DB, returning ``(items, total_count)``."""
    return fetch_id_name(
        AWXQuery.TEMPLATES, join_alias="ujt.", error_msg="Error fetching job templates from AWX database", **kwargs
    )


def fetch_projects(**kwargs) -> tuple[list[dict[str, Any]], int]:
    """Fetch projects from DB, returning ``(items, total_count)``."""
    return fetch_id_name(
        AWXQuery.PROJECTS, join_alias="ujt.", error_msg="Error fetching projects from AWX database", **kwargs
    )


def fetch_labels(**kwargs) -> tuple[list[dict[str, Any]], int]:
    """Fetch labels from DB, returning ``(items, total_count)``."""
    return fetch_id_name(AWXQuery.LABELS, error_msg="Error fetching labels from AWX database", **kwargs)

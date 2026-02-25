import logging
from typing import Any

logger = logging.getLogger(__name__)


def fetch_data_from_db(db_connection, query: str) -> tuple[list[Any], Any]:
    cursor = db_connection.cursor()
    cursor.execute(query)
    columns = [col[0] for col in cursor.description]
    data = cursor.fetchall()
    cursor.close()
    return columns, data


def fetch_templates(*args, **kwargs) -> list[dict[str, Any]]:
    db_connection = kwargs.get("db_connection")
    search_str = kwargs.get("search_str", None)
    pk = kwargs.get("pk", None)

    query = f"""
             SELECT
                ujt.id,
                ujt.name
             FROM main_unifiedjobtemplate ujt
             JOIN main_jobtemplate jt on jt.unifiedjobtemplate_ptr_id = ujt.id
             WHERE 1=1
             {"AND ujt.name ILIKE '%" + search_str + "%'" if search_str else ""}
             {"AND ujt.id = " + str(pk) if pk else ""}
             order by ujt.name
            """
    try:
        columns, rows = fetch_data_from_db(db_connection, query)
    except Exception as e:
        logger.error(f"Error fetching job templates from AWX database: {str(e)}")
        return []
    return [
        {'id': row[0], 'name': row[1]}
        for row in rows
    ]


def fetch_projects(*args, **kwargs) -> list[dict[str, Any]]:
    db_connection = kwargs.get("db_connection")
    search_str = kwargs.get("search_str", None)
    pk = kwargs.get("pk", None)

    query = f"""
             SELECT
                ujt.id,
                ujt.name
             FROM main_unifiedjobtemplate ujt
             JOIN main_project pj on pj.unifiedjobtemplate_ptr_id = ujt.id
             WHERE 1=1
             {"AND ujt.name ILIKE '%" + search_str + "%'" if search_str else ""}
             {"AND ujt.id = " + str(pk) if pk else ""}
             order by ujt.name
            """
    try:
        columns, rows = fetch_data_from_db(db_connection, query)
    except Exception as e:
        logger.error(f"Error fetching projects from AWX database: {str(e)}")
        return []
    return [
        {'id': row[0], 'name': row[1]}
        for row in rows
    ]


def fetch_labels(*args, **kwargs) -> list[dict[str, Any]]:
    db_connection = kwargs.get("db_connection")
    search_str = kwargs.get("search_str", None)
    pk = kwargs.get("pk", None)

    query = f"""
             SELECT id, name
             FROM main_label
             WHERE 1=1
             {"AND name ILIKE '%" + search_str + "%'" if search_str else ""}
             {"AND id = " + str(pk) if pk else ""}
             order by name
            """
    try:
        columns, rows = fetch_data_from_db(db_connection, query)
    except Exception as e:
        logger.error(f"Error fetching labels from AWX database: {str(e)}")
        return []
    return [
        {'id': row[0], 'name': row[1]}
        for row in rows
    ]


def fetch_organizations(*args, **kwargs) -> list[dict[str, Any]]:
    db_connection = kwargs.get("db_connection")
    search_str = kwargs.get("search_str", None)
    pk = kwargs.get("pk", None)

    query = f"""
             SELECT id, name
             FROM main_organization
             WHERE 1=1
             {"AND name ILIKE '%" + search_str + "%'" if search_str else ""}
             {"AND id = " + str(pk) if pk else ""}
             order by name
            """
    try:
        columns, rows = fetch_data_from_db(db_connection, query)
    except Exception as e:
        logger.error(f"Error fetching labels from AWX database: {str(e)}")
        return []
    return [
        {'id': row[0], 'name': row[1]}
        for row in rows
    ]

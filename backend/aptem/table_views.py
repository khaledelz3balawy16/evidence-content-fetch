"""Read-only browsing of every table the project reads or writes."""
from django.db import connection
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from .permissions import IsAuthenticatedStaff


# key -> (schema, table, description, columns hidden from row output)
TABLES = {
    "evidence_items": (
        "fetching_evidence", "evidence_items",
        "Source evidence catalogue pulled from Aptem (read-only input).", (),
    ),
    "aptem_evidence": (
        "fetching_evidence", "aptem_aptemevidence",
        "Downloaded evidence files + AI analysis (Django model AptemEvidence).", ("extracted_text",),
    ),
    "cache_refresh_control": (
        "fetching_evidence", "aptem_cacherefreshcontrol",
        "Lease/cursor rows for the scheduled sync cycle.", (),
    ),
    "learners": (
        "Learner", "learners",
        "Learner profiles mirrored from Aptem.", (),
    ),
    "reviews": (
        "Learner", "reviews",
        "Modern reviews + signatures, document identity, lifecycle snapshot.", ("raw_text", "review_data"),
    ),
    "review_sections": (
        "Learner", "review_sections",
        "Sections extracted from each review.", (),
    ),
    "review_documents": (
        "Learner", "review_documents",
        "Review PDFs archived to Azure Blob Storage.",
        ("azure_account_url", "azure_container", "azure_blob_name", "azure_etag"),
    ),
    "review_pdf_sync_lease": (
        "Learner", "review_pdf_sync_lease",
        "Coordinator lease for the PDF sync worker.", ("owner_token",),
    ),
}

MAX_LIMIT = 200


def _qualified(schema, table):
    q = connection.ops.quote_name
    return f"{q(schema)}.{q(table)}"


def _columns(cursor, schema, table):
    cursor.execute(
        """select column_name, data_type, is_nullable = 'YES'
           from information_schema.columns
           where table_schema = %s and table_name = %s
           order by ordinal_position""",
        [schema, table],
    )
    return [{"name": n, "type": t, "nullable": nl} for n, t, nl in cursor.fetchall()]


@api_view(["GET"])
@permission_classes([IsAuthenticatedStaff])
def table_list(request):
    tables = []
    with connection.cursor() as cursor:
        for key, (schema, table, description, hidden) in TABLES.items():
            columns = _columns(cursor, schema, table)
            exists = bool(columns)
            row_count = None
            if exists:
                cursor.execute(f"select count(*) from {_qualified(schema, table)}")
                row_count = cursor.fetchone()[0]
            tables.append(
                {
                    "key": key,
                    "schema": schema,
                    "table": table,
                    "qualified_name": f'"{schema}".{table}',
                    "description": description,
                    "exists": exists,
                    "row_count": row_count,
                    "columns": columns,
                    "hidden_in_rows": list(hidden),
                    "rows_url": request.build_absolute_uri(f"{key}/"),
                }
            )
    return Response({"database": connection.settings_dict.get("NAME", ""), "tables": tables})


@api_view(["GET"])
@permission_classes([IsAuthenticatedStaff])
def table_rows(request, key):
    if key not in TABLES:
        return Response({"detail": f"Unknown table '{key}'."}, status=status.HTTP_404_NOT_FOUND)
    schema, table, _, hidden = TABLES[key]
    try:
        limit = min(max(int(request.query_params.get("limit", 50)), 1), MAX_LIMIT)
        offset = max(int(request.query_params.get("offset", 0)), 0)
    except ValueError:
        return Response({"detail": "limit and offset must be numbers."}, status=status.HTTP_400_BAD_REQUEST)

    with connection.cursor() as cursor:
        columns = _columns(cursor, schema, table)
        if not columns:
            return Response({"detail": f"Table {schema}.{table} does not exist."}, status=status.HTTP_404_NOT_FOUND)
        names = [c["name"] for c in columns if c["name"] not in hidden]
        order = "id" if any(c["name"] == "id" for c in columns) else names[0]
        q = connection.ops.quote_name
        select = ", ".join(q(n) for n in names)
        cursor.execute(
            f"select {select} from {_qualified(schema, table)} order by {q(order)} limit %s offset %s",
            [limit, offset],
        )
        rows = [dict(zip(names, row)) for row in cursor.fetchall()]
        cursor.execute(f"select count(*) from {_qualified(schema, table)}")
        total = cursor.fetchone()[0]

    return Response(
        {
            "key": key,
            "qualified_name": f'"{schema}".{table}',
            "total": total,
            "limit": limit,
            "offset": offset,
            "columns": names,
            "rows": rows,
        }
    )

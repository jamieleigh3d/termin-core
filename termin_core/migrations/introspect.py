# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""SQLite schema introspection.

Per docs/migration-classifier-design.md §3.2 — when the
`_termin_schema` metadata table is absent (first-time-v0.9 boot
against a v0.8 DB), we fall back to introspecting via
`sqlite_master` + PRAGMA. The introspector reconstructs an
IR-shaped content list that the classifier can compare against the
target IR.

Caveats called out in §6: CHECK constraints have to be parsed out
of the `sqlite_master.sql` text best-effort. Unrecognized clauses
are skipped with a warning; the classifier will then see them as
"added" and require ack.
"""

from __future__ import annotations

import re
from typing import Any, List, Mapping, Optional


# Maps SQLite declared type → IR business_type. Best-effort; the v0.8
# emit path uses the keys in storage._SQL_TYPES, so the inverse is
# computable.
_SQL_TO_BUSINESS_TYPE: dict = {
    "TEXT": "text",
    "INTEGER": "whole_number",
    "REAL": "number",
    "TIMESTAMP": "datetime",
    # Note: "boolean" was stored as INTEGER in v0.8; we can't
    # disambiguate from a real whole_number without IR. The
    # introspector returns "whole_number" — the classifier sees
    # "type_changed: whole_number → boolean" on the v0.9 IR if
    # that field is now declared boolean, which then classifies
    # as a lossless type change (medium risk, rebuild).
}


# Internal Termin tables that should NOT appear in the introspected
# content list (they're metadata, not user content).
_INTERNAL_TABLES: frozenset = frozenset({
    "_termin_schema",
    "sqlite_sequence",
})


async def introspect_sqlite_schema(db) -> List[Mapping[str, Any]]:
    """Reconstruct an IR-shaped content schema list from a live
    SQLite connection.

    Returns a list of dicts mirroring AppSpec.content's JSON shape:
        [
            {
                "name": {"snake": "tickets", "display": "tickets",
                         "pascal": "Tickets"},
                "fields": [
                    {"name": "title", "business_type": "text",
                     "required": True, "unique": False, ...},
                    ...
                ],
                "state_machines": [...],   # detected from columns named
                                            # in sqlite_sequence + DEFAULT
            },
            ...
        ]

    Best-effort. Returns empty list for an empty database.
    """
    cursor = await db.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    rows = await cursor.fetchall()

    out: list = []
    for row in rows:
        table_name = row["name"] if hasattr(row, "__getitem__") else row[0]
        ddl = row["sql"] if hasattr(row, "__getitem__") else row[1]
        if table_name in _INTERNAL_TABLES:
            continue
        schema = await _introspect_one_table(db, table_name, ddl or "")
        out.append(schema)
    return out


async def _introspect_one_table(
    db, table_name: str, ddl: str,
) -> Mapping[str, Any]:
    """Introspect a single SQLite table into an IR-shaped content
    schema dict."""
    # PRAGMA table_info gives column name, type, notnull, dflt_value, pk
    cursor = await db.execute(f'PRAGMA table_info("{table_name}")')
    cols = await cursor.fetchall()

    # PRAGMA foreign_key_list gives the FK references (with
    # on_delete which is what tells us cascade/restrict in v0.8 the
    # value will be "NO ACTION" since v0.8 emitted no ON DELETE).
    cursor = await db.execute(f'PRAGMA foreign_key_list("{table_name}")')
    fk_rows = await cursor.fetchall()
    fk_by_col: dict = {}
    for fk in fk_rows:
        col = fk["from"] if hasattr(fk, "__getitem__") else fk[3]
        target = fk["table"] if hasattr(fk, "__getitem__") else fk[2]
        on_delete = fk["on_delete"] if hasattr(fk, "__getitem__") else fk[6]
        fk_by_col[col] = {
            "target": target,
            "on_delete": (on_delete or "NO ACTION").upper(),
        }

    fields: list = []
    state_machines: list = []
    for col in cols:
        cname = col["name"] if hasattr(col, "__getitem__") else col[1]
        ctype = col["type"] if hasattr(col, "__getitem__") else col[2]
        notnull = bool(col["notnull"] if hasattr(col, "__getitem__") else col[3])
        dflt = col["dflt_value"] if hasattr(col, "__getitem__") else col[4]
        is_pk = bool(col["pk"] if hasattr(col, "__getitem__") else col[5])

        if cname == "id" and is_pk:
            continue  # implicit PK

        if cname in fk_by_col:
            fk_info = fk_by_col[cname]
            field = {
                "name": cname,
                "business_type": "reference",
                "column_type": "INTEGER",
                "required": notnull,
                "unique": False,
                "foreign_key": fk_info["target"],
                "cascade_mode": _on_delete_to_cascade_mode(fk_info["on_delete"]),
            }
            fields.append(field)
            continue

        # Detect state-machine columns: TEXT NOT NULL with a string
        # DEFAULT. The convention from storage.py:170-171 is
        # `"col" TEXT NOT NULL DEFAULT 'state'`. We don't have the IR
        # to know for sure, so we list the column as a regular field;
        # the classifier handles the v0.8 → v0.9 case by leaving the
        # column structure alone.
        ir_type = _SQL_TO_BUSINESS_TYPE.get((ctype or "TEXT").upper(), "text")
        field = {
            "name": cname,
            "business_type": ir_type,
            "column_type": (ctype or "TEXT").upper(),
            "required": notnull,
            "unique": _is_unique_in_ddl(cname, ddl),
            "foreign_key": None,
            "cascade_mode": None,
        }
        if dflt is not None:
            field["default_expr"] = str(dflt)
        fields.append(field)

    return {
        "name": {
            "snake": table_name,
            "display": table_name,
            "pascal": _pascal(table_name),
        },
        "fields": tuple(fields),
        "state_machines": tuple(state_machines),
    }


def _on_delete_to_cascade_mode(on_delete: str) -> Optional[str]:
    """Map SQLite's `on_delete` PRAGMA value to our cascade_mode.

    v0.8 schemas have "NO ACTION" (no ON DELETE clause emitted by
    init_db). v0.9 schemas have CASCADE or RESTRICT. The classifier
    uses None as the v0.8 sentinel — when the v0.9 IR declares a
    cascade_mode and introspection finds None, that's a
    cascade_mode_changed FieldChange (high risk per §3.3).
    """
    od = (on_delete or "NO ACTION").upper()
    if od == "CASCADE":
        return "cascade"
    if od == "RESTRICT":
        return "restrict"
    # NO ACTION, SET NULL, SET DEFAULT — treat as v0.8 unspecified.
    return None


def _is_unique_in_ddl(column: str, ddl: str) -> bool:
    """Best-effort: check whether the column has a UNIQUE constraint
    declared in its CREATE TABLE DDL. Looks for `"col" ... UNIQUE`
    on a single column line; doesn't handle multi-column UNIQUE
    indices."""
    if not ddl:
        return False
    pattern = re.compile(
        rf'"{re.escape(column)}"\s+[A-Z]+(?:\s+\([^)]*\))?\s+[^,]*\bUNIQUE\b',
        re.IGNORECASE,
    )
    return bool(pattern.search(ddl))


def _pascal(snake: str) -> str:
    return "".join(p.capitalize() for p in snake.split("_") if p)

# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Transaction staging for Compute execution (snapshot isolation).

Provides a staging layer that intercepts reads and writes during Compute
execution. Reads go to production unless the transaction has written that
value (read-your-writes). Writes go to staging. After completion,
postconditions are evaluated against the staging state. If all pass,
writes are committed to production in write order (journaling).

Usage:
    tx = Transaction(storage_adapter)
    tx.write("employees", 42, {"salary": 100000})
    tx.write("employees", 43, {"salary": 90000})
    record = tx.read("employees", 42)  # returns staged value

    # Evaluate postconditions...
    if postconditions_pass:
        await tx.commit(db)   # writes to prod in order
    else:
        tx.rollback()         # discards staging
"""

import uuid
from datetime import datetime, timezone


class ContentSnapshot:
    """A frozen, read-only snapshot of content state for postcondition evaluation.

    Supports .content_query(content_name) which returns a list of records.
    This is injected into the CEL context as Before and After objects.
    """

    def __init__(self, data: dict[str, list[dict]], result=None):
        """Initialize a snapshot.

        Args:
            data: Dict of {content_name: [record_dicts]} for all content types.
            result: The compute result value (if any).
        """
        self._data = {k: list(v) for k, v in data.items()}
        self._result = result

    def content_query(self, content_name: str) -> list[dict]:
        """Return the list of records for a content type."""
        return list(self._data.get(content_name, []))

    @property
    def result(self):
        """The compute result value."""
        return self._result

    def __getattr__(self, name):
        """Allow attribute-style access for content types: snapshot.findings."""
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._data:
            return list(self._data[name])
        raise AttributeError(f"ContentSnapshot has no content type '{name}'")

    def __getitem__(self, key):
        """Allow dict-style access: snapshot['findings']."""
        if key == "result":
            return self._result
        if key in self._data:
            return list(self._data[key])
        raise KeyError(key)


class StagedWrite:
    """A single write operation in the staging area."""
    __slots__ = ("content_name", "record_id", "data", "operation", "sequence")

    def __init__(self, content_name: str, record_id, data: dict, operation: str, sequence: int):
        self.content_name = content_name
        self.record_id = record_id
        self.data = data
        self.operation = operation  # "create", "update", "delete"
        self.sequence = sequence


class Transaction:
    """Snapshot isolation transaction for Compute execution.

    Provides read-your-writes semantics: reads check the staging area
    first, falling through to the production storage if no staged value
    exists. All writes are captured in a journal and only committed to
    production when commit() is called successfully.
    """

    def __init__(self, storage_read_fn=None):
        """Initialize a transaction.

        Args:
            storage_read_fn: async function(content_name, record_id) -> dict
                Used for read-through to production. If None, reads only
                check the staging area.
        """
        self.id = str(uuid.uuid4())
        self.started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._storage_read = storage_read_fn
        self._journal: list[StagedWrite] = []
        self._staging: dict[tuple[str, any], dict] = {}  # (content, id) -> latest staged data
        self._deleted: set[tuple[str, any]] = set()       # (content, id) pairs that were deleted
        self._sequence = 0
        self._committed = False
        self._rolled_back = False

    @property
    def is_active(self) -> bool:
        return not self._committed and not self._rolled_back

    def write(self, content_name: str, record_id, data: dict, operation: str = "update"):
        """Stage a write operation.

        Args:
            content_name: snake_case content type name
            record_id: record identifier (usually integer id)
            data: the record data to write
            operation: "create", "update", or "delete"
        """
        if not self.is_active:
            raise RuntimeError("Transaction is no longer active")

        key = (content_name, record_id)
        self._sequence += 1
        entry = StagedWrite(content_name, record_id, data, operation, self._sequence)
        self._journal.append(entry)

        if operation == "delete":
            self._deleted.add(key)
            self._staging.pop(key, None)
        else:
            self._staging[key] = data
            self._deleted.discard(key)

    async def read(self, content_name: str, record_id) -> dict | None:
        """Read a record, checking staging first then production.

        Returns None if the record doesn't exist or was deleted in staging.
        """
        key = (content_name, record_id)

        # Check if deleted in staging
        if key in self._deleted:
            return None

        # Check staging (read-your-writes)
        if key in self._staging:
            return self._staging[key]

        # Fall through to production
        if self._storage_read:
            return await self._storage_read(content_name, record_id)

        return None

    async def read_all(self, content_name: str, prod_records: list[dict]) -> list[dict]:
        """Read all records for a content type, merging staged changes.

        Args:
            prod_records: production records (from storage query)

        Returns merged list: production records with staged updates applied,
        staged creates appended, staged deletes removed.
        """
        result = []
        seen_ids = set()

        for record in prod_records:
            rid = record.get("id")
            key = (content_name, rid)

            if key in self._deleted:
                continue  # deleted in staging
            if key in self._staging:
                result.append(self._staging[key])  # use staged version
            else:
                result.append(record)  # use production version
            seen_ids.add(rid)

        # Append staged creates (records not in production)
        for (cn, rid), data in self._staging.items():
            if cn == content_name and rid not in seen_ids:
                result.append(data)

        return result

    def get_snapshot(self, content_name: str = None) -> dict:
        """Get the current staging state for postcondition evaluation.

        Returns a dict of {content_name: {record_id: data}} for all
        staged writes. If content_name is provided, returns only that
        content's staged data.
        """
        snapshot = {}
        for (cn, rid), data in self._staging.items():
            if content_name and cn != content_name:
                continue
            snapshot.setdefault(cn, {})[rid] = data
        return snapshot

    async def commit(self, db, storage_write_fn):
        """Commit all staged writes to production in journal order.

        Args:
            db: database connection
            storage_write_fn: async function(db, content_name, record_id, data, operation)
                Applies a single write to production storage.

        Raises RuntimeError if transaction is not active.
        """
        if not self.is_active:
            raise RuntimeError("Transaction is no longer active")

        for entry in self._journal:
            await storage_write_fn(db, entry.content_name, entry.record_id,
                                   entry.data, entry.operation)

        self._committed = True

    def rollback(self):
        """Discard all staged writes. No side effects."""
        self._journal.clear()
        self._staging.clear()
        self._deleted.clear()
        self._rolled_back = True

    @property
    def write_count(self) -> int:
        """Number of writes in the journal."""
        return len(self._journal)

    @property
    def journal(self) -> list[dict]:
        """Journal entries as dicts (for audit logging)."""
        return [
            {
                "content": e.content_name,
                "record_id": e.record_id,
                "operation": e.operation,
                "sequence": e.sequence,
            }
            for e in self._journal
        ]

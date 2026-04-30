# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""State machine engine for any conforming Termin runtime.

Takes transition tables as config (dict-shaped, typically derived
from the IR's StateMachineSpec at app startup) and provides
``do_state_transition``. The transition is applied via the
StorageProvider's ``update_if`` Protocol method so the read-and-write
window is atomic per the storage contract — two callers racing to
advance the same record from the same source state result in exactly
one winning transition; the loser sees a TerminConflictError with
the post-winner state.

Slice 7.2.c of Phase 7 (2026-04-30) moved this module from
``termin_runtime/state.py`` into ``termin-core``. The functions here
raise :class:`termin_core.errors` exception types instead of
``fastapi.HTTPException``; HTTP adapters register handlers that
translate to the appropriate status codes (400 / 403 / 404 / 409).
"""

from datetime import datetime, timezone

from ..errors import (
    TerminError,
    TerminBadRequestError,
    TerminScopeError,
    TerminNotFoundError,
    TerminConflictError,
)
from ..providers.storage_contract import Eq


def _principal_dict_for_event(user: dict) -> dict:
    """Project a request ``user`` dict into the BRD #3 §4.2-shaped
    principal dict that goes into transition event payloads.

    Mirrors ``the_user`` from the runtime's identity layer — id /
    display_name / is_anonymous / is_system / scopes. Defensive: a
    malformed user dict (legacy callers, tests) returns an empty-id
    system principal so payload shape stays stable.
    """
    the_user = user.get("the_user") if isinstance(user, dict) else None
    if isinstance(the_user, dict):
        return {
            "id": the_user.get("id", ""),
            "display_name": the_user.get("display_name", ""),
            "is_anonymous": the_user.get("is_anonymous", False),
            "is_system": the_user.get("is_system", False),
            "scopes": list(the_user.get("scopes", []) or []),
        }
    # Fallback for callers that still pass the v0.8-shaped dict.
    return {
        "id": "",
        "display_name": str(user.get("role", "")) if isinstance(user, dict) else "",
        "is_anonymous": True,
        "is_system": False,
        "scopes": list(user.get("scopes", []) if isinstance(user, dict) else ()),
    }


async def do_state_transition(storage, table: str, record_id: int,
                              machine_name: str, target_state: str,
                              user: dict, state_machines: dict,
                              terminator=None, event_bus=None):
    """Attempt a state transition on a specific state machine.

    Args:
        storage: StorageProvider (typically ``ctx.storage``). The
            transition is applied via ``storage.update_if`` so the
            read-and-write is atomic per the contract.
        table: content table name (snake_case).
        record_id: integer primary key.
        machine_name: snake_case identifier of the state machine on
            this content. Same value as the SQL column. A content
            with two state machines (e.g. ``lifecycle`` and
            ``approval_status``) selects which machine to drive via
            this argument.
        target_state: desired target state string.
        user: user dict with ``scopes`` key.
        state_machines: dict of ``{table_name: list[sm_dict]}`` where
            each ``sm_dict`` has keys ``{machine_name, column,
            initial, transitions}``. The ``transitions`` value is a
            dict of ``{(from_state, to_state): required_scope}``.
        terminator: optional TerminAtor for error routing.
        event_bus: optional EventBus for publishing events.

    Self-transitions (``from_state == to_state``) are valid when
    declared in the transition table — they write the same value
    back and still publish the WebSocket event.

    Concurrency: if two callers race to advance the same record from
    the same source state, exactly one's ``update_if`` wins; the
    loser receives :class:`TerminConflictError` with the post-winner
    state in the response.

    Raises:
        TerminBadRequestError: unknown table or unknown machine name.
        TerminNotFoundError: record doesn't exist (or was deleted
            between the read and the CAS).
        TerminConflictError: undeclared transition, or concurrent
            transition advanced the record before our CAS could land.
        TerminScopeError: caller's scope set doesn't satisfy the
            transition's required scope.
    """
    if table not in state_machines:
        raise TerminBadRequestError(f"No state machine for {table}")

    sm_list = state_machines[table]
    sm = next((s for s in sm_list if s["machine_name"] == machine_name), None)
    if sm is None:
        raise TerminBadRequestError(
            f"No state machine '{machine_name}' on {table}")

    column = sm["column"]
    record = await storage.read(table, record_id)
    if not record:
        raise TerminNotFoundError("Record not found")

    current = record.get(column, "")
    key = (current, target_state)
    if key not in sm["transitions"]:
        if terminator:
            terminator.route(TerminError(
                source=f"state:{table}:{machine_name}",
                kind="state",
                message=f"Cannot transition from '{current}' to '{target_state}'",
                context=f"record_id={record_id}",
            ))
        raise TerminConflictError(
            f"Cannot transition from '{current}' to '{target_state}'",
            extra={
                "table": table,
                "machine_name": machine_name,
                "from_state": current,
                "to_state": target_state,
            },
        )

    required_scope = sm["transitions"][key]
    if required_scope and required_scope not in user["scopes"]:
        if terminator:
            terminator.route(TerminError(
                source=f"state:{table}:{machine_name}",
                kind="authorization",
                message=f"Transition requires scope: {required_scope}",
                context=f"record_id={record_id}, user_role={user.get('role', '')}",
            ))
        raise TerminScopeError(
            f"Transition requires scope: {required_scope}",
            extra={"required_scope": required_scope},
        )

    # Atomic CAS: the update lands only if the column is still
    # `current` at write time. A racing transition from the same
    # source state will hit condition_failed.
    result = await storage.update_if(
        table, record_id,
        condition=Eq(field=column, value=current),
        patch={column: target_state},
    )

    if not result.applied:
        if result.reason == "not_found":
            # Race: record was deleted between our read and CAS.
            raise TerminNotFoundError("Record not found")
        # condition_failed: another transition advanced the state
        # between our read and our CAS. Surface the current state so
        # the caller can display "already X" in the UI.
        post_race_state = (result.record or {}).get(column, "")
        if terminator:
            terminator.route(TerminError(
                source=f"state:{table}:{machine_name}",
                kind="state",
                message=(
                    f"Concurrent transition: another caller advanced "
                    f"this record to '{post_race_state}' before our "
                    f"transition from '{current}' could land"
                ),
                context=f"record_id={record_id}",
            ))
        raise TerminConflictError(
            f"Cannot transition from '{current}' to '{target_state}': "
            f"record is now '{post_race_state}'",
            extra={
                "table": table,
                "machine_name": machine_name,
                "from_state": current,
                "to_state": target_state,
                "current_state": post_race_state,
            },
        )

    updated_record = result.record or {"id": record_id, column: target_state}

    if event_bus:
        # Per BRD #3 §5: emit transition events.
        #   1. <content>.<machine>.<from>.exited (before update_if
        #      conceptually, but published here since we needed the
        #      CAS to succeed before knowing the transition was real).
        #   2. <content>.<machine>.<to>.entered (after update_if).
        # The legacy `content.<X>.updated` event is preserved for
        # back-compat with WebSocket subscribers built before §5.
        principal = _principal_dict_for_event(user)
        triggered_at = datetime.now(timezone.utc).isoformat()
        # Per BRD §5.3, on_behalf_of and invoked_by are equal for
        # direct user actions (the most common case in v0.9). Agent
        # actions split them.
        payload = {
            "record_id": record_id,
            "from_state": current,
            "to_state": target_state,
            "on_behalf_of": principal,
            "invoked_by": principal,
            "triggered_at": triggered_at,
            "trigger_kind": "user_action",
        }
        await event_bus.publish({
            "channel_id": f"{table}.{machine_name}.{current}.exited",
            "data": payload,
        })
        await event_bus.publish({
            "channel_id": f"{table}.{machine_name}.{target_state}.entered",
            "data": payload,
        })
        # Legacy event for v0.8 subscribers — record-shaped, not the
        # new typed payload.
        await event_bus.publish({
            "channel_id": f"content.{table}.updated",
            "data": updated_record,
        })

    return updated_record

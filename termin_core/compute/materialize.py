# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Conversation + tool-spec materialization (v0.9.3 extraction).

These helpers transform Termin's IR-level shapes (conversation
field entries, ``Invokes`` declarations, ``Accesses`` content
lists, output-field tuples) into the wire-shape dicts a provider
implementation passes to its underlying SDK.

The shapes here match Anthropic's documented format
(``messages: list[{role, content: list[block]}]`` plus their
tool-schema shape) because Anthropic was the first concrete
provider; alternate providers consume the same dicts and translate
internally where needed.

Per the v0.9.2 close-out (2026-05-05) and the v0.9.2 conversation-
field tech design §11.4 — that is the canonical reference for
shapes here. This file is a code-only extraction; it doesn't
re-specify what's already specified there.
"""

from __future__ import annotations

from typing import Iterable, Mapping


# ── Kind sets ──

CANONICAL_KINDS_USER_ROLE: frozenset[str] = frozenset({
    "user", "tool_result", "system_event",
})
"""Termin entry kinds that map to Anthropic role ``user``."""

CANONICAL_KINDS_ASSISTANT_ROLE: frozenset[str] = frozenset({
    "agent", "assistant", "tool_call",
})
"""Termin entry kinds that map to Anthropic role ``assistant``.

Includes both the v0.9.2 canonical ``agent`` kind and the legacy
``assistant`` kind (which is preserved as a back-compat read shape
per the v0.9.2 close-out)."""


# ── Purpose-field constants (v0.9.2 close-out) ──

PURPOSE_MAX_WORDS: int = 12
"""Hard cap on the ``purpose`` field of tool_call entries.

Agent supplies a short display string per tool call; runtime
truncates with ellipsis on persistence per JL's Q2 (v0.9.2
close-out)."""

PURPOSE_TOOL_DESCRIPTION: str = (
    "Short, 6-words-or-less, plain-English description of why you're "
    "calling this tool — for chat-UI display. Examples: "
    "'checking the time', 'looking up the order', 'updating the "
    "ticket status'. Hard truncated at 12 words with ellipsis on "
    "persistence."
)
"""The schema-property description for ``purpose`` on tool inputs."""


# ── Exceptions ──

class ConversationMaterializationError(Exception):
    """Raised when a conversation entry list violates the canonical
    materialization contract (e.g. a ``tool_result`` whose
    ``tool_call_id`` doesn't match any preceding ``tool_call``).

    The runtime treats this as a server-side error: the conversation
    field is the source of truth, and the runtime can't translate
    bad data into a valid provider call.
    """


# ── Purpose helpers ──

def truncate_purpose(text: str, max_words: int = PURPOSE_MAX_WORDS) -> str:
    """Hard-truncate a ``purpose`` string to ``max_words`` (default 12)
    with ellipsis when over. Collapses runs of whitespace via
    ``str.split()`` default semantics."""
    if not text:
        return ""
    words = text.split()
    if len(words) <= max_words:
        return " ".join(words)
    return " ".join(words[:max_words]) + "..."


def purpose_property() -> dict:
    """The Anthropic JSON-schema property dict for the ``purpose``
    field. Added to every tool's ``input_schema`` by the conversation-
    mode tool-surface builder so the agent is consistently prompted
    to supply intent."""
    return {
        "type": "string",
        "description": PURPOSE_TOOL_DESCRIPTION,
    }


def add_purpose_to_tool(tool: dict) -> dict:
    """Return a shallow copy of ``tool`` with ``purpose`` added to its
    ``input_schema`` properties (idempotent — no-op if already present).

    Does NOT mark ``purpose`` as required; agents are encouraged to
    supply it but the chat UI falls back to body when absent.
    """
    tool = dict(tool)
    schema = dict(tool.get("input_schema") or {})
    props = dict(schema.get("properties") or {})
    if "purpose" not in props:
        props["purpose"] = purpose_property()
    schema["properties"] = props
    tool["input_schema"] = schema
    return tool


# ── Conversation materialization ──

def entry_role(kind: str) -> str:
    """Map a Termin entry kind to its Anthropic role.

    Defensive: callers should validate kind upstream (the append
    handler enforces ``CANONICAL_KINDS``); falls back to ``user``
    for unknown kinds.
    """
    if kind in CANONICAL_KINDS_ASSISTANT_ROLE:
        return "assistant"
    if kind in CANONICAL_KINDS_USER_ROLE:
        return "user"
    return "user"


def build_content_blocks(entry: Mapping) -> list[dict]:
    """Build the Anthropic content-blocks list for a single entry.

    The block shape depends on the entry's kind:

      - ``user`` / ``assistant`` / ``agent`` / ``system_event`` →
        text block (+ attachments for user)
      - ``tool_call`` → ``tool_use`` block
      - ``tool_result`` → ``tool_result`` block
    """
    kind = entry.get("kind", "")
    body = entry.get("body", "")

    if kind == "tool_call":
        return [{
            "type": "tool_use",
            "id": entry.get("tool_call_id", ""),
            "name": entry.get("tool_name", ""),
            "input": entry.get("tool_args") or {},
        }]

    if kind == "tool_result":
        block: dict = {
            "type": "tool_result",
            "tool_use_id": entry.get("tool_call_id", ""),
            "content": body,
        }
        if entry.get("is_error"):
            block["is_error"] = True
        return [block]

    # Text-bearing kinds: user, assistant/agent, system_event.
    if kind == "system_event":
        # Wrap with source prefix so the in-band context is
        # distinguishable from real user input. Per §11.4.
        source = entry.get("source", "system") or "system"
        text = f"[{source}] {body}"
    else:
        text = body

    blocks: list[dict] = [{"type": "text", "text": text}]

    # Attachments ride alongside text in the same content array.
    # Only user-kind entries carry attachments in v0.9.2; assistant
    # attachments depend on per-model image-acceptance and are out
    # of scope.
    if kind == "user":
        for att in entry.get("attachments") or ():
            media_type = (att.get("media_type") or "").lower()
            source_block = att.get("source") or {}
            if media_type.startswith("image/"):
                blocks.append({
                    "type": "image",
                    "source": source_block,
                })
            elif media_type == "application/pdf":
                blocks.append({
                    "type": "document",
                    "source": source_block,
                })
            # Unknown media types drop silently — append-time
            # validation should have caught them.

    return blocks


def materialize_to_anthropic(entries: Iterable[Mapping]) -> list[dict]:
    """Translate a Termin conversation field's entry list into
    Anthropic's ``messages`` array per §11.4.

    Returns: ``list[{role, content: list[block]}]`` ready to pass to
    ``anthropic.messages.create(messages=...)``.

    Raises ``ConversationMaterializationError`` on:

      - ``tool_result`` with ``tool_call_id`` that doesn't match a
        prior ``tool_call`` entry (orphan).

    v0.9.2 close-out (2026-05-05): orphan ``tool_call`` entries (a
    ``tool_call`` with no matching ``tool_result``) are SILENTLY
    DROPPED. Anthropic rejects messages arrays with unmatched
    ``tool_use`` blocks; the runtime never writes orphan tool_calls
    in v0.9.2+, but legacy chat data may contain them. Skipping at
    materialization time means existing chats recover cleanly
    without requiring a data migration.

    Adjacent same-role entries merge into one message with the
    blocks concatenated (Anthropic requires alternating user/
    assistant roles).
    """
    if not entries:
        return []

    entries_list = list(entries)

    # First pass: collect tool_call_ids that have matching results.
    # Orphan tool_calls (no matching result) get dropped per the
    # v0.9.2 close-out mitigation.
    matched_tool_call_ids: set[str] = set()
    for entry in entries_list:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("kind") == "tool_result":
            tcid = entry.get("tool_call_id", "")
            if tcid:
                matched_tool_call_ids.add(tcid)

    seen_tool_call_ids: set[str] = set()
    messages: list[dict] = []

    for entry in entries_list:
        if not isinstance(entry, Mapping):
            continue
        kind = entry.get("kind", "")
        if not kind:
            continue

        # Tool linkage validation — done before mapping so we don't
        # half-build a turn before failing.
        if kind == "tool_call":
            tcid = entry.get("tool_call_id", "")
            # Orphan tool_call (no matching tool_result later) — drop.
            if tcid and tcid not in matched_tool_call_ids:
                continue
            if tcid:
                seen_tool_call_ids.add(tcid)
        elif kind == "tool_result":
            tcid = entry.get("tool_call_id", "")
            if not tcid or tcid not in seen_tool_call_ids:
                raise ConversationMaterializationError(
                    f"tool_result entry references unknown "
                    f"tool_call_id {tcid!r}; no preceding tool_call "
                    f"with that id."
                )

        role = entry_role(kind)
        blocks = build_content_blocks(entry)
        if not blocks:
            continue

        if messages and messages[-1]["role"] == role:
            # Adjacent same-role merge.
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": list(blocks)})

    return messages


# ── Tool-spec assembly ──

def build_invokable_compute_tools(
    invokes_list: list[str],
    computes_lookup: Mapping[str, Mapping],
) -> list[dict]:
    """Build Anthropic-shape tool schemas for each compute named in
    the agent's ``Invokes`` declarations.

    Per the v0.9.2 design §11: tool name = compute snake_name;
    description from the compute's display_name + first-line
    directive (when present); input schema derived from the
    compute's input_params.

    v0.9.2 supports ``default-CEL`` providers only. Computes with
    ``provider="llm"`` or ``"ai-agent"`` are skipped (logged as
    future). Unknown invokes (compute not in lookup) are also
    skipped — the analyzer should have caught this at compile time,
    but the runtime is forgiving.
    """
    tools: list[dict] = []
    for invoke_name in invokes_list:
        comp = computes_lookup.get(invoke_name)
        if comp is None:
            continue
        provider = comp.get("provider") or "cel"
        if provider not in ("cel", "default-CEL", None, ""):
            continue
        name = comp.get("name", {})
        snake = name.get("snake") if isinstance(name, Mapping) else invoke_name
        display = name.get("display") if isinstance(name, Mapping) else invoke_name
        directive_lines = (comp.get("directive") or "").strip().splitlines()
        first_line = directive_lines[0] if directive_lines else ""
        description = display
        if first_line:
            description = f"{display} — {first_line}"
        properties: dict = {}
        required: list[str] = []
        for param in comp.get("input_params") or ():
            pname = (
                param.get("name") if isinstance(param, Mapping)
                else getattr(param, "name", None)
            )
            ptype = (
                param.get("type_name") if isinstance(param, Mapping)
                else getattr(param, "type_name", None)
            )
            if not pname:
                continue
            properties[pname] = {
                "type": "object",
                "description": (
                    f"The {ptype} record this compute operates on."
                    if ptype else
                    f"The {pname} input."
                ),
                "additionalProperties": True,
            }
            required.append(pname)
        schema = {
            "type": "object",
            "properties": properties,
            "required": required,
        }
        tools.append({
            "name": snake,
            "description": description,
            "input_schema": schema,
        })
    return tools


def build_output_tool(
    output_fields: list[tuple[str, str]],
    content_lookup: Mapping[str, Mapping],
) -> dict:
    """Build the ``set_output`` tool schema from Compute output field
    declarations.

    Args:
        output_fields: List of ``(content_ref, field_name)`` from IR.
        content_lookup: Dict of ``snake_name → content schema dict``.

    Returns:
        Anthropic-format tool schema dict with ``name="set_output"``.
    """
    properties: dict = {}
    required: list[str] = []

    for content_ref, field_name in output_fields:
        # Resolve content schema to find field type and enum constraints.
        # content_ref is the singular (e.g., "completion") — find the
        # matching content by name OR singular.
        schema = None
        for name, s in content_lookup.items():
            singular = s.get("singular", "")
            if name == content_ref or singular == content_ref:
                schema = s
                break
        if not schema:
            properties[field_name] = {
                "type": "string",
                "description": f"Field: {content_ref}.{field_name}",
            }
            required.append(field_name)
            continue

        field_def = None
        for f in schema.get("fields", []):
            if f.get("name", "") == field_name:
                field_def = f
                break

        if field_def:
            prop: dict = {"description": f"Field: {content_ref}.{field_name}"}
            enum_vals = field_def.get("enum_values", [])
            if enum_vals:
                prop["type"] = "string"
                prop["enum"] = list(enum_vals)
            elif field_def.get("column_type") in ("INTEGER", "REAL"):
                prop["type"] = "number"
            elif field_def.get("column_type") == "BOOLEAN":
                prop["type"] = "boolean"
            else:
                prop["type"] = "string"
            properties[field_name] = prop
            required.append(field_name)
        else:
            properties[field_name] = {
                "type": "string",
                "description": f"Field: {content_ref}.{field_name}",
            }
            required.append(field_name)

    return {
        "name": "set_output",
        "description": (
            "Set the output fields for this computation. Always call "
            "this tool to provide your response."
        ),
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
        },
    }


def build_agent_tools(
    accesses: list[str],
    content_lookup: Mapping[str, Mapping],
) -> list[dict]:
    """Build the standard CRUD tool schemas for an ai-agent compute.

    Args:
        accesses: Content type snake_names the agent can touch.
        content_lookup: Dict of ``snake_name → content schema dict``
            (used by callers to elaborate per-content schemas if
            desired; this implementation uses the names list only).

    Returns:
        List of Anthropic-format tool schemas — ``content_query``,
        ``content_create``, ``content_update``, ``content_delete``,
        each gated by the ``accesses`` enum.

    Note: this is the v0.9.2 baseline tool surface. Concrete provider
    implementations may extend this set (or substitute their own
    per-CRUD-verb tool surface) before passing tools to the SDK.
    """
    return [
        {
            "name": "content_query",
            "description": (
                "Query records from a content table. Returns a list of records."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "content_name": {
                        "type": "string",
                        "description": (
                            f"Content type to query. "
                            f"Allowed: {', '.join(accesses)}"
                        ),
                        "enum": accesses,
                    },
                    "filters": {
                        "type": "object",
                        "description": (
                            "Optional key-value filters (field_name: value)."
                        ),
                        "additionalProperties": True,
                    },
                },
                "required": ["content_name"],
            },
        },
        {
            "name": "content_create",
            "description": "Create a new record in a content table.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "content_name": {
                        "type": "string",
                        "enum": accesses,
                        "description": "Content type to create in.",
                    },
                    "fields": {
                        "type": "object",
                        "description": "Field values for the new record.",
                        "additionalProperties": True,
                    },
                },
                "required": ["content_name", "fields"],
            },
        },
        {
            "name": "content_update",
            "description": "Update an existing record by id.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "content_name": {
                        "type": "string",
                        "enum": accesses,
                    },
                    "id": {"type": "string"},
                    "fields": {
                        "type": "object",
                        "additionalProperties": True,
                    },
                },
                "required": ["content_name", "id", "fields"],
            },
        },
        {
            "name": "content_delete",
            "description": "Delete a record by id.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "content_name": {
                        "type": "string",
                        "enum": accesses,
                    },
                    "id": {"type": "string"},
                },
                "required": ["content_name", "id"],
            },
        },
    ]

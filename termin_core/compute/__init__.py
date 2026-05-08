# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Compute orchestration helpers (v0.9.3 issue #3).

The Provider Protocols themselves (DefaultCelComputeProvider,
LlmComputeProvider, AiAgentComputeProvider) live in
``termin_core.providers.compute_contract`` — that surface is
unchanged by v0.9.3. The (category, contract) -> Protocol routing
goes through ``ProviderRegistry`` (also unchanged).

What this package adds in v0.9.3:

  - ``materialize`` — SDK-agnostic transformation helpers used by
    any LLM/agent provider to translate Termin conversation entries
    into a wire-shape ``messages`` array (Anthropic-shaped, but
    consumable by any provider that accepts the documented
    Anthropic format) and to assemble tool schemas from
    ``Invokes`` / ``Accesses`` declarations on the IR.

What v0.9.3 does NOT extract:

  - CEL execution (``_execute_cel_compute``) stays in
    ``termin_server.compute_runner``. It's tangled with transaction
    staging, audit-trace writing, and state-machine integration
    that haven't themselves been extracted. Pulling it out is a
    separate refactor; not blocking for an alt runtime that brings
    its own CEL evaluator. Tracked as v0.10 backlog if a real
    consumer surfaces.
"""

from .materialize import (  # noqa: F401
    CANONICAL_KINDS_USER_ROLE,
    CANONICAL_KINDS_ASSISTANT_ROLE,
    PURPOSE_MAX_WORDS,
    PURPOSE_TOOL_DESCRIPTION,
    ConversationMaterializationError,
    materialize_to_anthropic,
    entry_role,
    build_content_blocks,
    build_invokable_compute_tools,
    build_output_tool,
    build_agent_tools,
    truncate_purpose,
    purpose_property,
    add_purpose_to_tool,
)

__all__ = [
    "CANONICAL_KINDS_USER_ROLE",
    "CANONICAL_KINDS_ASSISTANT_ROLE",
    "PURPOSE_MAX_WORDS",
    "PURPOSE_TOOL_DESCRIPTION",
    "ConversationMaterializationError",
    "materialize_to_anthropic",
    "entry_role",
    "build_content_blocks",
    "build_invokable_compute_tools",
    "build_output_tool",
    "build_agent_tools",
    "truncate_purpose",
    "purpose_property",
    "add_purpose_to_tool",
]

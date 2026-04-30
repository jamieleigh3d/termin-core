# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""CEL → Predicate AST compiler (v0.9 Phase 2.x f).

Per BRD §6.2:

> "the runtime compiles source-level CEL down to the AST and
>  evaluates the residual in-process."

This module is the source-level CEL → Predicate AST half of that
contract. Given a CEL expression string and a schema, it returns
either:

  - a Predicate AST node that the storage provider can push down
    via its existing predicate compiler (free for any provider
    that implements `query()`), OR
  - a NotCompilable exception, signaling the runtime should
    evaluate the expression in-process via cel-python instead

What compiles
  - <field> == <literal>   → Eq
  - <field> != <literal>   → Ne
  - <field> > / < / >= / <= <numeric-literal> → Gt/Lt/Gte/Lte
  - <field> in [<lit>, ...]  → In
  - <field>.contains(<string>) → Contains
  - <field> == null  → Eq(field, None)  (compiles to IS NULL)
  - <expr> && <expr>  → And
  - <expr> || <expr>  → Or
  - !<expr>            → Not

What does NOT compile (NotCompilable, with reason)
  - identifiers other than schema fields (User.X, dotted paths
    that aren't .contains, etc.)
  - function calls beyond .contains()
  - arithmetic, ternaries, macros (has(), all(), exists())
  - mixed-type comparisons the schema doesn't know how to
    coerce

The runtime is expected to catch NotCompilable and fall back to
in-process evaluation (cel-python against fetched records).
v0.9 ships the compiler; the in-process-residual fallback is
part of the runtime's CEL evaluator already.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import celpy

from ..providers.storage_contract import (
    Predicate, Eq, Ne, Gt, Gte, Lt, Lte, In, Contains, And, Or, Not,
)


class NotCompilable(ValueError):
    """The CEL expression doesn't reduce to a Predicate. The runtime
    should fall back to in-process cel-python evaluation against
    fetched records."""


# ── Public API ──────────────────────────────────────────────────────


def compile_cel_to_predicate(
    cel_expr: str,
    *,
    field_names: Optional[set] = None,
) -> Predicate:
    """Parse `cel_expr` and return an equivalent Predicate AST.

    `field_names`, if given, constrains which identifiers are
    accepted as field references; identifiers outside this set
    raise NotCompilable. (E.g., the runtime can pass the
    content's field set; user-references that look like fields
    but aren't get rejected before they confuse the storage
    provider.)

    Raises NotCompilable on any expression shape outside the
    compilable subset. Raises celpy.CELParseError for syntactically
    invalid CEL.
    """
    env = celpy.Environment()
    ast = env.compile(cel_expr)  # Lark Tree
    return _walk(ast, field_names)


# ── Internals ───────────────────────────────────────────────────────


def _walk(node, field_names: Optional[set]) -> Predicate:
    """Walk a celpy Lark Tree node, returning a Predicate or
    raising NotCompilable."""
    name = _node_name(node)

    if name == "expr":
        return _walk(node.children[0], field_names)
    if name == "conditionalor":
        # `a || b || c` — children are a list of conditionaland
        # subtrees. Single child = passthrough. Multiple = Or.
        kids = list(node.children)
        if len(kids) == 1:
            return _walk(kids[0], field_names)
        return Or(predicates=tuple(_walk(k, field_names) for k in kids))
    if name == "conditionaland":
        kids = list(node.children)
        if len(kids) == 1:
            return _walk(kids[0], field_names)
        return And(predicates=tuple(_walk(k, field_names) for k in kids))
    if name == "relation":
        kids = list(node.children)
        if len(kids) == 1:
            # Bare relation (no comparison) → drill in.
            return _walk(kids[0], field_names)
        if len(kids) == 2:
            # Comparison: children[0] is `relation_OP` whose single
            # child is the LHS; children[1] is the RHS subtree.
            op_node = kids[0]
            rhs = kids[1]
            op_name = _node_name(op_node)
            if op_name in (
                "relation_eq", "relation_ne",
                "relation_gt", "relation_lt",
                "relation_ge", "relation_le",
            ):
                # The operator subtree wraps the LHS as its single
                # child.
                lhs = op_node.children[0] if op_node.children else None
                return _compile_comparison(op_name, lhs, rhs, field_names)
            if op_name == "relation_in":
                lhs = op_node.children[0] if op_node.children else None
                return _compile_in(lhs, rhs, field_names)
        raise NotCompilable(
            f"unexpected relation shape with {len(node.children)} children")
    if name == "addition":
        if len(node.children) == 1:
            return _walk(node.children[0], field_names)
        raise NotCompilable("arithmetic addition is not pushable")
    if name == "multiplication":
        if len(node.children) == 1:
            return _walk(node.children[0], field_names)
        raise NotCompilable("arithmetic multiplication is not pushable")
    if name == "unary":
        return _compile_unary(node, field_names)
    if name == "member":
        return _compile_member(node, field_names)
    if name == "primary":
        return _walk(node.children[0], field_names)
    if name == "paren_expr":
        # Parenthesized expression: drill through.
        return _walk(node.children[0], field_names)

    raise NotCompilable(
        f"unsupported CEL node {name!r} for predicate pushdown")


def _node_name(node) -> str:
    """Return the rule name of a Lark Tree node (or 'TOKEN' for
    Token leaves)."""
    if hasattr(node, "data"):
        return str(node.data)
    return "TOKEN"


def _compile_comparison(rel_name: str, lhs, rhs, field_names) -> Predicate:
    """Build the right Predicate for a comparison. `lhs` is the LHS
    subtree (drills down to a field identifier), `rhs` is the RHS
    subtree (drills down to a literal)."""
    if lhs is None or rhs is None:
        raise NotCompilable(f"{rel_name} missing operand")
    field = _extract_field_name(lhs, field_names)
    value = _extract_literal(rhs)
    if rel_name == "relation_eq":
        return Eq(field=field, value=value)
    if rel_name == "relation_ne":
        return Ne(field=field, value=value)
    if rel_name == "relation_gt":
        return Gt(field=field, value=value)
    if rel_name == "relation_lt":
        return Lt(field=field, value=value)
    if rel_name == "relation_ge":
        return Gte(field=field, value=value)
    if rel_name == "relation_le":
        return Lte(field=field, value=value)
    raise NotCompilable(f"internal error: unknown comparison {rel_name!r}")


def _compile_in(lhs, rhs, field_names) -> Predicate:
    """relation_in: `lhs` is a field subtree, `rhs` is a list-literal
    subtree."""
    if lhs is None or rhs is None:
        raise NotCompilable("relation_in missing operand")
    field = _extract_field_name(lhs, field_names)
    values = _extract_list_literal(rhs)
    return In(field=field, values=tuple(values))


def _compile_unary(node, field_names) -> Predicate:
    """unary: either bare member (single child) or unary_not + member."""
    kids = list(node.children)
    if len(kids) == 1:
        return _walk(kids[0], field_names)
    if len(kids) == 2 and _node_name(kids[0]) == "unary_not":
        return Not(predicate=_walk(kids[1], field_names))
    raise NotCompilable(f"unsupported unary shape with {len(kids)} children")


def _compile_member(node, field_names) -> Predicate:
    """member: either bare primary, or a member_dot_arg method call
    like `field.contains("substring")`."""
    kids = list(node.children)
    if len(kids) == 1:
        child = kids[0]
        if _node_name(child) == "member_dot_arg":
            return _compile_method_call(child, field_names)
        return _walk(child, field_names)
    if kids:
        return _walk(kids[0], field_names)
    raise NotCompilable("empty member")


def _compile_method_call(node, field_names) -> Predicate:
    """member_dot_arg: member.method(args). Currently supports
    `field.contains("substring")`."""
    kids = list(node.children)
    # Expected children: [member (target), method_name token, exprlist (args)]
    if len(kids) < 3:
        raise NotCompilable(
            f"member_dot_arg with {len(kids)} children")
    target, method_name, args = kids[0], kids[1], kids[2]
    method_str = str(method_name).strip().lower()
    if method_str != "contains":
        raise NotCompilable(
            f"only .contains() is pushable; got .{method_str}()")
    field = _extract_field_name(target, field_names)
    # Args is exprlist with one expr child; that expr should evaluate
    # to a string literal.
    arg_exprs = [c for c in args.children if _node_name(c) == "expr"]
    if len(arg_exprs) != 1:
        raise NotCompilable(
            f".contains() expects exactly 1 argument, got {len(arg_exprs)}")
    substring = _extract_literal(arg_exprs[0])
    if not isinstance(substring, str):
        raise NotCompilable(
            f".contains() argument must be a string, got {type(substring).__name__}")
    return Contains(field=field, substring=substring)


def _extract_field_name(node, field_names) -> str:
    """Extract a field reference from a node. Drills through the
    layers (expr → conditionalor → ... → primary → ident) and
    requires the leaf to be a single identifier with no member
    accesses or function calls."""
    n = node
    while True:
        nname = _node_name(n)
        if nname == "TOKEN":
            # Leaf token. Treat as the field name if it's an
            # identifier-shaped string.
            value = str(n)
            if not value.isidentifier():
                raise NotCompilable(
                    f"left-hand side of comparison must be a field "
                    f"identifier, got {value!r}")
            if field_names is not None and value not in field_names:
                raise NotCompilable(
                    f"identifier {value!r} is not a known field")
            return value
        kids = list(n.children)
        if nname == "primary":
            # primary should have a single ident child.
            if len(kids) != 1:
                raise NotCompilable(
                    f"primary with {len(kids)} children unexpected")
            inner = kids[0]
            iname = _node_name(inner)
            if iname == "ident":
                token = inner.children[0]
                value = str(token)
                if field_names is not None and value not in field_names:
                    raise NotCompilable(
                        f"identifier {value!r} is not a known field")
                return value
            raise NotCompilable(
                f"left-hand side must be a field identifier; got {iname!r}")
        if nname == "ident":
            token = kids[0] if kids else n
            value = str(token)
            if field_names is not None and value not in field_names:
                raise NotCompilable(
                    f"identifier {value!r} is not a known field")
            return value
        # Single-child passthrough rules.
        if len(kids) == 1:
            n = kids[0]
            continue
        raise NotCompilable(
            f"left-hand side too complex (node {nname!r} with "
            f"{len(kids)} children)")


def _extract_literal(node):
    """Extract a literal value from a node — drills through the
    expression chain and returns the underlying Python value
    (str/int/float/bool/None). Raises NotCompilable if the node
    is not a pure literal."""
    n = node
    while True:
        nname = _node_name(n)
        kids = list(n.children) if hasattr(n, "children") else []
        if nname == "TOKEN":
            return _parse_token_literal(str(n))
        if nname == "primary" and len(kids) == 1:
            inner = kids[0]
            iname = _node_name(inner)
            if iname == "literal":
                return _parse_token_literal(str(inner.children[0]))
            if iname == "ident":
                # `null` is parsed as an ident in some grammars.
                ident_value = str(inner.children[0])
                if ident_value in ("null", "None"):
                    return None
                if ident_value in ("true", "True"):
                    return True
                if ident_value in ("false", "False"):
                    return False
                raise NotCompilable(
                    f"right-hand side must be a literal; got identifier "
                    f"{ident_value!r}")
            raise NotCompilable(
                f"right-hand side must be a literal; got {iname!r}")
        if nname == "literal":
            return _parse_token_literal(str(kids[0]))
        # Single-child passthrough.
        if len(kids) == 1:
            n = kids[0]
            continue
        raise NotCompilable(
            f"right-hand side too complex (node {nname!r} with "
            f"{len(kids)} children)")


def _extract_list_literal(node) -> list:
    """For `field in [v1, v2, v3]` — extract the list of literals
    on the right-hand side."""
    n = node
    while True:
        nname = _node_name(n)
        kids = list(n.children) if hasattr(n, "children") else []
        if nname == "list_lit":
            # Children: [exprlist] (or empty for [])
            if not kids:
                return []
            exprlist = kids[0]
            return [_extract_literal(c) for c in exprlist.children
                    if _node_name(c) == "expr"]
        if nname == "primary" and len(kids) == 1:
            inner = kids[0]
            if _node_name(inner) == "list_lit":
                return _extract_list_literal(inner)
        if len(kids) == 1:
            n = kids[0]
            continue
        raise NotCompilable(
            f"`in` expects a list literal on the right; got {nname!r}")


def _parse_token_literal(s: str):
    """Parse a token literal string back to its Python value.
    Handles numbers, strings (single + double quoted), bool, null."""
    s = s.strip()
    if s in ("null", "None"):
        return None
    if s in ("true", "True"):
        return True
    if s in ("false", "False"):
        return False
    if (s.startswith('"') and s.endswith('"')) or (
            s.startswith("'") and s.endswith("'")):
        # CEL escaping is mostly JSON-compatible for the basic
        # strings we'll see in pushable predicates.
        try:
            return json.loads(s) if s.startswith('"') else s[1:-1]
        except json.JSONDecodeError:
            return s[1:-1]
    # Try integer first, then float.
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            # Last-resort: return as string token.
            return s

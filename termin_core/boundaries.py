# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Block C: Boundary containment map and identity enforcement.

The app itself is always a boundary. Content not in any explicit sub-boundary
lives in the implicit app boundary "__app__". There is no "unrestricted" —
every content type and every Compute is in exactly one boundary.
"""

APP_BOUNDARY = "__app__"


def build_boundary_maps(ir: dict) -> tuple[dict, dict, dict]:
    """Build boundary containment maps from the IR.

    Returns:
        (boundary_for_content, boundary_for_compute, boundary_identity_scopes)
    """
    boundary_for_content: dict[str, str] = {}
    boundary_for_compute: dict[str, str] = {}
    boundary_identity_scopes: dict[str, list[str]] = {}

    # Assign content to explicit sub-boundaries
    for bnd in ir.get("boundaries", []):
        bnd_snake = bnd["name"]["snake"]
        for content_snake in bnd.get("contains_content", []):
            boundary_for_content[content_snake] = bnd_snake

    # Content not in any explicit boundary → app boundary
    for ct in ir.get("content", []):
        ct_snake = ct["name"]["snake"]
        if ct_snake not in boundary_for_content:
            boundary_for_content[ct_snake] = APP_BOUNDARY

    # Infer boundary for each Compute from its Accesses
    for comp in ir.get("computes", []):
        comp_snake = comp["name"]["snake"]
        for acc in comp.get("accesses", []):
            if acc in boundary_for_content:
                boundary_for_compute[comp_snake] = boundary_for_content[acc]
                break
        if comp_snake not in boundary_for_compute:
            boundary_for_compute[comp_snake] = APP_BOUNDARY

    # C2: Boundary identity restriction map
    for bnd in ir.get("boundaries", []):
        if bnd.get("identity_mode") == "restrict" and bnd.get("identity_scopes"):
            for content_snake in bnd.get("contains_content", []):
                boundary_identity_scopes[content_snake] = list(bnd["identity_scopes"])

    return boundary_for_content, boundary_for_compute, boundary_identity_scopes


def check_boundary_access(boundary_for_compute: dict, boundary_for_content: dict,
                          compute_snake: str, target_content: str) -> str | None:
    """Check if a Compute can access a content type across boundaries.

    Returns None if access is allowed, or an error message if denied.
    """
    compute_bnd = boundary_for_compute.get(compute_snake, APP_BOUNDARY)
    content_bnd = boundary_for_content.get(target_content, APP_BOUNDARY)
    if compute_bnd == content_bnd:
        return None
    return (f"Cross-boundary access denied: Compute '{compute_snake}' "
            f"(boundary '{compute_bnd}') cannot directly access "
            f"content '{target_content}' (boundary '{content_bnd}'). "
            f"Cross-boundary access requires a channel.")


def check_boundary_identity(boundary_identity_scopes: dict, boundary_for_content: dict,
                            content_snake: str, user_scopes: list[str]) -> str | None:
    """C2: Check if the caller's identity satisfies boundary restrictions.

    Returns None if OK, error message if denied.
    """
    required = boundary_identity_scopes.get(content_snake)
    if not required:
        return None
    missing = [s for s in required if s not in user_scopes]
    if missing:
        bnd_name = boundary_for_content.get(content_snake, APP_BOUNDARY)
        return (f"Boundary identity restriction: content '{content_snake}' "
                f"is in boundary '{bnd_name}' which requires scopes "
                f"{required}. Missing: {missing}")
    return None

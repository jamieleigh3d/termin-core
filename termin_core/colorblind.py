# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Color-vision-deficiency (CVD) simulation and WCAG contrast helpers.

v0.9 Phase 5a.5. Per BRD #2 §6.4, first-party Presentation providers
must satisfy a colorblind-safety conformance check on information-
carrying palettes (severity, status, highlight, badge): deuteranopia
/ protanopia / tritanopia simulation plus WCAG AA luminance contrast
verification.

Pure-Python implementation. No new dependencies — the conformance
test budget did not justify pulling in `colour-science` or similar
for what is essentially three matrix multiplications plus a WCAG
formula. Sources for the simulation matrices:

  - Brettel, Viénot & Mollon (1997) and the simplified Machado et al.
    (2009) matrices commonly cited in CVD-simulation libraries.
  - The values below are the linear-RGB Machado matrices for severity
    8 (close to total CVD); they're conservative — if a palette
    distinguishes pairs under these, milder real-world CVD will too.

Limits of this approximation:
  - Linear-RGB only; no gamma correction beyond the WCAG formula.
  - Single severity level per CVD type (no continuous range).
  - Does not model anomalous trichromacy (mild CVD) — the goal is to
    catch palettes that fail the worst-case observer.

For provider-side rendering, the pattern this enforces is:
**information communicated by color must also be communicated by
luminance contrast OR a non-color signal** (icon, label, weight,
shape) per BRD §6.4 last paragraph. This module's role is just the
detection side; the fix lives in the provider's class strings.
"""

from __future__ import annotations

from typing import Tuple


# ── Tailwind default palette: the hex colors used by tailwind-default ──

# Subset relevant to v0.9 severity / status / highlight rendering.
# Sourced from tailwind.config.js v3.x default palette. Hex literals
# match what Tailwind's compiled CSS emits for these utility classes.
# When palette changes (e.g., switching to a custom color set), update
# this map; the test fixture checks against it.
TAILWIND_HEX: dict[str, str] = {
    # Reds — used for error / urgent / critical / highlighted.
    "red-50":  "#fef2f2",
    "red-100": "#fee2e2",
    "red-200": "#fecaca",
    "red-500": "#ef4444",
    "red-600": "#dc2626",
    "red-700": "#b91c1c",
    "red-800": "#991b1b",
    # Ambers — used for warning.
    "amber-50":  "#fffbeb",
    "amber-100": "#fef3c7",
    "amber-500": "#f59e0b",
    # Greens — used for success.
    "green-50":  "#f0fdf4",
    "green-100": "#dcfce7",
    "green-200": "#bbf7d0",
    "green-500": "#22c55e",
    "green-600": "#16a34a",
    "green-700": "#15803d",
    "green-800": "#166534",
    # Blues — used for info / chat user bubbles.
    "blue-100": "#dbeafe",
    "blue-500": "#3b82f6",
    "blue-600": "#2563eb",
    # Grays — neutrals for body text and surfaces.
    "gray-100": "#f3f4f6",
    "gray-200": "#e5e7eb",
    "gray-700": "#374151",
    "gray-800": "#1f2937",
    "gray-900": "#111827",
    "white":   "#ffffff",
    "black":   "#000000",
}


def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    """Parse a `#rrggbb` (or `rrggbb`) string into an (r, g, b)
    tuple of 0–255 ints. Whitespace tolerant, case-insensitive."""
    s = hex_color.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        raise ValueError(f"Bad hex color: {hex_color!r}")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _channel_to_linear(c8: int) -> float:
    """sRGB 8-bit channel → linear-RGB 0.0–1.0. Per WCAG 2.x."""
    cs = c8 / 255.0
    if cs <= 0.03928:
        return cs / 12.92
    return ((cs + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance (0.0 = black, 1.0 = white)."""
    r, g, b = hex_to_rgb(hex_color)
    return (
        0.2126 * _channel_to_linear(r)
        + 0.7152 * _channel_to_linear(g)
        + 0.0722 * _channel_to_linear(b)
    )


def contrast_ratio(fg_hex: str, bg_hex: str) -> float:
    """WCAG contrast ratio between two colors. Range 1.0–21.0.
    AA passes at ≥4.5:1 for normal text, ≥3:1 for large text."""
    l1 = relative_luminance(fg_hex)
    l2 = relative_luminance(bg_hex)
    lighter, darker = max(l1, l2), min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


# ── CVD simulation: Machado et al. (2009) matrices, severity 100% ──

# Apply to linear-RGB triples (0.0–1.0) and clamp before converting
# back to sRGB. Indexing convention: each matrix is row-major, so
# row 0 maps to simulated R, row 1 to G, row 2 to B.
_CVD_MATRICES: dict[str, tuple] = {
    "deuteranopia": (
        (0.367_322, 0.860_646, -0.227_968),
        (0.280_085, 0.672_501, 0.047_413),
        (-0.011_820, 0.042_940, 0.968_881),
    ),
    "protanopia": (
        (0.152_286, 1.052_583, -0.204_868),
        (0.114_503, 0.786_281, 0.099_216),
        (-0.003_882, -0.048_116, 1.051_998),
    ),
    "tritanopia": (
        (1.255_528, -0.076_749, -0.178_779),
        (-0.078_411, 0.930_809, 0.147_602),
        (0.004_733, 0.691_367, 0.303_900),
    ),
}


def simulate_cvd(hex_color: str, kind: str) -> str:
    """Simulate how `hex_color` appears under one CVD type.

    `kind` ∈ {"deuteranopia", "protanopia", "tritanopia"}.
    Returns the simulated color as `#rrggbb`.
    """
    if kind not in _CVD_MATRICES:
        raise ValueError(
            f"kind must be one of {sorted(_CVD_MATRICES)}; got {kind!r}"
        )
    m = _CVD_MATRICES[kind]
    r, g, b = hex_to_rgb(hex_color)
    lr = _channel_to_linear(r)
    lg = _channel_to_linear(g)
    lb = _channel_to_linear(b)
    nr = m[0][0] * lr + m[0][1] * lg + m[0][2] * lb
    ng = m[1][0] * lr + m[1][1] * lg + m[1][2] * lb
    nb = m[2][0] * lr + m[2][1] * lg + m[2][2] * lb
    # Clamp + linear → sRGB.
    def _clamp(x: float) -> float:
        return max(0.0, min(1.0, x))
    def _linear_to_srgb(c: float) -> int:
        c = _clamp(c)
        if c <= 0.003_130_8:
            s = 12.92 * c
        else:
            s = 1.055 * (c ** (1 / 2.4)) - 0.055
        return max(0, min(255, round(s * 255)))
    return "#{:02x}{:02x}{:02x}".format(
        _linear_to_srgb(nr),
        _linear_to_srgb(ng),
        _linear_to_srgb(nb),
    )


def cvd_distinguishable(
    hex_a: str,
    hex_b: str,
    kind: str,
    min_distance: float = 24.0,
) -> bool:
    """True iff two colors remain visually distinguishable under
    CVD simulation.

    Uses simple per-channel sum-of-absolute-differences in sRGB after
    simulation. Threshold tuned conservatively — `24` corresponds
    to a JND ("just noticeable difference") of about 8 per channel,
    which is roughly the minimum where adjacent palette entries
    won't collide in practice.
    """
    sa = hex_to_rgb(simulate_cvd(hex_a, kind))
    sb = hex_to_rgb(simulate_cvd(hex_b, kind))
    return (
        abs(sa[0] - sb[0]) + abs(sa[1] - sb[1]) + abs(sa[2] - sb[2])
        >= min_distance
    )

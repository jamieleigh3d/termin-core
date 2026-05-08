# Copyright 2026 Jamie-Leigh Blake and Termin project contributors
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0

"""Markdown sanitizer for the v0.9 `presentation-base.markdown` contract.

Per BRD #2 §7.3 and §5.1.3 + JL's morning resolutions (2026-04-27):

  Allowed:
    - emphasis: bold (**), italic (*), strike-through (~~)
    - links (URL-allowlisted: http://, https://, mailto:)
    - headers (#..######)
    - horizontal rules (---)
    - ordered lists (1. 2. 3.)
    - unordered lists (- * +)

  Stripped:
    - raw HTML (escaped to text)
    - inline HTML tags
    - embedded media (images)
    - code blocks (fenced ```...``` and indented)
    - inline backticks (code spans)
    - tables
    - blockquotes
    - underline (no markdown-standard syntax; deliberately deferred)

  URL safety:
    - link href must match http://, https://, or mailto: scheme
    - any other scheme (javascript:, data:, file:, etc.) → strip the
      link, leave the text content as plain text

The sanitizer renders to safe HTML. The output is suitable for
embedding inside a content fragment passed to a presentation provider
that implements `presentation-base.markdown`. Per BRD §7.3 ("contract-
specified, not provider discretion"), this layer is the runtime's
responsibility — providers receive already-sanitized HTML and decide
visual treatment, never raw markdown.

Implementation: markdown-it-py with disabled rules + a custom link
opener that filters URLs to the allowlist.
"""

from __future__ import annotations

import re
from typing import Final

from markdown_it import MarkdownIt
from markdown_it.token import Token


# Allowed URL schemes per BRD §7.3. mailto: included so contact-link
# patterns (`<mailto:foo@bar>`) work without inventing a separate
# verb. javascript:, data:, file:, vbscript:, etc. all rejected.
_ALLOWED_SCHEMES: Final = ("http://", "https://", "mailto:")

# Pattern for "starts with one of the allowed schemes." Case-insensitive
# because URL schemes are case-insensitive per RFC 3986.
_ALLOWED_SCHEME_RE: Final = re.compile(
    r"^(https?://|mailto:)",
    re.IGNORECASE,
)

# Pattern for "starts with any scheme" per RFC 3986: ALPHA *(ALPHA / DIGIT
# / "+" / "-" / ".") followed by `:`. Used to distinguish "has a scheme"
# from "relative reference" — the latter is allowed without checking the
# allowlist (relative URLs can't escape the app surface).
_HAS_SCHEME_RE: Final = re.compile(
    r"^[a-z][a-z0-9+.-]*:",
    re.IGNORECASE,
)


def _is_safe_url(url: str) -> bool:
    """Return True if the URL is safe per the v0.9 envelope.

    Logic:
      - If the URL has any scheme (matches `[a-z][a-z0-9+.-]*:`), the
        scheme MUST be on the allowlist (http, https, mailto). Reject
        javascript:, data:, file:, vbscript:, ftp:, ssh:, etc.
      - Otherwise (no scheme — relative URL), allow it. Relative URLs
        cannot escape the application's surface; they're commonly used
        for in-app navigation (#anchors, /paths, page.html, etc.).
    """
    if not isinstance(url, str):
        return False
    stripped = url.strip()
    if not stripped:
        return False
    if _HAS_SCHEME_RE.match(stripped):
        # Has a scheme — must match the allowlist
        return bool(_ALLOWED_SCHEME_RE.match(stripped))
    # No scheme — relative URL, safe by construction.
    return True


def _build_sanitizer() -> MarkdownIt:
    """Build the markdown-it instance with the v0.9 envelope.

    Starts from commonmark (the formal markdown spec) and disables
    rules outside the envelope. Disabling at the parser level is more
    robust than post-rendering scrub: tokens we don't want never get
    constructed.
    """
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})

    # Disable rules outside the envelope:
    #   - image: embedded media (BRD §7.3)
    #   - html_inline / html_block: raw HTML (BRD §7.3)
    #   - code (indented): code blocks (BRD §7.3)
    #   - fence: ``` fenced code blocks (BRD §7.3)
    #   - backticks: inline code spans (out of envelope; BRD lists
    #     only "headers, lists, links, etc." — no inline code in v0.9)
    #   - blockquote: out of envelope (not listed in BRD §7.3)
    md.disable(["image", "html_inline", "html_block", "code", "fence",
                "backticks", "blockquote"])

    # Enable strike-through (commonmark doesn't include it by default).
    md.enable("strikethrough")

    # Custom link renderer: URL allowlist. If link target fails the
    # allowlist, render the link text as plain text and drop the
    # anchor tag. This preserves user intent (the visible content)
    # while denying the unsafe action (the bad URL).
    _install_link_validator(md)

    return md


def _install_link_validator(md: MarkdownIt) -> None:
    """Replace `link_open` and `link_close` renderer rules with
    versions that filter URLs against the allowlist."""

    def render_link_open(self, tokens, idx, options, env):
        token = tokens[idx]
        href = ""
        for name, value in token.attrs.items():
            if name == "href":
                href = value
                break
        if not _is_safe_url(href):
            # Mark the token so render_link_close knows to drop the
            # closing tag. Marking via env to avoid mutating tokens.
            env.setdefault("_termin_dropped_links", set()).add(idx)
            return ""
        # Fall through to default behavior — but rebuild the open tag
        # from the safe attrs so we don't leak unexpected attributes.
        attrs_html = "".join(
            f' {k}="{_escape_attr(v)}"'
            for k, v in token.attrs.items()
            if k in ("href", "title")
        )
        return f"<a{attrs_html}>"

    def render_link_close(self, tokens, idx, options, env):
        # Find the matching link_open by walking backwards.
        depth = 0
        for j in range(idx - 1, -1, -1):
            if tokens[j].type == "link_close":
                depth += 1
            elif tokens[j].type == "link_open":
                if depth == 0:
                    if j in env.get("_termin_dropped_links", set()):
                        return ""
                    return "</a>"
                depth -= 1
        return "</a>"

    md.add_render_rule("link_open", render_link_open)
    md.add_render_rule("link_close", render_link_close)


def _escape_attr(value: str) -> str:
    """Escape a string for use as an HTML attribute value."""
    return (
        value.replace("&", "&amp;")
             .replace('"', "&quot;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
    )


# Module-level instance. markdown-it instances are stateless once
# configured; reusing one across calls saves per-call setup.
_SANITIZER: Final = _build_sanitizer()


def sanitize_markdown(text: str) -> str:
    """Render the given markdown text to safe HTML per the BRD §7.3
    envelope. Returns an empty string for empty/None input."""
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    return _SANITIZER.render(text)

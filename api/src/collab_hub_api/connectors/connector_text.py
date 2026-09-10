from __future__ import annotations

import re

# These helpers neutralize link-shaped text in connector output at the Collab Hub
# boundary, so every Apollo agent runtime receives the same safe contract. The
# original driver (apollo-desktop#365 — a chat renderer that crashed on links in
# tool output) is now fixed, but the masking is retained as defense-in-depth
# against link/markup injection through untrusted tool output. Retiring the shared
# bare-domain masking is a separate, security-reviewed change (see the
# generic-read threat model), not something to drop here.

_LINK_PLACEHOLDER = "[link]"
_TRAILING_PUNCTUATION = ").,;:!?]}>\"'"
_LINK_TARGET = r"(?:(?:https?|ftp|mailto|tel|slack):|www\.)"
_MARKDOWN_LINK = re.compile(
    rf"\[(?P<label>[^\]]*)\]\(\s*<?{_LINK_TARGET}[^\s)>]+>?\s*\)",
    re.IGNORECASE,
)
_PLAIN_URL = re.compile(rf"{_LINK_TARGET}[^\s<>|]+", re.IGNORECASE)
_EMAIL_ADDRESS = re.compile(
    r"(?<![\w.+-])(?P<local>[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+)@"
    r"(?P<domain>(?:[A-Z0-9-]+\.)+[A-Z]{2,})(?![\w.-])",
    re.IGNORECASE,
)
_BARE_DOMAIN = re.compile(
    r"(?<![@\w.-])(?:[A-Z0-9-]+\.)+[A-Z]{2,}(?:/[A-Z0-9._~:/?#\[\]@!$&'()*+,;=%-]*)?",
    re.IGNORECASE,
)


def _render_markdown_link(match: re.Match[str]) -> str:
    label = match.group("label").strip()
    return label or _LINK_PLACEHOLDER


def _render_plain_url(match: re.Match[str]) -> str:
    url = match.group(0)
    trailing = ""
    while url and url[-1] in _TRAILING_PUNCTUATION:
        trailing = url[-1] + trailing
        url = url[:-1]
    return _LINK_PLACEHOLDER + trailing


def _render_email(match: re.Match[str]) -> str:
    """Keep addresses useful to the model without leaving a mailto-shaped token."""
    local = match.group("local")
    domain = match.group("domain").replace(".", " [dot] ")
    return f"{local} [at] {domain}"


def sanitize_github_api_text(text: str) -> str:
    """Neutralize link-shaped text while preserving code-shaped text.

    The generic GitHub read tool returns diffs, file contents, and dotted
    identifiers (``config.py``, ``os.path``, SHAs, refs) where bare-domain masking
    is destructive: every dotted attribute access reads as a bare domain, so a
    code diff comes back riddled with ``[link]``. This variant drops bare-domain
    masking entirely — reading code diffs is the tool's headline use case — while
    still masking scheme (``https://``, ``mailto:``, ...), ``www.``, Markdown-link,
    and email shapes, which are unambiguous and injection-relevant regardless of
    code context. (The renderer-crash driver, apollo-desktop#365, is now fixed;
    the strict :func:`sanitize_connector_text` still masks bare domains as
    defense-in-depth.)

    This is the shared core: :func:`sanitize_connector_text` is exactly this plus
    bare-domain masking, so the two never drift on the markdown/URI/email steps.
    """
    if not text:
        return text
    without_markdown_targets = _MARKDOWN_LINK.sub(_render_markdown_link, text)
    without_urls = _PLAIN_URL.sub(_render_plain_url, without_markdown_targets)
    return _EMAIL_ADDRESS.sub(_render_email, without_urls)


def sanitize_connector_text(text: str) -> str:
    """Replace Markdown, URI, email, AND bare-domain targets with safe text.

    The strict variant used by every non-generic connector surface
    (Gmail/Calendar/Drive/Slack + the curated GitHub tools). Defined as the
    code-aware :func:`sanitize_github_api_text` followed by bare-domain masking so
    the injection-relevant regex sequence lives in exactly one place. Behavior is
    byte-identical to the previous inline pipeline.
    """
    if not text:
        return text
    return _BARE_DOMAIN.sub(_render_plain_url, sanitize_github_api_text(text))

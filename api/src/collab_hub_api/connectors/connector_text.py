from __future__ import annotations

import re

# Apollo currently cannot safely render link-shaped text returned by tools
# (apollo-desktop#365). Keep Google Workspace connector output link-free until
# that renderer issue is fixed. The sanitization lives at the Collab Hub boundary so
# every Apollo agent runtime receives the same safe contract.

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


def sanitize_connector_text(text: str) -> str:
    """Replace Markdown, URI, email, and bare-domain targets with safe text."""
    if not text:
        return text
    without_markdown_targets = _MARKDOWN_LINK.sub(_render_markdown_link, text)
    without_urls = _PLAIN_URL.sub(_render_plain_url, without_markdown_targets)
    without_emails = _EMAIL_ADDRESS.sub(_render_email, without_urls)
    return _BARE_DOMAIN.sub(_render_plain_url, without_emails)

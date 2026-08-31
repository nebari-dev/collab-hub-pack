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
# A bare domain and a dotted code token are syntactically indistinguishable in
# general (``config.py`` could be a host). Keep the small exception set limited
# to common source/config file suffixes, which are frequent in diffs and useful
# to preserve. Everything else remains link-free for the Apollo renderer.
_GITHUB_CODE_FILE_SUFFIXES = frozenset(
    {
        "c",
        "cc",
        "cpp",
        "cs",
        "css",
        "go",
        "h",
        "hpp",
        "html",
        "java",
        "js",
        "json",
        "jsx",
        "kt",
        "md",
        "mjs",
        "php",
        "py",
        "rb",
        "rs",
        "sh",
        "sql",
        "swift",
        "toml",
        "ts",
        "tsx",
        "xml",
        "yaml",
        "yml",
    }
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


def _render_github_bare_domain(match: re.Match[str]) -> str:
    """Preserve common filename tokens, but mask all other bare domains."""
    token = match.group(0)
    if "/" not in token and token.rsplit(".", 1)[-1].lower() in _GITHUB_CODE_FILE_SUFFIXES:
        return token
    return _render_plain_url(match)


def sanitize_connector_text(text: str) -> str:
    """Replace Markdown, URI, email, and bare-domain targets with safe text."""
    if not text:
        return text
    without_markdown_targets = _MARKDOWN_LINK.sub(_render_markdown_link, text)
    without_urls = _PLAIN_URL.sub(_render_plain_url, without_markdown_targets)
    without_emails = _EMAIL_ADDRESS.sub(_render_email, without_urls)
    return _BARE_DOMAIN.sub(_render_plain_url, without_emails)


def sanitize_github_api_text(text: str) -> str:
    """Neutralize link-shaped text while preserving code-shaped text.

    The generic GitHub read tool returns diffs, file contents, and dotted
    identifiers (``config.py``, SHAs, refs) where the shared
    :func:`sanitize_connector_text`'s bare-domain masking is destructive
    (``config.py`` -> ``[link]``). Bare domains are still masked because they can
    crash Apollo's renderer; only a bounded set of common filename suffixes is
    preserved. This variant otherwise runs the same Markdown / URI / email
    masking.

    This is additive: :func:`sanitize_connector_text` and all its callers
    (Gmail/Calendar/Drive/Slack + the curated GitHub tools) are unchanged.
    """
    if not text:
        return text
    without_markdown_targets = _MARKDOWN_LINK.sub(_render_markdown_link, text)
    without_urls = _PLAIN_URL.sub(_render_plain_url, without_markdown_targets)
    without_emails = _EMAIL_ADDRESS.sub(_render_email, without_urls)
    return _BARE_DOMAIN.sub(_render_github_bare_domain, without_emails)

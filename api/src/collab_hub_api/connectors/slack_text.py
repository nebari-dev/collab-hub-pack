from __future__ import annotations

import re
import unicodedata

# Slack message text embeds links as ``<target|label>`` angle-bracket entities. This
# module renders that markup as readable text and keeps the exact http(s) target of
# every link, so the model can cite or follow it: Slack shortens long labels with an
# ellipsis, so a label alone cannot recover its target (#143). Only http and https
# targets are kept; any other scheme (javascript:, data:, file:, tel:, slack://, ...)
# is reduced to its label or ``[link]``, and so is an http(s) target carrying
# characters a URL never needs. Links were once stripped entirely to work around an
# Apollo renderer crash on link-shaped text (apollo-desktop#365); that crash was
# fixed in apollo-desktop PR #414.

_LINK_PLACEHOLDER = "[link]"
_TRAILING_PUNCTUATION = ").,;:!?]}>\"'"

# Slack wraps links and mentions in angle brackets: <target>, <target|label>. Real
# ``<`` / ``>`` in message text arrive HTML-escaped (``&lt;`` / ``&gt;``), so a bare
# ``<...>`` is always Slack markup, never literal user text.
_SLACK_ENTITY = re.compile(r"<(?P<target>[^<>|]*)(?:\|(?P<label>[^<>]*))?>")
_HTTP_SCHEME = re.compile(r"https?://", re.IGNORECASE)
# URLs inside the label of a link whose target is not kept.
_PLAIN_URL = re.compile(r"(?:https?://|www\.)[^\s<>|]+", re.IGNORECASE)
_UNSAFE_URL_CHARACTERS = frozenset("<>\"'")


def _http_target(target: str) -> str | None:
    """Return the exact http(s) URL for a link target, or ``None`` to neutralize it."""
    if not _HTTP_SCHEME.match(target):
        return None
    # Slack escapes ``&`` in message text, including inside link targets.
    url = target.replace("&amp;", "&")
    for char in url:
        if char.isspace() or char in _UNSAFE_URL_CHARACTERS or unicodedata.category(char) == "Cc":
            return None
    return url


def _comparable_url(url: str) -> str:
    return _HTTP_SCHEME.sub("", url.replace("&amp;", "&"), count=1).rstrip("/").lower()


def _render_entity(match: re.Match[str]) -> str:
    target = match.group("target") or ""
    label = (match.group("label") or "").strip()
    if target.startswith("@"):
        # User/bot mention: <@U123> or <@U123|name>.
        return label or target
    if target.startswith("#"):
        # Channel mention: <#C123|general> -> #general (fall back to the raw id).
        return f"#{label}" if label else target
    if target.startswith("!"):
        # Special mention: <!here>, <!channel>, <!subteam^S1|team>.
        if label:
            return f"@{label}"
        return f"@{target[1:].split('^', 1)[0]}"
    url = _http_target(target)
    if url is not None:
        # Auto-links repeat the target as the label (<https://x.test|x.test>).
        if not label or _comparable_url(label) == _comparable_url(url):
            return url
        return f"{label} ({url})"
    # Any other target (mailto, tel, javascript, slack action links, unsafe http)
    # keeps only its label, with URLs in the label neutralized too.
    if label:
        return _PLAIN_URL.sub(_render_plain_url, label)
    if target.lower().startswith("mailto:"):
        return target[len("mailto:") :] or _LINK_PLACEHOLDER
    return _LINK_PLACEHOLDER


def _render_plain_url(match: re.Match[str]) -> str:
    url = match.group(0)
    trailing = ""
    while url and url[-1] in _TRAILING_PUNCTUATION:
        trailing = url[-1] + trailing
        url = url[:-1]
    return _LINK_PLACEHOLDER + trailing


def sanitize_slack_text(text: str) -> str:
    """Return ``text`` with Slack entities rendered as readable text.

    User/channel mentions keep their human-readable form (``@name``, ``#general``).
    An http(s) link keeps its exact target, after its label when the label differs
    (``label (url)``); any other link is replaced with its label, or ``[link]`` when
    there is none. Plain URLs typed into the message are left as they are.
    """
    if not text:
        return text
    return _SLACK_ENTITY.sub(_render_entity, text)

from __future__ import annotations

import re

# Apollo's chat renderer crashes on link-shaped text anywhere in a tool result
# (apollo-desktop#365), not only in a dedicated URL field. Slack message text can
# embed links two ways -- Slack's own ``<target|label>`` angle-bracket entities and
# plain inline URLs -- so we reduce both to link-free, human-readable text before the
# connector ever returns a Slack string. This keeps the model from echoing a link
# into chat and re-triggering the crash. Workaround tied to #365; revisit once the
# renderer handles links safely.

_LINK_PLACEHOLDER = "[link]"
_TRAILING_PUNCTUATION = ").,;:!?]}>\"'"

# Slack wraps links and mentions in angle brackets: <target>, <target|label>. Real
# ``<`` / ``>`` in message text arrive HTML-escaped (``&lt;`` / ``&gt;``), so a bare
# ``<...>`` is always Slack markup, never literal user text.
_SLACK_ENTITY = re.compile(r"<(?P<target>[^<>|]*)(?:\|(?P<label>[^<>]*))?>")
# Plain URLs the user typed directly into the message body.
_PLAIN_URL = re.compile(r"(?:https?://|www\.)[^\s<>|]+", re.IGNORECASE)


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
    # Anything else is a link target (http(s), mailto, tel, slack action links).
    if label:
        # The label can itself be a bare URL; the plain-URL pass below neutralizes it.
        return label
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
    """Return ``text`` with Slack link entities and plain URLs reduced to link-free text.

    User/channel mentions keep their human-readable form (``@name``, ``#general``);
    link targets are replaced with their label, or ``[link]`` when there is none.
    """
    if not text:
        return text
    without_entities = _SLACK_ENTITY.sub(_render_entity, text)
    return _PLAIN_URL.sub(_render_plain_url, without_entities)

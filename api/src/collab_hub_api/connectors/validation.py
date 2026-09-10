from __future__ import annotations


def has_control_or_nonprintable(text: str) -> bool:
    """True if ``text`` carries a control, DEL, zero-width, bidi, or otherwise
    non-printable code point.

    Shared by the generic ``api_get`` path check and the curated file-path/ref
    checks (routers/connectors.py) so this rejection class is defined once across
    the GitHub connector validators: ``str.isprintable()`` is False for every C0
    control (< 0x20), DEL (0x7f), and non-printable Unicode (non-breaking space,
    zero-width space ``\\u200b`` and friends, bidi marks), while ordinary ASCII
    spaces stay printable — callers that must also reject whitespace add their own
    ``str.isspace()`` test. Hardening this predicate covers every connector
    validator at once instead of leaving a bypass live in whichever wasn't patched.

    Scope: this deliberately does not reach ``web.py``'s ``_safe_next`` or
    ``frames/audit.py``, which keep their own narrower ``ord(ch) < 0x20 or
    ord(ch) == 0x7F`` idiom for unrelated surfaces (redirect-target and audit-log
    sanitization); widening those is a separate change with its own blast radius.
    """
    return any(not ch.isprintable() for ch in text)

from __future__ import annotations


def has_control_or_nonprintable(text: str) -> bool:
    """True if ``text`` carries a control, DEL, zero-width, or otherwise
    non-printable code point.

    Shared by the generic ``api_get`` path check and the curated file-path/ref
    checks so this rejection class is defined once: ``str.isprintable()`` is False
    for every C0 control (< 0x20), DEL (0x7f), and non-printable Unicode
    (zero-width space ``\\u200b`` and friends), while ordinary spaces stay
    printable — callers that must also reject whitespace add their own
    ``str.isspace()`` test. Hardening this predicate covers every validator at
    once instead of leaving a bypass live in whichever one wasn't patched.
    """
    return any(not ch.isprintable() for ch in text)

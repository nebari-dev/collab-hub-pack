"""The onboarding email template (#155).

The copy lives in exactly one place — ``web/onboarding_email.txt`` — so there
is no second copy to drift from. What these tests protect instead is the
template's contract with the renderer: every placeholder the copy uses is one
something substitutes, the two that cannot be derived from an invitation are the
only ones that can survive a partial render, and the file stays wrapped for
plain-text email.

The failure these are written against is a future copy edit — someone adds
``[ORGANIZATION]`` to the template, nothing fills it in, and an invitee reads
it verbatim.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from importlib.resources import files

import pytest

from collab_hub_api.web.data_statement import DATA_STATEMENT_TEXT
from collab_hub_api.web.onboarding_email import (
    APP_INSTRUCTIONS_PLACEHOLDER,
    AUTOMATED_GREETING_NAME,
    DATA_STATEMENT_PLACEHOLDER,
    EXPIRY_PLACEHOLDER,
    LINK_PLACEHOLDER,
    NAME_PLACEHOLDER,
    RECIPIENT_PLACEHOLDER,
    SUBJECT,
    TEMPLATE_FILENAME,
    WRAP_WIDTH,
    format_expiry,
    render_for_automated_delivery,
    render_onboarding_email,
)

PLACEHOLDER_PATTERN = re.compile(r"\[[A-Z][A-Z /]*\]")

SUBSTITUTED = {
    LINK_PLACEHOLDER,
    RECIPIENT_PLACEHOLDER,
    EXPIRY_PLACEHOLDER,
    DATA_STATEMENT_PLACEHOLDER,
}
"""Placeholders the renderer fills in. No sender among them: the copy names
none, so nothing per-operator reaches the rendered message (#153)."""

LEFT_FOR_THE_CALLER = {NAME_PLACEHOLDER, APP_INSTRUCTIONS_PLACEHOLDER}
"""Placeholders the invitation cannot supply — a greeting and the app
instructions. The sending path supplies both and refuses to send otherwise;
these tests measure that they are the only two it *has* to."""


def _template_text() -> str:
    return files("collab_hub_api.web").joinpath(TEMPLATE_FILENAME).read_text(encoding="utf-8")


def _rendered(**overrides) -> str:
    kwargs = {
        "link": "https://frames.example.test/invite/accept#token=abc",
        "recipient": "invitee@example.test",
        "expires_at": datetime(2026, 8, 14, 21, 30, tzinfo=timezone.utc),
    }
    kwargs.update(overrides)
    return render_onboarding_email(**kwargs)


def test_the_template_ships_with_the_package() -> None:
    """Read as package data, not from the source tree.

    If this fails in a built artifact but passes in a checkout, the template
    was left out of the wheel and every invitation page would raise instead of
    rendering.
    """

    assert _template_text().startswith("Subject: ")
    assert SUBJECT == "Your invitation to the OpenTeams Collab beta"


CONDITIONAL_MARKERS = {"[IF VERIFIED EMAIL REQUIRED]", "[END IF]"}
"""Spans kept or dropped by configuration rather than filled in.

Their own set, so the contract test keeps saying which slots are *substituted*
and which are *selected* -- two different failure modes, and folding them
together would let a marker be renamed into a placeholder unnoticed.
"""


def test_every_placeholder_in_the_copy_is_one_something_fills_in() -> None:
    """The guard on future copy edits, in both directions.

    A placeholder added to the template that nothing substitutes would be sent
    to an invitee verbatim; a placeholder the renderer substitutes that no
    longer appears in the template is a rename that silently stopped working.

    Conditional markers are spelled like placeholders on purpose -- see
    ``CONDITIONAL_BLOCK`` -- so they appear here too, and one added without
    being taught to the renderer fails this rather than reaching an invitee.
    """

    in_template = set(PLACEHOLDER_PATTERN.findall(_template_text()))
    assert in_template == SUBSTITUTED | LEFT_FOR_THE_CALLER | CONDITIONAL_MARKERS


def test_only_the_two_unknowable_placeholders_survive_rendering() -> None:
    """Anything else left in ``[BRACKETS]`` would be read by a real person."""

    assert set(PLACEHOLDER_PATTERN.findall(_rendered())) == LEFT_FOR_THE_CALLER


def test_the_email_carries_the_data_statement_verbatim() -> None:
    """#146's constant is the single source; the email must not paraphrase it.

    Whitespace-insensitive because the statement is wrapped for plain text
    here and rendered by CSS on the page — the words must match, the line
    breaks need not.
    """

    words = lambda text: " ".join(text.split())  # noqa: E731 - one-line local
    assert words(DATA_STATEMENT_TEXT) in words(_rendered())


def test_the_invitation_specifics_are_filled_in_not_left_to_the_operator() -> None:
    """The whole point of #155: link, address and expiry are already there."""

    rendered = _rendered()
    assert "https://frames.example.test/invite/accept#token=abc" in rendered
    assert "invitee@example.test" in rendered
    assert "2026-08-14 21:30 UTC" in rendered
    assert rendered.rstrip().endswith("The OpenTeams Collab team")


def test_the_copy_stays_wrapped_for_plain_text_email() -> None:
    """A guard on copy edits: unwrapped paragraphs render badly in mail clients.

    Rendered with short substitutions so the only thing under test is the
    template's own wrapping. The invitation link is exempt — it is a URL on its
    own line and wrapping it would break it, which is why the renderer never
    re-flows anything it substitutes.
    """

    rendered = _rendered(link="https://x.test/a#token=b", recipient="a@b.test")
    too_long = [line for line in rendered.splitlines() if len(line) > WRAP_WIDTH]
    assert too_long == []


def test_a_naive_expiry_is_refused_rather_than_assumed_to_be_utc() -> None:
    """An expiry is a deadline; reinterpreting its timezone moves it."""

    with pytest.raises(ValueError, match="timezone-aware"):
        format_expiry(datetime(2026, 8, 14, 21, 30))


# ===========================================================================
# Automated delivery (#93): the same copy, with nothing left for a human
# ===========================================================================

DELIVERY_KWARGS = {
    "link": "https://frames.example.test/invite/accept#token=abc",
    "recipient": "invitee@example.test",
    "expires_at": datetime(2026, 8, 14, 21, 30, tzinfo=timezone.utc),
    "app_instructions": "Download the app from https://example.test/download",
}


def test_automated_delivery_leaves_no_placeholder_for_the_reader() -> None:
    """Nobody proof-reads a sent message: the invitee just reads what is there."""

    body = render_for_automated_delivery(**DELIVERY_KWARGS)
    assert not PLACEHOLDER_PATTERN.findall(body)
    assert body.startswith(f"Hi {AUTOMATED_GREETING_NAME},")
    assert DELIVERY_KWARGS["app_instructions"] in body


def test_automated_delivery_still_renders_the_same_approved_copy() -> None:
    """The point of #93: one text, whoever sends it.

    The sent body must differ from a partial render *only* by the two
    substitutions a human would otherwise have made — which is what made
    automating delivery a substitution rather than a rewrite.
    """

    sent = render_for_automated_delivery(**DELIVERY_KWARGS)
    pasted = render_onboarding_email(
        **{k: v for k, v in DELIVERY_KWARGS.items() if k != "app_instructions"}
    )
    assert sent == (
        pasted.replace(NAME_PLACEHOLDER, AUTOMATED_GREETING_NAME).replace(
            APP_INSTRUCTIONS_PLACEHOLDER, DELIVERY_KWARGS["app_instructions"]
        )
    )


def test_automated_delivery_refuses_empty_app_instructions() -> None:
    """No truthful default exists, so there is nothing to fall back to."""

    with pytest.raises(ValueError, match="app_instructions"):
        render_for_automated_delivery(**{**DELIVERY_KWARGS, "app_instructions": "   "})


def test_automated_delivery_refuses_a_placeholder_added_to_the_copy_later() -> None:
    """The guard is a pattern, not a list of the two known slots.

    A future copy edit that adds ``[ORGANIZATION]`` and nothing to fill it must
    fail here rather than mail the brackets to somebody.
    """

    import collab_hub_api.web.onboarding_email as module

    original = module._BODY_TEMPLATE
    module._BODY_TEMPLATE = original.replace(
        "Thanks,", "Sent on behalf of [ORGANIZATION].\n\nThanks,", 1
    )
    try:
        with pytest.raises(ValueError, match=r"\[ORGANIZATION\]"):
            render_for_automated_delivery(**DELIVERY_KWARGS)
    finally:
        module._BODY_TEMPLATE = original


# ---------------------------------------------------------------------------
# Copy that follows the configuration (#45)
# ---------------------------------------------------------------------------


def _rendered_with(*, require_verified_email: bool) -> str:
    return render_for_automated_delivery(
        link="https://web.test/invite#token=" + "T" * 43,
        recipient="invitee@example.com",
        expires_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        app_instructions="Download it from https://example.test/collab",
        require_verified_email=require_verified_email,
    )


def test_the_verification_instruction_follows_the_configuration() -> None:
    """The whole point: the copy describes the flow the deployment runs.

    A deployment that stopped requiring verification while still telling
    invitees to watch for a verification email would have recreated the defect
    #171 fixed, with the sign flipped -- and the invitee would be waiting for
    mail that never comes, exactly as they were before #171.
    """

    strict = _rendered_with(require_verified_email=True)
    relaxed = _rendered_with(require_verified_email=False)

    assert "verify the address" in strict
    assert "verify" not in relaxed.lower(), "no instruction about a mail that will not arrive"


def test_neither_variant_leaks_a_marker_to_the_reader() -> None:
    """Kept or dropped, the markers themselves must never be sent."""

    for flag in (True, False):
        body = _rendered_with(require_verified_email=flag)
        assert "[IF" not in body and "[END IF]" not in body
        assert set(PLACEHOLDER_PATTERN.findall(body)) == set()


def test_dropping_the_block_leaves_the_paragraphs_correctly_spaced() -> None:
    """Whitespace asserted rather than reasoned about.

    A dropped span that took its separator with it would run two paragraphs
    together; one that left its separator behind would open a double blank
    before step 3. Both read fine in a diff and wrong in a mail client.
    """

    relaxed = _rendered_with(require_verified_email=False).splitlines()
    step_three = relaxed.index("3. ACCEPT YOUR INVITATION")
    assert relaxed[step_three - 1] == "", "step 3 keeps its blank line above"
    assert relaxed[step_three - 2] == "   not substitute another one.", (
        "the paragraph above step 3 is the one the dropped span followed"
    )
    assert relaxed[step_three - 3] != "", "exactly one blank line, not two"


def test_the_step_count_is_the_same_either_way() -> None:
    """The variable span is a paragraph inside step 2, not a step.

    Which is what keeps this simple: nothing renumbers, and the opening line
    that promises four steps stays true under both configurations.
    """

    for flag in (True, False):
        body = _rendered_with(require_verified_email=flag)
        numbered = [line for line in body.splitlines() if re.match(r"^\d\. ", line)]
        assert len(numbered) == 4, numbered
        assert "four steps" in body


def test_the_strict_variant_is_what_a_caller_gets_by_default() -> None:
    """Fail safe: copy that over-explains is a nuisance, copy that omits a
    required step strands the reader."""

    default = render_for_automated_delivery(
        link="https://web.test/invite#token=" + "T" * 43,
        recipient="invitee@example.com",
        expires_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        app_instructions="Download it from https://example.test/collab",
    )
    assert default == _rendered_with(require_verified_email=True)


def test_a_marker_the_pattern_cannot_match_names_itself(monkeypatch) -> None:
    """CRLF or a trailing space must not turn resolution into a silent no-op.

    Without this guard the failure surfaces far away and unrecognisably: the
    placeholder net raises, `deliver` catches it, and every invitation email
    stops sending under `invalid_invitation_email` -- an error code pointing at
    recipient and URL validation rather than at the template. `_load_template`
    is CRLF-tolerant, so the conditional narrowed an existing tolerance.
    """

    import collab_hub_api.web.onboarding_email as module

    crlf = module._BODY_TEMPLATE.replace("[IF VERIFIED EMAIL REQUIRED]\n", "[IF VERIFIED EMAIL REQUIRED]\r\n")
    monkeypatch.setattr(module, "_BODY_TEMPLATE", crlf)

    for flag in (True, False):
        with pytest.raises(ValueError, match="conditional marker survived"):
            render_onboarding_email(
                link="https://web.test/invite#token=T",
                recipient="invitee@example.com",
                expires_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
                name="Ada",
                app_instructions="Download it",
                require_verified_email=flag,
            )


def test_a_trailing_space_after_a_marker_is_caught_too(monkeypatch) -> None:
    """The copy-edit version of the same hazard."""

    import collab_hub_api.web.onboarding_email as module

    spaced = module._BODY_TEMPLATE.replace("[END IF]\n", "[END IF] \n")
    monkeypatch.setattr(module, "_BODY_TEMPLATE", spaced)

    with pytest.raises(ValueError, match="conditional marker survived"):
        render_onboarding_email(
            link="https://web.test/invite#token=T",
            recipient="invitee@example.com",
            expires_at=datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
            name="Ada",
            app_instructions="Download it",
            require_verified_email=True,
        )

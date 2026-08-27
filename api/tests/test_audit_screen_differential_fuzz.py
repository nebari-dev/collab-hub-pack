"""Differential fuzz of the audit SQL screen against a live PostgreSQL server.

The property under test is the soundness direction of the screen: any
statement text ``_screen_sql`` ALLOWS must be unable to end, replace, or
commit the explicit transaction it runs in, under EITHER of PostgreSQL's
quoting regimes (``standard_conforming_strings`` on and off). Over-refusal
is by design — the screen refuses legal-but-ambiguous text — so a payload
the screen refuses needs no live verdict and gets none.

Why this exists as a standing gate rather than a one-time claim: every hole
found in the screen has been a *lexing* disagreement with the server (five
review rounds, five distinct disagreements — backslash literals, ``name'...'``
constants, identifier-absorbed dollar tags, PostgreSQL's byte-level identifier
characters, and CR-terminated line comments). Each round's fix is pinned by a
curated regression payload in ``test_operator_foundation.py``; this fuzz is
the instrument that looks for the next disagreement **among recombinations of
the constructs that have already bitten**, so it has to live in the repo and
run against the same live server as the other proofs.

That qualifier is the honest bound and not modesty. The alphabet below is
closed and was derived from five known holes, so what this finds is new
*combinations* of known constructs. A genuinely novel construct has no
fragment here and therefore cannot be generated at all — the ordinary limit
of grammar-based fuzzing, worth stating so the sweep is not read as a search
over PostgreSQL's whole lexical surface.

The generator is deterministic (seeded), built from fragments chosen at the
lexer's decision points: quote openers and their prefix forms, dollar-quote
tags in both their opening and identifier-absorbed readings, both comment
forms and both their terminators, statement separators, and the guarded
keywords themselves. Pairwise products run always; a seeded sample of longer
compositions and single-fragment mutations of every historical escape round
it out.

Gated on ``COLLAB_HUB_TEST_POSTGRES_URL`` like the other live suites (CI provides
a disposable ``postgres:16-alpine``).

**The default sweep is the gate.** It is what runs on every PR, and it is
therefore the whole of the coverage CI provides. ``NEXUS_FUZZ_FULL=1``
selects a roughly 11,000-input sweep, and *nothing in this repository sets
it* — no workflow schedules it and no job exports it. It is a developer
affordance for someone deliberately widening the search by hand, not a
standing gate, and the larger number must not be read as coverage anyone is
receiving. If that should change, the change is a scheduled workflow running
the full sweep against the same disposable server, and this paragraph should
be rewritten when it lands rather than left to imply it already has.
"""

from __future__ import annotations

import itertools
import os
import random

import psycopg
import pytest
from psycopg import pq

from collab_hub_api.frames.audit import AuditTransactionViolation, _screen_sql

POSTGRES_URL = os.environ.get("COLLAB_HUB_TEST_POSTGRES_URL", "")
FULL_SWEEP = os.environ.get("NEXUS_FUZZ_FULL", "") == "1"

live_postgres = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="set COLLAB_HUB_TEST_POSTGRES_URL to a disposable database to run the differential fuzz",
)

# The three shapes an escape leaves behind; see _escape_shape for what each
# one means and why a bare error in place is deliberately not among them.
TERMINATED = "terminated"
SWAPPED_THEN_ABORTED = "swapped-then-aborted"
SWAPPED = "swapped"

ESCAPE_SHAPES = (TERMINATED, SWAPPED_THEN_ABORTED, SWAPPED)

# Fragments sit at the scanner's decision points; the products below are what
# exercise the boundaries *between* them, which is where every historical
# disagreement lived.
_FRAGMENTS = (
    # quote machinery, including every prefix the lexer special-cases
    "'",
    "''",
    "\\'",
    "\\",
    '"',
    "E'",
    "e'",
    "n'",
    "U&'",
    # the identifier form of the unicode-escape prefix, distinct from U&' above:
    # it opens a quoted *identifier*, so the lexer leaves it by a different door.
    'U&"',
    "UESCAPE",
    "B'",
    "x'",
    "name",
    "name'x'",
    # dollar quoting: openers, the identifier-absorbed reading, positional params
    "$",
    "$$",
    "$a$",
    "$tag$",
    "x$tag$",
    "á$tag$",
    # non-ASCII *inside* a tag, not merely before the opener. Round 5 widened
    # both _is_tag_start and _is_tag_char to admit any non-ASCII code point;
    # the fragment above it only ever exercised the character preceding the
    # dollar, leaving the half of that fix governing tag bodies ungenerated.
    "$á$",
    "$1",
    # both comment forms and both line-comment terminators
    "--",
    "\n",
    "\r",
    "/*",
    "*/",
    # statement structure
    ";",
    " ",
    "SELECT 1",
    "AS x",
    # the guarded keywords, in shapes that lead a statement when exposed
    "COMMIT",
    "commit",
    ";COMMIT;",
    "ROLLBACK",
    "END",
    "ABORT",
)

# Every escape a past round proved live (the curated regression list keeps
# these refused; here they seed mutations that probe *near* known holes).
_HISTORICAL_ESCAPES = (
    r"SELECT '\'; COMMIT; SELECT 'x'",
    r"SELECT '\', 'x; COMMIT; --'",
    r"SELECT name'abc\'; COMMIT; SELECT name'x'",
    "/* /* */ ' */ COMMIT; SELECT '",
    "SELECT 1 AS x$tag$; COMMIT; SELECT $tag$foo$tag$;",
    "SELECT 1 AS á$tag$; COMMIT; SELECT $tag$foo$tag$;",
    "SELECT 1; -- x\rCOMMIT; SELECT 2",
    "SELECT 1 -- x\r; COMMIT",
)


# One entry above is a *fragment*, not a complete statement, and standalone it
# is not an escape at all: it ends having opened a literal, so PostgreSQL
# rejects the whole string at parse time and the smuggled COMMIT never runs.
# It becomes live the moment anything closes that literal — which is exactly
# what the generator does below when it appends a fragment to an escape, and
# what surrounding SQL would do in a hand-written audited() body. A lone quote
# is the smallest such closer; with it, the payload escapes under both regimes.
#
# Recorded here rather than by editing the payload, because the payload is
# also the screen-refusal regression fixture and must stay byte-for-byte what
# the round that found it refused.
_ESCAPE_CLOSERS = {
    "/* /* */ ' */ COMMIT; SELECT '": "'",
}

# Inputs built to drive one detector path each. Deliberately not lexing
# disagreements and deliberately not in _HISTORICAL_ESCAPES: the screen
# refuses all three on sight, which is asserted before they are run. They
# exist because the historical corpus reaches only the first shape.
_DETECTOR_FIXTURES = (
    (TERMINATED, "COMMIT"),
    (SWAPPED, "COMMIT; BEGIN"),
    (SWAPPED_THEN_ABORTED, "ROLLBACK; BEGIN; SELECT 1/0"),
)


def _generated_inputs() -> list[str]:
    inputs: list[str] = []
    seen: set[str] = set()

    def add(text: str) -> None:
        if text and text not in seen:
            seen.add(text)
            inputs.append(text)

    for pair in itertools.product(_FRAGMENTS, repeat=2):
        add("".join(pair))

    # Seeded, so a failure names a payload that will be generated again on
    # every future run — a fuzz that cannot re-run is a measurement, not a
    # gate.
    rng = random.Random(0x5EED_2026)
    for _ in range(9000 if FULL_SWEEP else 700):
        add("".join(rng.choices(_FRAGMENTS, k=rng.randint(3, 6))))

    for escape in _HISTORICAL_ESCAPES:
        add(escape)
        for fragment in _FRAGMENTS:
            add(fragment + escape)
            add(escape + fragment)

    return inputs


def _screen_allows(text: str) -> bool:
    try:
        _screen_sql(text)
    except AuditTransactionViolation:
        return False
    return True


def _escape_shape(conn: psycopg.Connection, payload: str) -> str | None:
    """*How* did *payload* end or replace the transaction it ran in — or None.

    Three detectors, because escapes leave three different shapes behind:
    a terminated session (``COMMIT``/``ROLLBACK`` alone → IDLE), a *swapped*
    transaction (``COMMIT; BEGIN`` → still INTRANS, different txid), and a
    swapped-then-aborted one (``ROLLBACK; BEGIN; <error>`` → INERROR, but the
    probe savepoint died with the original transaction). A payload that
    merely errors in place leaves the original transaction aborted but
    intact, which the audited() backstop already turns into a loud failure —
    not a silent escape.

    Returning *which* detector fired, rather than a bare bool, is what lets
    :func:`test_live_every_historical_escape_is_seen_by_the_detector` assert
    that all three paths actually execute. The sweep itself only needs the
    bool, and :func:`_escapes_live` still gives it that — the semantics of
    the two are identical by construction, since one is defined as the other.
    """

    conn.execute("BEGIN")
    try:
        before = conn.execute("SELECT txid_current()").fetchone()[0]
        conn.execute("SAVEPOINT fuzz_probe")
        try:
            conn.execute(payload)
        except psycopg.Error:
            pass
        status = conn.info.transaction_status
        if status == pq.TransactionStatus.IDLE:
            return TERMINATED
        if status == pq.TransactionStatus.INERROR:
            try:
                conn.execute("ROLLBACK TO SAVEPOINT fuzz_probe")
            except psycopg.Error:
                return SWAPPED_THEN_ABORTED
        if conn.execute("SELECT txid_current()").fetchone()[0] != before:
            return SWAPPED
        return None
    finally:
        if conn.info.transaction_status != pq.TransactionStatus.IDLE:
            conn.execute("ROLLBACK")


def _escapes_live(conn: psycopg.Connection, payload: str) -> bool:
    """Did *payload* end or replace the explicit transaction it ran in?"""

    return _escape_shape(conn, payload) is not None


def test_the_generator_reproduces_every_historical_escape_as_refused():
    """Cheap, no server: the screen still refuses everything a past round
    proved live, and the generator is not degenerate (it must produce a
    healthy allowed set, or the live sweep proves nothing)."""

    for escape in _HISTORICAL_ESCAPES:
        assert not _screen_allows(escape), f"historical escape is allowed again: {escape!r}"

    inputs = _generated_inputs()
    allowed = sum(1 for text in inputs if _screen_allows(text))
    assert allowed >= len(inputs) // 20, "the generator collapsed into all-refused inputs"
    assert allowed <= len(inputs) - len(_HISTORICAL_ESCAPES), "the generator collapsed into all-allowed inputs"


@live_postgres
def test_live_every_historical_escape_is_seen_by_the_detector():
    """Positive coverage for the detector itself — the one part of this gate
    that nothing else checks.

    ``_escape_shape`` is called by the sweep over screen-*allowed* payloads
    only, and every historical escape is refused by the screen, so in a
    passing run none of them ever reaches it. Its three detection paths
    therefore never execute: a detector path could be wrong and the sweep
    would report zero disagreements for ever, which is indistinguishable
    from a clean run.

    Two things are asserted, and the second is the point:

    1. Every historical escape is detected — under **at least one** quoting
       regime, not both. ``SELECT '\\'; COMMIT; SELECT 'x'`` is live only with
       ``standard_conforming_strings`` **on**; its mirror
       ``SELECT '\\', 'x; COMMIT; --'`` only with it **off**. Demanding both
       would fail on every payload in the list, and that asymmetry is the
       reason the screen scans under both regimes in the first place.
    2. One payload is a *fragment*, and is run in the form in which it is
       actually live — see ``_ESCAPE_CLOSERS``.

    This does not duplicate
    ``test_live_lexer_disagreement_payloads_really_escape_and_are_really_refused``
    in ``test_operator_foundation.py``. That proves the payloads escape, by
    row persistence after a rollback — a different mechanism, which does not
    exercise these transaction-status detectors at all. It also runs five of
    these eight; the three it omits get their only live check here.

    Coverage of the other two detection paths is *not* asserted here, because
    this corpus cannot reach them — every historical escape ends in a bare
    ``COMMIT`` and so only ever produces ``TERMINATED``. That is what
    :func:`test_live_each_detector_path_fires_on_an_input_shaped_to_reach_it`
    is for.
    """

    seen: dict[str, dict[str, str]] = {}
    for regime in ("on", "off"):
        with psycopg.connect(POSTGRES_URL, autocommit=True) as conn:
            conn.execute(f"SET standard_conforming_strings = {regime}")
            for escape in _HISTORICAL_ESCAPES:
                live_form = escape + _ESCAPE_CLOSERS.get(escape, "")
                shape = _escape_shape(conn, live_form)
                if shape is not None:
                    seen.setdefault(escape, {})[regime] = shape

    undetected = [escape for escape in _HISTORICAL_ESCAPES if escape not in seen]
    assert not undetected, (
        f"{len(undetected)} historical escape(s) went undetected under BOTH quoting regimes — "
        f"the detector no longer sees an escape a past round proved live: "
        + "; ".join(repr(text) for text in undetected)
    )


@live_postgres
def test_live_each_detector_path_fires_on_an_input_shaped_to_reach_it():
    """Every detection path executes at least once, on an input built to reach it.

    The historical corpus cannot do this on its own. Every escape in it ends
    with a bare ``COMMIT``, which terminates the session, so all eight produce
    ``TERMINATED`` and the other two paths stay dark — which is the same
    invisible-failure problem one layer down. These three inputs are detector
    fixtures, not lexing disagreements: nothing here is a hole in the screen,
    and the assertion below keeps it that way by requiring the screen to
    refuse each of them.
    """

    for expected, payload in _DETECTOR_FIXTURES:
        assert not _screen_allows(payload), (
            f"detector fixture {payload!r} is ALLOWED by the screen — it is meant to be an "
            "obviously-refused input used to drive a detector path, so this is a screen hole"
        )

    observed: dict[str, str] = {}
    with psycopg.connect(POSTGRES_URL, autocommit=True) as conn:
        for expected, payload in _DETECTOR_FIXTURES:
            observed[expected] = _escape_shape(conn, payload) or "not detected at all"

    wrong = {expected: got for expected, got in observed.items() if got != expected}
    assert not wrong, "detector path(s) did not fire as expected — the detector's own logic has changed: " + "; ".join(
        f"expected {exp!r}, got {got!r}" for exp, got in wrong.items()
    )
    assert set(observed) == set(ESCAPE_SHAPES), "a detection path has no fixture driving it: " + ", ".join(
        sorted(set(ESCAPE_SHAPES) - set(observed))
    )


@live_postgres
def test_fuzz_no_allowed_statement_ends_the_transaction_under_either_regime():
    inputs = _generated_inputs()
    allowed = [text for text in inputs if _screen_allows(text)]

    disagreements: list[tuple[str, str]] = []
    for regime in ("on", "off"):
        with psycopg.connect(POSTGRES_URL, autocommit=True) as conn:
            conn.execute(f"SET standard_conforming_strings = {regime}")
            for text in allowed:
                if _escapes_live(conn, text):
                    disagreements.append((regime, text))

    assert not disagreements, (
        f"{len(disagreements)} allowed payload(s) ended the transaction on a live server — a new "
        f"lexing disagreement between _screen_sql and PostgreSQL: "
        + "; ".join(f"[scs={regime}] {text!r}" for regime, text in disagreements[:10])
    )

"""Strict reader for the CogSpec v0.1 ``COG.md`` frontmatter subset.

The specification bounds frontmatter to a small YAML subset -- scalars, lists
of scalars, and one flat ``metadata`` mapping of strings, every value written
on the line of its key -- so that an index can list and filter a catalog
"by reading only the Markdown frontmatter", with no YAML implementation and
no manifest parser. This module is that reader. It is a port of the spec
authors' reference validator: the grammar decisions are theirs, the shape is
this codebase's.

Two properties matter more than convenience:

- **Fail closed.** Anything outside the subset (block scalars, anchors,
  aliases, tags, nested mappings other than ``metadata``, tabs, values
  continued across lines) is reported, never guessed at. The spec's sharper
  guarantee is that frontmatter it accepts is valid YAML; a reader that
  accepted more would break that in the other direction.
- **Type-preserving.** Everything is read from text, but the reader tracks
  whether an unquoted scalar *is* a string under YAML 1.1 or 1.2 resolution.
  ``version: 0.1`` is a float to every consumer that runs a real YAML loader,
  so where the spec requires a string the reader reports it rather than hand
  the catalog a number that a later reader will disagree about.

The reader never raises on document content: every problem is a message in
``FrontmatterDocument.errors`` and the caller decides what a partially read
document is worth. It is not a conformance validator -- the spec's bundled
link-containment check (its check 6) is a publisher's concern, not a
catalog's -- but its field rules are the spec's own.
"""

from __future__ import annotations

import datetime
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

# Unquoted scalars whose plain form resolves to something other than a string
# are not strings. The forms below are the UNION of what YAML 1.1 and YAML 1.2
# resolve as non-strings, because a Cog does not get to choose which YAML its
# consumer runs: over-reporting costs an author one pair of quotes, while
# under-reporting hands a consumer an integer where it expected a name.
_INT_RE = re.compile(
    r"""^[+-]?(?:
          0b[01_]+                          # YAML 1.1 binary
        | 0o[0-7_]+                         # YAML 1.2 octal
        | 0[0-7_]+                          # YAML 1.1 octal
        | 0x[0-9a-fA-F_]+                   # hexadecimal
        | [0-9][0-9_]*(?::[0-5]?[0-9])*     # decimal, and 1.1 sexagesimal
    )$""",
    re.VERBOSE,
)
_FLOAT_RE = re.compile(
    r"""^(?:
          [+-]?(?:[0-9][0-9_]*\.[0-9_]*|\.[0-9][0-9_]*)(?:[eE][+-]?[0-9]+)?
        | [+-]?[0-9][0-9_]*[eE][+-]?[0-9]+              # 1.2 exponent without a dot
        | [+-]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*   # 1.1 sexagesimal float
        | [+-]?\.(?:inf|Inf|INF)
        | \.(?:nan|NaN|NAN)
    )$""",
    re.VERBOSE,
)
# ``y`` and ``n`` are booleans in YAML 1.1 even though several parsers read
# them as strings, so they belong in the union.
_BOOL_NULL_RE = re.compile(r"^(?:y|n|yes|no|true|false|on|off|null|~)$", re.IGNORECASE)
# ``0b_`` and ``0x_`` are a radix prefix with no digits: a loader matches the
# integer shape and then fails to build one, so the document does not load.
_EMPTY_RADIX_RE = re.compile(r"^[+-]?0[box]_+$")

# YAML 1.1 timestamps resolve to date/datetime objects.
_DATE_ONLY_RE = re.compile(r"^(?P<y>[0-9]{4})-(?P<m>[0-9]{2})-(?P<d>[0-9]{2})$")
_DATETIME_RE = re.compile(
    r"""^(?P<y>[0-9]{4})-(?P<m>[0-9]{1,2})-(?P<d>[0-9]{1,2})
        (?:[Tt]|[ \t]+)
        (?P<H>[0-9]{1,2}):(?P<M>[0-9]{2}):(?P<S>[0-9]{2})(?:\.[0-9]*)?
        (?:[ \t]*(?:Z|[-+](?P<zh>[0-9]{1,2})(?::(?P<zm>[0-9]{2}))?))?$""",
    re.VERBOSE,
)

# YAML forbids a plain scalar from beginning with an indicator character.
# ``-``, ``?`` and ``:`` are conditional: they may begin one when what follows
# is not whitespace, which is why ``-foo`` is a scalar and ``- foo`` is a block
# sequence entry. ``#`` is absent because a line beginning with it is a
# comment, handled before any value is parsed.
_PLAIN_FORBIDDEN_FIRST = ",[]{}&*!|>'\"%@`"
_PLAIN_CONDITIONAL_FIRST = "-?:"

# A field key is a plain scalar with no whitespace, ending at the first ``:``
# that is followed by whitespace or end of line -- the same place YAML ends
# it. The key itself may contain a colon (``a:b: v`` has the key ``a:b``) and a
# profile is free to use ``x.y`` or ``example.org/thing``. Only the leading
# character is constrained, to keep a key from starting with a character that
# would make the line something other than a mapping entry.
_KEY_INDICATORS = "&*!|>%@`,[]{}#\"'"
_TOP_LINE_RE = re.compile(r"^(\S+?):(?=[ \t]|$)[ \t]*(.*)$")
_META_LINE_RE = re.compile(r"^(\s+)(\S+?):(?=[ \t]|$)[ \t]*(.*?)[ \t]*$")
_BLOCK_ITEM_RE = re.compile(r"^(\s+)-[ \t]+(.*?)[ \t]*$")
_MAPPING_ITEM_RE = re.compile(r"^[^:'\"]*:(?:\s|$)")

_FLOW_CLOSERS = {"[": "]", "{": "}"}

# YAML's double-quoted escapes. The subset does not shorten this list: an
# escape is ordinary single-line syntax, so restricting it would refuse valid
# frontmatter for no benefit. ``\<newline>`` continuation is absent because a
# value never spans lines here.
_ESCAPES = {
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "t": "\t",
    "\t": "\t",
    "n": "\n",
    "v": "\v",
    "f": "\f",
    "r": "\r",
    "e": "\x1b",
    " ": " ",
    '"': '"',
    "/": "/",
    "\\": "\\",
    "N": "\x85",
    "_": "\xa0",
    "L": "\u2028",
    "P": "\u2029",
}
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}

# Field rules from the spec. v0.1 accepts only ``cog`` and ``cog [0.1]``:
# another well-formed version such as ``cog [0.2]`` names a Cog but must not be
# reported as v0.1 conforming.
TYPE_RE = re.compile(r"^cog( \[0\.1\])?$")
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NAME_MAX_LENGTH = 64
DESCRIPTION_MAX_LENGTH = 1024
KINDS = ("model", "context", "complete")

# Recommended fields that are strings by meaning. The spec's own validator
# checks only the required trio, ``kind`` and ``metadata``; the catalog also
# holds ``version`` and friends to the "values that must be strings" rule
# because a ``version: 0.1`` that one loader reads as a float and another as
# text is exactly the disagreement the rule exists to prevent.
_OPTIONAL_STRING_FIELDS = ("version", "publisher", "license", "homepage", "repository", "manifest_schema")

# Absolute references and ones that escape the root cannot name a bundled
# file. Backslash counts as a separator so a Windows-style path cannot hide
# either shape from normalization.
_WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


@dataclass
class _Scalar:
    """A parsed scalar plus whether it was written as a string.

    ``is_string`` is False when an unquoted value has a plain YAML int/float/
    bool/null/timestamp form, so field checks can reject non-string values even
    though everything is read from text.
    """

    value: str
    is_string: bool


_Parsed = dict[str, "_Scalar | list[_Scalar] | dict[str, _Scalar] | None"]


@dataclass
class FrontmatterDocument:
    """The readable content of one ``COG.md``.

    ``fields`` is the frontmatter as plain JSON-shaped data: scalar fields map
    to their text, lists to lists of text, ``metadata`` to a dict of text, and
    a bare ``key:`` to ``None``. A non-string plain scalar keeps its source
    text here (``"0.1"``), with the type problem reported in ``errors`` --
    the catalog shows what the author wrote, and says why it is wrong.

    ``parsed`` says whether the frontmatter block was found and read within
    the spec subset. Field-level problems (a missing ``name``, a non-string
    ``version``) leave it ``True``; only an unreadable document -- invalid
    UTF-8, no frontmatter, syntax outside the subset -- leaves it ``False``,
    in which case ``fields`` is empty because nothing could be trusted.

    ``manifest_path`` is set only when the ``manifest`` pointer is a usable
    relative path inside the bundle; ``manifest_schema`` only when it is a
    non-empty string. Both are ``None`` for a draft.
    """

    parsed: bool = False
    fields: dict[str, Any] = field(default_factory=dict)
    raw: str = ""
    body: str = ""
    errors: list[str] = field(default_factory=list)
    manifest_path: str | None = None
    manifest_schema: str | None = None


def read_cog_document(data: bytes) -> FrontmatterDocument:
    """Read ``COG.md`` bytes into a ``FrontmatterDocument``. Never raises on content."""

    doc = FrontmatterDocument()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        doc.errors.append("COG.md is not valid UTF-8")
        return doc
    # A UTF-8 BOM is valid at the start of a YAML stream and invisible in an
    # editor; refusing the whole file over it would be a puzzle, not a rule.
    text = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")

    frontmatter, body, err = _split_document(text)
    if err:
        doc.errors.append(err)
        # With no frontmatter the whole file is body: the store still serves
        # the Markdown even when it cannot be catalogued.
        doc.body = text if body is None else body
        return doc

    doc.raw = frontmatter
    doc.body = body
    parsed, err = _parse_frontmatter(frontmatter)
    if err:
        doc.errors.append(f"frontmatter is outside the supported YAML subset: {err}")
        return doc

    doc.parsed = True
    doc.fields = _plain(parsed)
    doc.errors.extend(_check_fields(parsed))
    doc.manifest_path, doc.manifest_schema, pointer_errors = _manifest_pointer(parsed)
    doc.errors.extend(pointer_errors)
    return doc


# ---------------------------------------------------------------------------
# Document splitting
# ---------------------------------------------------------------------------


def _split_document(text: str) -> tuple[str | None, str | None, str | None]:
    """Split into ``(frontmatter, body, error)``.

    ``frontmatter`` is ``None`` on error. ``body`` is still returned for an
    empty frontmatter block, since the Markdown after it is readable.
    """

    lines = text.split("\n")
    if lines[0] != "---":
        return None, None, "file does not begin with YAML frontmatter"
    for i in range(1, len(lines)):
        if lines[i] == "---":
            frontmatter = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            if frontmatter.strip() == "":
                return None, body, "frontmatter block is empty"
            return frontmatter, body, None
    return None, None, "unterminated frontmatter block"


# ---------------------------------------------------------------------------
# Subset parser
# ---------------------------------------------------------------------------


def _parse_frontmatter(text: str) -> tuple[_Parsed | None, str | None]:
    """Parse the frontmatter block. Return ``(data, None)`` or ``(None, error)``."""

    # YAML admits no tab as indentation or as separation around a plain
    # scalar. Refusing every tab is a shade stricter than that -- a tab inside
    # a quoted scalar is legal -- and buys a rule with no edge to miss: write
    # ``\t`` in a double-quoted value instead, where it is visible anyway.
    if "\t" in text:
        line_no = text[: text.index("\t")].count("\n") + 1
        return None, f"frontmatter line {line_no} contains a tab character"

    lines = text.split("\n")
    data: _Parsed = {}
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        if raw.strip() == "" or raw.lstrip().startswith("#"):
            i += 1
            continue
        if raw[:1] == " ":
            return None, f"unexpected indentation at frontmatter line {i + 1}"
        match = _TOP_LINE_RE.match(raw)
        if not match:
            if re.search(r"[ \t].*?:(?:[ \t]|$)", raw):
                return None, f"field key at frontmatter line {i + 1} contains whitespace or is quoted"
            return None, f"cannot parse frontmatter line {i + 1}"
        key = match.group(1)
        err = _key_error(key)
        if err:
            return None, err
        value_text, err = _strip_inline_comment(match.group(2))
        if err:
            return None, f"field {key!r}: {err}"
        if key in data:
            return None, f"duplicate key: {key}"
        if value_text == "":
            # Collect the following indented lines as a block: a list, or the
            # ``metadata`` mapping. Anything else nested is outside the subset.
            block = []
            j = i + 1
            while j < n:
                nxt = lines[j]
                if nxt.strip() == "" or nxt.lstrip().startswith("#"):
                    j += 1
                    continue
                if nxt[:1] == " ":
                    block.append(nxt)
                    j += 1
                else:
                    break
            if block:
                entries = [_BLOCK_ITEM_RE.match(line) for line in block]
                if all(entries):
                    items, err = _parse_block_list(entries)
                    if err:
                        return None, f"field {key!r}: {err}"
                    data[key] = items
                    i = j
                    continue
                if key != "metadata":
                    # A block with no ``key: value`` line is not a mapping at
                    # all: the author almost certainly continued the value onto
                    # the next line, which deserves its own message.
                    if not any(_META_LINE_RE.match(line) for line in block):
                        return None, (
                            f"value for {key!r} continues on the next line; "
                            "a value must be written on the line of its key"
                        )
                    return None, f"nested mapping under {key!r} is outside the supported subset"
                meta, err = _parse_metadata_block(block)
                if err:
                    return None, err
                data[key] = meta
                i = j
                continue
            # Bare ``key:`` (or a comment-only value) with no block is YAML null.
            data[key] = None
            i += 1
            continue
        if value_text[0] == "[":
            items, err = _parse_flow_list(value_text)
            if err:
                return None, f"field {key!r}: {err}"
            data[key] = items
            i += 1
            continue
        scalar, err = _parse_scalar(value_text)
        if err:
            return None, f"field {key!r}: {err}"
        data[key] = scalar
        i += 1
    return data, None


def _timestamp_match(text: str) -> re.Match[str] | None:
    return _DATE_ONLY_RE.match(text) or _DATETIME_RE.match(text)


def _timestamp_is_constructible(match: re.Match[str]) -> bool:
    """Whether a timestamp-shaped scalar names a real instant.

    A loader resolves this shape to a date and raises when the components are
    out of range, so ``9999-99-99`` is a value no consumer can construct.
    """

    parts = match.groupdict()
    try:
        datetime.date(int(parts["y"]), int(parts["m"]), int(parts["d"]))
    except ValueError:
        return False
    if parts.get("H") is None:
        return True
    if not (int(parts["H"]) < 24 and int(parts["M"]) < 60 and int(parts["S"]) < 60):
        return False
    if parts.get("zh") is not None and int(parts["zh"]) > 23:
        return False
    if parts.get("zm") is not None and int(parts["zm"]) > 59:
        return False
    return True


def _unconstructible_error(text: str) -> str | None:
    """Why a plain scalar resolves to a type a loader cannot build, or ``None``.

    Such a scalar makes the whole document fail to load, not merely the one
    value, and a key is as capable of being a broken date as a value is.
    """

    if _EMPTY_RADIX_RE.match(text):
        return "a radix prefix with no digits is not a number any loader can build; quote it"
    stamp = _timestamp_match(text)
    if stamp and not _timestamp_is_constructible(stamp):
        return "resembles a YAML timestamp but names no real date or time; quote it"
    return None


def _key_error(key: str) -> str | None:
    if key[0] in _KEY_INDICATORS:
        return f"key {key!r} begins with a YAML indicator character"
    err = _unconstructible_error(key)
    if err:
        return f"key {key!r} {err}"
    return None


def _plain_is_string(text: str) -> bool:
    """Whether an unquoted plain scalar is a string under both YAML 1.1 and 1.2."""

    return not (_INT_RE.match(text) or _FLOAT_RE.match(text) or _BOOL_NULL_RE.match(text) or _timestamp_match(text))


def _strip_inline_comment(raw: str) -> tuple[str | None, str | None]:
    """Return ``(text, error)`` for a raw post-colon value, honoring YAML comments.

    A comment begins at a ``#`` that starts the value or is preceded by
    whitespace, outside every quoted scalar and flow collection. Finding it
    means tracking quote state (``\\"`` inside double quotes and a doubled
    ``''`` inside single quotes do not close the run) and flow depth, so
    ``["a # b", c]`` keeps its hash. The same scan turns an unterminated quote
    or collection into an error here instead of a confusing failure later.
    """

    s = raw.strip()
    if s == "" or s[0] == "#":
        return "", None

    if s[0] not in "\"'[{":
        # A plain scalar: quotes and brackets are ordinary characters inside
        # it (``bad"name`` is not an unterminated string). Only a
        # whitespace-preceded ``#`` matters.
        match = re.search(r"[ \t]#", s)
        if match:
            s = s[: match.start()].rstrip()
        return s, None

    stack: list[str] = []
    quote: str | None = None
    i = 0
    end = len(s)
    while i < end:
        ch = s[i]
        if quote == '"':
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                quote = None
            i += 1
            continue
        if quote == "'":
            if ch == "'":
                if s[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "[{":
            stack.append(_FLOW_CLOSERS[ch])
        elif ch in "]}":
            if not stack:
                return None, f"{ch!r} closes a flow collection that was never opened"
            if stack.pop() != ch:
                return None, "mismatched flow collection delimiters"
        elif ch == "#" and (i == 0 or s[i - 1] in " \t[{,"):
            # Outside quotes a ``#`` opens a comment where a node could begin.
            # Truncating inside an open collection leaves it unterminated,
            # which is the error YAML reports for the same input.
            end = i
            break
        i += 1

    if quote:
        return None, "unterminated quoted string"
    if stack:
        return None, "unterminated flow collection"
    return s[:end].rstrip(), None


def _looks_like_mapping(text: str) -> bool:
    """Whether a list item is really a ``key: value`` pair rather than a scalar."""

    return bool(_MAPPING_ITEM_RE.match(text))


def _split_flow_items(inner: str) -> tuple[list[str] | None, str | None]:
    """Split a flow-sequence body on top-level commas, respecting quotes.

    Any nested ``[`` or ``{`` is refused rather than guessed at. A quote opens
    a quoted item only where the item begins, matching YAML: mid-item quotes
    are ordinary characters, so in ``[a"]"]`` the ``]`` is a real delimiter
    and the sequence is malformed even though the comment scan, which reads
    every quote, saw it as balanced. A quote this scan does open closes where
    that scan closed it, so an unterminated one never reaches here.
    """

    items: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(inner):
        ch = inner[i]
        if quote == '"':
            if ch == "\\":
                buf.append(inner[i : i + 2])
                i += 2
                continue
            buf.append(ch)
            if ch == '"':
                quote = None
            i += 1
            continue
        if quote == "'":
            if ch == "'" and inner[i + 1 : i + 2] == "'":
                buf.append("''")
                i += 2
                continue
            buf.append(ch)
            if ch == "'":
                quote = None
            i += 1
            continue
        if ch in "\"'" and not "".join(buf).strip():
            quote = ch
            buf.append(ch)
        elif ch in "[{":
            return None, "lists of collections are outside the supported subset"
        elif ch in "]}":
            return None, f"{ch!r} closes a flow collection that was never opened"
        elif ch == ",":
            items.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        # An empty tail is the trailing comma YAML permits before the closing
        # bracket. An empty item anywhere else survives into the list and is
        # refused by the caller.
        items.append(tail)
    return items, None


def _parse_flow_list(text: str) -> tuple[list[_Scalar] | None, str | None]:
    """Parse ``[a, b, "c"]`` into scalars. The value is already known to be balanced."""

    if not text.endswith("]"):
        return None, "unexpected text after the end of the flow sequence"
    raw_items, err = _split_flow_items(text[1:-1])
    if err or raw_items is None:
        return None, err
    out = []
    for item in raw_items:
        if item == "":
            return None, "empty item in list"
        if _looks_like_mapping(item):
            return None, "lists of mappings are outside the supported subset"
        if item[0] not in "\"'":
            # Flow context narrows what a plain scalar may be: ``?`` is the
            # explicit-key indicator there and a leading ``:`` the value
            # indicator, so ``[a?b]`` and ``[:00]`` are rejected by a YAML
            # parser even though the block-context forms are fine scalars.
            if "?" in item:
                return None, "'?' in a plain flow-sequence item; quote the item"
            if item[0] == ":":
                return None, "a plain flow-sequence item may not begin with ':'"
        scalar, serr = _parse_scalar(item)
        if serr or scalar is None:
            return None, f"list item: {serr}"
        out.append(scalar)
    return out, None


def _parse_block_list(entries: list[re.Match[str] | None]) -> tuple[list[_Scalar] | None, str | None]:
    """Parse an indented ``- item`` block (every line already matched ``_BLOCK_ITEM_RE``) into scalars."""

    out = []
    indent = None
    for match in entries:
        assert match is not None  # the caller only reaches here when every line matched
        this_indent, raw = match.group(1), match.group(2)
        if indent is None:
            indent = this_indent
        elif this_indent != indent:
            return None, "inconsistent list indentation (nested lists are unsupported)"
        value_text, err = _strip_inline_comment(raw)
        if err or value_text is None:
            return None, f"list item: {err}"
        if value_text == "":
            return None, "empty item in list"
        if value_text[0] in "[{":
            return None, "lists of collections are outside the supported subset"
        if _looks_like_mapping(value_text):
            return None, "lists of mappings are outside the supported subset"
        scalar, err = _parse_scalar(value_text)
        if err or scalar is None:
            return None, f"list item: {err}"
        out.append(scalar)
    return out, None


def _unescape_double_quoted(inner: str) -> tuple[str | None, str | None]:
    """Resolve the body of a double-quoted scalar.

    ``inner`` never ends in a lone backslash: the comment scan that ran first
    reads ``\\"`` as an escaped quote, so a value whose closing quote is
    escaped was already reported as unterminated.
    """

    out = []
    i = 0
    n = len(inner)
    while i < n:
        ch = inner[i]
        if ch == '"':
            # Only a backslash-escaped quote may appear inside the body, so a
            # bare one means the quoting is malformed rather than merely odd.
            return None, "unterminated double-quoted string"
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        code = inner[i + 1]
        if code in _ESCAPES:
            out.append(_ESCAPES[code])
            i += 2
            continue
        width = _HEX_ESCAPES.get(code)
        if width is None:
            return None, f"unsupported escape {'\\' + code!r} in double-quoted string"
        digits = inner[i + 2 : i + 2 + width]
        if len(digits) != width or any(d not in "0123456789abcdefABCDEF" for d in digits):
            return None, f"malformed \\{code} escape in double-quoted string"
        point = int(digits, 16)
        if point > 0x10FFFF or 0xD800 <= point <= 0xDFFF:
            # A surrogate or out-of-range code point is not a Unicode scalar
            # value, so a Cog carrying one would not survive a round trip
            # through its own UTF-8 file.
            return None, f"\\{code} escape is not a Unicode scalar value"
        out.append(chr(point))
        i += 2 + width
    return "".join(out), None


def _plain_scalar_error(text: str) -> str | None:
    """Why a plain scalar is not one, or ``None`` if it is.

    ``: `` and a trailing ``:`` are mapping indicators, so YAML reads
    ``description: A: sample`` as a nested mapping. A colon with no space after
    it is not an indicator, which keeps ``urn:x:y`` and ``https://...`` the
    scalars they look like.
    """

    first = text[0]
    if first in _PLAIN_FORBIDDEN_FIRST:
        return f"a plain scalar may not begin with {first!r}; quote the value"
    if first in _PLAIN_CONDITIONAL_FIRST and text[1:2] in ("", " ", "\t"):
        return f"{first!r} followed by whitespace is a YAML indicator, not a value; quote the value"
    if ": " in text or ":\t" in text:
        return "':' followed by whitespace is a mapping indicator; quote the value"
    if text.endswith(":"):
        return "a plain scalar may not end with ':'; quote the value"
    return _unconstructible_error(text)


def _parse_scalar(text: str) -> tuple[_Scalar | None, str | None]:
    """Parse a single-line non-empty scalar."""

    first = text[0]
    if first == "[":
        return None, "flow sequences are supported only as a top-level field value"
    if first == "{":
        return None, "flow mappings are outside the supported subset"
    if first in "&*!|>":
        return None, "anchors, aliases, tags, and block scalars are outside the supported subset"
    if first == '"':
        if len(text) < 2 or text[-1] != '"':
            return None, "unterminated double-quoted string"
        value, err = _unescape_double_quoted(text[1:-1])
        if err or value is None:
            return None, err
        return _Scalar(value, True), None
    if first == "'":
        if len(text) < 2 or text[-1] != "'":
            return None, "unterminated single-quoted string"
        inner = text[1:-1]
        # In a valid single-quoted scalar every inner quote is doubled; a lone
        # one is malformed and must not be silently accepted.
        if "'" in inner.replace("''", ""):
            return None, "malformed single-quoted string (single quotes must be doubled)"
        return _Scalar(inner.replace("''", "'"), True), None
    err = _plain_scalar_error(text)
    if err:
        return None, err
    return _Scalar(text, _plain_is_string(text)), None


def _parse_metadata_block(block_lines: list[str]) -> tuple[dict[str, _Scalar] | None, str | None]:
    """Parse the indented ``metadata`` block: one indentation level, string keys, scalar values."""

    result: dict[str, _Scalar] = {}
    indent = None
    for line in block_lines:
        match = _META_LINE_RE.match(line)
        if not match:
            return None, "metadata entries must be single-line 'key: value' pairs"
        this_indent, key, raw_value = match.group(1), match.group(2), match.group(3)
        if indent is None:
            indent = this_indent
        elif this_indent != indent:
            return None, "inconsistent metadata indentation (nested mappings are unsupported)"
        err = _key_error(key)
        if err:
            return None, f"metadata {err}"
        if not _plain_is_string(key):
            return None, f"metadata key {key!r} is not a string"
        if key in result:
            return None, f"duplicate metadata key: {key}"
        value_text, err = _strip_inline_comment(raw_value)
        if err or value_text is None:
            return None, f"metadata value for {key!r}: {err}"
        if value_text == "":
            return None, f"metadata value for {key!r} is missing"
        scalar, err = _parse_scalar(value_text)
        if err or scalar is None:
            return None, f"metadata value for {key!r}: {err}"
        result[key] = scalar
    return result, None


# ---------------------------------------------------------------------------
# Field checks and the plain view
# ---------------------------------------------------------------------------


def _plain(parsed: _Parsed) -> dict[str, Any]:
    """The JSON-shaped view of parsed frontmatter, type flags dropped."""

    out: dict[str, Any] = {}
    for key, value in parsed.items():
        if isinstance(value, _Scalar):
            out[key] = value.value
        elif isinstance(value, list):
            out[key] = [item.value for item in value]
        elif isinstance(value, dict):
            out[key] = {k: v.value for k, v in value.items()}
        else:
            out[key] = None
    return out


def _string_field(parsed: _Parsed, key: str) -> tuple[str | None, str | None]:
    """Return ``(value, error)``; ``error`` is ``None`` when a string is present."""

    scalar = parsed.get(key)
    if scalar is None:
        return None, f"frontmatter is missing the required {key!r} field"
    if not isinstance(scalar, _Scalar) or not scalar.is_string:
        return None, f"{key!r} must be a string"
    return scalar.value, None


def _check_fields(parsed: _Parsed) -> list[str]:
    """The spec's field checks (its checks 3-5) plus the string rule on recommended fields."""

    errors = []

    type_value, err = _string_field(parsed, "type")
    if err:
        errors.append(err)
    elif not TYPE_RE.match(type_value or ""):
        errors.append(f"'type' value {type_value!r} is not a valid CogSpec v0.1 type (expected 'cog' or 'cog [0.1]')")

    name, err = _string_field(parsed, "name")
    if err:
        errors.append(err)
    elif not (1 <= len(name or "") <= NAME_MAX_LENGTH) or not NAME_RE.match(name or ""):
        errors.append(
            f"'name' value {name!r} violates the naming rules (1-64 chars; lowercase ASCII letters, "
            "digits, and hyphens; no leading, trailing, or consecutive hyphens)"
        )

    description, err = _string_field(parsed, "description")
    if err:
        errors.append(err)
    elif description == "":
        errors.append("'description' must be non-empty")
    elif len(description or "") > DESCRIPTION_MAX_LENGTH:
        errors.append(f"'description' must be no more than {DESCRIPTION_MAX_LENGTH} characters")

    # ``kind`` has a defined value set; an unrecognized value would silently
    # break the catalog filtering the spec describes.
    kind = parsed.get("kind")
    if kind is not None:
        if not isinstance(kind, _Scalar) or not kind.is_string:
            errors.append("'kind' must be a string")
        elif kind.value not in KINDS:
            errors.append(f"'kind' value {kind.value!r} is not one of 'model', 'context', or 'complete'")

    meta = parsed.get("metadata")
    if meta is not None:
        if not isinstance(meta, dict):
            errors.append("'metadata' must be a mapping of string keys to string values")
        else:
            for key, scalar in meta.items():
                if not scalar.is_string:
                    errors.append(f"'metadata' value for {key!r} is not a string")

    for key in _OPTIONAL_STRING_FIELDS:
        value = parsed.get(key)
        if value is not None and (not isinstance(value, _Scalar) or not value.is_string):
            errors.append(f"{key!r} must be a string")

    return errors


def _manifest_pointer(parsed: _Parsed) -> tuple[str | None, str | None, list[str]]:
    """Resolve the ``manifest``/``manifest_schema`` pair (the spec's check 7, minus existence).

    Return ``(path, schema, errors)``. ``path`` is normalized to a bundle-
    relative POSIX path or ``None`` when absent or unusable. Whether the file
    exists is the caller's question -- it has the bundle, this module has the
    text. A document that names neither is a draft, which is a defined
    artifact rather than an error; naming one without the other is the
    spec violation.
    """

    manifest = parsed.get("manifest")
    schema = parsed.get("manifest_schema")
    if manifest is None and schema is None:
        return None, None, []

    errors = []
    path = None
    if manifest is None:
        errors.append("Cog is missing the required 'manifest' field")
    elif not isinstance(manifest, _Scalar) or not manifest.is_string:
        errors.append("'manifest' must be a string")
    else:
        path, err = _bundle_relative_path(manifest.value.strip())
        if err:
            errors.append(err)

    schema_value = None
    if schema is None:
        errors.append("Cog is missing the required 'manifest_schema' field")
    elif not isinstance(schema, _Scalar) or not schema.is_string:
        # Already reported by the string rule on recommended fields.
        pass
    elif schema.value.strip() == "":
        errors.append("'manifest_schema' must not be empty")
    else:
        schema_value = schema.value.strip()
    return path, schema_value, errors


def _bundle_relative_path(value: str) -> tuple[str | None, str | None]:
    """Normalize a bundled-file reference, refusing absolute and escaping paths."""

    if value == "":
        return None, "'manifest' must not be empty"
    if _WIN_DRIVE_RE.match(value) or value.lower().startswith("file:"):
        return None, f"'manifest' uses an absolute path: {value}"
    if _URL_SCHEME_RE.match(value):
        return None, f"'manifest' is not a bundled file path: {value}"
    clean = value.replace("\\", "/")
    if clean.startswith("/"):
        return None, f"'manifest' uses an absolute path: {value}"
    normalized = posixpath.normpath(clean)
    if normalized == ".." or normalized.startswith("../"):
        return None, f"'manifest' escapes the bundle root: {value}"
    return normalized, None

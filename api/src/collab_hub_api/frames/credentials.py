"""The one type allowed to hold a live credential in this service.

Extracted into its own module for a boring reason and a good one. The boring
one: :mod:`.invitations` and :mod:`.invitation_email` both need it, and they
already import from each other's direction, so a shared home is the only way
to avoid a cycle. The good one: this is a cross-cutting primitive rather than
invitation policy, and keeping it separate makes "which types hold a secret"
answerable by grepping for one import.
"""

from __future__ import annotations

REDACTED = "<redacted>"
"""What every rendering and serialization of a live secret emits instead."""


def refuse_to_serialize(owner: str):
    """Build the ``__getstate__`` that keeps a credential out of a pickle.

    Belt to :class:`InvitationSecret`'s braces: a carrier that holds the
    wrapper is already unpicklable because the wrapper refuses, but a carrier
    that grows a second, unwrapped field later should still fail loudly
    rather than quietly ship it.
    """

    def __getstate__(self):
        raise TypeError(
            f"{owner} holds a live invitation secret and must not be serialized: pickling would "
            "write an access-granting credential into whatever queue, cache, or crash dump the "
            "object was travelling to. Pass the invitation id instead."
        )

    return __getstate__


class InvitationSecret:
    """A live invitation secret, in the only type allowed to hold one.

    Rounds two and three of review taught this the expensive way: suppressing
    ``__repr__`` and patching ``model_dump`` and ``__getstate__`` closes the
    exits *someone thought of*. It does not close ``dataclasses.asdict``,
    ``astuple``, ``vars()``, ``json.dumps(..., default=vars)``, or — the one
    that mattered — FastAPI's own response encoder, which walks dataclass
    fields and will happily return a credential in a 200 body. Every one of
    those reads the *field value*, so the field value is what had to change.

    So the guarantee is structural rather than enumerated. Nothing in this
    system stores a raw ``str`` credential; carriers store this, and anything
    that walks a carrier generically finds an object that

    - renders as ``InvitationSecret(<redacted>)`` from ``repr``, ``str``,
      ``format``, and f-strings;
    - refuses ``copy``, ``deepcopy``, and pickling at every protocol —
      which is also what stops ``asdict``/``astuple``, since those deep-copy
      each field value;
    - has ``__slots__`` and therefore no ``__dict__``, so ``vars()`` and
      ``default=vars`` fail rather than flattening it;
    - is not iterable and not a mapping, so ``dict()`` fails;
    - serializes to a redaction under pydantic in **both** modes, so a model
      that holds one cannot dump it either;
    - cannot be mutated or subclassed, so a carrier's ``frozen=True`` is not
      quietly undone by reaching into the nested wrapper, and no subclass can
      re-open the renderings above.

    **What this is and is not.** :meth:`reveal` is the only *supported*
    accessor, and every supported escape is therefore one greppable call. It
    is not the only physically possible one: ``secret._value``,
    ``object.__getattribute__(secret, "_value")`` and ``gc.get_referents``
    all reach the string, and nothing here tries to stop them. That is the
    correct scope rather than a gap — any code able to write those
    expressions can equally write ``.reveal()``, so defending against it
    would buy nothing and cost obfuscation. The threat this type exists for
    is **accidental** escape through machinery that walks objects
    generically: serializers, encoders, loggers, error reporters, pickle,
    and the framework's own response path. Against that it is complete;
    against deliberate extraction it does not compete, and the docstring
    says so instead of implying otherwise.

    Refusing copy/pickle rather than yielding a redacted clone is deliberate.
    These objects live for one request and have no business crossing a
    process boundary; a loud ``TypeError`` at the mistake beats a degraded
    object whose "secret" is the literal string ``<redacted>``, which would
    travel on happily and fail later, somewhere else, as an invitation nobody
    can redeem.

    **Equality and hashing are deliberately by identity.** Two wrappers
    holding the same string are not equal and do not hash alike, and so
    neither do two :class:`~.invitations.MintedSecret` values built from one
    secret. This is a decision, not an oversight: nothing in this system
    compares or hashes a secret — redemption is a digest lookup in Postgres
    and the address match is :func:`hmac.compare_digest` on strings — so a
    value-comparing ``__eq__`` would add a second supported reader of the
    value for no consumer at all. Two independently minted secrets *are*
    different secrets; "are these two handles the same handle" is the only
    question this type answers. If a caller ever genuinely needs value
    equality, add it here as a constant-time comparison against another
    wrapper with a ``__hash__`` that does not depend on the secret — do not
    reach for ``.reveal()`` at the call site.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("InvitationSecret wraps a str")
        # Through object.__setattr__ because __setattr__ below refuses: the
        # instance is immutable from the moment it exists, including to its
        # own later code.
        object.__setattr__(self, "_value", value)

    def __init_subclass__(cls, **kwargs):
        # A subclass could override __repr__ or __str__ and re-open every
        # rendering this type closes, while still satisfying "the field holds
        # an InvitationSecret" for the carrier detector. One method is a
        # cheap price for the type meaning exactly one thing.
        raise TypeError(
            "InvitationSecret is final: a subclass could override __repr__/__str__ and undo the "
            "redaction while still passing every check that looks for this type."
        )

    def reveal(self) -> str:
        """The raw secret — the only supported way to read it.

        Deliberately a method rather than a property, so every escape is a
        call that greps as one, and so it reads as an action at the call site
        rather than as an ordinary attribute access.
        """

        return self._value

    def __repr__(self) -> str:
        return f"InvitationSecret({REDACTED})"

    __str__ = __repr__

    def __format__(self, _spec: str) -> str:
        # Without this, f"{secret:>40}" would fall back to __format__ on the
        # object and pad the *repr* — harmless — but an explicit override
        # keeps every format spec landing on the redaction.
        return repr(self)

    def _refuse(self, operation: str):
        return TypeError(
            f"an InvitationSecret cannot be {operation}: it holds a live, access-granting "
            "credential that must not be duplicated, stored, or sent anywhere. Use .reveal() at "
            "the one place the raw value is genuinely needed."
        )

    def __setattr__(self, _name: str, _value) -> None:
        # Immutable, and for a reason beyond tidiness: the carriers are
        # frozen dataclasses, and a frozen container holding a mutable value
        # is only as frozen as that value. Rebinding a wrapper's slot would
        # swap the credential inside an object that advertises it cannot
        # change.
        raise self._refuse("modified")

    def __delattr__(self, _name: str) -> None:
        raise self._refuse("modified")

    def __getstate__(self):
        raise self._refuse("serialized")

    def __reduce__(self):
        raise self._refuse("pickled")

    def __reduce_ex__(self, _protocol):
        # Overridden as well as __reduce__ because pickle consults this one
        # first, and protocols 2-5 would otherwise reach the default
        # __reduce_ex__ implementation rather than the override above.
        raise self._refuse("pickled")

    def __copy__(self):
        raise self._refuse("copied")

    def __deepcopy__(self, _memo):
        # This is also what makes dataclasses.asdict() and astuple() fail on
        # any carrier holding one: both deep-copy every field value.
        raise self._refuse("deep-copied")

    @classmethod
    def __get_pydantic_core_schema__(cls, _source, _handler):
        """Validate from a plain string; serialize to a redaction, both modes.

        A pydantic field annotated with this type accepts the string off the
        wire and keeps the wrapper in memory, while ``model_dump`` and
        ``model_dump_json`` both emit :data:`REDACTED` — the second mode
        stated explicitly because it does not route through the first.
        """

        from pydantic_core import core_schema

        from_string = core_schema.no_info_after_validator_function(cls, core_schema.str_schema())
        return core_schema.json_or_python_schema(
            json_schema=from_string,
            python_schema=core_schema.union_schema([core_schema.is_instance_schema(cls), from_string]),
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda _value: REDACTED,
                when_used="always",
                return_schema=core_schema.str_schema(),
            ),
        )

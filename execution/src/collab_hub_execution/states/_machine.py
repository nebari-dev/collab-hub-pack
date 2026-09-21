"""The state pattern the four machines share.

Each machine has an interface class — ``RunState``, ``WorkerState`` and so on —
with one method per event, each of which refuses by default. A concrete state
overrides the events it accepts, returning a :class:`Transition`: the context
after the move and the records that report it. A context object holds the
data and delegates every event to its current state.

Transitions are pure. A handler reads its context and its arguments, and
returns a new context; it performs no I/O, reads no clock and calls no
executor. Whoever applies the transition writes its records.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar, Generic, TypeVar

C = TypeVar("C")


class InvalidTransition(ValueError):
    """An event the current state does not accept, or whose guard refused it."""

    def __init__(self, state: State, event: str, reason: str | None = None) -> None:
        self.state = state
        self.event = event
        self.reason = reason
        message = f"{state.name} does not accept {event!r}"
        super().__init__(f"{message}: {reason}" if reason else message)


@dataclass(frozen=True, slots=True)
class Record:
    """One fact a transition asks its caller to record."""

    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Transition(Generic[C]):
    """The result of an event: the context after it, and what to record."""

    after: C
    records: tuple[Record, ...] = ()


def move(context: C, state: State, *records: Record, **changes: Any) -> Transition[C]:
    """The transition to ``state``: a copy of the context with its new state and data, and what to record."""
    return Transition(replace(context, state=state, **changes), records)  # type: ignore[type-var]


def accepts(*targets: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare that a state accepts an event, and the states it may move to.

    The declarations are the machine's transitions: the diagram test compares
    them with ``docs/cog-execution/states.md``, and every transition a handler
    returns is checked against them.
    """

    def mark(handler: Callable[..., Any]) -> Callable[..., Any]:
        handler.__targets__ = targets  # type: ignore[attr-defined]
        return handler

    return mark


class State:
    """A state: a stateless singleton, named in capitals, sent in lower case."""

    name: ClassVar[str]
    machine: ClassVar[Machine]

    @property
    def value(self) -> str:
        """The state's wire value: its name in lower case."""
        return self.name.lower()

    @property
    def final(self) -> bool:
        """True when no event leaves this state."""
        return not any(edge[0] is self for edge in self.machine.edges)

    def refuse(self, event: str, reason: str | None = None) -> Any:
        raise InvalidTransition(self, event, reason)

    def __str__(self) -> str:
        return self.value

    def __repr__(self) -> str:
        return f"{self.machine.interface.__name__}.{self.name}"


class Context:
    """Delegates events to the current state and checks what comes back."""

    state: Any

    def dispatch(self, event: str, **arguments: Any) -> Transition[Any]:
        state = self.state
        transition = getattr(state, event)(self, **arguments)
        target = transition.after.state
        if (state, event, target) not in state.machine.edges:
            raise RuntimeError(f"{state.name} --{event}--> {target.name} is not a declared transition")
        return transition


class Machine:
    """One machine: its interface, its states, and their declared transitions."""

    def __init__(self, name: str, interface: type[State], states: Iterable[type[State]], *, initial: str) -> None:
        self.name = name
        self.interface = interface
        self.events: tuple[str, ...] = tuple(
            attribute
            for attribute, value in vars(interface).items()
            if callable(value) and not attribute.startswith("_")
        )
        self.states: tuple[State, ...] = ()
        by_name: dict[str, State] = {}
        for cls in states:
            cls.machine = self
            instance = cls()
            by_name[cls.name] = instance
            setattr(interface, cls.name, instance)
        self.states = tuple(by_name.values())
        self.initial = by_name[initial]
        edges: set[tuple[State, str, State]] = set()
        for instance in self.states:
            for event in self.events:
                handler = getattr(type(instance), event)
                for target in getattr(handler, "__targets__", ()):
                    edges.add((instance, event, by_name[target]))
        self.edges = frozenset(edges)
        self._by_value = {state.value: state for state in self.states}

    def __getitem__(self, value: str) -> State:
        """The state whose wire value (or name) is ``value``."""
        return self._by_value[value.lower()]

    def accepts(self, state: State, event: str) -> bool:
        return any(edge[0] is state and edge[1] == event for edge in self.edges)

    @property
    def pairs(self) -> frozenset[tuple[str, str]]:
        """The (from, to) pairs of every transition, by name."""
        return frozenset((source.name, target.name) for source, _, target in self.edges)

    def __repr__(self) -> str:
        return f"Machine({self.name!r})"

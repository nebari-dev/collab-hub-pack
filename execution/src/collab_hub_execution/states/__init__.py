"""The four state machines of Cog execution, on the state pattern.

One machine per level — a Cog's install, a worker, a step attempt, a run —
with every state and transition of ``docs/cog-execution/states.md`` and no
other. A test holds that document to these classes.
"""

from ._machine import InvalidTransition, Machine, Record, State, Transition
from .install import INSTALL, CogInstall, InstallState
from .run import RUN, Run, RunState, StaleEscalation
from .step import STEP_ATTEMPT, StepAttempt, StepAttemptState
from .worker import WORKER, Worker, WorkerState

MACHINES: dict[str, Machine] = {machine.name: machine for machine in (INSTALL, WORKER, STEP_ATTEMPT, RUN)}

# The one move between machines: a step of a run materializes an INVOKABLE Cog.
CROSS_MACHINE: frozenset[tuple[State, State]] = frozenset({(InstallState.INVOKABLE, WorkerState.MATERIALIZED)})

__all__ = [
    "CROSS_MACHINE",
    "INSTALL",
    "MACHINES",
    "RUN",
    "STEP_ATTEMPT",
    "WORKER",
    "CogInstall",
    "InstallState",
    "InvalidTransition",
    "Machine",
    "Record",
    "Run",
    "RunState",
    "StaleEscalation",
    "State",
    "StepAttempt",
    "StepAttemptState",
    "Transition",
    "Worker",
    "WorkerState",
]

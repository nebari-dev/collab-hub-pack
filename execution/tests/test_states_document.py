"""docs/cog-execution/states.md and the machines agree, state for state and transition for transition."""

import re
from pathlib import Path

import pytest

from collab_hub_execution.states import CROSS_MACHINE, MACHINES

DOCUMENT = Path(__file__).resolve().parents[2] / "docs" / "cog-execution" / "states.md"

# The diagram's composite states, by the machine each one draws.
COMPOSITES = {"cog": "install", "worker": "worker", "step": "step_attempt", "run": "run"}

EDGE = re.compile(r"^\s*(\[\*\]|[A-Z_]+)\s*-->\s*(\[\*\]|[A-Z_]+)\s*(?::.*)?$")


def _diagram():
    text = DOCUMENT.read_text()
    [block] = re.findall(r"```mermaid\n(stateDiagram-v2\n.*?)```", text, re.S)
    machines = {name: {"pairs": set(), "initial": set(), "final": set()} for name in COMPOSITES.values()}
    cross = set()
    current = None
    for line in block.splitlines():
        opened = re.match(r'^\s*state\s+"[^"]*"\s+as\s+(\w+)\s*\{\s*$', line)
        if opened:
            current = COMPOSITES[opened.group(1)]
            continue
        if line.strip() == "}":
            current = None
            continue
        edge = EDGE.match(line)
        if not edge:
            continue
        source, target = edge.groups()
        if current is None:
            cross.add((source, target))
        elif source == "[*]":
            machines[current]["initial"].add(target)
        elif target == "[*]":
            machines[current]["final"].add(source)
        else:
            machines[current]["pairs"].add((source, target))
    return machines, cross


def _table_states():
    text = DOCUMENT.read_text()
    section = text.split("## The states", 1)[1].split("\n## ", 1)[0]
    return sorted(re.findall(r"^\| `([A-Z_]+)` \| ([^|]+?) \|", section, re.M))


DIAGRAM, CROSS = _diagram()


@pytest.mark.parametrize("name", sorted(MACHINES))
def test_the_diagram_draws_each_machine_s_transitions_exactly(name):
    machine = MACHINES[name]
    drawn = DIAGRAM[name]
    assert drawn["pairs"] == machine.pairs
    assert drawn["initial"] == {machine.initial.name}
    assert drawn["final"] == {state.name for state in machine.states if state.final}


def test_the_only_move_between_machines_is_materializing_an_invokable_cog():
    assert CROSS == {(source.name, target.name) for source, target in CROSS_MACHINE}


def test_the_state_table_lists_every_state_of_every_machine_once():
    owner = {"install": "the Cog", "worker": "a worker", "step_attempt": "a step attempt", "run": "a run"}
    expected = sorted((state.name, owner[name]) for name, machine in MACHINES.items() for state in machine.states)
    assert _table_states() == expected  # a list, so a row written twice is caught

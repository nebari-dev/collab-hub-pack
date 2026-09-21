"""No module outside ``collab_hub_execution.states`` compares or assigns a state by string."""

import ast
from pathlib import Path

import collab_hub_execution
from collab_hub_execution.states import MACHINES

PACKAGE = Path(collab_hub_execution.__file__).parent
STATE_STRINGS = {spelling for machine in MACHINES.values() for state in machine.states
                 for spelling in (state.name, state.value)}


def _string(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _offences(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for operand in (node.left, *node.comparators):
                if _string(operand) in STATE_STRINGS:
                    yield node.lineno, f"compares with {_string(operand)!r}"
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {getattr(target, "attr", getattr(target, "id", None)) for target in targets}
            if names & {"state", "status"} and _string(node.value) in STATE_STRINGS:
                yield node.lineno, f"assigns {_string(node.value)!r}"


def test_states_are_compared_and_assigned_as_states_never_as_strings():
    offences = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "states" in path.relative_to(PACKAGE).parts:
            continue
        for line, what in _offences(ast.parse(path.read_text(), filename=str(path))):
            offences.append(f"{path.relative_to(PACKAGE)}:{line} {what}")
    assert offences == []

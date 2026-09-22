"""Gates on Op steps: the policy, and how approve, reject and send back drive a run."""

import pytest

from collab_hub_execution import (
    DEFAULT_APPROVERS,
    DurableWorkflowEngine,
    Gate,
    GateOutcome,
    InMemoryCogExecutor,
    InMemoryTrackStore,
    OpDefinition,
    OpStep,
    Problem,
    ResultEnvelope,
    RunState,
    StaleEscalation,
    TrackEvent,
    escalation_id,
)
from collab_hub_execution.orchestration import _NO_SIGNAL

ERROR = Problem("grounding", "quote not verbatim", "error")
WARN = Problem("style", "long sentence", "warn")


@pytest.mark.parametrize("policy,problems,expected", [
    ("error", [], GateOutcome.PASS),
    ("error", [WARN], GateOutcome.PASS_WITH_PROBLEMS),
    ("error", [WARN, ERROR], GateOutcome.ESCALATE),
    ("warn", [WARN], GateOutcome.ESCALATE),
    ("warn", [], GateOutcome.PASS),
    ("always", [], GateOutcome.ESCALATE),
    ("never", [ERROR], GateOutcome.PASS_WITH_PROBLEMS),
])
def test_a_gate_decides_from_the_envelope_s_problems(policy, problems, expected):
    outcome, why = Gate(escalate=policy).evaluate(ResultEnvelope.success("x", problems=problems))
    assert outcome is expected
    assert (why is not None) == (expected is GateOutcome.ESCALATE)


def test_the_default_gate_escalates_errors_and_is_decided_by_owners_and_operators():
    assert Gate().escalate == "error"
    assert Gate().deciders == DEFAULT_APPROVERS == ("owner", "operator")
    assert Gate(approvers=("reviewer",)).deciders == ("reviewer",)


@pytest.mark.parametrize("arguments", [{"escalate": "sometimes"}, {"approvers": "owner"}, {"approvers": ("",)}])
def test_a_gate_is_refused_when_its_policy_or_approvers_are_not_ones_it_knows(arguments):
    with pytest.raises(ValueError):
        Gate(**arguments)


def test_an_escalation_id_is_minted_over_the_attempt_and_the_envelope():
    envelope = ResultEnvelope.success("draft", problems=[ERROR])
    assert escalation_id("r", "s", 0, envelope) == escalation_id("r", "s", 0, envelope)
    assert escalation_id("r", "s", 1, envelope) != escalation_id("r", "s", 0, envelope)
    assert escalation_id("r", "s", 0, ResultEnvelope.success("draft 2", problems=[ERROR])) \
        != escalation_id("r", "s", 0, envelope)


# --- a Cog cannot pause a run; only a step's Gate can ---------------------------------


def test_a_cog_asking_to_pause_is_only_answering_with_data():
    track = InMemoryTrackStore()
    engine = DurableWorkflowEngine(executor=InMemoryCogExecutor({"c": lambda e, v: {"pause": True}}), track=track)
    assert engine.submit(OpDefinition("r", (OpStep("s", "c", "run"),))) is RunState.COMPLETED


def test_an_error_problem_escalates_through_the_default_gate_without_the_cog_asking():
    track = InMemoryTrackStore()
    cog = InMemoryCogExecutor({"c": lambda e, v: ResultEnvelope.success(v, problems=[ERROR])})
    engine = DurableWorkflowEngine(executor=cog, track=track)
    assert engine.submit(OpDefinition("r", (OpStep("s", "c", "run", "draft"),))) is RunState.WAITING_AT_GATE
    escalation = engine.open_escalation("r")
    assert escalation["step"] == "s" and escalation["reason"] == "a problem with severity error"
    assert escalation["approvers"] == ["owner", "operator"] and escalation["gate"] == "error"
    assert ResultEnvelope.parse(escalation["envelope"]).payload == "draft"


# --- approve, reject and send back ------------------------------------------------------


class _Engine:
    """A two-step Op whose first step always needs a sign-off."""

    def __init__(self, approvers=()):
        self.track = InMemoryTrackStore()
        self.calls = []
        self.op = OpDefinition("r", (
            OpStep("draft", "writer", "write", "v0", gate=Gate(escalate="always", approvers=approvers)),
            OpStep("publish", "publisher", "run", "p"),
        ))

    def writer(self, entry, value, *, signal=_NO_SIGNAL):
        self.calls.append(("draft", None if signal is _NO_SIGNAL else signal))
        return ResultEnvelope.success({"version": len(self.calls)})

    def publisher(self, entry, value):
        self.calls.append(("publish", None))
        return ResultEnvelope.success("published")

    def engine(self, **kwargs):
        executor = InMemoryCogExecutor({"writer": self.writer, "publisher": self.publisher})
        return DurableWorkflowEngine(executor=executor, track=self.track, **kwargs)

    def open(self):
        return self.engine().open_escalation("r")["escalation"]

    def events(self, kind):
        return [e for e in self.track.replay("r") if e.event_type == kind]


def test_approve_completes_the_step_with_the_envelope_the_approver_saw_and_advances():
    op = _Engine()
    assert op.engine().submit(op.op) is RunState.WAITING_AT_GATE
    escalation = op.open()
    assert op.engine().decide("r", escalation=escalation, actor="alice", outcome="approve") is RunState.COMPLETED
    assert op.calls == [("draft", None), ("publish", None)]  # the approved step did not run again
    draft = next(e for e in op.events("step_completed") if e.payload["step"] == "draft")
    assert draft.payload["output"] == {"version": 1} and draft.payload["escalation"] == escalation


def test_reject_ends_the_run_rejected_and_nothing_after_it_runs():
    op = _Engine()
    op.engine().submit(op.op)
    state = op.engine().decide("r", escalation=op.open(), actor="alice", outcome="reject", findings=["off topic"])
    assert state is RunState.REJECTED
    assert op.engine().observe("r") is RunState.REJECTED
    assert op.calls == [("draft", None)]
    [rejected] = op.events("rejected")
    assert rejected.payload["actor"] == "alice" and rejected.payload["value"] == ["off topic"]


def test_send_back_re_runs_the_step_with_the_findings_under_a_new_escalation():
    op = _Engine()
    op.engine().submit(op.op)
    first = op.open()
    state = op.engine().decide("r", escalation=first, actor="alice", outcome="send_back", findings=["add a source"])
    assert state is RunState.WAITING_AT_GATE
    assert op.calls == [("draft", None), ("draft", ["add a source"])]
    assert op.open() != first  # a new revision, a new escalation


def test_each_decision_is_recorded_with_its_escalation_actor_outcome_findings_and_envelope():
    op = _Engine()
    op.engine().submit(op.op)
    escalation = op.open()
    op.engine().decide("r", escalation=escalation, actor="alice", outcome="send_back", findings=["add a source"])
    [decision] = op.events("signal_received")
    assert decision.payload == {"step": "draft", "outcome": "send_back", "value": ["add a source"],
                                "escalation": escalation, "actor": "alice"}
    # The escalation it names holds the envelope the decision was made on.
    [escalated] = [e for e in op.events("paused") if e.payload["escalation"] == escalation]
    assert ResultEnvelope.parse(escalated.payload["envelope"]).payload == {"version": 1}
    assert escalated.payload["attempt"] == 0


def test_a_decision_on_an_escalation_a_send_back_closed_is_refused_and_the_run_is_unchanged():
    op = _Engine()
    op.engine().submit(op.op)
    first = op.open()
    op.engine().decide("r", escalation=first, actor="alice", outcome="send_back", findings=["again"])
    before = op.track.replay("r")
    with pytest.raises(StaleEscalation, match=first):
        op.engine().decide("r", escalation=first, actor="bob", outcome="approve")  # a late approval of revision 1
    assert op.track.replay("r") == before
    assert op.engine().observe("r") is RunState.WAITING_AT_GATE


def test_a_gate_s_approvers_are_recorded_for_the_run_api_to_authorize_against():
    op = _Engine(approvers=("editor",))
    op.engine().submit(op.op)
    assert op.engine().open_escalation("r")["approvers"] == ["editor"]


def test_a_decision_names_its_actor_and_answers_a_run_that_waits():
    op = _Engine()
    with pytest.raises(ValueError, match="not waiting at a Gate"):
        op.engine().decide("r", escalation="esc", actor="alice", outcome="approve")
    op.engine().submit(op.op)
    with pytest.raises(ValueError, match="actor"):
        op.engine().decide("r", escalation=op.open(), actor="", outcome="approve")


def test_an_approval_survives_a_crash_before_the_step_is_completed_without_running_it_again():
    op = _Engine()
    op.engine().submit(op.op)
    escalation = op.open()

    class Crash(Exception):
        pass

    real_append = op.track.append

    def crash_on_completion(event):
        if event.event_type == "step_completed":
            op.track.append = real_append
            raise Crash("the process stopped after recording the approval")
        return real_append(event)

    op.track.append = crash_on_completion
    with pytest.raises(Crash):
        op.engine().decide("r", escalation=escalation, actor="alice", outcome="approve")
    assert op.engine().observe("r") is RunState.RUNNING  # the approval is on the Track
    assert op.engine().submit(op.op) is RunState.COMPLETED
    assert op.calls == [("draft", None), ("publish", None)]


# --- the Gate is part of the recorded Op ------------------------------------------------


def test_a_resubmission_with_a_different_gate_is_refused():
    op = _Engine()
    op.engine().submit(op.op)
    changed = OpDefinition("r", (OpStep("draft", "writer", "write", "v0"), op.op.steps[1]))
    with pytest.raises(ValueError, match="different Op"):
        op.engine().submit(changed)


def test_a_step_recorded_before_gates_has_the_default_gate():
    track = InMemoryTrackStore()
    track.append(TrackEvent(run_id="old", event_type="op_submitted", payload={
        "op": {"run_id": "old", "steps": [{"name": "s", "cog": "c", "entry_point": "run", "input": None,
                                           "digest": None}]},
    }))
    cog = InMemoryCogExecutor({"c": lambda e, v: ResultEnvelope.success(v, problems=[ERROR])})
    engine = DurableWorkflowEngine(executor=cog, track=track)
    assert engine.submit(OpDefinition("old", (OpStep("s", "c", "run"),))) is RunState.WAITING_AT_GATE

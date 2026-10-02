"""needs-review: its first answer carries an `error` problem, so the step's default Gate escalates.

Sent back with findings, it answers again without the problem, so a person's
send back and approval can both be seen at dev level 1.
"""

from collab_hub_execution import Problem, ResultEnvelope


def handle(entry_point, value, **feedback):
    if "signal" in feedback:
        return ResultEnvelope.success({"draft": value, "revised_for": feedback["signal"]})
    return ResultEnvelope.success({"draft": value}, problems=[Problem("grounding", "a claim cites no source", "error")])

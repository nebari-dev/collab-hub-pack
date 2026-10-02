"""fails: answers ok: false with an error code, so the step fails and the run with it."""

from collab_hub_execution import ResultEnvelope


def handle(entry_point, value, **feedback):
    return ResultEnvelope.failure("model-call-failed", "the fake Cog always fails")

"""spender: reports the `tokens` it spent (100 by default), to run into a budget."""

from collab_hub_execution import ResultEnvelope


def handle(entry_point, value, **feedback):
    tokens = int((value or {}).get("tokens", 100))
    return ResultEnvelope.success({"spent": tokens}, usage={"tokens": tokens, "cost": tokens / 10000})

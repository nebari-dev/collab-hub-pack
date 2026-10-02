"""echo: answers with its input. The Op that always completes."""


def handle(entry_point, value, **feedback):
    return {"echo": value}

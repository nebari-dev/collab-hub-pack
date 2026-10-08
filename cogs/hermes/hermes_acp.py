"""`hermes acp` with no tools: what the Cog starts Hermes with. Runs in the Cog's environment.

The first Hermes run is prompt in, answer out (decision 14 of the plan): no
command, no file, no browser, no web, nothing Hermes can do but answer. Hermes
0.19's ACP adapter enables its whole `hermes-acp` toolset for every session and
reads no configuration that narrows it, so this replaces the one function that
names a session's toolsets, before the adapter starts, with one that names
none. The pin on `hermes-agent` is exact, and the Cog's tests check, against a
model that asks for a command, that none runs.
"""

import acp_adapter.server as server
import acp_adapter.session as session
from acp_adapter.entry import main


def no_toolsets(toolsets=None, mcp_server_names=None):
    return []


session._expand_acp_enabled_toolsets = no_toolsets
server._expand_acp_enabled_toolsets = no_toolsets

if __name__ == "__main__":
    main()

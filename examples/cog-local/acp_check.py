"""What an ACP client such as Toad does, scripted: prompts a running Cog through `collab-hub run connect`.

    python acp_check.py RUN_ID PROMPT [PROMPT ...]

The Agent Client Protocol is JSON-RPC over the agent's stdin and stdout, one
message per line (https://agentclientprotocol.com). This client starts the
agent, initializes it, opens a session, sends each prompt, and prints what the
agent says back. It exits non-zero if any prompt goes unanswered. `make
acp-check` runs it; `make connect` does the same through Toad's interface.
"""

import json
import os
import subprocess
import sys


def main(run_id, prompts):
    command = os.environ.get("COLLAB_HUB_CLI", "collab-hub").split() + ["run", "connect", run_id]
    agent = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    next_id = 0

    def call(method, params):
        nonlocal next_id
        next_id += 1
        agent.stdin.write(json.dumps({"jsonrpc": "2.0", "id": next_id, "method": method, "params": params}) + "\n")
        agent.stdin.flush()
        said = []
        for line in agent.stdout:  # updates arrive before the request's own answer
            message = json.loads(line)
            if message.get("method") == "session/update":
                said.append(message["params"]["update"]["content"]["text"])
            elif message.get("id") == next_id:
                if "error" in message:
                    sys.exit(f"{method} failed: {message['error']['message']}")
                return message["result"], "".join(said)
        sys.exit(f"the agent stopped before answering {method}")

    hello, _ = call("initialize", {"protocolVersion": 1, "clientCapabilities": {},
                                   "clientInfo": {"name": "acp-check", "version": "1"}})
    print(f"agent: {hello['agentInfo']['title']} (ACP {hello['protocolVersion']})")
    session, _ = call("session/new", {"cwd": os.getcwd(), "mcpServers": []})
    for prompt in prompts:
        result, answer = call("session/prompt", {"sessionId": session["sessionId"],
                                                 "prompt": [{"type": "text", "text": prompt}]})
        print(f"\n> {prompt}\n{answer}")
        if result["stopReason"] != "end_turn" or not answer or answer.startswith(("The Cog did not", "The hub could")):
            sys.exit(f"no answer to {prompt!r}")
    agent.stdin.close()
    agent.wait(timeout=10)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2:])

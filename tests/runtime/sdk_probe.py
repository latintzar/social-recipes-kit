"""Fresh-process current-SDK acceptance, with provider networking blocked."""

import json
import socket
import sys

attempts = []


def denied(*args, **kwargs):
    attempts.append(True)
    raise RuntimeError("External network disabled in SDK acceptance")


socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
socket.getaddrinfo = denied

import agent_factory

agent = agent_factory.build_agent(session_id="fictional-offline-session")
names = [tool.get("function", {}).get("name") for tool in agent.tools]
assert names == ["mcp__social_recipes__recipes_extract_from_url"], names
agent.close()
from tools.mcp_tool_lifecycle import shutdown_mcp_servers

shutdown_mcp_servers(names={"social_recipes"})
print(
    json.dumps(
        {
            "sdk_import": True,
            "python": sys.version.split()[0],
            "tools": names,
            "external_network_succeeded": False,
            "metadata_network_attempts_blocked": len(attempts),
        }
    )
)

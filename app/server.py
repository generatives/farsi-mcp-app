"""Farsi practice MCP server: smoke-test version with one tool.

The MCP endpoint is served at /mcp/<MCP_SECRET>. The secret path is the only auth,
so keep the full URL private (it lives in your claude.ai connector settings).
"""

import os
import sys

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from app import db

SECRET = os.environ.get("MCP_SECRET", "")
if len(SECRET) < 32:
    sys.exit("MCP_SECRET must be set to at least 32 random characters (try: openssl rand -hex 24)")

mcp = MCPServer(
    name="farsi-practice",
    version="0.1.0",
    instructions="Tools for practising spoken colloquial Persian (Farsi).",
)


@mcp.tool(
    description=(
        "Returns one colloquial Persian (Farsi) word with an example sentence for the learner to practise. "
        "Call this whenever the learner asks for a Persian or Farsi word to practise. "
        "Say the word, then the example sentence, then ask the learner to repeat the sentence aloud."
    )
)
def get_practice_word() -> dict:
    return db.pick_word()


def build_app():
    db.init_db()
    return mcp.streamable_http_app(
        streamable_http_path=f"/mcp/{SECRET}",
        # Stateless JSON responses: no session affinity needed, which suits Cloud Run.
        stateless_http=True,
        json_response=True,
        # Cloud Run fronts the container with its own hostname; the secret path is the guard.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",
    )


if __name__ == "__main__":
    uvicorn.run(build_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")

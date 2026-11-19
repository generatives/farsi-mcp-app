import asyncio
import re
from pathlib import Path

from app.server import mcp


def test_deploy_check_lists_the_server_tools():
    """The deploy workflow checks the live server lists these tools, so keep it in step with the server."""
    workflow = Path(".github/workflows/deploy.yml").read_text(encoding="utf-8")
    checked = set(re.search(r"for tool in ([\w ]+); do", workflow).group(1).split())
    tools = {t.name for t in asyncio.run(mcp.list_tools())}
    assert checked == tools

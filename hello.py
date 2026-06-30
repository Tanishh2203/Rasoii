"""
hello.py — call get_addresses on Swiggy Food MCP.

Reads the access token saved by login.py and uses the official MCP Python SDK
to connect via streamable HTTP. Calls a single tool (`get_addresses`) and
prints the response.

If you see your real Swiggy account's saved addresses → Phase 1 done.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


SWIGGY_FOOD_URL = "https://mcp.swiggy.com/food"
TOKEN_FILE = "token.json"


def load_token():
    path = Path(TOKEN_FILE)
    if not path.exists():
        print("✗ token.json not found. Run `python login.py` first.")
        sys.exit(1)
    with open(path) as f:
        data = json.load(f)
    return data["access_token"]


async def main():
    token = load_token()
    headers = {"Authorization": f"Bearer {token}"}

    print(f"→ Connecting to {SWIGGY_FOOD_URL}...")

    async with streamablehttp_client(
        url=SWIGGY_FOOD_URL,
        headers=headers,
    ) as (read_stream, write_stream, _get_session_id):

        async with ClientSession(read_stream, write_stream) as session:
            # MCP handshake: initialize the session
            print("→ Initializing MCP session...")
            init_result = await session.initialize()
            print(f"  server: {init_result.serverInfo.name} "
                  f"v{init_result.serverInfo.version}")

            # List available tools (sanity check — should show 14 Food tools)
            print("→ Listing available tools...")
            tools = await session.list_tools()
            print(f"  found {len(tools.tools)} tools:")
            for t in tools.tools:
                print(f"    - {t.name}")

            # Call get_addresses
            print("\n→ Calling get_addresses...")
            result = await session.call_tool("get_addresses", {})

            print("\n--- Response ---")
            for content in result.content:
                if hasattr(content, "text"):
                    # Try to pretty-print if it's JSON
                    try:
                        parsed = json.loads(content.text)
                        print(json.dumps(parsed, indent=2))
                    except (json.JSONDecodeError, TypeError):
                        print(content.text)
                else:
                    print(content)

            if result.isError:
                print("\n✗ Tool returned an error. Check the response above.")
                sys.exit(1)

            print("\n✓ Phase 1 complete.")


if __name__ == "__main__":
    asyncio.run(main())

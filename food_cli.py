"""
food_cli.py — Phase 2: Conversational food ordering CLI agent.

Architecture:
  - MCP Python SDK talks to Swiggy Food MCP (streamable HTTP)
  - Anthropic SDK runs Claude as the orchestrator
  - Manual tool-use loop: Claude picks tool → we execute via MCP → feed result back

Run:
  export ANTHROPIC_API_KEY=sk-ant-...
  python food_cli.py

Then talk to it:
  > order me chicken biryani
  > make it 2
  > confirm
"""

import asyncio
import json
import sys
from pathlib import Path

from anthropic import Anthropic
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


SWIGGY_FOOD_URL = "https://mcp.swiggy.com/food"
TOKEN_FILE = "token.json"
MODEL = "claude-sonnet-4-6"  # cheap + fast for tool-heavy agents
MAX_TOKENS = 4096


SYSTEM_PROMPT = """You are a helpful food ordering agent that uses the Swiggy Food MCP server.

CRITICAL RULES — never violate these:

1. PAYMENT IS ALWAYS CASH ON DELIVERY (COD).
   Don't ask the user how they want to pay. Don't suggest other methods.

2. NEVER place an order without explicit confirmation.
   Always show a cart summary first (items, prices, total), THEN ask the user
   to confirm with words like "yes", "confirm", "ok", "haan", "kar de".
   Only call place_food_order AFTER they confirm.

3. The cart cannot exceed ₹1000 (Builders Club limit).
   If adding items would push it over, warn the user and ask what to remove.

4. A Food cart can hold items from ONLY ONE restaurant at a time.
   If the user wants something from a different restaurant, warn them that the
   current cart will be cleared, and ask before proceeding.

5. ALWAYS call get_food_cart before confirmation steps.
   The user might have edited the cart in the Swiggy app between turns; don't
   trust your memory of the cart.

6. The user has 16 saved addresses. Default to "Home" (or "Jaypee" /
   the Noida addresses) unless they say otherwise. Ask if unsure.

7. Be concise. Don't dump full JSON. Speak naturally.
   If a server response shows a rich UI widget is rendered, DON'T re-list that
   data — just give a brief recommendation or next question.

8. The user might write in Hindi, Hinglish, or English. Handle all three.
   "haan", "kar de", "order kar do", "place karo" all mean confirm.
   "nahi", "ruko", "cancel" mean don't.

9. If a tool returns an error, explain it plainly. Don't panic. Suggest a fix.

10. Track order status uses track_food_order. Use it when the user asks
    "where's my order" or similar.
"""


def load_token():
    path = Path(TOKEN_FILE)
    if not path.exists():
        print("✗ token.json not found. Run `python login.py` first.")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)["access_token"]


def mcp_tools_to_anthropic(mcp_tools):
    """Convert MCP tool definitions to Anthropic's tool schema."""
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema,
        }
        for t in mcp_tools
    ]


def extract_text_from_mcp_result(result):
    """Pull plain text out of an MCP tool result. The result.content is a list
    of content blocks (usually TextContent)."""
    parts = []
    for c in result.content:
        if hasattr(c, "text"):
            parts.append(c.text)
        else:
            parts.append(str(c))
    return "\n".join(parts)


async def run_agent():
    token = load_token()
    headers = {"Authorization": f"Bearer {token}"}

    anthropic = Anthropic()  # reads ANTHROPIC_API_KEY from env

    async with streamablehttp_client(
        url=SWIGGY_FOOD_URL,
        headers=headers,
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools_result = await session.list_tools()
            tools = mcp_tools_to_anthropic(tools_result.tools)

            print(f"\n🍽  Food agent ready — {len(tools)} tools loaded.")
            print("    Type 'exit' or Ctrl-C to quit.")
            print("    Try: 'order me chicken biryani', 'what's in my cart',")
            print("         'track my order', etc.\n")

            messages = []  # conversation history

            while True:
                try:
                    user_input = input("You: ").strip()
                except (KeyboardInterrupt, EOFError):
                    print("\nGoodbye.")
                    return

                if not user_input:
                    continue
                if user_input.lower() in {"exit", "quit", "/exit", "/quit"}:
                    print("Goodbye.")
                    return

                messages.append({"role": "user", "content": user_input})

                # Inner loop: keep calling Claude until it returns text only
                # (no more tool calls requested).
                while True:
                    response = anthropic.messages.create(
                        model=MODEL,
                        max_tokens=MAX_TOKENS,
                        system=SYSTEM_PROMPT,
                        tools=tools,
                        messages=messages,
                    )

                    # Persist Claude's response (text + any tool_use blocks)
                    # into history exactly as returned.
                    messages.append(
                        {"role": "assistant", "content": response.content}
                    )

                    tool_uses = [b for b in response.content if b.type == "tool_use"]
                    text_blocks = [b for b in response.content if b.type == "text"]

                    # Print any text Claude generated this turn
                    for tb in text_blocks:
                        print(f"\nAgent: {tb.text}\n")

                    if response.stop_reason != "tool_use":
                        # Claude is done. Wait for next user input.
                        break

                    # Execute each tool call, collect results
                    tool_results = []
                    for tu in tool_uses:
                        # Compact log of what's being called
                        args_preview = json.dumps(tu.input, ensure_ascii=False)
                        if len(args_preview) > 80:
                            args_preview = args_preview[:77] + "..."
                        print(f"  ↪ {tu.name}({args_preview})")

                        try:
                            result = await session.call_tool(tu.name, tu.input)
                            result_text = extract_text_from_mcp_result(result)
                            is_err = result.isError
                        except Exception as e:
                            result_text = f"Tool error: {type(e).__name__}: {e}"
                            is_err = True

                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tu.id,
                                "content": result_text,
                                "is_error": is_err,
                            }
                        )

                    # Send tool results back as a user-role message
                    messages.append({"role": "user", "content": tool_results})


if __name__ == "__main__":
    try:
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        print("\nBye.")
"""
instamart_cli.py — Conversational grocery ordering agent.

Same architecture as food_cli.py, different server + system prompt:
  - MCP Python SDK talks to Swiggy Instamart MCP (https://mcp.swiggy.com/im)
  - Anthropic SDK runs Claude as orchestrator
  - Manual tool-use loop

Why test here first instead of Food:
  - No documented cart cap (Food is capped at ₹1000 in Builders Club)
  - Test orders can be tiny (₹40 of onions is enough to validate the full flow)
  - Quick delivery (10-20 min) → fast feedback loop
  - Same code patterns as Food, so anything you learn here transfers

WARNING — please read before placing real orders:
  - Orders CANNOT be cancelled once checkout succeeds
  - COD only (Cash on Delivery)
  - Keep the Swiggy app CLOSED on your phone while testing
    (running both can cause session conflicts and order failures)

Run:
  export ANTHROPIC_API_KEY=sk-ant-...
  python instamart_cli.py
"""

import asyncio
import json
import sys
from pathlib import Path

from anthropic import Anthropic
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


SWIGGY_IM_URL = "https://mcp.swiggy.com/im"
TOKEN_FILE = "token.json"
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096


SYSTEM_PROMPT = """You are a helpful grocery shopping agent that uses the Swiggy Instamart MCP server.

CRITICAL RULES — never violate these:

1. PAYMENT IS ALWAYS CASH ON DELIVERY (COD).
   Don't ask the user how they want to pay. Don't suggest other methods.

2. NEVER call checkout without explicit user confirmation.
   Always show a clear cart summary first (items, quantities, prices, total),
   then ask the user to confirm with words like "yes", "confirm", "ok",
   "haan", "kar de", "checkout karo". Only checkout AFTER they confirm.

3. ORDERS CANNOT BE CANCELLED ONCE PLACED.
   This is critical. Once checkout succeeds, there's no undo. Be extra clear
   about the total and items in the confirmation step. If the user seems
   hesitant or unclear, ASK AGAIN — don't push them.

4. ALWAYS call get_cart before confirmation steps.
   The user might have edited the cart in the Swiggy app between turns
   (though they shouldn't be running it). Don't trust your memory of the cart.

5. INSTAMART CARTS ARE TIED TO A DELIVERY ADDRESS.
   Product availability and prices differ by location. If the user switches
   address mid-conversation, call clear_cart first to avoid errors, then
   ask them to rebuild.

6. The user has multiple saved addresses. Default to "Home" or the user's
   stated location (likely Noida) unless they say otherwise.

7. Be concise. Don't dump full JSON.
   If a tool response mentions a "rich UI widget is being shown", DON'T
   re-list the same data — just give a brief recommendation or next question.

8. The user might write in Hindi, Hinglish, or English. Handle all three.
   "haan", "kar de", "checkout kar do" → confirm.
   "nahi", "ruko", "wait", "cancel" → don't.

9. For product searches: be specific in queries.
   "milk" returns many variants. Ask preferences (brand, size, full-fat vs
   toned) if unclear. Or pick a reasonable default and show what you picked.

10. If a tool returns an error, explain it plainly. Suggest a fix.
    Don't keep retrying the same tool with the same arguments.

11. For quick reorders, use your_go_to_items — it returns the user's
    frequently-ordered products for one-tap reorder.

REMINDER: The user is currently in TESTING MODE. They want to validate the
agent loop end-to-end. Encourage them to start with a small order
(one or two cheap items) so they don't spend too much during testing.
"""


def load_token():
    path = Path(TOKEN_FILE)
    if not path.exists():
        print("✗ token.json not found. Run `python login.py` first.")
        sys.exit(1)
    with open(path) as f:
        return json.load(f)["access_token"]


def mcp_tools_to_anthropic(mcp_tools):
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema,
        }
        for t in mcp_tools
    ]


def extract_text_from_mcp_result(result):
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

    anthropic = Anthropic()  # reads ANTHROPIC_API_KEY

    async with streamablehttp_client(
        url=SWIGGY_IM_URL,
        headers=headers,
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            tools_result = await session.list_tools()
            tools = mcp_tools_to_anthropic(tools_result.tools)

            print(f"\n🛒  Instamart agent ready — {len(tools)} tools loaded.")
            print("    ⚠️  Close the Swiggy app on your phone before placing orders.")
            print("    ⚠️  Orders cannot be cancelled once placed.")
            print("    Type 'exit' or Ctrl-C to quit.\n")
            print("    Examples:")
            print("      > what are my saved addresses?")
            print("      > show me my usual items")
            print("      > search for onions")
            print("      > add 1kg amul milk to cart")
            print("      > show me the cart\n")

            messages = []

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

                while True:
                    response = anthropic.messages.create(
                        model=MODEL,
                        max_tokens=MAX_TOKENS,
                        system=SYSTEM_PROMPT,
                        tools=tools,
                        messages=messages,
                    )

                    messages.append(
                        {"role": "assistant", "content": response.content}
                    )

                    tool_uses = [b for b in response.content if b.type == "tool_use"]
                    text_blocks = [b for b in response.content if b.type == "text"]

                    for tb in text_blocks:
                        print(f"\nAgent: {tb.text}\n")

                    if response.stop_reason != "tool_use":
                        break

                    tool_results = []
                    for tu in tool_uses:
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

                    messages.append({"role": "user", "content": tool_results})


if __name__ == "__main__":
    try:
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        print("\nBye.")
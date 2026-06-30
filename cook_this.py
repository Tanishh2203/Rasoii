"""
cook_this.py — end-to-end: image/URL → ingredients → Instamart agent → cart.

Usage:
  python cook_this.py --image path/to/recipe.jpg
  python cook_this.py --url "https://www.youtube.com/watch?v=..."

Flow:
  1. Parse recipe (image vision OR YouTube transcript) → JSON ingredients
  2. Ask user upfront which pantry staples they already have
  3. Hand the filtered list to the Instamart agent
  4. Agent searches each ingredient on Instamart
  5. For ambiguous matches, agent asks user to pick (per Phase 3 design choice)
  6. Builds cart, shows summary, awaits confirmation, checks out

INGREDIENT_MATCHING_MODE:
  "ask"      — agent asks user to pick from multiple SKU matches (current)
  "auto"     — agent silently picks best match (cheapest/highest-rated)
  "auto+log" — agent picks but tells user what was chosen (recommended)

Change INGREDIENT_MATCHING_MODE below to flip behavior.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

from anthropic import Anthropic
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from parse_image import parse_image
from parse_youtube import parse_youtube
from recipe_prompt import summarize_ingredients


SWIGGY_IM_URL = "https://mcp.swiggy.com/im"
TOKEN_FILE = "token.json"
MODEL = "claude-haiku-4-5"
MAX_TOKENS = 4096

# === CHANGE THIS to flip ingredient-matching behavior ===
INGREDIENT_MATCHING_MODE = "auto+log" # "ask" | "auto" | "auto+log"
# ========================================================


MATCHING_INSTRUCTIONS = {
    "ask": """
INGREDIENT MATCHING: For each recipe ingredient, search Instamart with search_products.
If multiple SKUs match (different brands, sizes, etc.), STOP and ask the user which one
to pick. Show 3-4 options max with brand, size, and price. Wait for their reply before
adding to cart. Then move to the next ingredient.
""",
    "auto": """
INGREDIENT MATCHING: For each recipe ingredient, search Instamart with search_products.
Silently pick the best match: smallest pack that meets the recipe quantity, top-rated
brand, cheapest if tied. Add to cart without asking. Tell the user the final cart at
the end.
""",
    "auto+log": """
INGREDIENT MATCHING: For each recipe ingredient, search Instamart with search_products.
Pick the best match automatically: smallest pack that meets the recipe quantity, top-rated
brand, cheapest if tied. Add it. After each pick, briefly tell the user what you picked
(name, brand, size, price). Don't ask permission per item — let them see everything and
edit at the end if they want.
""",
}


SYSTEM_PROMPT_BASE = """You are a grocery shopping agent that converts a recipe ingredient
list into an Instamart cart on Swiggy.

CRITICAL RULES:

1. Payment is ALWAYS Cash on Delivery (COD). Don't ask.

2. NEVER call checkout without explicit user confirmation showing the final cart.

3. Orders CANNOT be cancelled once placed. Be extra careful with the confirmation step.

4. Always call get_cart before the final confirmation.

5. Process ingredients ONE AT A TIME, not all at once. The user can keep up with you better.

6. SKIP ingredients the user said they already have (pantry staples).

7. If an ingredient cannot be found on Instamart, note it and continue with the rest.
   At the end, list the ones that couldn't be found.

8. Use the user's default delivery address (likely Noida) unless they specify another.

9. If a server response mentions a "rich UI widget is being shown", don't re-list
   the data — just give a brief comment or next question.

10. Be concise. Don't dump JSON.

{matching_instructions}

The user will give you a structured list of ingredients to shop for. Process them in order.
"""


def load_token():
    if not Path(TOKEN_FILE).exists():
        print("✗ token.json not found. Run `python login.py` first.")
        sys.exit(1)
    with open(TOKEN_FILE) as f:
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


def ask_about_pantry_staples(ingredients: list) -> list:
    """Show pantry staples to user, ask which ones they already have."""
    staples = [ing for ing in ingredients if ing.get("pantry_staple")]
    non_staples = [ing for ing in ingredients if not ing.get("pantry_staple")]

    if not staples:
        return ingredients

    print("\n🧂  Pantry staples found in this recipe:")
    for i, ing in enumerate(staples, 1):
        print(f"  {i}. {ing['name']}")

    print("\nWhich of these do you ALREADY have at home? (we'll skip those)")
    print("Reply with comma-separated numbers (e.g. '1,2,3'), or 'all' if you have")
    print("all of them, or 'none' if you need to buy all of them.")

    answer = input("> ").strip().lower()

    if answer == "all":
        skip_indices = set(range(len(staples)))
    elif answer == "none" or answer == "":
        skip_indices = set()
    else:
        try:
            skip_indices = {
                int(x.strip()) - 1
                for x in answer.split(",")
                if x.strip().isdigit()
            }
        except ValueError:
            print("Couldn't parse that. Assuming you need to buy all staples.")
            skip_indices = set()

    kept_staples = [s for i, s in enumerate(staples) if i not in skip_indices]
    skipped_staples = [s for i, s in enumerate(staples) if i in skip_indices]

    if skipped_staples:
        print(f"  ✓ Skipping {len(skipped_staples)} staple(s): "
              f"{', '.join(s['name'] for s in skipped_staples)}")
    if kept_staples:
        print(f"  • Will shop for {len(kept_staples)} staple(s): "
              f"{', '.join(s['name'] for s in kept_staples)}")

    return non_staples + kept_staples


def format_ingredients_for_agent(ingredients: list) -> str:
    """Build the initial user message for the Instamart agent."""
    lines = [
        f"Please shop these {len(ingredients)} ingredients on Instamart, "
        f"one at a time, in this order:\n"
    ]
    for i, ing in enumerate(ingredients, 1):
        qty = ing.get("quantity")
        unit = ing.get("unit") or ""
        name = ing["name"]
        notes = ing.get("notes", "")

        if qty is not None:
            line = f"{i}. {qty} {unit} {name}".strip()
        else:
            line = f"{i}. {name} (quantity not specified — use your judgment)"
        if notes:
            line += f"  [{notes}]"
        lines.append(line)

    lines.append("\nStart with the first one. Use my default Noida address.")
    return "\n".join(lines)


async def run_shopping_session(initial_message: str):
    """Run the Instamart agent loop with a pre-built ingredient list."""
    token = load_token()
    headers = {"Authorization": f"Bearer {token}"}

    anthropic = Anthropic()

    system_prompt = SYSTEM_PROMPT_BASE.format(
        matching_instructions=MATCHING_INSTRUCTIONS[INGREDIENT_MATCHING_MODE]
    )

    async with streamablehttp_client(
        url=SWIGGY_IM_URL,
        headers=headers,
    ) as (read_stream, write_stream, _):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tools = mcp_tools_to_anthropic(tools_result.tools)

            print(f"\n🛒 Shopping agent ready ({len(tools)} tools, "
                  f"mode={INGREDIENT_MATCHING_MODE})\n")

            messages = [{"role": "user", "content": initial_message}]

            while True:
                # Inner loop: keep talking to Claude until it stops requesting tools
                while True:
                    response = anthropic.messages.create(
                        model=MODEL,
                        max_tokens=MAX_TOKENS,
                        system=system_prompt,
                        tools=tools,
                        messages=messages,
                    )

                    messages.append({"role": "assistant", "content": response.content})

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

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": result_text,
                            "is_error": is_err,
                        })

                    messages.append({"role": "user", "content": tool_results})

                # Agent is waiting on the user
                try:
                    user_input = input("You: ").strip()
                except (KeyboardInterrupt, EOFError):
                    print("\nExiting. Cart left as-is (no checkout).")
                    return

                if not user_input:
                    continue
                if user_input.lower() in {"exit", "quit", "/exit"}:
                    print("Exiting. Cart left as-is (no checkout).")
                    return

                messages.append({"role": "user", "content": user_input})


def main():
    parser = argparse.ArgumentParser(description="Recipe → Instamart cart")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--image", type=Path, help="Path to recipe image")
    group.add_argument("--url", type=str, help="YouTube video URL")
    args = parser.parse_args()

    # Step 1: parse the recipe
    if args.image:
        print(f"📷 Parsing image: {args.image}")
        ingredients = parse_image(args.image)
    else:
        print(f"📺 Parsing YouTube video: {args.url}")
        ingredients = parse_youtube(args.url)

    print(f"\n✓ Extracted {len(ingredients)} ingredients:")
    print(summarize_ingredients(ingredients))

    # Step 2: ask about pantry staples
    ingredients_to_shop = ask_about_pantry_staples(ingredients)

    if not ingredients_to_shop:
        print("\nNothing left to shop. Exiting.")
        return

    # Step 3: hand off to the agent
    initial_message = format_ingredients_for_agent(ingredients_to_shop)
    print(f"\n📋 Handing {len(ingredients_to_shop)} ingredients to the agent...")

    asyncio.run(run_shopping_session(initial_message))


if __name__ == "__main__":
    main()
# """
# telegram_bot.py — Phase 5a: Telegram interface for the Swiggy MCP agent.

# Features:
#   - /start         welcome message
#   - /whoami        prints your Telegram user ID (use it to allowlist yourself)
#   - /mode <food|instamart>   switch agent mode
#   - /reset         clears your conversation history
#   - text           routed to current mode's agent
#   - photo          parsed as recipe → Instamart shopping flow
#   - YouTube URL    same as photo, parsed via transcript

# Single-user for now: uses your existing token.json. Multi-user OAuth is Phase 5b.

# Setup:
#   export TELEGRAM_BOT_TOKEN="7891234:AAFxxx..."
#   export TELEGRAM_ALLOWED_USERS="123456789"   # your Telegram user ID
#   export ANTHROPIC_API_KEY="sk-ant-..."
#   python telegram_bot.py

# Then open Telegram, find your bot by its username, send /start.
# """

# import asyncio
# import json
# import logging
# import os
# import re
# import sys
# from pathlib import Path
# from typing import Optional

# from anthropic import Anthropic
# from mcp import ClientSession
# from mcp.client.streamable_http import streamablehttp_client
# from telegram import Update
# from telegram.constants import ChatAction
# from telegram.ext import (
#     Application,
#     CommandHandler,
#     ContextTypes,
#     MessageHandler,
#     filters,
# )

# from parse_image import parse_image_bytes
# from parse_youtube import parse_youtube
# from recipe_prompt import PANTRY_STAPLES, summarize_ingredients


# # ===== Config =====================================================

# BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
# if not BOT_TOKEN:
#     print("✗ Set TELEGRAM_BOT_TOKEN env var (get from @BotFather)")
#     sys.exit(1)

# ALLOWED_USERS: set[int] = set()
# if os.environ.get("TELEGRAM_ALLOWED_USERS"):
#     ALLOWED_USERS = {
#         int(x.strip())
#         for x in os.environ["TELEGRAM_ALLOWED_USERS"].split(",")
#         if x.strip()
#     }

# SWIGGY_URLS = {
#     "food": "https://mcp.swiggy.com/food",
#     "instamart": "https://mcp.swiggy.com/im",
# }
# TOKEN_FILE = "token.json"
# MODEL = "claude-haiku-4-5"
# MAX_TOKENS = 4096
# TELEGRAM_MSG_LIMIT = 4000  # safe under Telegram's 4096 limit

# HISTORY_DIR = Path("data/histories")
# HISTORY_DIR.mkdir(parents=True, exist_ok=True)


# SYSTEM_PROMPTS = {
#     "instamart": """You are a grocery shopping agent using the Swiggy Instamart MCP server.

# RULES:
# 1. Payment is ALWAYS Cash on Delivery (COD). Don't ask, don't suggest alternatives.
# 2. NEVER call checkout without explicit user confirmation with the full cart summary.
# 3. Orders CANNOT be cancelled once placed. Confirm carefully.
# 4. Always call get_cart before final confirmation.
# 5. Process ingredients one at a time. If multiple SKU matches, ask the user to pick.
# 6. Skip pantry staples the user already has.
# 7. Be concise. Don't dump JSON. Speak naturally.
# 8. Use the user's default Noida address unless they say otherwise.
# 9. Handle Hindi, Hinglish, and English seamlessly.
# 10. If a tool response mentions a "rich UI widget", don't re-list that data.
# """,
#     "food": """You are a food ordering agent using the Swiggy Food MCP server.

# RULES:
# 1. Payment is ALWAYS Cash on Delivery (COD).
# 2. NEVER place an order without explicit user confirmation with cart summary.
# 3. Cart total cannot exceed ₹1000 (Builders Club limit).
# 4. A cart holds items from ONE restaurant only. Warn before switching.
# 5. Always call get_food_cart before confirmation.
# 6. Use the user's Home/Noida address unless specified.
# 7. Handle Hindi, Hinglish, English. "haan", "kar de", "order karo" → confirm.
# 8. Be concise. Don't dump JSON.
# """,
# }


# # ===== Logging ====================================================

# logging.basicConfig(
#     format="%(asctime)s %(levelname)s %(name)s: %(message)s",
#     level=logging.INFO,
# )
# # python-telegram-bot is noisy at INFO; silence it
# logging.getLogger("httpx").setLevel(logging.WARNING)
# logging.getLogger("telegram").setLevel(logging.WARNING)
# log = logging.getLogger("rasoi")


# # ===== Per-user session state ====================================
# # In-memory dict. Persisted to disk on every change so a bot restart
# # doesn't nuke active conversations.

# user_state: dict[int, dict] = {}


# def default_state() -> dict:
#     return {"mode": "instamart", "history": [], "pending": None}


# def state_path(user_id: int) -> Path:
#     return HISTORY_DIR / f"{user_id}.json"


# def load_state(user_id: int) -> dict:
#     if user_id in user_state:
#         return user_state[user_id]
#     path = state_path(user_id)
#     if path.exists():
#         try:
#             user_state[user_id] = json.loads(path.read_text())
#         except json.JSONDecodeError:
#             log.warning(f"corrupt state file for {user_id}, resetting")
#             user_state[user_id] = default_state()
#     else:
#         user_state[user_id] = default_state()
#     return user_state[user_id]


# def save_state(user_id: int):
#     state = user_state.get(user_id)
#     if not state:
#         return
#     state_path(user_id).write_text(json.dumps(state, default=str, indent=2))


# # ===== Anthropic content block serialization =====================
# # When Claude returns content blocks, they're pydantic objects.
# # We need them as plain dicts for JSON storage AND for re-sending to Claude.

# def serialize_content_blocks(content) -> list:
#     """Convert Anthropic content blocks to JSON-safe dicts."""
#     if isinstance(content, str):
#         return content
#     out = []
#     for block in content:
#         if hasattr(block, "model_dump"):
#             out.append(block.model_dump(exclude_none=True))
#         elif isinstance(block, dict):
#             out.append(block)
#         else:
#             out.append({"type": "text", "text": str(block)})
#     return out


# # ===== Auth / allowlist ==========================================

# def is_allowed(user_id: int) -> bool:
#     if not ALLOWED_USERS:
#         # If no allowlist configured, allow everyone (dev mode warning shown at startup)
#         return True
#     return user_id in ALLOWED_USERS


# def load_swiggy_token() -> str:
#     path = Path(TOKEN_FILE)
#     if not path.exists():
#         raise FileNotFoundError(
#             "token.json not found. Run `python login.py` first to OAuth into Swiggy."
#         )
#     return json.loads(path.read_text())["access_token"]


# # ===== MCP tool conversion =======================================

# def mcp_tools_to_anthropic(mcp_tools) -> list:
#     return [
#         {
#             "name": t.name,
#             "description": t.description or "",
#             "input_schema": t.inputSchema,
#         }
#         for t in mcp_tools
#     ]


# def extract_text_from_mcp_result(result) -> str:
#     parts = []
#     for c in result.content:
#         if hasattr(c, "text"):
#             parts.append(c.text)
#         else:
#             parts.append(str(c))
#     return "\n".join(parts)


# # ===== The agent loop, refactored for per-request use ============

# async def run_agent_turn(user_id: int, user_message: str) -> str:
#     """One agent turn: append user message, loop until Claude returns text,
#     save state, return the text reply."""
#     state = load_state(user_id)
#     mode = state["mode"]
#     state["history"].append({"role": "user", "content": user_message})

#     token = load_swiggy_token()
#     url = SWIGGY_URLS[mode]
#     headers = {"Authorization": f"Bearer {token}"}
#     system_prompt = SYSTEM_PROMPTS[mode]

#     anthropic = Anthropic()
#     final_text_parts: list[str] = []

#     async with streamablehttp_client(url=url, headers=headers) as (
#         read_stream, write_stream, _
#     ):
#         async with ClientSession(read_stream, write_stream) as session:
#             await session.initialize()
#             tools_result = await session.list_tools()
#             tools = mcp_tools_to_anthropic(tools_result.tools)

#             # Inner agent loop — keep going until Claude stops requesting tools
#             for _ in range(20):  # safety cap: max 20 tool calls per turn
#                 response = anthropic.messages.create(
#                     model=MODEL,
#                     max_tokens=MAX_TOKENS,
#                     system=system_prompt,
#                     tools=tools,
#                     messages=state["history"],
#                 )

#                 state["history"].append(
#                     {
#                         "role": "assistant",
#                         "content": serialize_content_blocks(response.content),
#                     }
#                 )

#                 tool_uses = [b for b in response.content if b.type == "tool_use"]
#                 text_blocks = [b for b in response.content if b.type == "text"]

#                 for tb in text_blocks:
#                     final_text_parts.append(tb.text)

#                 if response.stop_reason != "tool_use":
#                     break

#                 # Execute each tool call
#                 tool_results = []
#                 for tu in tool_uses:
#                     log.info(f"[user {user_id}] → {tu.name}({json.dumps(tu.input)[:100]})")
#                     try:
#                         result = await session.call_tool(tu.name, tu.input)
#                         result_text = extract_text_from_mcp_result(result)
#                         is_err = result.isError
#                     except Exception as e:
#                         result_text = f"Tool error: {type(e).__name__}: {e}"
#                         is_err = True

#                     tool_results.append(
#                         {
#                             "type": "tool_result",
#                             "tool_use_id": tu.id,
#                             "content": result_text,
#                             "is_error": is_err,
#                         }
#                     )

#                 state["history"].append({"role": "user", "content": tool_results})
#             else:
#                 final_text_parts.append(
#                     "(agent hit max tool calls per turn; stopping for safety)"
#                 )

#     save_state(user_id)
#     return "\n\n".join(final_text_parts) if final_text_parts else "(no reply)"


# # ===== Recipe → cart helpers =====================================

# def detect_youtube_url(text: str) -> Optional[str]:
#     """Returns the YouTube URL if found in the text, else None."""
#     match = re.search(
#         r"(https?://(?:www\.)?(?:youtube\.com/[^\s]+|youtu\.be/[^\s]+))",
#         text,
#     )
#     return match.group(1) if match else None


# def format_pantry_question(ingredients: list, staples: list) -> str:
#     lines = [f"Found {len(ingredients)} ingredients in the recipe.\n"]
#     lines.append("These look like pantry staples — which do you already have at home?\n")
#     for i, ing in enumerate(staples, 1):
#         lines.append(f"  {i}. {ing['name']}")
#     lines.append("")
#     lines.append("Reply with:")
#     lines.append("  • 'all'   — you have all of these")
#     lines.append("  • 'none'  — you need to buy all of these")
#     lines.append("  • Names separated by commas: e.g. 'salt, oil, cumin'")
#     return "\n".join(lines)


# def filter_ingredients_by_pantry_reply(
#     ingredients: list, staples: list, reply: str
# ) -> list:
#     reply = reply.strip().lower()
#     if reply == "all":
#         # Skip all staples, keep only non-staples
#         return [i for i in ingredients if not i.get("pantry_staple")]
#     if reply == "none" or not reply:
#         return ingredients

#     # Parse names
#     names_have = {n.strip() for n in reply.split(",") if n.strip()}
#     skip_names = set()
#     for staple in staples:
#         for have in names_have:
#             if have in staple["name"].lower() or staple["name"].lower() in have:
#                 skip_names.add(staple["name"])
#                 break
#     return [i for i in ingredients if i["name"] not in skip_names]


# def format_ingredient_message(ingredients: list) -> str:
#     lines = [
#         f"Please shop these {len(ingredients)} ingredients on Instamart, "
#         "one at a time, in this order:\n"
#     ]
#     for i, ing in enumerate(ingredients, 1):
#         qty = ing.get("quantity")
#         unit = ing.get("unit") or ""
#         name = ing["name"]
#         notes = ing.get("notes", "")
#         if qty is not None:
#             line = f"{i}. {qty} {unit} {name}".strip()
#         else:
#             line = f"{i}. {name}"
#         if notes:
#             line += f"  [{notes}]"
#         lines.append(line)
#     lines.append("\nUse my default Noida address. Start with the first one.")
#     return "\n".join(lines)


# # ===== Telegram message sending helpers =========================

# async def send_long(update: Update, text: str):
#     """Telegram has a 4096 char limit per message; split if needed."""
#     if len(text) <= TELEGRAM_MSG_LIMIT:
#         await update.message.reply_text(text)
#         return
#     # Split on newlines where possible
#     chunks = []
#     current = ""
#     for line in text.split("\n"):
#         if len(current) + len(line) + 1 > TELEGRAM_MSG_LIMIT:
#             chunks.append(current)
#             current = line
#         else:
#             current = current + "\n" + line if current else line
#     if current:
#         chunks.append(current)
#     for chunk in chunks:
#         await update.message.reply_text(chunk)


# async def typing(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     await context.bot.send_chat_action(
#         chat_id=update.effective_chat.id, action=ChatAction.TYPING
#     )


# # ===== Telegram command handlers =================================

# async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user = update.effective_user
#     if not is_allowed(user.id):
#         await update.message.reply_text(
#             f"Your Telegram ID ({user.id}) isn't allowlisted.\n"
#             f"The bot owner needs to add it to TELEGRAM_ALLOWED_USERS."
#         )
#         return

#     load_state(user.id)  # initialize
#     await update.message.reply_text(
#         f"Hi {user.first_name}! I'm your Swiggy ordering bot.\n\n"
#         f"What I can do:\n"
#         f"• Order groceries from Instamart (default mode)\n"
#         f"• Order food via Swiggy Food — type /mode food\n"
#         f"• Parse a recipe image → grocery cart\n"
#         f"• Parse a YouTube cooking video URL → grocery cart\n\n"
#         f"Commands:\n"
#         f"  /mode food  or  /mode instamart\n"
#         f"  /reset      — clear my memory of our conversation\n"
#         f"  /whoami     — show your Telegram ID\n\n"
#         f"Try: 'search for onions' or send me a recipe image."
#     )


# async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user = update.effective_user
#     await update.message.reply_text(
#         f"Your Telegram user ID is: `{user.id}`\n"
#         f"Username: @{user.username or '(none)'}\n"
#         f"Allowlisted: {'✓ yes' if is_allowed(user.id) else '✗ no'}",
#         parse_mode="Markdown",
#     )


# async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user_id = update.effective_user.id
#     if not is_allowed(user_id):
#         return

#     args = context.args
#     if not args or args[0] not in {"food", "instamart"}:
#         state = load_state(user_id)
#         await update.message.reply_text(
#             f"Current mode: *{state['mode']}*\n\n"
#             f"Switch with:\n"
#             f"  /mode food\n"
#             f"  /mode instamart",
#             parse_mode="Markdown",
#         )
#         return

#     state = load_state(user_id)
#     state["mode"] = args[0]
#     state["history"] = []  # mode switch clears history (different MCP server)
#     state["pending"] = None
#     save_state(user_id)
#     await update.message.reply_text(
#         f"Switched to *{args[0]}* mode. Conversation history cleared.",
#         parse_mode="Markdown",
#     )


# async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user_id = update.effective_user.id
#     if not is_allowed(user_id):
#         return
#     state = load_state(user_id)
#     state["history"] = []
#     state["pending"] = None
#     save_state(user_id)
#     await update.message.reply_text("Memory cleared. Fresh start.")


# # ===== Telegram message handlers =================================

# async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user_id = update.effective_user.id
#     if not is_allowed(user_id):
#         await update.message.reply_text(
#             f"Not allowlisted (your ID: {user_id})."
#         )
#         return

#     text = update.message.text
#     state = load_state(user_id)

#     # 1. Check for pending pantry-staple reply
#     if state.get("pending") and state["pending"]["type"] == "pantry_staples":
#         pending = state["pending"]
#         filtered = filter_ingredients_by_pantry_reply(
#             pending["ingredients"], pending["staples"], text
#         )
#         state["pending"] = None
#         save_state(user_id)

#         if not filtered:
#             await update.message.reply_text("Nothing left to shop. Done!")
#             return

#         await update.message.reply_text(
#             f"Shopping {len(filtered)} items. Starting now..."
#         )
#         await typing(update, context)

#         agent_input = format_ingredient_message(filtered)
#         # Force Instamart mode for recipe flows
#         state["mode"] = "instamart"
#         state["history"] = []
#         save_state(user_id)

#         reply = await run_agent_turn(user_id, agent_input)
#         await send_long(update, reply)
#         return

#     # 2. Check for YouTube URL
#     yt_url = detect_youtube_url(text)
#     if yt_url:
#         await update.message.reply_text(
#             "Found a YouTube URL — fetching transcript & extracting ingredients..."
#         )
#         await typing(update, context)
#         try:
#             ingredients = parse_youtube(yt_url)
#         except Exception as e:
#             await update.message.reply_text(f"Couldn't parse video: {e}")
#             return

#         await handle_parsed_ingredients(update, context, user_id, ingredients)
#         return

#     # 3. Normal text → current agent
#     await typing(update, context)
#     try:
#         reply = await run_agent_turn(user_id, text)
#     except FileNotFoundError as e:
#         await update.message.reply_text(f"⚠️ {e}")
#         return
#     except Exception as e:
#         log.exception("agent error")
#         await update.message.reply_text(f"⚠️ Agent error: {type(e).__name__}: {e}")
#         return

#     await send_long(update, reply)


# async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user_id = update.effective_user.id
#     if not is_allowed(user_id):
#         return

#     await update.message.reply_text("Got the image. Extracting ingredients...")
#     await typing(update, context)

#     # Telegram sends multiple sizes; get the largest
#     photo = update.message.photo[-1]
#     photo_file = await photo.get_file()
#     image_bytes = bytes(await photo_file.download_as_bytearray())

#     try:
#         ingredients = parse_image_bytes(image_bytes, mime_type="image/jpeg")
#     except Exception as e:
#         await update.message.reply_text(f"Couldn't parse image: {e}")
#         return

#     await handle_parsed_ingredients(update, context, user_id, ingredients)


# async def handle_parsed_ingredients(
#     update: Update,
#     context: ContextTypes.DEFAULT_TYPE,
#     user_id: int,
#     ingredients: list,
# ):
#     """After we have a parsed ingredient list, ask about pantry staples
#     (or skip the question if there are none)."""
#     state = load_state(user_id)

#     summary = (
#         f"Found {len(ingredients)} ingredients:\n\n"
#         + summarize_ingredients(ingredients)
#     )
#     await send_long(update, summary)

#     staples = [ing for ing in ingredients if ing.get("pantry_staple")]
#     if not staples:
#         await update.message.reply_text("No pantry staples. Starting to shop...")
#         await typing(update, context)
#         agent_input = format_ingredient_message(ingredients)
#         state["mode"] = "instamart"
#         state["history"] = []
#         save_state(user_id)
#         reply = await run_agent_turn(user_id, agent_input)
#         await send_long(update, reply)
#         return

#     # Save pending state, ask the question
#     state["pending"] = {
#         "type": "pantry_staples",
#         "ingredients": ingredients,
#         "staples": staples,
#     }
#     save_state(user_id)

#     await update.message.reply_text(format_pantry_question(ingredients, staples))


# # ===== Bot bootstrap =============================================

# def main():
#     if not ALLOWED_USERS:
#         log.warning("⚠️  TELEGRAM_ALLOWED_USERS not set — bot will reply to ANYONE.")
#         log.warning("    For safety, set TELEGRAM_ALLOWED_USERS=<your_id> and restart.")
#     else:
#         log.info(f"Allowlist: {ALLOWED_USERS}")

#     # Sanity: token.json must exist
#     try:
#         load_swiggy_token()
#     except FileNotFoundError:
#         log.error("token.json missing — run `python login.py` first.")
#         sys.exit(1)

#     app = Application.builder().token(BOT_TOKEN).build()

#     app.add_handler(CommandHandler("start", start_cmd))
#     app.add_handler(CommandHandler("whoami", whoami_cmd))
#     app.add_handler(CommandHandler("mode", mode_cmd))
#     app.add_handler(CommandHandler("reset", reset_cmd))
#     app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
#     app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

#     log.info("Bot starting — polling for messages...")
#     app.run_polling(allowed_updates=Update.ALL_TYPES)


# if __name__ == "__main__":
#     main()









# """
# telegram_bot.py — Phase 5a: Telegram interface for the Swiggy MCP agent.

# Features:
#   - /start         welcome message
#   - /whoami        prints your Telegram user ID (use it to allowlist yourself)
#   - /mode <food|instamart>   switch agent mode
#   - /reset         clears your conversation history
#   - text           routed to current mode's agent
#   - photo          parsed as recipe → Instamart shopping flow
#   - YouTube URL    same as photo, parsed via transcript

# Single-user for now: uses your existing token.json. Multi-user OAuth is Phase 5b.

# Setup:
#   export TELEGRAM_BOT_TOKEN="7891234:AAFxxx..."
#   export TELEGRAM_ALLOWED_USERS="123456789"   # your Telegram user ID
#   export ANTHROPIC_API_KEY="sk-ant-..."
#   python telegram_bot.py

# Then open Telegram, find your bot by its username, send /start.
# """

# import asyncio
# import io
# import json
# import logging
# import os
# import re
# import sys
# from pathlib import Path
# from typing import Optional

# from anthropic import Anthropic
# from mcp import ClientSession
# from mcp.client.streamable_http import streamablehttp_client
# from openai import OpenAI
# from telegram import Update
# from telegram.constants import ChatAction
# from telegram.ext import (
#     Application,
#     CommandHandler,
#     ContextTypes,
#     MessageHandler,
#     filters,
# )

# from parse_image import parse_image_bytes
# from parse_youtube import parse_youtube
# from recipe_prompt import PANTRY_STAPLES, summarize_ingredients


# # ===== Config =====================================================

# BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
# if not BOT_TOKEN:
#     print("✗ Set TELEGRAM_BOT_TOKEN env var (get from @BotFather)")
#     sys.exit(1)

# OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
# if not OPENAI_API_KEY:
#     print("✗ Set OPENAI_API_KEY env var (for Whisper voice transcription)")
#     print("  Get one at: https://platform.openai.com/api-keys")
#     sys.exit(1)

# # OpenAI client for Whisper. Cost: ~$0.006/min (~₹0.50/min) — trivial for demos.
# openai_client = OpenAI(api_key=OPENAI_API_KEY)
# WHISPER_MODEL = "whisper-1"

# # TTS config — when user sends voice and the reply is short, we synthesize speech.
# TTS_MODEL = "tts-1"
# TTS_VOICE = "nova"  # alloy | echo | fable | onyx | nova | shimmer
# TTS_MAX_REPLY_CHARS = 300  # over this → text only, regardless of input mode

# ALLOWED_USERS: set[int] = set()
# if os.environ.get("TELEGRAM_ALLOWED_USERS"):
#     ALLOWED_USERS = {
#         int(x.strip())
#         for x in os.environ["TELEGRAM_ALLOWED_USERS"].split(",")
#         if x.strip()
#     }

# SWIGGY_URLS = {
#     "food": "https://mcp.swiggy.com/food",
#     "instamart": "https://mcp.swiggy.com/im",
# }
# TOKEN_FILE = "token.json"
# MODEL = "claude-haiku-4-5"
# MAX_TOKENS = 4096
# TELEGRAM_MSG_LIMIT = 4000  # safe under Telegram's 4096 limit

# HISTORY_DIR = Path("data/histories")
# HISTORY_DIR.mkdir(parents=True, exist_ok=True)


# SYSTEM_PROMPTS = {
#     "instamart": """You are a grocery shopping agent using the Swiggy Instamart MCP server.

# RULES:
# 1. Payment is ALWAYS Cash on Delivery (COD). Don't ask, don't suggest alternatives.
# 2. NEVER call checkout without explicit user confirmation with the full cart summary.
# 3. Orders CANNOT be cancelled once placed. Confirm carefully.
# 4. Always call get_cart before final confirmation.
# 5. Process ingredients one at a time. If multiple SKU matches, ask the user to pick.
# 6. Skip pantry staples the user already has.
# 7. Be concise. Don't dump JSON. Speak naturally.
# 8. Use the user's default Noida address unless they say otherwise.
# 9. Handle Hindi, Hinglish, and English seamlessly.
# 10. If a tool response mentions a "rich UI widget", don't re-list that data.

# LANGUAGE OUTPUT:
# - If the user wrote in English, reply in English.
# - If the user wrote in Hindi (Devanagari script) or Hinglish (Roman script),
#   reply in HINGLISH using Roman/Latin script — e.g. "Theek hai, paneer mil
#   gaya, 95 rupees ka hai. Confirm karein?"
# - NEVER use Devanagari script in replies. Always Roman/Latin letters,
#   even for Hindi words. This is critical for text-to-speech quality.
# - Numbers should be written as digits (95, 200), not spelled out.
# - Currency: write as "95 rupees" or "Rs 95", not "₹95" (TTS reads symbols poorly).
# """,
#     "food": """You are a food ordering agent using the Swiggy Food MCP server.

# RULES:
# 1. Payment is ALWAYS Cash on Delivery (COD).
# 2. NEVER place an order without explicit user confirmation with cart summary.
# 3. Cart total cannot exceed 1000 rupees (Builders Club limit).
# 4. A cart holds items from ONE restaurant only. Warn before switching.
# 5. Always call get_food_cart before confirmation.
# 6. Use the user's Home/Noida address unless specified.
# 7. Handle Hindi, Hinglish, English. "haan", "kar de", "order karo" → confirm.
# 8. Be concise. Don't dump JSON.

# LANGUAGE OUTPUT:
# - If the user wrote in English, reply in English.
# - If the user wrote in Hindi (Devanagari script) or Hinglish (Roman script),
#   reply in HINGLISH using Roman/Latin script — e.g. "Biryani House se chicken
#   biryani mil gayi, 349 rupees. Cart total 349 rupees hai. Place karein?"
# - NEVER use Devanagari script in replies. Always Roman/Latin letters.
# - Numbers as digits. Currency as "rupees" or "Rs", not the ₹ symbol.
# """,
# }


# # ===== Logging ====================================================

# logging.basicConfig(
#     format="%(asctime)s %(levelname)s %(name)s: %(message)s",
#     level=logging.INFO,
# )
# # python-telegram-bot is noisy at INFO; silence it
# logging.getLogger("httpx").setLevel(logging.WARNING)
# logging.getLogger("telegram").setLevel(logging.WARNING)
# log = logging.getLogger("rasoi")


# # ===== Per-user session state ====================================
# # In-memory dict. Persisted to disk on every change so a bot restart
# # doesn't nuke active conversations.

# user_state: dict[int, dict] = {}


# def default_state() -> dict:
#     return {
#         "mode": "instamart",
#         "history": [],
#         "pending": None,
#         "last_input_voice": False,  # tracks whether last user message was a voice note
#     }


# def state_path(user_id: int) -> Path:
#     return HISTORY_DIR / f"{user_id}.json"


# def load_state(user_id: int) -> dict:
#     if user_id in user_state:
#         return user_state[user_id]
#     path = state_path(user_id)
#     if path.exists():
#         try:
#             user_state[user_id] = json.loads(path.read_text())
#         except json.JSONDecodeError:
#             log.warning(f"corrupt state file for {user_id}, resetting")
#             user_state[user_id] = default_state()
#     else:
#         user_state[user_id] = default_state()
#     return user_state[user_id]


# def save_state(user_id: int):
#     state = user_state.get(user_id)
#     if not state:
#         return
#     state_path(user_id).write_text(json.dumps(state, default=str, indent=2))


# # ===== Anthropic content block serialization =====================
# # When Claude returns content blocks, they're pydantic objects.
# # We need them as plain dicts for JSON storage AND for re-sending to Claude.

# def serialize_content_blocks(content) -> list:
#     """Convert Anthropic content blocks to JSON-safe dicts."""
#     if isinstance(content, str):
#         return content
#     out = []
#     for block in content:
#         if hasattr(block, "model_dump"):
#             out.append(block.model_dump(exclude_none=True))
#         elif isinstance(block, dict):
#             out.append(block)
#         else:
#             out.append({"type": "text", "text": str(block)})
#     return out


# # ===== Auth / allowlist ==========================================

# def is_allowed(user_id: int) -> bool:
#     if not ALLOWED_USERS:
#         # If no allowlist configured, allow everyone (dev mode warning shown at startup)
#         return True
#     return user_id in ALLOWED_USERS


# def load_swiggy_token() -> str:
#     path = Path(TOKEN_FILE)
#     if not path.exists():
#         raise FileNotFoundError(
#             "token.json not found. Run `python login.py` first to OAuth into Swiggy."
#         )
#     return json.loads(path.read_text())["access_token"]


# # ===== MCP tool conversion =======================================

# def mcp_tools_to_anthropic(mcp_tools) -> list:
#     return [
#         {
#             "name": t.name,
#             "description": t.description or "",
#             "input_schema": t.inputSchema,
#         }
#         for t in mcp_tools
#     ]


# def extract_text_from_mcp_result(result) -> str:
#     parts = []
#     for c in result.content:
#         if hasattr(c, "text"):
#             parts.append(c.text)
#         else:
#             parts.append(str(c))
#     return "\n".join(parts)


# # ===== The agent loop, refactored for per-request use ============

# async def run_agent_turn(user_id: int, user_message: str) -> str:
#     """One agent turn: append user message, loop until Claude returns text,
#     save state, return the text reply."""
#     state = load_state(user_id)
#     mode = state["mode"]
#     state["history"].append({"role": "user", "content": user_message})

#     token = load_swiggy_token()
#     url = SWIGGY_URLS[mode]
#     headers = {"Authorization": f"Bearer {token}"}
#     system_prompt = SYSTEM_PROMPTS[mode]

#     anthropic = Anthropic()
#     final_text_parts: list[str] = []

#     async with streamablehttp_client(url=url, headers=headers) as (
#         read_stream, write_stream, _
#     ):
#         async with ClientSession(read_stream, write_stream) as session:
#             await session.initialize()
#             tools_result = await session.list_tools()
#             tools = mcp_tools_to_anthropic(tools_result.tools)

#             # Inner agent loop — keep going until Claude stops requesting tools
#             for _ in range(20):  # safety cap: max 20 tool calls per turn
#                 response = anthropic.messages.create(
#                     model=MODEL,
#                     max_tokens=MAX_TOKENS,
#                     system=system_prompt,
#                     tools=tools,
#                     messages=state["history"],
#                 )

#                 state["history"].append(
#                     {
#                         "role": "assistant",
#                         "content": serialize_content_blocks(response.content),
#                     }
#                 )

#                 tool_uses = [b for b in response.content if b.type == "tool_use"]
#                 text_blocks = [b for b in response.content if b.type == "text"]

#                 for tb in text_blocks:
#                     final_text_parts.append(tb.text)

#                 if response.stop_reason != "tool_use":
#                     break

#                 # Execute each tool call
#                 tool_results = []
#                 for tu in tool_uses:
#                     log.info(f"[user {user_id}] → {tu.name}({json.dumps(tu.input)[:100]})")
#                     try:
#                         result = await session.call_tool(tu.name, tu.input)
#                         result_text = extract_text_from_mcp_result(result)
#                         is_err = result.isError
#                     except Exception as e:
#                         result_text = f"Tool error: {type(e).__name__}: {e}"
#                         is_err = True

#                     tool_results.append(
#                         {
#                             "type": "tool_result",
#                             "tool_use_id": tu.id,
#                             "content": result_text,
#                             "is_error": is_err,
#                         }
#                     )

#                 state["history"].append({"role": "user", "content": tool_results})
#             else:
#                 final_text_parts.append(
#                     "(agent hit max tool calls per turn; stopping for safety)"
#                 )

#     save_state(user_id)
#     return "\n\n".join(final_text_parts) if final_text_parts else "(no reply)"


# # ===== Recipe → cart helpers =====================================

# def detect_youtube_url(text: str) -> Optional[str]:
#     """Returns the YouTube URL if found in the text, else None."""
#     match = re.search(
#         r"(https?://(?:www\.)?(?:youtube\.com/[^\s]+|youtu\.be/[^\s]+))",
#         text,
#     )
#     return match.group(1) if match else None


# def format_pantry_question(ingredients: list, staples: list) -> str:
#     lines = [f"Found {len(ingredients)} ingredients in the recipe.\n"]
#     lines.append("These look like pantry staples — which do you already have at home?\n")
#     for i, ing in enumerate(staples, 1):
#         lines.append(f"  {i}. {ing['name']}")
#     lines.append("")
#     lines.append("Reply with:")
#     lines.append("  • 'all'   — you have all of these")
#     lines.append("  • 'none'  — you need to buy all of these")
#     lines.append("  • Names separated by commas: e.g. 'salt, oil, cumin'")
#     return "\n".join(lines)


# def filter_ingredients_by_pantry_reply(
#     ingredients: list, staples: list, reply: str
# ) -> list:
#     reply = reply.strip().lower()
#     if reply == "all":
#         # Skip all staples, keep only non-staples
#         return [i for i in ingredients if not i.get("pantry_staple")]
#     if reply == "none" or not reply:
#         return ingredients

#     # Parse names
#     names_have = {n.strip() for n in reply.split(",") if n.strip()}
#     skip_names = set()
#     for staple in staples:
#         for have in names_have:
#             if have in staple["name"].lower() or staple["name"].lower() in have:
#                 skip_names.add(staple["name"])
#                 break
#     return [i for i in ingredients if i["name"] not in skip_names]


# def format_ingredient_message(ingredients: list) -> str:
#     lines = [
#         f"Please shop these {len(ingredients)} ingredients on Instamart, "
#         "one at a time, in this order:\n"
#     ]
#     for i, ing in enumerate(ingredients, 1):
#         qty = ing.get("quantity")
#         unit = ing.get("unit") or ""
#         name = ing["name"]
#         notes = ing.get("notes", "")
#         if qty is not None:
#             line = f"{i}. {qty} {unit} {name}".strip()
#         else:
#             line = f"{i}. {name}"
#         if notes:
#             line += f"  [{notes}]"
#         lines.append(line)
#     lines.append("\nUse my default Noida address. Start with the first one.")
#     return "\n".join(lines)


# # ===== Telegram message sending helpers =========================

# async def send_long(update: Update, text: str):
#     """Telegram has a 4096 char limit per message; split if needed."""
#     if len(text) <= TELEGRAM_MSG_LIMIT:
#         await update.message.reply_text(text)
#         return
#     # Split on newlines where possible
#     chunks = []
#     current = ""
#     for line in text.split("\n"):
#         if len(current) + len(line) + 1 > TELEGRAM_MSG_LIMIT:
#             chunks.append(current)
#             current = line
#         else:
#             current = current + "\n" + line if current else line
#     if current:
#         chunks.append(current)
#     for chunk in chunks:
#         await update.message.reply_text(chunk)


# async def typing(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     await context.bot.send_chat_action(
#         chat_id=update.effective_chat.id, action=ChatAction.TYPING
#     )


# # ===== Text-to-speech (OpenAI TTS) ===============================

# def synthesize_speech(text: str) -> bytes:
#     """Generate Opus audio bytes from text via OpenAI TTS.
#     Telegram voice messages use .ogg/Opus, so we request Opus directly.
#     Blocking call — wrap in asyncio.to_thread when calling from async code.
#     """
#     response = openai_client.audio.speech.create(
#         model=TTS_MODEL,
#         voice=TTS_VOICE,
#         input=text,
#         response_format="opus",
#     )
#     return response.content


# async def send_smart(
#     update: Update,
#     context: ContextTypes.DEFAULT_TYPE,
#     user_id: int,
#     text: str,
# ):
#     """Decide voice vs text for the agent reply based on user state.

#     Rule (per Phase 6 design):
#       - If last input was voice AND reply is short (<= TTS_MAX_REPLY_CHARS):
#           → send as voice note only (no text)
#       - Otherwise:
#           → send as text (split if needed)
#     """
#     state = load_state(user_id)
#     use_voice = (
#         state.get("last_input_voice", False)
#         and len(text) <= TTS_MAX_REPLY_CHARS
#     )

#     if not use_voice:
#         await send_long(update, text)
#         return

#     # Generate TTS, send as Telegram voice note
#     await context.bot.send_chat_action(
#         chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE
#     )
#     try:
#         audio_bytes = await asyncio.to_thread(synthesize_speech, text)
#     except Exception as e:
#         log.exception("TTS failed")
#         # Fall back to text on TTS failure
#         await update.message.reply_text(f"(TTS failed: {e})\n\n{text}")
#         return

#     audio_buf = io.BytesIO(audio_bytes)
#     audio_buf.name = "reply.ogg"
#     await update.message.reply_voice(voice=audio_buf)


# # ===== Telegram command handlers =================================

# async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user = update.effective_user
#     if not is_allowed(user.id):
#         await update.message.reply_text(
#             f"Your Telegram ID ({user.id}) isn't allowlisted.\n"
#             f"The bot owner needs to add it to TELEGRAM_ALLOWED_USERS."
#         )
#         return

#     load_state(user.id)  # initialize
#     await update.message.reply_text(
#         f"Hi {user.first_name}! I'm your Swiggy ordering bot.\n\n"
#         f"What I can do:\n"
#         f"• Order groceries from Instamart (default mode)\n"
#         f"• Order food via Swiggy Food — type /mode food\n"
#         f"• Parse a recipe image → grocery cart\n"
#         f"• Parse a YouTube cooking video URL → grocery cart\n"
#         f"• Voice notes in Hindi, English, or Hinglish 🎙️\n\n"
#         f"Commands:\n"
#         f"  /mode food  or  /mode instamart\n"
#         f"  /reset      — clear my memory of our conversation\n"
#         f"  /whoami     — show your Telegram ID\n\n"
#         f"Try: 'search for onions', send a recipe image, or hold the\n"
#         f"mic button and say what you want."
#     )


# async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user = update.effective_user
#     await update.message.reply_text(
#         f"Your Telegram user ID is: `{user.id}`\n"
#         f"Username: @{user.username or '(none)'}\n"
#         f"Allowlisted: {'✓ yes' if is_allowed(user.id) else '✗ no'}",
#         parse_mode="Markdown",
#     )


# async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user_id = update.effective_user.id
#     if not is_allowed(user_id):
#         return

#     args = context.args
#     if not args or args[0] not in {"food", "instamart"}:
#         state = load_state(user_id)
#         await update.message.reply_text(
#             f"Current mode: *{state['mode']}*\n\n"
#             f"Switch with:\n"
#             f"  /mode food\n"
#             f"  /mode instamart",
#             parse_mode="Markdown",
#         )
#         return

#     state = load_state(user_id)
#     state["mode"] = args[0]
#     state["history"] = []  # mode switch clears history (different MCP server)
#     state["pending"] = None
#     save_state(user_id)
#     await update.message.reply_text(
#         f"Switched to *{args[0]}* mode. Conversation history cleared.",
#         parse_mode="Markdown",
#     )


# async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user_id = update.effective_user.id
#     if not is_allowed(user_id):
#         return
#     state = load_state(user_id)
#     state["history"] = []
#     state["pending"] = None
#     save_state(user_id)
#     await update.message.reply_text("Memory cleared. Fresh start.")


# # ===== Telegram message handlers =================================

# async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user_id = update.effective_user.id
#     if not is_allowed(user_id):
#         await update.message.reply_text(
#             f"Not allowlisted (your ID: {user_id})."
#         )
#         return

#     # User typed (not voice) → reply mode goes back to text
#     state = load_state(user_id)
#     state["last_input_voice"] = False
#     save_state(user_id)

#     await _process_text_input(update, context, user_id, update.message.text)


# async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     """Transcribe a Telegram voice note via Whisper, show transcript, then
#     route through the same pipeline as a typed message."""
#     user_id = update.effective_user.id
#     if not is_allowed(user_id):
#         return

#     await typing(update, context)

#     # Download voice note from Telegram. Voice notes come as .ogg (Opus codec)
#     # which Whisper accepts natively — no transcoding needed.
#     voice = update.message.voice
#     duration = voice.duration  # in seconds
#     log.info(f"[user {user_id}] voice note received ({duration}s)")

#     if duration > 120:
#         await update.message.reply_text(
#             "Voice note is over 2 minutes — please keep it shorter."
#         )
#         return

#     try:
#         voice_file = await voice.get_file()
#         voice_bytes = bytes(await voice_file.download_as_bytearray())
#     except Exception as e:
#         log.exception("voice download failed")
#         await update.message.reply_text(f"⚠️ Couldn't download voice: {e}")
#         return

#     # Transcribe via Whisper API.
#     # OpenAI SDK reads format from filename — set .name on the BytesIO so it
#     # knows this is .ogg/Opus.
#     audio_buf = io.BytesIO(voice_bytes)
#     audio_buf.name = "voice.ogg"

#     try:
#         # Run blocking SDK call in a thread so we don't block the event loop
#         transcription = await asyncio.to_thread(
#             openai_client.audio.transcriptions.create,
#             model=WHISPER_MODEL,
#             file=audio_buf,
#             # No language= set → Whisper auto-detects (handles Hindi/English/mixed)
#         )
#         transcript_text = transcription.text.strip()
#     except Exception as e:
#         log.exception("whisper failed")
#         await update.message.reply_text(f"⚠️ Transcription failed: {e}")
#         return

#     if not transcript_text:
#         await update.message.reply_text(
#             "Couldn't make out what you said. Try again with clearer audio?"
#         )
#         return

#     # Show transcript before processing — transparency for the user
#     await update.message.reply_text(f"🎙️ Heard: {transcript_text}")

#     # Flag the session so subsequent short agent replies come back as voice
#     state = load_state(user_id)
#     state["last_input_voice"] = True
#     save_state(user_id)

#     # Route through the same logic as a typed text message
#     await _process_text_input(update, context, user_id, transcript_text)


# async def _process_text_input(
#     update: Update,
#     context: ContextTypes.DEFAULT_TYPE,
#     user_id: int,
#     text: str,
# ):
#     """The core text-routing logic, shared by text_handler and voice_handler.

#     Routes to: pending pantry-staple reply → YouTube URL parser → normal agent.
#     """
#     state = load_state(user_id)

#     # 1. Check for pending pantry-staple reply
#     if state.get("pending") and state["pending"]["type"] == "pantry_staples":
#         pending = state["pending"]
#         filtered = filter_ingredients_by_pantry_reply(
#             pending["ingredients"], pending["staples"], text
#         )
#         state["pending"] = None
#         save_state(user_id)

#         if not filtered:
#             await update.message.reply_text("Nothing left to shop. Done!")
#             return

#         await update.message.reply_text(
#             f"Shopping {len(filtered)} items. Starting now..."
#         )
#         await typing(update, context)

#         agent_input = format_ingredient_message(filtered)
#         state["mode"] = "instamart"
#         state["history"] = []
#         save_state(user_id)

#         reply = await run_agent_turn(user_id, agent_input)
#         await send_smart(update, context, user_id, reply)
#         return

#     # 2. Check for YouTube URL
#     yt_url = detect_youtube_url(text)
#     if yt_url:
#         await update.message.reply_text(
#             "Found a YouTube URL — fetching transcript & extracting ingredients..."
#         )
#         await typing(update, context)
#         try:
#             ingredients = parse_youtube(yt_url)
#         except Exception as e:
#             await update.message.reply_text(f"Couldn't parse video: {e}")
#             return

#         await handle_parsed_ingredients(update, context, user_id, ingredients)
#         return

#     # 3. Normal text → current agent
#     await typing(update, context)
#     try:
#         reply = await run_agent_turn(user_id, text)
#     except FileNotFoundError as e:
#         await update.message.reply_text(f"⚠️ {e}")
#         return
#     except Exception as e:
#         log.exception("agent error")
#         await update.message.reply_text(f"⚠️ Agent error: {type(e).__name__}: {e}")
#         return

#     await send_smart(update, context, user_id, reply)


# async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user_id = update.effective_user.id
#     if not is_allowed(user_id):
#         return

#     # Image input → text replies
#     state = load_state(user_id)
#     state["last_input_voice"] = False
#     save_state(user_id)

#     await update.message.reply_text("Got the image. Extracting ingredients...")
#     await typing(update, context)

#     # Telegram sends multiple sizes; get the largest
#     photo = update.message.photo[-1]
#     photo_file = await photo.get_file()
#     image_bytes = bytes(await photo_file.download_as_bytearray())

#     try:
#         ingredients = parse_image_bytes(image_bytes, mime_type="image/jpeg")
#     except Exception as e:
#         await update.message.reply_text(f"Couldn't parse image: {e}")
#         return

#     await handle_parsed_ingredients(update, context, user_id, ingredients)


# async def handle_parsed_ingredients(
#     update: Update,
#     context: ContextTypes.DEFAULT_TYPE,
#     user_id: int,
#     ingredients: list,
# ):
#     """After we have a parsed ingredient list, ask about pantry staples
#     (or skip the question if there are none)."""
#     state = load_state(user_id)

#     summary = (
#         f"Found {len(ingredients)} ingredients:\n\n"
#         + summarize_ingredients(ingredients)
#     )
#     await send_long(update, summary)

#     staples = [ing for ing in ingredients if ing.get("pantry_staple")]
#     if not staples:
#         await update.message.reply_text("No pantry staples. Starting to shop...")
#         await typing(update, context)
#         agent_input = format_ingredient_message(ingredients)
#         state["mode"] = "instamart"
#         state["history"] = []
#         save_state(user_id)
#         reply = await run_agent_turn(user_id, agent_input)
#         await send_smart(update, context, user_id, reply)
#         return

#     # Save pending state, ask the question
#     state["pending"] = {
#         "type": "pantry_staples",
#         "ingredients": ingredients,
#         "staples": staples,
#     }
#     save_state(user_id)

#     await update.message.reply_text(format_pantry_question(ingredients, staples))


# # ===== Bot bootstrap =============================================

# def main():
#     if not ALLOWED_USERS:
#         log.warning("⚠️  TELEGRAM_ALLOWED_USERS not set — bot will reply to ANYONE.")
#         log.warning("    For safety, set TELEGRAM_ALLOWED_USERS=<your_id> and restart.")
#     else:
#         log.info(f"Allowlist: {ALLOWED_USERS}")

#     # Sanity: token.json must exist
#     try:
#         load_swiggy_token()
#     except FileNotFoundError:
#         log.error("token.json missing — run `python login.py` first.")
#         sys.exit(1)

#     app = Application.builder().token(BOT_TOKEN).build()

#     app.add_handler(CommandHandler("start", start_cmd))
#     app.add_handler(CommandHandler("whoami", whoami_cmd))
#     app.add_handler(CommandHandler("mode", mode_cmd))
#     app.add_handler(CommandHandler("reset", reset_cmd))
#     app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
#     app.add_handler(MessageHandler(filters.VOICE, voice_handler))
#     app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

#     log.info("Bot starting — polling for messages...")
#     app.run_polling(allowed_updates=Update.ALL_TYPES)


# if __name__ == "__main__":
#     main()








# """
# telegram_bot.py — Phase 5a: Telegram interface for the Swiggy MCP agent.

# Features:
#   - /start         welcome message
#   - /whoami        prints your Telegram user ID (use it to allowlist yourself)
#   - /mode <food|instamart>   switch agent mode
#   - /reset         clears your conversation history
#   - text           routed to current mode's agent
#   - photo          parsed as recipe → Instamart shopping flow
#   - YouTube URL    same as photo, parsed via transcript

# Single-user for now: uses your existing token.json. Multi-user OAuth is Phase 5b.

# Setup:
#   export TELEGRAM_BOT_TOKEN="7891234:AAFxxx..."
#   export TELEGRAM_ALLOWED_USERS="123456789"   # your Telegram user ID
#   export ANTHROPIC_API_KEY="sk-ant-..."
#   python telegram_bot.py

# Then open Telegram, find your bot by its username, send /start.
# """

# import asyncio
# import io
# import json
# import logging
# import os
# import re
# import sys
# from pathlib import Path
# from typing import Optional

# from anthropic import Anthropic
# from mcp import ClientSession
# from mcp.client.streamable_http import streamablehttp_client
# from openai import OpenAI
# from telegram import Update
# from telegram.constants import ChatAction
# from telegram.ext import (
#     Application,
#     CommandHandler,
#     ContextTypes,
#     MessageHandler,
#     filters,
# )

# from parse_image import parse_image_bytes
# from parse_youtube import parse_youtube
# from recipe_prompt import PANTRY_STAPLES, summarize_ingredients


# # ===== Config =====================================================

# BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
# if not BOT_TOKEN:
#     print("✗ Set TELEGRAM_BOT_TOKEN env var (get from @BotFather)")
#     sys.exit(1)

# OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
# if not OPENAI_API_KEY:
#     print("✗ Set OPENAI_API_KEY env var (for Whisper voice transcription)")
#     print("  Get one at: https://platform.openai.com/api-keys")
#     sys.exit(1)

# # OpenAI client for Whisper. Cost: ~$0.006/min (~₹0.50/min) — trivial for demos.
# openai_client = OpenAI(api_key=OPENAI_API_KEY)
# WHISPER_MODEL = "whisper-1"

# # TTS config — when user sends voice and the reply is short, we synthesize speech.
# TTS_MODEL = "tts-1"
# TTS_VOICE = "nova"  # alloy | echo | fable | onyx | nova | shimmer
# TTS_MAX_REPLY_CHARS = 500  # over this → text only; voice_mode in prompt aims for <200

# ALLOWED_USERS: set[int] = set()
# if os.environ.get("TELEGRAM_ALLOWED_USERS"):
#     ALLOWED_USERS = {
#         int(x.strip())
#         for x in os.environ["TELEGRAM_ALLOWED_USERS"].split(",")
#         if x.strip()
#     }

# SWIGGY_URLS = {
#     "food": "https://mcp.swiggy.com/food",
#     "instamart": "https://mcp.swiggy.com/im",
# }
# TOKEN_FILE = "token.json"
# MODEL = "claude-haiku-4-5"
# MAX_TOKENS = 4096
# TELEGRAM_MSG_LIMIT = 4000  # safe under Telegram's 4096 limit

# HISTORY_DIR = Path("data/histories")
# HISTORY_DIR.mkdir(parents=True, exist_ok=True)


# SYSTEM_PROMPTS = {
#     "instamart": """You are a grocery shopping agent using the Swiggy Instamart MCP server.

# ⚠️ TOP-PRIORITY OUTPUT RULES — these override everything else:

# A. SCRIPT: Reply in ROMAN/LATIN letters only. NEVER use Devanagari (हिंदी) script,
#    EVER. If the user wrote in Hindi or Hinglish, you MUST reply in Hinglish written
#    with ENGLISH letters.
#      ✓ "Theek hai, paneer mil gaya, 95 rupees ka hai. Add karu cart mein?"
#      ✗ "ठीक है, पनीर मिल गया, 95 rupees का है।"
#    This is non-negotiable. Failure breaks the text-to-speech system.

# B. NUMBERS: Write as digits (1, 33, 200). Currency: write "rupees" or "Rs",
#    NEVER use the ₹ symbol.

# C. BREVITY: 1-3 short sentences per reply unless the user explicitly asks for
#    a long list. Confirmations and questions should be very short.

# D. NO MARKDOWN: No **bold**, no *italics*, no bullet points. Plain prose only.

# NOW the business rules:
# 1. Payment is ALWAYS Cash on Delivery (COD). Don't ask, don't suggest alternatives.
# 2. NEVER call checkout without explicit user confirmation with the full cart summary.
# 3. Orders CANNOT be cancelled once placed. Confirm carefully.
# 4. Always call get_cart before final confirmation.
# 5. Process ingredients one at a time. If multiple SKU matches, ask the user to pick.
# 6. Skip pantry staples the user already has.
# 7. Use the user's default Noida address unless they say otherwise.
# 8. Handle Hindi, Hinglish, and English input — but ALWAYS reply per Rule A.
# 9. If a tool response mentions a "rich UI widget", don't re-list that data.
# """,
#     "food": """You are a food ordering agent using the Swiggy Food MCP server.

# ⚠️ TOP-PRIORITY OUTPUT RULES — these override everything else:

# A. SCRIPT: Reply in ROMAN/LATIN letters only. NEVER use Devanagari (हिंदी) script,
#    EVER. If the user wrote in Hindi or Hinglish, you MUST reply in Hinglish written
#    with ENGLISH letters.
#      ✓ "Biryani House se chicken biryani mil gayi, 349 rupees. Place karein?"
#      ✗ "बिरयानी हाउस से चिकन बिरयानी मिल गई।"
#    This is non-negotiable. Failure breaks the text-to-speech system.

# B. NUMBERS: Write as digits. Currency: "rupees" or "Rs", NEVER the ₹ symbol.

# C. BREVITY: 1-3 short sentences per reply. Confirmations especially short.

# D. NO MARKDOWN: plain prose only.

# NOW the business rules:
# 1. Payment is ALWAYS Cash on Delivery (COD).
# 2. NEVER place an order without explicit user confirmation with cart summary.
# 3. Cart total cannot exceed 1000 rupees (Builders Club limit).
# 4. A cart holds items from ONE restaurant only. Warn before switching.
# 5. Always call get_food_cart before confirmation.
# 6. Use the user's Home/Noida address unless specified.
# 7. Recognize Hindi/Hinglish confirmations: "haan", "kar de", "order karo" → confirm.
# """,
# }

# # Extra instruction appended to system prompt when user input was a voice note.
# # Keeps replies short enough to play as voice + extra-emphatic about script.
# VOICE_MODE_SUFFIX = """

# ⚠️ VOICE MODE ACTIVE — the user spoke this message. Your reply will be read
# aloud. Therefore:
# - Keep reply UNDER 200 characters total. Cut anything non-essential.
# - Roman/Latin script only — no Devanagari at all.
# - No symbols, no markdown, no asterisks. Plain text.
# - Speak naturally as if telling a friend a quick fact.
# """


# # ===== Logging ====================================================

# logging.basicConfig(
#     format="%(asctime)s %(levelname)s %(name)s: %(message)s",
#     level=logging.INFO,
# )
# # python-telegram-bot is noisy at INFO; silence it
# logging.getLogger("httpx").setLevel(logging.WARNING)
# logging.getLogger("telegram").setLevel(logging.WARNING)
# log = logging.getLogger("rasoi")


# # ===== Per-user session state ====================================
# # In-memory dict. Persisted to disk on every change so a bot restart
# # doesn't nuke active conversations.

# user_state: dict[int, dict] = {}


# def default_state() -> dict:
#     return {
#         "mode": "instamart",
#         "history": [],
#         "pending": None,
#         "last_input_voice": False,  # tracks whether last user message was a voice note
#     }


# def state_path(user_id: int) -> Path:
#     return HISTORY_DIR / f"{user_id}.json"


# def load_state(user_id: int) -> dict:
#     if user_id in user_state:
#         return user_state[user_id]
#     path = state_path(user_id)
#     if path.exists():
#         try:
#             user_state[user_id] = json.loads(path.read_text())
#         except json.JSONDecodeError:
#             log.warning(f"corrupt state file for {user_id}, resetting")
#             user_state[user_id] = default_state()
#     else:
#         user_state[user_id] = default_state()
#     return user_state[user_id]


# def save_state(user_id: int):
#     state = user_state.get(user_id)
#     if not state:
#         return
#     state_path(user_id).write_text(json.dumps(state, default=str, indent=2))


# # ===== Anthropic content block serialization =====================
# # When Claude returns content blocks, they're pydantic objects.
# # We need them as plain dicts for JSON storage AND for re-sending to Claude.

# def serialize_content_blocks(content) -> list:
#     """Convert Anthropic content blocks to JSON-safe dicts."""
#     if isinstance(content, str):
#         return content
#     out = []
#     for block in content:
#         if hasattr(block, "model_dump"):
#             out.append(block.model_dump(exclude_none=True))
#         elif isinstance(block, dict):
#             out.append(block)
#         else:
#             out.append({"type": "text", "text": str(block)})
#     return out


# # ===== Auth / allowlist ==========================================

# def is_allowed(user_id: int) -> bool:
#     if not ALLOWED_USERS:
#         # If no allowlist configured, allow everyone (dev mode warning shown at startup)
#         return True
#     return user_id in ALLOWED_USERS


# def load_swiggy_token() -> str:
#     path = Path(TOKEN_FILE)
#     if not path.exists():
#         raise FileNotFoundError(
#             "token.json not found. Run `python login.py` first to OAuth into Swiggy."
#         )
#     return json.loads(path.read_text())["access_token"]


# # ===== MCP tool conversion =======================================

# def mcp_tools_to_anthropic(mcp_tools) -> list:
#     return [
#         {
#             "name": t.name,
#             "description": t.description or "",
#             "input_schema": t.inputSchema,
#         }
#         for t in mcp_tools
#     ]


# def extract_text_from_mcp_result(result) -> str:
#     parts = []
#     for c in result.content:
#         if hasattr(c, "text"):
#             parts.append(c.text)
#         else:
#             parts.append(str(c))
#     return "\n".join(parts)


# # ===== The agent loop, refactored for per-request use ============

# async def run_agent_turn(user_id: int, user_message: str, voice_mode: bool = False) -> str:
#     """One agent turn: append user message, loop until Claude returns text,
#     save state, return the text reply.

#     voice_mode: if True, augments system prompt with brevity + script rules
#     so the reply is suitable for TTS playback.
#     """
#     state = load_state(user_id)
#     mode = state["mode"]
#     state["history"].append({"role": "user", "content": user_message})

#     token = load_swiggy_token()
#     url = SWIGGY_URLS[mode]
#     headers = {"Authorization": f"Bearer {token}"}
#     system_prompt = SYSTEM_PROMPTS[mode]
#     if voice_mode:
#         system_prompt = system_prompt + VOICE_MODE_SUFFIX

#     anthropic = Anthropic()
#     final_text_parts: list[str] = []

#     async with streamablehttp_client(url=url, headers=headers) as (
#         read_stream, write_stream, _
#     ):
#         async with ClientSession(read_stream, write_stream) as session:
#             await session.initialize()
#             tools_result = await session.list_tools()
#             tools = mcp_tools_to_anthropic(tools_result.tools)

#             # Inner agent loop — keep going until Claude stops requesting tools
#             for _ in range(20):  # safety cap: max 20 tool calls per turn
#                 response = anthropic.messages.create(
#                     model=MODEL,
#                     max_tokens=MAX_TOKENS,
#                     system=system_prompt,
#                     tools=tools,
#                     messages=state["history"],
#                 )

#                 state["history"].append(
#                     {
#                         "role": "assistant",
#                         "content": serialize_content_blocks(response.content),
#                     }
#                 )

#                 tool_uses = [b for b in response.content if b.type == "tool_use"]
#                 text_blocks = [b for b in response.content if b.type == "text"]

#                 for tb in text_blocks:
#                     final_text_parts.append(tb.text)

#                 if response.stop_reason != "tool_use":
#                     break

#                 # Execute each tool call
#                 tool_results = []
#                 for tu in tool_uses:
#                     log.info(f"[user {user_id}] → {tu.name}({json.dumps(tu.input)[:100]})")
#                     try:
#                         result = await session.call_tool(tu.name, tu.input)
#                         result_text = extract_text_from_mcp_result(result)
#                         is_err = result.isError
#                     except Exception as e:
#                         result_text = f"Tool error: {type(e).__name__}: {e}"
#                         is_err = True

#                     tool_results.append(
#                         {
#                             "type": "tool_result",
#                             "tool_use_id": tu.id,
#                             "content": result_text,
#                             "is_error": is_err,
#                         }
#                     )

#                 state["history"].append({"role": "user", "content": tool_results})
#             else:
#                 final_text_parts.append(
#                     "(agent hit max tool calls per turn; stopping for safety)"
#                 )

#     save_state(user_id)
#     return "\n\n".join(final_text_parts) if final_text_parts else "(no reply)"


# # ===== Recipe → cart helpers =====================================

# def detect_youtube_url(text: str) -> Optional[str]:
#     """Returns the YouTube URL if found in the text, else None."""
#     match = re.search(
#         r"(https?://(?:www\.)?(?:youtube\.com/[^\s]+|youtu\.be/[^\s]+))",
#         text,
#     )
#     return match.group(1) if match else None


# def format_pantry_question(ingredients: list, staples: list) -> str:
#     lines = [f"Found {len(ingredients)} ingredients in the recipe.\n"]
#     lines.append("These look like pantry staples — which do you already have at home?\n")
#     for i, ing in enumerate(staples, 1):
#         lines.append(f"  {i}. {ing['name']}")
#     lines.append("")
#     lines.append("Reply with:")
#     lines.append("  • 'all'   — you have all of these")
#     lines.append("  • 'none'  — you need to buy all of these")
#     lines.append("  • Names separated by commas: e.g. 'salt, oil, cumin'")
#     return "\n".join(lines)


# def filter_ingredients_by_pantry_reply(
#     ingredients: list, staples: list, reply: str
# ) -> list:
#     reply = reply.strip().lower()
#     if reply == "all":
#         # Skip all staples, keep only non-staples
#         return [i for i in ingredients if not i.get("pantry_staple")]
#     if reply == "none" or not reply:
#         return ingredients

#     # Parse names
#     names_have = {n.strip() for n in reply.split(",") if n.strip()}
#     skip_names = set()
#     for staple in staples:
#         for have in names_have:
#             if have in staple["name"].lower() or staple["name"].lower() in have:
#                 skip_names.add(staple["name"])
#                 break
#     return [i for i in ingredients if i["name"] not in skip_names]


# def format_ingredient_message(ingredients: list) -> str:
#     lines = [
#         f"Please shop these {len(ingredients)} ingredients on Instamart, "
#         "one at a time, in this order:\n"
#     ]
#     for i, ing in enumerate(ingredients, 1):
#         qty = ing.get("quantity")
#         unit = ing.get("unit") or ""
#         name = ing["name"]
#         notes = ing.get("notes", "")
#         if qty is not None:
#             line = f"{i}. {qty} {unit} {name}".strip()
#         else:
#             line = f"{i}. {name}"
#         if notes:
#             line += f"  [{notes}]"
#         lines.append(line)
#     lines.append("\nUse my default Noida address. Start with the first one.")
#     return "\n".join(lines)


# # ===== Telegram message sending helpers =========================

# async def send_long(update: Update, text: str):
#     """Telegram has a 4096 char limit per message; split if needed."""
#     if len(text) <= TELEGRAM_MSG_LIMIT:
#         await update.message.reply_text(text)
#         return
#     # Split on newlines where possible
#     chunks = []
#     current = ""
#     for line in text.split("\n"):
#         if len(current) + len(line) + 1 > TELEGRAM_MSG_LIMIT:
#             chunks.append(current)
#             current = line
#         else:
#             current = current + "\n" + line if current else line
#     if current:
#         chunks.append(current)
#     for chunk in chunks:
#         await update.message.reply_text(chunk)


# async def typing(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     await context.bot.send_chat_action(
#         chat_id=update.effective_chat.id, action=ChatAction.TYPING
#     )


# # ===== Text-to-speech (OpenAI TTS) ===============================

# def synthesize_speech(text: str) -> bytes:
#     """Generate Opus audio bytes from text via OpenAI TTS.
#     Telegram voice messages use .ogg/Opus, so we request Opus directly.
#     Blocking call — wrap in asyncio.to_thread when calling from async code.
#     """
#     response = openai_client.audio.speech.create(
#         model=TTS_MODEL,
#         voice=TTS_VOICE,
#         input=text,
#         response_format="opus",
#     )
#     return response.content


# async def send_smart(
#     update: Update,
#     context: ContextTypes.DEFAULT_TYPE,
#     user_id: int,
#     text: str,
# ):
#     """Decide voice vs text for the agent reply based on user state.

#     Rule (per Phase 6 design):
#       - If last input was voice AND reply is short (<= TTS_MAX_REPLY_CHARS):
#           → send as voice note only (no text)
#       - Otherwise:
#           → send as text (split if needed)
#     """
#     state = load_state(user_id)
#     use_voice = (
#         state.get("last_input_voice", False)
#         and len(text) <= TTS_MAX_REPLY_CHARS
#     )

#     if not use_voice:
#         await send_long(update, text)
#         return

#     # Generate TTS, send as Telegram voice note
#     await context.bot.send_chat_action(
#         chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE
#     )
#     try:
#         audio_bytes = await asyncio.to_thread(synthesize_speech, text)
#     except Exception as e:
#         log.exception("TTS failed")
#         # Fall back to text on TTS failure
#         await update.message.reply_text(f"(TTS failed: {e})\n\n{text}")
#         return

#     audio_buf = io.BytesIO(audio_bytes)
#     audio_buf.name = "reply.ogg"
#     await update.message.reply_voice(voice=audio_buf)


# # ===== Telegram command handlers =================================

# async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user = update.effective_user
#     if not is_allowed(user.id):
#         await update.message.reply_text(
#             f"Your Telegram ID ({user.id}) isn't allowlisted.\n"
#             f"The bot owner needs to add it to TELEGRAM_ALLOWED_USERS."
#         )
#         return

#     load_state(user.id)  # initialize
#     await update.message.reply_text(
#         f"Hi {user.first_name}! I'm your Swiggy ordering bot.\n\n"
#         f"What I can do:\n"
#         f"• Order groceries from Instamart (default mode)\n"
#         f"• Order food via Swiggy Food — type /mode food\n"
#         f"• Parse a recipe image → grocery cart\n"
#         f"• Parse a YouTube cooking video URL → grocery cart\n"
#         f"• Voice notes in Hindi, English, or Hinglish 🎙️\n\n"
#         f"Commands:\n"
#         f"  /mode food  or  /mode instamart\n"
#         f"  /reset      — clear my memory of our conversation\n"
#         f"  /whoami     — show your Telegram ID\n\n"
#         f"Try: 'search for onions', send a recipe image, or hold the\n"
#         f"mic button and say what you want."
#     )


# async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user = update.effective_user
#     await update.message.reply_text(
#         f"Your Telegram user ID is: `{user.id}`\n"
#         f"Username: @{user.username or '(none)'}\n"
#         f"Allowlisted: {'✓ yes' if is_allowed(user.id) else '✗ no'}",
#         parse_mode="Markdown",
#     )


# async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user_id = update.effective_user.id
#     if not is_allowed(user_id):
#         return

#     args = context.args
#     if not args or args[0] not in {"food", "instamart"}:
#         state = load_state(user_id)
#         await update.message.reply_text(
#             f"Current mode: *{state['mode']}*\n\n"
#             f"Switch with:\n"
#             f"  /mode food\n"
#             f"  /mode instamart",
#             parse_mode="Markdown",
#         )
#         return

#     state = load_state(user_id)
#     state["mode"] = args[0]
#     state["history"] = []  # mode switch clears history (different MCP server)
#     state["pending"] = None
#     save_state(user_id)
#     await update.message.reply_text(
#         f"Switched to *{args[0]}* mode. Conversation history cleared.",
#         parse_mode="Markdown",
#     )


# async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user_id = update.effective_user.id
#     if not is_allowed(user_id):
#         return
#     state = load_state(user_id)
#     state["history"] = []
#     state["pending"] = None
#     save_state(user_id)
#     await update.message.reply_text("Memory cleared. Fresh start.")


# # ===== Telegram message handlers =================================

# async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user_id = update.effective_user.id
#     if not is_allowed(user_id):
#         await update.message.reply_text(
#             f"Not allowlisted (your ID: {user_id})."
#         )
#         return

#     # User typed (not voice) → reply mode goes back to text
#     state = load_state(user_id)
#     state["last_input_voice"] = False
#     save_state(user_id)

#     await _process_text_input(update, context, user_id, update.message.text)


# async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     """Transcribe a Telegram voice note via Whisper, show transcript, then
#     route through the same pipeline as a typed message."""
#     user_id = update.effective_user.id
#     if not is_allowed(user_id):
#         return

#     await typing(update, context)

#     # Download voice note from Telegram. Voice notes come as .ogg (Opus codec)
#     # which Whisper accepts natively — no transcoding needed.
#     voice = update.message.voice
#     duration = voice.duration  # in seconds
#     log.info(f"[user {user_id}] voice note received ({duration}s)")

#     if duration > 120:
#         await update.message.reply_text(
#             "Voice note is over 2 minutes — please keep it shorter."
#         )
#         return

#     try:
#         voice_file = await voice.get_file()
#         voice_bytes = bytes(await voice_file.download_as_bytearray())
#     except Exception as e:
#         log.exception("voice download failed")
#         await update.message.reply_text(f"⚠️ Couldn't download voice: {e}")
#         return

#     # Transcribe via Whisper API.
#     # OpenAI SDK reads format from filename — set .name on the BytesIO so it
#     # knows this is .ogg/Opus.
#     audio_buf = io.BytesIO(voice_bytes)
#     audio_buf.name = "voice.ogg"

#     try:
#         # Run blocking SDK call in a thread so we don't block the event loop
#         transcription = await asyncio.to_thread(
#             openai_client.audio.transcriptions.create,
#             model=WHISPER_MODEL,
#             file=audio_buf,
#             # No language= set → Whisper auto-detects (handles Hindi/English/mixed)
#         )
#         transcript_text = transcription.text.strip()
#     except Exception as e:
#         log.exception("whisper failed")
#         await update.message.reply_text(f"⚠️ Transcription failed: {e}")
#         return

#     if not transcript_text:
#         await update.message.reply_text(
#             "Couldn't make out what you said. Try again with clearer audio?"
#         )
#         return

#     # Show transcript before processing — transparency for the user
#     await update.message.reply_text(f"🎙️ Heard: {transcript_text}")

#     # Flag the session so subsequent short agent replies come back as voice
#     state = load_state(user_id)
#     state["last_input_voice"] = True
#     save_state(user_id)

#     # Route through the same logic as a typed text message, but with voice_mode
#     # so the system prompt enforces brevity + Roman script for TTS quality.
#     await _process_text_input(update, context, user_id, transcript_text, voice_mode=True)


# async def _process_text_input(
#     update: Update,
#     context: ContextTypes.DEFAULT_TYPE,
#     user_id: int,
#     text: str,
#     voice_mode: bool = False,
# ):
#     """The core text-routing logic, shared by text_handler and voice_handler.

#     Routes to: pending pantry-staple reply → YouTube URL parser → normal agent.
#     voice_mode flows through to run_agent_turn so the reply is TTS-friendly.
#     """
#     state = load_state(user_id)

#     # 1. Check for pending pantry-staple reply
#     if state.get("pending") and state["pending"]["type"] == "pantry_staples":
#         pending = state["pending"]
#         filtered = filter_ingredients_by_pantry_reply(
#             pending["ingredients"], pending["staples"], text
#         )
#         state["pending"] = None
#         save_state(user_id)

#         if not filtered:
#             await update.message.reply_text("Nothing left to shop. Done!")
#             return

#         await update.message.reply_text(
#             f"Shopping {len(filtered)} items. Starting now..."
#         )
#         await typing(update, context)

#         agent_input = format_ingredient_message(filtered)
#         state["mode"] = "instamart"
#         state["history"] = []
#         save_state(user_id)

#         reply = await run_agent_turn(user_id, agent_input, voice_mode=voice_mode)
#         await send_smart(update, context, user_id, reply)
#         return

#     # 2. Check for YouTube URL
#     yt_url = detect_youtube_url(text)
#     if yt_url:
#         await update.message.reply_text(
#             "Found a YouTube URL — fetching transcript & extracting ingredients..."
#         )
#         await typing(update, context)
#         try:
#             ingredients = parse_youtube(yt_url)
#         except Exception as e:
#             await update.message.reply_text(f"Couldn't parse video: {e}")
#             return

#         await handle_parsed_ingredients(update, context, user_id, ingredients)
#         return

#     # 3. Normal text → current agent
#     await typing(update, context)
#     try:
#         reply = await run_agent_turn(user_id, text, voice_mode=voice_mode)
#     except FileNotFoundError as e:
#         await update.message.reply_text(f"⚠️ {e}")
#         return
#     except Exception as e:
#         log.exception("agent error")
#         await update.message.reply_text(f"⚠️ Agent error: {type(e).__name__}: {e}")
#         return

#     await send_smart(update, context, user_id, reply)


# async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     user_id = update.effective_user.id
#     if not is_allowed(user_id):
#         return

#     # Image input → text replies
#     state = load_state(user_id)
#     state["last_input_voice"] = False
#     save_state(user_id)

#     await update.message.reply_text("Got the image. Extracting ingredients...")
#     await typing(update, context)

#     # Telegram sends multiple sizes; get the largest
#     photo = update.message.photo[-1]
#     photo_file = await photo.get_file()
#     image_bytes = bytes(await photo_file.download_as_bytearray())

#     try:
#         ingredients = parse_image_bytes(image_bytes, mime_type="image/jpeg")
#     except Exception as e:
#         await update.message.reply_text(f"Couldn't parse image: {e}")
#         return

#     await handle_parsed_ingredients(update, context, user_id, ingredients)


# async def handle_parsed_ingredients(
#     update: Update,
#     context: ContextTypes.DEFAULT_TYPE,
#     user_id: int,
#     ingredients: list,
# ):
#     """After we have a parsed ingredient list, ask about pantry staples
#     (or skip the question if there are none)."""
#     state = load_state(user_id)

#     summary = (
#         f"Found {len(ingredients)} ingredients:\n\n"
#         + summarize_ingredients(ingredients)
#     )
#     await send_long(update, summary)

#     staples = [ing for ing in ingredients if ing.get("pantry_staple")]
#     if not staples:
#         await update.message.reply_text("No pantry staples. Starting to shop...")
#         await typing(update, context)
#         agent_input = format_ingredient_message(ingredients)
#         state["mode"] = "instamart"
#         state["history"] = []
#         save_state(user_id)
#         reply = await run_agent_turn(user_id, agent_input)
#         await send_smart(update, context, user_id, reply)
#         return

#     # Save pending state, ask the question
#     state["pending"] = {
#         "type": "pantry_staples",
#         "ingredients": ingredients,
#         "staples": staples,
#     }
#     save_state(user_id)

#     await update.message.reply_text(format_pantry_question(ingredients, staples))


# # ===== Bot bootstrap =============================================

# def main():
#     if not ALLOWED_USERS:
#         log.warning("⚠️  TELEGRAM_ALLOWED_USERS not set — bot will reply to ANYONE.")
#         log.warning("    For safety, set TELEGRAM_ALLOWED_USERS=<your_id> and restart.")
#     else:
#         log.info(f"Allowlist: {ALLOWED_USERS}")

#     # Sanity: token.json must exist
#     try:
#         load_swiggy_token()
#     except FileNotFoundError:
#         log.error("token.json missing — run `python login.py` first.")
#         sys.exit(1)

#     app = Application.builder().token(BOT_TOKEN).build()

#     app.add_handler(CommandHandler("start", start_cmd))
#     app.add_handler(CommandHandler("whoami", whoami_cmd))
#     app.add_handler(CommandHandler("mode", mode_cmd))
#     app.add_handler(CommandHandler("reset", reset_cmd))
#     app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
#     app.add_handler(MessageHandler(filters.VOICE, voice_handler))
#     app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

#     log.info("Bot starting — polling for messages...")
#     app.run_polling(allowed_updates=Update.ALL_TYPES)


# if __name__ == "__main__":
#     main()



















"""
telegram_bot.py — Phase 5a: Telegram interface for the Swiggy MCP agent.

Features:
  - /start         welcome message
  - /whoami        prints your Telegram user ID (use it to allowlist yourself)
  - /mode <food|instamart>   switch agent mode
  - /reset         clears your conversation history
  - text           routed to current mode's agent
  - photo          parsed as recipe → Instamart shopping flow
  - YouTube URL    same as photo, parsed via transcript

Single-user for now: uses your existing token.json. Multi-user OAuth is Phase 5b.

Setup:
  export TELEGRAM_BOT_TOKEN="7891234:AAFxxx..."
  export TELEGRAM_ALLOWED_USERS="123456789"   # your Telegram user ID
  export ANTHROPIC_API_KEY="sk-ant-..."
  python telegram_bot.py

Then open Telegram, find your bot by its username, send /start.
"""

import asyncio
import io
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

from anthropic import Anthropic
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from openai import OpenAI
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from parse_image import parse_image_bytes
from parse_youtube import parse_youtube
from recipe_prompt import PANTRY_STAPLES, summarize_ingredients


# ===== Config =====================================================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not BOT_TOKEN:
    print("✗ Set TELEGRAM_BOT_TOKEN env var (get from @BotFather)")
    sys.exit(1)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    print("✗ Set OPENAI_API_KEY env var (for Whisper voice transcription)")
    print("  Get one at: https://platform.openai.com/api-keys")
    sys.exit(1)

# OpenAI client for Whisper. Cost: ~$0.006/min (~₹0.50/min) — trivial for demos.
openai_client = OpenAI(api_key=OPENAI_API_KEY)
WHISPER_MODEL = "whisper-1"

# TTS config — when user sends voice and the reply is short, we synthesize speech.
TTS_MODEL = "tts-1"
TTS_VOICE = "nova"  # alloy | echo | fable | onyx | nova | shimmer
TTS_MAX_REPLY_CHARS = 500  # over this → text only; voice_mode in prompt aims for <200

ALLOWED_USERS: set[int] = set()
if os.environ.get("TELEGRAM_ALLOWED_USERS"):
    ALLOWED_USERS = {
        int(x.strip())
        for x in os.environ["TELEGRAM_ALLOWED_USERS"].split(",")
        if x.strip()
    }

SWIGGY_URLS = {
    "food": "https://mcp.swiggy.com/food",
    "instamart": "https://mcp.swiggy.com/im",
}
TOKEN_FILE = "token.json"
MODEL = "claude-haiku-4-5"
MAX_TOKENS = 4096
TELEGRAM_MSG_LIMIT = 4000  # safe under Telegram's 4096 limit

HISTORY_DIR = Path("data/histories")
HISTORY_DIR.mkdir(parents=True, exist_ok=True)


SYSTEM_PROMPTS = {
    "instamart": """You are a grocery shopping agent using the Swiggy Instamart MCP server.

⚠️ TOP-PRIORITY OUTPUT RULES — these override everything else:

A. SCRIPT: Reply in ROMAN/LATIN letters only. NEVER use Devanagari (हिंदी) script,
   EVER. If the user wrote in Hindi or Hinglish, you MUST reply in Hinglish written
   with ENGLISH letters.
     ✓ "Theek hai, paneer mil gaya, 95 rupees ka hai. Add karu cart mein?"
     ✗ "ठीक है, पनीर मिल गया, 95 rupees का है।"
   This is non-negotiable. Failure breaks the text-to-speech system.

B. NUMBERS: Write as digits (1, 33, 200). Currency: write "rupees" or "Rs",
   NEVER use the ₹ symbol.

C. BREVITY: 1-3 short sentences per reply unless the user explicitly asks for
   a long list. Confirmations and questions should be very short.

D. NO MARKDOWN: No **bold**, no *italics*, no bullet points. Plain prose only.

NOW the business rules:
1. Payment is ALWAYS Cash on Delivery (COD). Don't ask, don't suggest alternatives.
2. NEVER call checkout without explicit user confirmation with the full cart summary.
3. Orders CANNOT be cancelled once placed. Confirm carefully.
4. Always call get_cart before final confirmation.
5. Process ingredients one at a time. If multiple SKU matches, ask the user to pick.
6. Skip pantry staples the user already has.
7. Use the user's default Noida address unless they say otherwise.
8. Handle Hindi, Hinglish, and English input — but ALWAYS reply per Rule A.
9. If a tool response mentions a "rich UI widget", don't re-list that data.
10. REORDER: If user says "reorder", "same as last time", "my usual", "phir se mangwa
    do", or similar, call get_orders FIRST to fetch their order history, show them
    the recent orders, and offer to reorder one. Don't guess what their usual is —
    look it up.
""",
    "food": """You are a food ordering agent using the Swiggy Food MCP server.

⚠️ TOP-PRIORITY OUTPUT RULES — these override everything else:

A. SCRIPT: Reply in ROMAN/LATIN letters only. NEVER use Devanagari (हिंदी) script,
   EVER. If the user wrote in Hindi or Hinglish, you MUST reply in Hinglish written
   with ENGLISH letters.
     ✓ "Biryani House se chicken biryani mil gayi, 349 rupees. Place karein?"
     ✗ "बिरयानी हाउस से चिकन बिरयानी मिल गई।"
   This is non-negotiable. Failure breaks the text-to-speech system.

B. NUMBERS: Write as digits. Currency: "rupees" or "Rs", NEVER the ₹ symbol.

C. BREVITY: 1-3 short sentences per reply. Confirmations especially short.

D. NO MARKDOWN: plain prose only.

NOW the business rules:
1. Payment is ALWAYS Cash on Delivery (COD).
2. NEVER place an order without explicit user confirmation with cart summary.
3. Cart total cannot exceed 1000 rupees (Builders Club limit).
4. A cart holds items from ONE restaurant only. Warn before switching.
5. Always call get_food_cart before confirmation.
6. Use the user's Home/Noida address unless specified.
7. Recognize Hindi/Hinglish confirmations: "haan", "kar de", "order karo" → confirm.
8. REORDER: If user says "reorder", "same as last time", "my usual", "phir se mangwa
    do", or similar, call get_food_orders FIRST to fetch their order history, show
    them the recent orders, and offer to reorder one. Don't guess — look it up.
""",
}

# Extra instruction appended to system prompt when user input was a voice note.
# Keeps replies short enough to play as voice + extra-emphatic about script.
VOICE_MODE_SUFFIX = """

⚠️ VOICE MODE ACTIVE — the user spoke this message. Your reply will be read
aloud. Therefore:
- Keep reply UNDER 200 characters total. Cut anything non-essential.
- Roman/Latin script only — no Devanagari at all.
- No symbols, no markdown, no asterisks. Plain text.
- Speak naturally as if telling a friend a quick fact.
"""


# ===== Logging ====================================================

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
# python-telegram-bot is noisy at INFO; silence it
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("rasoi")


# ===== Per-user session state ====================================
# In-memory dict. Persisted to disk on every change so a bot restart
# doesn't nuke active conversations.

user_state: dict[int, dict] = {}


def default_state() -> dict:
    return {
        "mode": "instamart",
        "history": [],
        "pending": None,
        "last_input_voice": False,  # tracks whether last user message was a voice note
    }


def state_path(user_id: int) -> Path:
    return HISTORY_DIR / f"{user_id}.json"


def load_state(user_id: int) -> dict:
    if user_id in user_state:
        return user_state[user_id]
    path = state_path(user_id)
    if path.exists():
        try:
            user_state[user_id] = json.loads(path.read_text())
        except json.JSONDecodeError:
            log.warning(f"corrupt state file for {user_id}, resetting")
            user_state[user_id] = default_state()
    else:
        user_state[user_id] = default_state()
    return user_state[user_id]


def save_state(user_id: int):
    state = user_state.get(user_id)
    if not state:
        return
    state_path(user_id).write_text(json.dumps(state, default=str, indent=2))


# ===== Anthropic content block serialization =====================
# When Claude returns content blocks, they're pydantic objects.
# We need them as plain dicts for JSON storage AND for re-sending to Claude.

def serialize_content_blocks(content) -> list:
    """Convert Anthropic content blocks to JSON-safe dicts."""
    if isinstance(content, str):
        return content
    out = []
    for block in content:
        if hasattr(block, "model_dump"):
            out.append(block.model_dump(exclude_none=True))
        elif isinstance(block, dict):
            out.append(block)
        else:
            out.append({"type": "text", "text": str(block)})
    return out


# ===== Auth / allowlist ==========================================

def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        # If no allowlist configured, allow everyone (dev mode warning shown at startup)
        return True
    return user_id in ALLOWED_USERS


def load_swiggy_token() -> str:
    path = Path(TOKEN_FILE)
    if not path.exists():
        raise FileNotFoundError(
            "token.json not found. Run `python login.py` first to OAuth into Swiggy."
        )
    return json.loads(path.read_text())["access_token"]


# ===== MCP tool conversion =======================================

def mcp_tools_to_anthropic(mcp_tools) -> list:
    return [
        {
            "name": t.name,
            "description": t.description or "",
            "input_schema": t.inputSchema,
        }
        for t in mcp_tools
    ]


def extract_text_from_mcp_result(result) -> str:
    parts = []
    for c in result.content:
        if hasattr(c, "text"):
            parts.append(c.text)
        else:
            parts.append(str(c))
    return "\n".join(parts)


# ===== Supervisor classifier (auto Food vs Instamart routing) ====
# Per-message classification. Avoids the "all 35 tools at once" trap by
# routing to a single-server agent each turn.

CLASSIFIER_PROMPT = """You are a routing classifier. Given a user message, decide:
- "food" — user wants restaurant food delivered (biryani, pizza, dosa, curries, restaurant meals)
- "instamart" — user wants groceries / packaged goods (vegetables, milk, snacks, household items)
- "stay" — the message is a continuation of an existing conversation (confirmation,
  cart edit, address change, follow-up question) — do not switch context

Current conversation mode: {current_mode}
Recent context (last few user messages, if any): {context}

User message: "{message}"

Reply with EXACTLY ONE word: food | instamart | stay
No punctuation, no explanation. Just the word.
"""


def get_recent_user_messages(history: list, n: int = 3) -> str:
    """Extract last N user-role plain-text messages for context."""
    msgs = []
    for msg in reversed(history):
        if msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str) and len(content) < 200:
                msgs.append(content)
                if len(msgs) >= n:
                    break
    return " | ".join(reversed(msgs)) if msgs else "(none)"


async def classify_intent(user_message: str, current_mode: str, history: list) -> str:
    """Return target mode: 'food' | 'instamart' | current_mode (no switch)."""
    anthropic = Anthropic()
    context = get_recent_user_messages(history)
    prompt = CLASSIFIER_PROMPT.format(
        current_mode=current_mode,
        context=context,
        message=user_message,
    )

    try:
        response = await asyncio.to_thread(
            anthropic.messages.create,
            model=MODEL,
            max_tokens=20,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:
        log.warning(f"classifier failed: {e}; staying in {current_mode}")
        return current_mode

    answer = response.content[0].text.strip().lower()
    # Strip punctuation / extra words defensively
    answer = re.sub(r"[^a-z]", "", answer.split()[0]) if answer else ""

    if answer == "stay" or answer not in {"food", "instamart"}:
        return current_mode
    return answer


# ===== Background order tracking =================================
# After a successful checkout/place_order, spawn an async task that polls
# the order status and pushes updates to the user.

# Maps order_id → asyncio.Task so we don't double-track the same order
active_trackers: dict[str, asyncio.Task] = {}

# Bot reference set in main() so background tasks can send messages
BOT_INSTANCE = None

ORDER_PLACEMENT_TOOLS = {"place_food_order", "checkout"}
ORDER_LIST_TOOLS = {"food": "get_food_orders", "instamart": "get_orders"}
ORDER_TRACK_TOOLS = {"food": "track_food_order", "instamart": "track_order"}

TRACK_POLL_INTERVAL = 60  # seconds between status checks
TRACK_MAX_DURATION = 90 * 60  # stop after 90 minutes regardless

# Detect "terminal" states in the tracking response — case-insensitive substrings
TERMINAL_STATUS_KEYWORDS = (
    "delivered", "completed", "cancelled", "canceled", "failed", "refund"
)


async def fetch_latest_order_id(user_id: int, mode: str) -> Optional[str]:
    """Call get_food_orders or get_orders and try to extract the most-recent
    order ID from the response. The MCP server returns human-formatted text,
    so we use a regex to find an order ID."""
    token = load_swiggy_token()
    url = SWIGGY_URLS[mode]
    headers = {"Authorization": f"Bearer {token}"}
    tool_name = ORDER_LIST_TOOLS[mode]

    try:
        async with streamablehttp_client(url=url, headers=headers) as (rs, ws, _):
            async with ClientSession(rs, ws) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, {})
                text = extract_text_from_mcp_result(result)
    except Exception as e:
        log.warning(f"fetch_latest_order_id failed: {e}")
        return None

    # Try multiple order-ID patterns we might see
    patterns = [
        r"\border[\s_]?id[:\s]+([A-Za-z0-9_-]+)",
        r"\b(ord[_-][A-Za-z0-9]+)\b",
        r"\bID[:\s]+([A-Za-z0-9_-]{8,})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


async def order_tracker_loop(user_id: int, order_id: str, mode: str):
    """Poll the order status every minute, push updates to user on change."""
    if BOT_INSTANCE is None:
        log.error("BOT_INSTANCE not set; cannot send tracker updates")
        return

    log.info(f"[tracker {order_id}] starting for user {user_id} in {mode} mode")
    token = load_swiggy_token()
    url = SWIGGY_URLS[mode]
    headers = {"Authorization": f"Bearer {token}"}
    tool_name = ORDER_TRACK_TOOLS[mode]

    last_status_text = None
    elapsed = 0

    try:
        async with streamablehttp_client(url=url, headers=headers) as (rs, ws, _):
            async with ClientSession(rs, ws) as session:
                await session.initialize()

                while elapsed < TRACK_MAX_DURATION:
                    try:
                        result = await session.call_tool(tool_name, {"orderId": order_id})
                        status_text = extract_text_from_mcp_result(result)
                    except Exception as e:
                        log.warning(f"[tracker {order_id}] poll failed: {e}")
                        await asyncio.sleep(TRACK_POLL_INTERVAL)
                        elapsed += TRACK_POLL_INTERVAL
                        continue

                    # Send update only on a meaningful status change
                    if status_text and status_text != last_status_text:
                        short = status_text[:300]
                        await BOT_INSTANCE.send_message(
                            chat_id=user_id,
                            text=f"📦 Order update:\n\n{short}",
                        )
                        last_status_text = status_text

                    # Stop polling if we hit a terminal state
                    if any(kw in status_text.lower() for kw in TERMINAL_STATUS_KEYWORDS):
                        log.info(f"[tracker {order_id}] terminal state, stopping")
                        break

                    await asyncio.sleep(TRACK_POLL_INTERVAL)
                    elapsed += TRACK_POLL_INTERVAL
    finally:
        active_trackers.pop(order_id, None)
        log.info(f"[tracker {order_id}] finished")


async def maybe_start_tracking(user_id: int, tools_called: list, mode: str):
    """If the latest agent turn included a successful checkout, spawn a tracker.
    Looks up the most recent order ID via get_food_orders / get_orders."""
    if not any(t in ORDER_PLACEMENT_TOOLS for t in tools_called):
        return

    order_id = await fetch_latest_order_id(user_id, mode)
    if not order_id:
        log.warning(f"order placed but couldn't extract order ID for user {user_id}")
        return
    if order_id in active_trackers:
        return  # already tracking

    log.info(f"[user {user_id}] starting tracker for order {order_id}")
    task = asyncio.create_task(order_tracker_loop(user_id, order_id, mode))
    active_trackers[order_id] = task


# ===== The agent loop, refactored for per-request use ============

async def run_agent_turn(
    user_id: int, user_message: str, voice_mode: bool = False
) -> tuple[str, list[str]]:
    """One agent turn: append user message, loop until Claude returns text,
    save state, return (reply_text, tool_names_called).

    voice_mode: if True, augments system prompt with brevity + script rules
    so the reply is suitable for TTS playback.
    Returns the tool names called during the turn so callers can detect
    post-turn side effects (like a successful checkout).
    """
    state = load_state(user_id)
    mode = state["mode"]
    state["history"].append({"role": "user", "content": user_message})

    token = load_swiggy_token()
    url = SWIGGY_URLS[mode]
    headers = {"Authorization": f"Bearer {token}"}
    system_prompt = SYSTEM_PROMPTS[mode]
    if voice_mode:
        system_prompt = system_prompt + VOICE_MODE_SUFFIX

    anthropic = Anthropic()
    final_text_parts: list[str] = []
    tools_called: list[str] = []

    async with streamablehttp_client(url=url, headers=headers) as (
        read_stream, write_stream, _
    ):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tools = mcp_tools_to_anthropic(tools_result.tools)

            # Inner agent loop — keep going until Claude stops requesting tools
            for _ in range(20):  # safety cap: max 20 tool calls per turn
                response = anthropic.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=system_prompt,
                    tools=tools,
                    messages=state["history"],
                )

                state["history"].append(
                    {
                        "role": "assistant",
                        "content": serialize_content_blocks(response.content),
                    }
                )

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                text_blocks = [b for b in response.content if b.type == "text"]

                for tb in text_blocks:
                    final_text_parts.append(tb.text)

                if response.stop_reason != "tool_use":
                    break

                # Execute each tool call
                tool_results = []
                for tu in tool_uses:
                    tools_called.append(tu.name)
                    log.info(f"[user {user_id}] → {tu.name}({json.dumps(tu.input)[:100]})")
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

                state["history"].append({"role": "user", "content": tool_results})
            else:
                final_text_parts.append(
                    "(agent hit max tool calls per turn; stopping for safety)"
                )

    save_state(user_id)
    reply_text = "\n\n".join(final_text_parts) if final_text_parts else "(no reply)"
    return reply_text, tools_called


# ===== Recipe → cart helpers =====================================

def detect_youtube_url(text: str) -> Optional[str]:
    """Returns the YouTube URL if found in the text, else None."""
    match = re.search(
        r"(https?://(?:www\.)?(?:youtube\.com/[^\s]+|youtu\.be/[^\s]+))",
        text,
    )
    return match.group(1) if match else None


def format_pantry_question(ingredients: list, staples: list) -> str:
    lines = [f"Found {len(ingredients)} ingredients in the recipe.\n"]
    lines.append("These look like pantry staples — which do you already have at home?\n")
    for i, ing in enumerate(staples, 1):
        lines.append(f"  {i}. {ing['name']}")
    lines.append("")
    lines.append("Reply with:")
    lines.append("  • 'all'   — you have all of these")
    lines.append("  • 'none'  — you need to buy all of these")
    lines.append("  • Names separated by commas: e.g. 'salt, oil, cumin'")
    return "\n".join(lines)


def filter_ingredients_by_pantry_reply(
    ingredients: list, staples: list, reply: str
) -> list:
    reply = reply.strip().lower()
    if reply == "all":
        # Skip all staples, keep only non-staples
        return [i for i in ingredients if not i.get("pantry_staple")]
    if reply == "none" or not reply:
        return ingredients

    # Parse names
    names_have = {n.strip() for n in reply.split(",") if n.strip()}
    skip_names = set()
    for staple in staples:
        for have in names_have:
            if have in staple["name"].lower() or staple["name"].lower() in have:
                skip_names.add(staple["name"])
                break
    return [i for i in ingredients if i["name"] not in skip_names]


def format_ingredient_message(ingredients: list) -> str:
    lines = [
        f"Please shop these {len(ingredients)} ingredients on Instamart, "
        "one at a time, in this order:\n"
    ]
    for i, ing in enumerate(ingredients, 1):
        qty = ing.get("quantity")
        unit = ing.get("unit") or ""
        name = ing["name"]
        notes = ing.get("notes", "")
        if qty is not None:
            line = f"{i}. {qty} {unit} {name}".strip()
        else:
            line = f"{i}. {name}"
        if notes:
            line += f"  [{notes}]"
        lines.append(line)
    lines.append("\nUse my default Noida address. Start with the first one.")
    return "\n".join(lines)


# ===== Telegram message sending helpers =========================

async def send_long(update: Update, text: str):
    """Telegram has a 4096 char limit per message; split if needed."""
    if len(text) <= TELEGRAM_MSG_LIMIT:
        await update.message.reply_text(text)
        return
    # Split on newlines where possible
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > TELEGRAM_MSG_LIMIT:
            chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)
    for chunk in chunks:
        await update.message.reply_text(chunk)


async def typing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.TYPING
    )


# ===== Text-to-speech (OpenAI TTS) ===============================

def synthesize_speech(text: str) -> bytes:
    """Generate Opus audio bytes from text via OpenAI TTS.
    Telegram voice messages use .ogg/Opus, so we request Opus directly.
    Blocking call — wrap in asyncio.to_thread when calling from async code.
    """
    response = openai_client.audio.speech.create(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        input=text,
        response_format="opus",
    )
    return response.content


async def send_smart(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    text: str,
):
    """Decide voice vs text for the agent reply based on user state.

    Rule (per Phase 6 design):
      - If last input was voice AND reply is short (<= TTS_MAX_REPLY_CHARS):
          → send as voice note only (no text)
      - Otherwise:
          → send as text (split if needed)
    """
    state = load_state(user_id)
    use_voice = (
        state.get("last_input_voice", False)
        and len(text) <= TTS_MAX_REPLY_CHARS
    )

    if not use_voice:
        await send_long(update, text)
        return

    # Generate TTS, send as Telegram voice note
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE
    )
    try:
        audio_bytes = await asyncio.to_thread(synthesize_speech, text)
    except Exception as e:
        log.exception("TTS failed")
        # Fall back to text on TTS failure
        await update.message.reply_text(f"(TTS failed: {e})\n\n{text}")
        return

    audio_buf = io.BytesIO(audio_bytes)
    audio_buf.name = "reply.ogg"
    await update.message.reply_voice(voice=audio_buf)


# ===== Telegram command handlers =================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_allowed(user.id):
        await update.message.reply_text(
            f"Your Telegram ID ({user.id}) isn't allowlisted.\n"
            f"The bot owner needs to add it to TELEGRAM_ALLOWED_USERS."
        )
        return

    load_state(user.id)  # initialize
    await update.message.reply_text(
        f"Hi {user.first_name}! I'm your Swiggy ordering bot.\n\n"
        f"What I can do:\n"
        f"• Order food OR groceries — I auto-route based on what you ask\n"
        f"• Parse a recipe image → grocery cart\n"
        f"• Parse a YouTube cooking video URL → grocery cart\n"
        f"• Voice notes in Hindi, English, or Hinglish 🎙️\n"
        f"• Reorder your usual — just say 'reorder my last order'\n"
        f"• Push order updates automatically after checkout 📦\n\n"
        f"Commands:\n"
        f"  /mode food  or  /mode instamart  — force a mode (auto by default)\n"
        f"  /reset      — clear my memory of our conversation\n"
        f"  /whoami     — show your Telegram ID\n\n"
        f"Try: 'mujhe pyaaz mangwana hai', send a recipe image, or hold the\n"
        f"mic button and say what you want."
    )


async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Your Telegram user ID is: `{user.id}`\n"
        f"Username: @{user.username or '(none)'}\n"
        f"Allowlisted: {'✓ yes' if is_allowed(user.id) else '✗ no'}",
        parse_mode="Markdown",
    )


async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    args = context.args
    if not args or args[0] not in {"food", "instamart"}:
        state = load_state(user_id)
        await update.message.reply_text(
            f"Current mode: *{state['mode']}*\n\n"
            f"Switch with:\n"
            f"  /mode food\n"
            f"  /mode instamart",
            parse_mode="Markdown",
        )
        return

    state = load_state(user_id)
    state["mode"] = args[0]
    state["history"] = []  # mode switch clears history (different MCP server)
    state["pending"] = None
    save_state(user_id)
    await update.message.reply_text(
        f"Switched to *{args[0]}* mode. Conversation history cleared.",
        parse_mode="Markdown",
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return
    state = load_state(user_id)
    state["history"] = []
    state["pending"] = None
    save_state(user_id)
    await update.message.reply_text("Memory cleared. Fresh start.")


# ===== Telegram message handlers =================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text(
            f"Not allowlisted (your ID: {user_id})."
        )
        return

    # User typed (not voice) → reply mode goes back to text
    state = load_state(user_id)
    state["last_input_voice"] = False
    save_state(user_id)

    await _process_text_input(update, context, user_id, update.message.text)


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Transcribe a Telegram voice note via Whisper, show transcript, then
    route through the same pipeline as a typed message."""
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    await typing(update, context)

    # Download voice note from Telegram. Voice notes come as .ogg (Opus codec)
    # which Whisper accepts natively — no transcoding needed.
    voice = update.message.voice
    duration = voice.duration  # in seconds
    log.info(f"[user {user_id}] voice note received ({duration}s)")

    if duration > 120:
        await update.message.reply_text(
            "Voice note is over 2 minutes — please keep it shorter."
        )
        return

    try:
        voice_file = await voice.get_file()
        voice_bytes = bytes(await voice_file.download_as_bytearray())
    except Exception as e:
        log.exception("voice download failed")
        await update.message.reply_text(f"⚠️ Couldn't download voice: {e}")
        return

    # Transcribe via Whisper API.
    # OpenAI SDK reads format from filename — set .name on the BytesIO so it
    # knows this is .ogg/Opus.
    audio_buf = io.BytesIO(voice_bytes)
    audio_buf.name = "voice.ogg"

    try:
        # Run blocking SDK call in a thread so we don't block the event loop
        transcription = await asyncio.to_thread(
            openai_client.audio.transcriptions.create,
            model=WHISPER_MODEL,
            file=audio_buf,
            # No language= set → Whisper auto-detects (handles Hindi/English/mixed)
        )
        transcript_text = transcription.text.strip()
    except Exception as e:
        log.exception("whisper failed")
        await update.message.reply_text(f"⚠️ Transcription failed: {e}")
        return

    if not transcript_text:
        await update.message.reply_text(
            "Couldn't make out what you said. Try again with clearer audio?"
        )
        return

    # Show transcript before processing — transparency for the user
    await update.message.reply_text(f"🎙️ Heard: {transcript_text}")

    # Flag the session so subsequent short agent replies come back as voice
    state = load_state(user_id)
    state["last_input_voice"] = True
    save_state(user_id)

    # Route through the same logic as a typed text message, but with voice_mode
    # so the system prompt enforces brevity + Roman script for TTS quality.
    await _process_text_input(update, context, user_id, transcript_text, voice_mode=True)


async def _process_text_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    text: str,
    voice_mode: bool = False,
):
    """The core text-routing logic, shared by text_handler and voice_handler.

    Routes to: pending pantry-staple reply → YouTube URL parser → supervisor
    classifier → normal agent. After the turn, auto-spawns order tracker if a
    placement tool was called.
    """
    state = load_state(user_id)

    # 1. Check for pending pantry-staple reply
    if state.get("pending") and state["pending"]["type"] == "pantry_staples":
        pending = state["pending"]
        filtered = filter_ingredients_by_pantry_reply(
            pending["ingredients"], pending["staples"], text
        )
        state["pending"] = None
        save_state(user_id)

        if not filtered:
            await update.message.reply_text("Nothing left to shop. Done!")
            return

        await update.message.reply_text(
            f"Shopping {len(filtered)} items. Starting now..."
        )
        await typing(update, context)

        agent_input = format_ingredient_message(filtered)
        state["mode"] = "instamart"
        state["history"] = []
        save_state(user_id)

        reply, tools_called = await run_agent_turn(
            user_id, agent_input, voice_mode=voice_mode
        )
        await send_smart(update, context, user_id, reply)
        await maybe_start_tracking(user_id, tools_called, state["mode"])
        return

    # 2. Check for YouTube URL
    yt_url = detect_youtube_url(text)
    if yt_url:
        await update.message.reply_text(
            "Found a YouTube URL — fetching transcript & extracting ingredients..."
        )
        await typing(update, context)
        try:
            ingredients = parse_youtube(yt_url)
        except Exception as e:
            await update.message.reply_text(f"Couldn't parse video: {e}")
            return

        await handle_parsed_ingredients(update, context, user_id, ingredients)
        return

    # 3. SUPERVISOR — classify intent, switch mode silently if needed
    current_mode = state["mode"]
    target_mode = await classify_intent(text, current_mode, state["history"])
    if target_mode != current_mode:
        log.info(f"[user {user_id}] supervisor switching {current_mode} → {target_mode}")
        state["mode"] = target_mode
        state["history"] = []  # fresh history for new server context
        state["pending"] = None
        save_state(user_id)
        await update.message.reply_text(
            f"Switching to {target_mode} mode."
        )

    # 4. Normal text → current agent
    await typing(update, context)
    try:
        reply, tools_called = await run_agent_turn(
            user_id, text, voice_mode=voice_mode
        )
    except FileNotFoundError as e:
        await update.message.reply_text(f"⚠️ {e}")
        return
    except Exception as e:
        log.exception("agent error")
        await update.message.reply_text(f"⚠️ Agent error: {type(e).__name__}: {e}")
        return

    await send_smart(update, context, user_id, reply)
    await maybe_start_tracking(user_id, tools_called, state["mode"])


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_allowed(user_id):
        return

    # Image input → text replies
    state = load_state(user_id)
    state["last_input_voice"] = False
    save_state(user_id)

    await update.message.reply_text("Got the image. Extracting ingredients...")
    await typing(update, context)

    # Telegram sends multiple sizes; get the largest
    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    image_bytes = bytes(await photo_file.download_as_bytearray())

    try:
        ingredients = parse_image_bytes(image_bytes, mime_type="image/jpeg")
    except Exception as e:
        await update.message.reply_text(f"Couldn't parse image: {e}")
        return

    await handle_parsed_ingredients(update, context, user_id, ingredients)


async def handle_parsed_ingredients(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    ingredients: list,
):
    """After we have a parsed ingredient list, ask about pantry staples
    (or skip the question if there are none)."""
    state = load_state(user_id)

    summary = (
        f"Found {len(ingredients)} ingredients:\n\n"
        + summarize_ingredients(ingredients)
    )
    await send_long(update, summary)

    staples = [ing for ing in ingredients if ing.get("pantry_staple")]
    if not staples:
        await update.message.reply_text("No pantry staples. Starting to shop...")
        await typing(update, context)
        agent_input = format_ingredient_message(ingredients)
        state["mode"] = "instamart"
        state["history"] = []
        save_state(user_id)
        reply, tools_called = await run_agent_turn(user_id, agent_input)
        await send_smart(update, context, user_id, reply)
        await maybe_start_tracking(user_id, tools_called, "instamart")
        return

    # Save pending state, ask the question
    state["pending"] = {
        "type": "pantry_staples",
        "ingredients": ingredients,
        "staples": staples,
    }
    save_state(user_id)

    await update.message.reply_text(format_pantry_question(ingredients, staples))


# ===== Bot bootstrap =============================================

def main():
    if not ALLOWED_USERS:
        log.warning("⚠️  TELEGRAM_ALLOWED_USERS not set — bot will reply to ANYONE.")
        log.warning("    For safety, set TELEGRAM_ALLOWED_USERS=<your_id> and restart.")
    else:
        log.info(f"Allowlist: {ALLOWED_USERS}")

    # Sanity: token.json must exist
    try:
        load_swiggy_token()
    except FileNotFoundError:
        log.error("token.json missing — run `python login.py` first.")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()

    # Expose bot to background tasks (order tracker, future async pushers)
    global BOT_INSTANCE
    BOT_INSTANCE = app.bot

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("whoami", whoami_cmd))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.VOICE, voice_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    log.info("Bot starting — polling for messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
# """
# parse_image.py — extract ingredients from a recipe image.

# Usage:
#   python parse_image.py path/to/recipe.jpg
#   python parse_image.py path/to/insta_screenshot.png

# Output: JSON array of ingredients (printed to stdout).

# This is a standalone parser. It doesn't touch Swiggy. Use it to validate
# that Claude's vision handles your recipe formats before wiring it to the
# shopping agent.
# """

# import base64
# import json
# import mimetypes
# import sys
# from pathlib import Path

# from anthropic import Anthropic

# from recipe_prompt import EXTRACTION_PROMPT, extract_json_array, summarize_ingredients


# MODEL = "claude-haiku-4-5"
# MAX_TOKENS = 4096


# def encode_image_to_base64(image_path: Path) -> tuple[str, str]:
#     """Returns (base64_data, mime_type)."""
#     mime, _ = mimetypes.guess_type(str(image_path))
#     if mime is None:
#         # Default to jpeg if we can't tell
#         mime = "image/jpeg"
#     if mime not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
#         raise ValueError(
#             f"Unsupported image type: {mime}. "
#             "Use JPEG, PNG, GIF, or WebP."
#         )
#     with open(image_path, "rb") as f:
#         data = base64.standard_b64encode(f.read()).decode("utf-8")
#     return data, mime


# def parse_image(image_path: Path) -> list:
#     if not image_path.exists():
#         raise FileNotFoundError(f"Image not found: {image_path}")

#     image_data, mime_type = encode_image_to_base64(image_path)
#     client = Anthropic()

#     print(f"→ Sending image ({mime_type}, {image_path.stat().st_size // 1024} KB) "
#           f"to Claude vision...", file=sys.stderr)

#     response = client.messages.create(
#         model=MODEL,
#         max_tokens=MAX_TOKENS,
#         messages=[
#             {
#                 "role": "user",
#                 "content": [
#                     {
#                         "type": "image",
#                         "source": {
#                             "type": "base64",
#                             "media_type": mime_type,
#                             "data": image_data,
#                         },
#                     },
#                     {"type": "text", "text": EXTRACTION_PROMPT},
#                 ],
#             }
#         ],
#     )

#     text = response.content[0].text
#     try:
#         ingredients = extract_json_array(text)
#     except json.JSONDecodeError as e:
#         print(f"✗ Couldn't parse JSON from response:\n{text}", file=sys.stderr)
#         raise

#     return ingredients


# def main():
#     if len(sys.argv) != 2:
#         print("Usage: python parse_image.py path/to/image.jpg", file=sys.stderr)
#         sys.exit(1)

#     image_path = Path(sys.argv[1])
#     ingredients = parse_image(image_path)

#     print("\nFound ingredients:", file=sys.stderr)
#     print(summarize_ingredients(ingredients), file=sys.stderr)
#     print("\nJSON output:", file=sys.stderr)
#     print(json.dumps(ingredients, indent=2, ensure_ascii=False))


# if __name__ == "__main__":
#     main()
















"""
parse_image.py — extract ingredients from a recipe image.

Usage:
  python parse_image.py path/to/recipe.jpg
  python parse_image.py path/to/insta_screenshot.png

Output: JSON array of ingredients (printed to stdout).

This is a standalone parser. It doesn't touch Swiggy. Use it to validate
that Claude's vision handles your recipe formats before wiring it to the
shopping agent.
"""

import base64
import json
import mimetypes
import sys
from pathlib import Path

from anthropic import Anthropic

from recipe_prompt import EXTRACTION_PROMPT, extract_json_array, summarize_ingredients


MODEL = "claude-haiku-4-5"
MAX_TOKENS = 4096


def encode_image_to_base64(image_path: Path) -> tuple[str, str]:
    """Returns (base64_data, mime_type)."""
    mime, _ = mimetypes.guess_type(str(image_path))
    if mime is None:
        # Default to jpeg if we can't tell
        mime = "image/jpeg"
    if mime not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        raise ValueError(
            f"Unsupported image type: {mime}. "
            "Use JPEG, PNG, GIF, or WebP."
        )
    with open(image_path, "rb") as f:
        data = base64.standard_b64encode(f.read()).decode("utf-8")
    return data, mime


def parse_image(image_path: Path) -> list:
    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    image_data, mime_type = encode_image_to_base64(image_path)
    return _call_claude_vision(image_data, mime_type, source=str(image_path))


def parse_image_bytes(image_bytes: bytes, mime_type: str = "image/jpeg") -> list:
    """Parse a recipe image from raw bytes (e.g., downloaded from Telegram)."""
    if mime_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
        mime_type = "image/jpeg"  # safe default for unknown sources
    data = base64.standard_b64encode(image_bytes).decode("utf-8")
    return _call_claude_vision(data, mime_type, source=f"bytes ({len(image_bytes)} B)")


def _call_claude_vision(image_data: str, mime_type: str, source: str) -> list:
    """Shared Claude vision call. Returns parsed ingredient list."""
    client = Anthropic()

    print(f"→ Sending image ({mime_type}, source={source}) to Claude vision...",
          file=sys.stderr)

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": image_data,
                        },
                    },
                    {"type": "text", "text": EXTRACTION_PROMPT},
                ],
            }
        ],
    )

    text = response.content[0].text
    try:
        return extract_json_array(text)
    except json.JSONDecodeError:
        print(f"✗ Couldn't parse JSON from response:\n{text}", file=sys.stderr)
        raise


def main():
    if len(sys.argv) != 2:
        print("Usage: python parse_image.py path/to/image.jpg", file=sys.stderr)
        sys.exit(1)

    image_path = Path(sys.argv[1])
    ingredients = parse_image(image_path)

    print("\nFound ingredients:", file=sys.stderr)
    print(summarize_ingredients(ingredients), file=sys.stderr)
    print("\nJSON output:", file=sys.stderr)
    print(json.dumps(ingredients, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
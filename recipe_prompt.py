"""
recipe_prompt.py — shared extraction prompt + JSON parsing helper.

Used by parse_image.py and parse_youtube.py. Keeps the extraction
schema consistent across input sources.
"""

import json
import re


# Pantry staples — flagged so the agent can ask the user upfront whether to skip
PANTRY_STAPLES = {
    "salt", "water", "sugar", "cooking oil", "vegetable oil", "sunflower oil",
    "mustard oil", "ghee", "turmeric", "red chili powder", "chili powder",
    "garam masala", "cumin powder", "coriander powder", "mustard seeds",
    "cumin seeds", "hing", "asafoetida", "black pepper", "pepper",
}


EXTRACTION_PROMPT = """You are extracting ingredients from a recipe for an automated grocery shopping agent.

Return ONLY a valid JSON array. No prose, no explanation, no markdown code fences. Just the JSON array.

Schema for each ingredient:
{
  "name": "lowercase ingredient name, no brand names",
  "quantity": <number, or null if not specified>,
  "unit": "g" | "kg" | "ml" | "l" | "pieces" | "tbsp" | "tsp" | "cup" | "pinch" | null,
  "essential": <boolean: true if recipe fails without it, false if optional/garnish>,
  "pantry_staple": <boolean: true if this is a common household staple (salt, oil, basic spices)>,
  "notes": "any clarifying info, e.g. 'finely chopped', 'fresh', 'frozen'"
}

Rules:
- Convert quantities to metric where possible
- "2 tomatoes" → quantity: 2, unit: "pieces"
- "a pinch of salt" → quantity: 1, unit: "pinch", pantry_staple: true
- "200g paneer" → quantity: 200, unit: "g"
- For vague quantities ("some coriander", "a handful"), use quantity: null
- Strip brand names — the shopping agent will pick the brand at search time
- Flag these as pantry_staple=true: salt, sugar, water, cooking oil (any kind),
  turmeric, red chili powder, garam masala, cumin powder, coriander powder,
  mustard seeds, cumin seeds, hing/asafoetida, black pepper, basic dal
- Output ONLY the JSON array. Nothing else.

Example output:
[
  {"name": "paneer", "quantity": 200, "unit": "g", "essential": true, "pantry_staple": false, "notes": "cubed"},
  {"name": "tomatoes", "quantity": 4, "unit": "pieces", "essential": true, "pantry_staple": false, "notes": "ripe"},
  {"name": "salt", "quantity": null, "unit": null, "essential": true, "pantry_staple": true, "notes": "to taste"}
]
"""


YOUTUBE_TRANSCRIPT_SUFFIX = """

This is a transcript from a cooking video. The presenter may:
- Mention the same ingredient multiple times — deduplicate
- Use vague quantities ("a handful", "some") — use null
- Skip mentioning quantities entirely for some items — use null
- Talk about technique/steps mixed with ingredients — extract only ingredients
- Mix Hindi and English — interpret both

Extract the FINAL unique ingredient list with the most specific quantity mentioned for each.

Transcript:
---
{transcript}
---
"""


def extract_json_array(text: str):
    """Pull a JSON array from Claude's response, defensively stripping any
    code fences or stray text."""
    text = text.strip()

    # Strip leading/trailing code fences
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    text = text.strip()

    # If there's still extra prose, try to find the first [ ... ]
    if not text.startswith("["):
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if match:
            text = match.group(0)

    return json.loads(text)


def summarize_ingredients(ingredients: list) -> str:
    """Human-friendly summary for printing or feeding to the agent."""
    if not ingredients:
        return "No ingredients found."

    lines = []
    for i, ing in enumerate(ingredients, 1):
        qty = ing.get("quantity")
        unit = ing.get("unit") or ""
        name = ing["name"]
        flags = []
        if ing.get("pantry_staple"):
            flags.append("pantry")
        if not ing.get("essential", True):
            flags.append("optional")
        flag_str = f" [{', '.join(flags)}]" if flags else ""

        if qty is not None:
            lines.append(f"  {i:2}. {qty} {unit} {name}{flag_str}".strip())
        else:
            lines.append(f"  {i:2}. {name} (qty unspecified){flag_str}")
    return "\n".join(lines)
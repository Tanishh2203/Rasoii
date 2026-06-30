"""
parse_youtube.py — extract ingredients from a cooking YouTube video.

Usage:
  python parse_youtube.py "https://www.youtube.com/watch?v=VIDEO_ID"
  python parse_youtube.py "https://youtu.be/VIDEO_ID"

Output: JSON array of ingredients (printed to stdout).

This is harder than image parsing because:
  - Transcripts contain lots of filler ("um", "so", "let's", "as you can see")
  - Quantities are often vague or repeated
  - Some videos have no transcript available
  - Hindi/English mixed content

The parser tries Hindi and English transcripts. If neither exists, it fails
loudly — there's no fallback to actually watching the video frames in v1.
"""

import json
import re
import sys

from anthropic import Anthropic
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
)

from recipe_prompt import (
    EXTRACTION_PROMPT,
    YOUTUBE_TRANSCRIPT_SUFFIX,
    extract_json_array,
    summarize_ingredients,
)


MODEL = "claude-haiku-4-5"
MAX_TOKENS = 4096
TRANSCRIPT_MAX_CHARS = 30000  # Truncate very long videos


def extract_video_id(url: str) -> str:
    """Pull the 11-char YouTube video ID out of various URL formats."""
    patterns = [
        r"(?:v=)([0-9A-Za-z_-]{11})",          # youtube.com/watch?v=ID
        r"(?:youtu\.be/)([0-9A-Za-z_-]{11})",   # youtu.be/ID
        r"(?:embed/)([0-9A-Za-z_-]{11})",       # youtube.com/embed/ID
        r"(?:shorts/)([0-9A-Za-z_-]{11})",      # youtube.com/shorts/ID
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    # If the URL is already just an ID
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", url):
        return url

    raise ValueError(f"Couldn't extract YouTube video ID from: {url}")


def fetch_transcript(video_id: str) -> str:
    """Fetch transcript. Prefers Hindi → English → any. Returns joined text.

    Compatible with youtube-transcript-api v1.x (instance-based API).
    """
    print(f"→ Fetching transcript for video {video_id}...", file=sys.stderr)

    api = YouTubeTranscriptApi()

    try:
        transcript_list = api.list(video_id)
    except TranscriptsDisabled:
        raise RuntimeError(
            f"Transcripts are disabled for this video ({video_id}). "
            "Try a different video that has captions enabled."
        )

    # Try manually-uploaded transcripts first (higher quality), then auto-generated.
    # Priority: Hindi manual → English manual → Hindi auto → English auto → any
    selected = None
    for is_generated in (False, True):
        for lang in ("hi", "en"):
            for transcript in transcript_list:
                if (transcript.language_code == lang and
                        transcript.is_generated == is_generated):
                    selected = transcript
                    break
            if selected:
                break
        if selected:
            break

    # Last resort: pick whatever is available
    if selected is None:
        for transcript in transcript_list:
            selected = transcript
            break

    if selected is None:
        raise RuntimeError(
            f"No transcript found for video {video_id}. "
            "Try a different video with captions enabled."
        )

    print(
        f"  using {selected.language} "
        f"({'auto-generated' if selected.is_generated else 'manual'})",
        file=sys.stderr,
    )

    fetched = selected.fetch()
    # v1.x returns FetchedTranscriptSnippet objects — use .text attribute
    text = " ".join(snippet.text for snippet in fetched)

    if len(text) > TRANSCRIPT_MAX_CHARS:
        print(
            f"  ⚠ transcript is {len(text)} chars, "
            f"truncating to {TRANSCRIPT_MAX_CHARS}",
            file=sys.stderr,
        )
        text = text[:TRANSCRIPT_MAX_CHARS]

    return text


def parse_youtube(url: str, debug: bool = False) -> list:
    video_id = extract_video_id(url)
    transcript = fetch_transcript(video_id)

    client = Anthropic()
    # Build prompt via concatenation — NOT .format() — because EXTRACTION_PROMPT
    # contains JSON examples with { } that would break str.format()
    prompt = EXTRACTION_PROMPT + YOUTUBE_TRANSCRIPT_SUFFIX.replace("{transcript}", transcript)

    print(f"→ Extracting ingredients from {len(transcript)} chars of transcript...",
          file=sys.stderr)

    if debug:
        print("\n--- RAW TRANSCRIPT (first 1000 chars) ---", file=sys.stderr)
        print(transcript[:1000], file=sys.stderr)
        print("--- END TRANSCRIPT ---\n", file=sys.stderr)

    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text

    if debug:
        print("\n--- CLAUDE RAW RESPONSE ---", file=sys.stderr)
        print(text, file=sys.stderr)
        print("--- END RESPONSE ---\n", file=sys.stderr)

    try:
        ingredients = extract_json_array(text)
    except json.JSONDecodeError:
        print(f"✗ Couldn't parse JSON from response:\n{text}", file=sys.stderr)
        raise

    return ingredients


def main():
    if len(sys.argv) < 2:
        print("Usage: python parse_youtube.py <youtube_url> [--debug]", file=sys.stderr)
        sys.exit(1)

    url = sys.argv[1]
    debug = "--debug" in sys.argv
    ingredients = parse_youtube(url, debug=debug)

    print("\nFound ingredients:", file=sys.stderr)
    print(summarize_ingredients(ingredients), file=sys.stderr)
    print("\nJSON output:", file=sys.stderr)
    print(json.dumps(ingredients, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
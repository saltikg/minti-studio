"""Editable prompt fragments for admin AI cover generation."""

BASE_PROMPT = """Edit this photo into a YouTube Shorts cover. Do NOT redraw, replace, or alter the people: keep every face, expression, hair, skin, glasses, clothing and pose exactly as in the photo. You may only: add the title text, adjust lighting/color grading, and restyle or extend the background behind the people. Keep the title in the upper third. Keep the bottom 20% and the right edge free of text and faces.
{people_instruction}
{title_instruction}
{look_instruction}
All visible title text must be spelled exactly as supplied. Turkish characters must render correctly, including ı, ş, ğ, ç, ö, ü, and İ. Produce a single finished vertical 9:16 cover at 1080x1920, not a collage or mockup."""

FACE_IDENTITY_COSINE_THRESHOLD = 0.40
FACE_IDENTITY_MAX_ATTEMPTS = 3

COVER_LOOKS = {
    "yellow_title": {
        "label": "Yellow Title",
        "hint": "bold yellow type, dark contrast",
        "prompt": "Use a big condensed bold yellow title with strong dark contrast and an energetic, high-impact mood, readable at thumbnail size.",
    },
    "white_box": {
        "label": "White Box",
        "hint": "simple, bright, clean",
        "prompt": "Use dark bold title text in a clean white box with a simple, bright mood; never cover a face.",
    },
    "minimal": {
        "label": "Minimal",
        "hint": "calm, uncluttered, professional",
        "prompt": "Use a thin clean white title with a calm, uncluttered, professional mood and generous spacing.",
    },
    "dramatic": {
        "label": "Dramatic",
        "hint": "cinematic, dark, intense",
        "prompt": "Use cinematic dark lighting, deep contrast, and a bold white title with a subtle red accent.",
    },
    "curiosity": {
        "label": "Curiosity",
        "hint": "intriguing, expressive, question-like",
        "prompt": "Create an intriguing, question-like mood with an expressive face and a bold yellow-and-white title, avoiding clickbait distortions.",
    },
    "no_text": {
        "label": "No Text",
        "hint": "people only, no lettering",
        "prompt": "Do not render any text, letters, captions, logos, or symbols; show only the selected person or people.",
    },
}


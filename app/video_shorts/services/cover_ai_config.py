"""Editable prompt fragments for admin AI cover generation."""

BASE_PROMPT = """Create one polished vertical 9:16 YouTube Shorts cover at 1080x1920.
Use the supplied source-video reference image(s) as identity references. Preserve each selected person's real identity, facial features, skin tone, age, hair, glasses, and clothing cues. Keep the full head visible and leave safe margins around faces. Do not add extra people, logos, watermarks, or unrelated objects.
{people_instruction}
{title_instruction}
{style_instruction}
{tone_instruction}
All visible title text must be spelled exactly as supplied. Turkish characters must render correctly, including ı, ş, ğ, ç, ö, ü, and İ. Produce a single finished cover image, not a collage or mockup."""

TITLE_STYLES = {
    "big_bold_yellow": {
        "label": "Big bold yellow",
        "prompt": "Use large condensed bold yellow title lettering with strong dark contrast, readable at thumbnail size.",
    },
    "white_box": {
        "label": "White box",
        "prompt": "Place the title in a clean white rectangular box with dark bold lettering; never cover a face.",
    },
    "minimal_clean": {
        "label": "Minimal clean",
        "prompt": "Use restrained modern typography, generous spacing, and a minimal clean editorial layout.",
    },
    "no_text": {
        "label": "No text",
        "prompt": "Do not render any text, letters, captions, logos, or symbols.",
    },
}

TONES = {
    "dramatic": {
        "label": "Dramatic",
        "prompt": "Use dramatic cinematic lighting, deep contrast, and strong visual tension without changing identity.",
    },
    "clean_professional": {
        "label": "Clean/professional",
        "prompt": "Use polished professional lighting, balanced composition, and credible editorial restraint.",
    },
    "interview_podcast": {
        "label": "Interview/podcast",
        "prompt": "Use an premium interview or podcast thumbnail composition with natural expressions and conversational energy.",
    },
    "curiosity": {
        "label": "Curiosity",
        "prompt": "Create a curiosity-driven composition with an intriguing expression and visual question, avoiding clickbait distortions.",
    },
}


import scripts.blog_pipeline as blog_pipeline
from scripts.blog_pipeline import CTA_URL, _normalize_article_payload, _revision_rejection_reason


def _base_article(content_md: str, visuals=None):
    return {
        "title": "Placeholder Test",
        "slug": "placeholder-test",
        "summary": "A short summary.",
        "content_md": content_md,
        "visuals": visuals if visuals is not None else [],
    }


def test_normalizes_bare_and_wrong_placeholder_syntax():
    article = _normalize_article_payload(
        _base_article(
            "\n\n".join(
                [
                    "Intro paragraph.",
                    "## First",
                    "Use IMAGE_1 here.",
                    "## Second",
                    "Use [IMAGE_2] here.",
                    "## Third",
                    "Use {{ IMAGE_3 }} here.",
                ]
            )
        )
    )

    assert article["content_md"].count("<!-- IMAGE_1 -->") == 1
    assert article["content_md"].count("<!-- IMAGE_2 -->") == 1
    assert article["content_md"].count("<!-- IMAGE_3 -->") == 1
    assert "IMAGE_1 here" not in article["content_md"]
    assert "[IMAGE_2]" not in article["content_md"]
    assert "{{ IMAGE_3 }}" not in article["content_md"]


def test_removes_duplicate_placeholders():
    article = _normalize_article_payload(
        _base_article(
            "\n\n".join(
                [
                    "Intro paragraph.",
                    "## First",
                    "<!--IMAGE_1-->",
                    "More copy.",
                    "<!-- IMAGE_1 -->",
                    "## Second",
                    "IMAGE_2",
                    "## Third",
                    "IMAGE_3",
                ]
            )
        )
    )

    assert article["content_md"].count("<!-- IMAGE_1 -->") == 1
    assert article["content_md"].count("<!-- IMAGE_2 -->") == 1
    assert article["content_md"].count("<!-- IMAGE_3 -->") == 1


def test_inserts_missing_placeholders_after_section_paragraphs():
    article = _normalize_article_payload(
        _base_article(
            "\n\n".join(
                [
                    "# Title",
                    "Intro paragraph.",
                    "## Plan the workflow",
                    "This paragraph explains the first workflow section.",
                    "- A list item should not receive an inserted marker.",
                    "## Batch the clips",
                    "This paragraph explains the second workflow section.",
                    "## Final CTA",
                    "Start creating clips today.",
                ]
            )
        )
    )

    content = article["content_md"]
    assert content.count("<!-- IMAGE_1 -->") == 1
    assert content.count("<!-- IMAGE_2 -->") == 1
    assert content.count("<!-- IMAGE_3 -->") == 1
    assert content.index("This paragraph explains the first workflow section.") < content.index("<!-- IMAGE_1 -->")
    assert content.index("This paragraph explains the second workflow section.") < content.index("<!-- IMAGE_2 -->")
    assert content.index("<!-- IMAGE_3 -->") < content.index("## Final CTA")


def test_visuals_are_filled_and_screenshot_count_is_capped(monkeypatch):
    monkeypatch.setattr(
        blog_pipeline,
        "_load_manifest",
        lambda: [
            {"id": "a", "alt": "A", "use_for": ["one"]},
            {"id": "b", "alt": "B", "use_for": ["two"]},
            {"id": "c", "alt": "C", "use_for": ["three"]},
        ],
    )
    article = _normalize_article_payload(
        _base_article(
            "\n\n".join(
                [
                    "## One",
                    "First context.",
                    "<!-- IMAGE_1 -->",
                    "## Two",
                    "Second context.",
                    "<!-- IMAGE_2 -->",
                    "## Three",
                    "Third context.",
                    "<!-- IMAGE_3 -->",
                ]
            ),
            visuals=[
                {"marker": "IMAGE_1", "type": "screenshot", "screenshot_id": "a"},
                {"marker": "IMAGE_2", "type": "screenshot", "screenshot_id": "b"},
                {"marker": "IMAGE_3", "type": "screenshot", "screenshot_id": "c"},
            ],
        )
    )

    assert [visual["marker"] for visual in article["visuals"]] == ["IMAGE_1", "IMAGE_2", "IMAGE_3"]
    assert sum(1 for visual in article["visuals"] if visual["type"] == "screenshot") == 2
    assert article["visuals"][2]["type"] == "generate"
    assert article["visuals"][2]["prompt"]


def test_empty_screenshot_id_repairs_to_relevant_available_screenshot(monkeypatch):
    monkeypatch.setattr(
        blog_pipeline,
        "_load_manifest",
        lambda: [
            {"id": "workflow-shot", "alt": "Workflow screenshot", "use_for": ["workflow", "shorts later"]},
            {"id": "analytics-shot", "alt": "Analytics screenshot", "use_for": ["analytics"]},
        ],
    )
    article = _normalize_article_payload(
        _base_article(
            "\n\n".join(
                [
                    "## How this becomes Shorts later",
                    "This section explains the workflow.",
                    "<!-- IMAGE_1 -->",
                    "## Second",
                    "More body.",
                    "<!-- IMAGE_2 -->",
                    "## Third",
                    "More body.",
                    "<!-- IMAGE_3 -->",
                ]
            ),
            visuals=[{"marker": "IMAGE_1", "type": "screenshot", "screenshot_id": ""}],
        )
    )

    assert article["visuals"][0]["type"] == "screenshot"
    assert article["visuals"][0]["screenshot_id"] == "workflow-shot"
    assert "IMAGE_1: repaired screenshot slot" in article["visual_repairs"][0]


def test_held_or_unknown_screenshot_id_repairs_to_generate(monkeypatch):
    monkeypatch.setattr(blog_pipeline, "_load_manifest", lambda: [])
    article = _normalize_article_payload(
        _base_article(
            "\n\n".join(
                [
                    "## Objection planning",
                    "Use buyer questions as the source.",
                    "<!-- IMAGE_1 -->",
                    "## Second",
                    "More body.",
                    "<!-- IMAGE_2 -->",
                    "## Third",
                    "More body.",
                    "<!-- IMAGE_3 -->",
                ]
            ),
            visuals=[{"marker": "IMAGE_1", "type": "screenshot", "screenshot_id": "held-shot"}],
        )
    )

    assert article["visuals"][0]["type"] == "generate"
    assert "Objection planning" in article["visuals"][0]["prompt"]
    assert "invalid id 'held-shot' to generate" in article["visual_repairs"][0]


def test_mislabeled_flow_and_compare_screenshot_slots_are_retyped(monkeypatch):
    monkeypatch.setattr(blog_pipeline, "_load_manifest", lambda: [])
    article = _normalize_article_payload(
        _base_article(
            "\n\n".join(
                [
                    "## Flow",
                    "A workflow section.",
                    ":::flow Workflow caption",
                    "video | Long video | Full recording",
                    "clips | Five Shorts | Best moments",
                    ":::",
                    "## Compare",
                    "The article mentions 100K views and +120 subscribers.",
                    ":::compare Metric caption",
                    "eye | 100K | Views on Shorts | up",
                    "users | +120 | New subscribers | flat",
                    "note: Views are useful.",
                    ":::",
                    "## Third",
                    "More body.",
                    "<!-- IMAGE_3 -->",
                ]
            ),
            visuals=[
                {"marker": "IMAGE_1", "type": "screenshot", "screenshot_id": ""},
                {"marker": "IMAGE_2", "type": "screenshot", "screenshot_id": ""},
                {"marker": "IMAGE_3", "type": "generate", "prompt": "A text-free support visual"},
            ],
        )
    )

    assert article["visuals"][0]["type"] == "flow"
    assert article["visuals"][1]["type"] == "compare"
    assert "<!-- IMAGE_1 -->" not in article["content_md"]
    assert "<!-- IMAGE_2 -->" not in article["content_md"]
    assert any("to flow" in note for note in article["visual_repairs"])
    assert any("to compare" in note for note in article["visual_repairs"])


def test_screenshot_cap_overflow_repairs_to_generate(monkeypatch):
    monkeypatch.setattr(
        blog_pipeline,
        "_load_manifest",
        lambda: [
            {"id": "a", "alt": "A", "use_for": ["one"]},
            {"id": "b", "alt": "B", "use_for": ["two"]},
            {"id": "c", "alt": "C", "use_for": ["three"]},
        ],
    )
    article = _normalize_article_payload(
        _base_article(
            "\n\n".join(
                [
                    "## One",
                    "First context.",
                    "<!-- IMAGE_1 -->",
                    "## Two",
                    "Second context.",
                    "<!-- IMAGE_2 -->",
                    "## Three",
                    "Third context.",
                    "<!-- IMAGE_3 -->",
                ]
            ),
            visuals=[
                {"marker": "IMAGE_1", "type": "screenshot", "screenshot_id": "a"},
                {"marker": "IMAGE_2", "type": "screenshot", "screenshot_id": "b"},
                {"marker": "IMAGE_3", "type": "screenshot", "screenshot_id": "c"},
            ],
        )
    )

    assert [visual["type"] for visual in article["visuals"]] == ["screenshot", "screenshot", "generate"]
    assert "IMAGE_3: repaired screenshot slot with invalid id 'c' to generate" in article["visual_repairs"]


def test_meta_fields_trim_at_word_boundary():
    article = _normalize_article_payload(
        {
            **_base_article("## One\n\nBody paragraph.\n\n<!-- IMAGE_1 -->\n\n<!-- IMAGE_2 -->\n\n<!-- IMAGE_3 -->"),
            "meta_title": "This is a deliberately long title that should be trimmed without chopping words in half",
            "meta_description": (
                "This is a deliberately long meta description for a MintiStudio blog article that should be "
                "trimmed cleanly at a word boundary instead of chopping the final word in half for display."
            ),
        }
    )

    assert len(article["meta_title"]) <= 60
    assert article["meta_title"].endswith("trimmed")
    assert len(article["meta_description"]) <= 155
    assert not article["meta_description"].endswith((" ", ",", ";", ":", "-"))
    assert "displa" not in article["meta_description"]


def test_revision_guard_rejects_article_body_loss():
    previous = _normalize_article_payload(
        _base_article(
            "\n\n".join(
                [
                    "## Plan",
                    f"Keep the CTA {CTA_URL} and enough words " * 80,
                    "<!-- IMAGE_1 -->",
                    "## Produce",
                    "This section survives in the real article body. " * 80,
                    "<!-- IMAGE_2 -->",
                    "## Review",
                    "This section also survives in the real article body. " * 80,
                    "<!-- IMAGE_3 -->",
                ]
            )
        )
    )
    candidate = _normalize_article_payload(
        _base_article("<!-- IMAGE_1 -->\n\n<!-- IMAGE_2 -->\n\n<!-- IMAGE_3 -->")
    )

    reason = _revision_rejection_reason(previous, candidate)

    assert reason
    assert "word count dropped" in reason


def test_revision_guard_rejects_missing_cta():
    previous = _normalize_article_payload(
        _base_article(
            "\n\n".join(
                [
                    "## Plan",
                    f"Read more at {CTA_URL}. " + "Useful planning copy. " * 80,
                    "<!-- IMAGE_1 -->",
                    "## Produce",
                    "Useful production copy. " * 80,
                    "<!-- IMAGE_2 -->",
                    "## Review",
                    "Useful review copy. " * 80,
                    "<!-- IMAGE_3 -->",
                ]
            )
        )
    )
    candidate = _normalize_article_payload(
        _base_article(
            "\n\n".join(
                [
                    "## Plan",
                    "Useful planning copy. " * 80,
                    "<!-- IMAGE_1 -->",
                    "## Produce",
                    "Useful production copy. " * 80,
                    "<!-- IMAGE_2 -->",
                    "## Review",
                    "Useful review copy. " * 80,
                    "<!-- IMAGE_3 -->",
                ]
            )
        )
    )

    assert _revision_rejection_reason(previous, candidate) == "CTA link disappeared"


def test_protected_blocks_round_trip():
    content = "\n\n".join(
        [
            "## Plan",
            "Keep the prose visible to the model.",
            ":::flow Workflow",
            "video | Long video | Full recording",
            "clips | Five Shorts | Best moments",
            ":::",
            "![Clips dashboard](/video_shorts/static/img/blog/library/transcript-clips.png \"Clips dashboard\")",
            ":::compare Metrics",
            "eye | 100K | Views on Shorts | up",
            "users | +120 | New subscribers | flat",
            "note: Views are useful.",
            ":::",
            "<!-- IMAGE_3 -->",
        ]
    )

    masked, blocks = blog_pipeline._mask_protected_blocks(content)
    restored, repairs = blog_pipeline._restore_masked_blocks(masked, masked, blocks)

    assert [block.token for block in blocks] == ["[[BLOCK_1]]", "[[BLOCK_2]]", "[[BLOCK_3]]", "[[BLOCK_4]]"]
    assert ":::flow" not in masked
    assert ":::compare" not in masked
    assert "<!-- IMAGE_3 -->" not in masked
    assert "![Clips dashboard]" not in masked
    assert restored == content
    assert repairs == []


def test_missing_protected_token_is_reinserted():
    content = "\n\n".join(
        [
            "## Plan",
            "Keep this paragraph.",
            ":::flow Workflow",
            "video | Long video | Full recording",
            "clips | Five Shorts | Best moments",
            ":::",
            "## Proof",
            "Keep the screenshot nearby.",
            "<!-- IMAGE_2 -->",
        ]
    )
    masked, blocks = blog_pipeline._mask_protected_blocks(content)
    candidate = masked.replace("[[BLOCK_1]]", "").replace("Keep this paragraph.", "Keep this paragraph. Add one fix.")

    restored, repairs = blog_pipeline._restore_masked_blocks(masked, candidate, blocks)

    assert ":::flow Workflow" in restored
    assert "<!-- IMAGE_2 -->" in restored
    assert "[[BLOCK_" not in restored
    assert any("[[BLOCK_1]]: reinserted missing protected token" == repair for repair in repairs)


def test_duplicate_protected_token_is_repaired_once():
    content = "## Plan\n\nFirst paragraph.\n\n<!-- IMAGE_1 -->"
    masked, blocks = blog_pipeline._mask_protected_blocks(content)
    candidate = masked + "\n\n[[BLOCK_1]]"

    restored, repairs = blog_pipeline._restore_masked_blocks(masked, candidate, blocks)

    assert restored.count("<!-- IMAGE_1 -->") == 1
    assert repairs == ["[[BLOCK_1]]: removed duplicate protected token"]


def test_designer_guard_ignores_protected_block_text():
    before = "\n\n".join(
        [
            "## Plan",
            f"Send readers to {CTA_URL}. The article sentence stays the same.",
            ":::flow Workflow",
            "video | Long video | Full recording",
            "clips | Five Shorts | Best moments",
            ":::",
            "<!-- IMAGE_2 -->",
        ]
    )
    after = before.replace(
        "video | Long video | Full recording\nclips | Five Shorts | Best moments",
        "calendar | Weekly calendar | Mon Wed Fri\ncheck | Done | Ready to publish",
    )

    ok, reason = blog_pipeline._guard_designer(before, after)

    assert ok
    assert reason == "similarity 1.000"


def test_revision_guard_ignores_protected_block_text():
    previous = _base_article(
        "\n\n".join(
            [
                "## Plan",
                f"Keep the CTA {CTA_URL}. " + "Stable prose only. " * 80,
                ":::compare Metrics",
                ("eye | noisy block words | not prose | up\n" * 120).strip(),
                "users | +120 | New subscribers | flat",
                ":::",
                "## Produce",
                "This section survives. " * 80,
            ]
        )
    )
    candidate = _base_article(
        "\n\n".join(
            [
                "## Plan",
                f"Keep the CTA {CTA_URL}. " + "Stable prose only. " * 80,
                ":::compare Metrics",
                "eye | compact | block | up",
                "users | +120 | New subscribers | flat",
                ":::",
                "## Produce",
                "This section survives. " * 80,
            ]
        )
    )

    assert _revision_rejection_reason(previous, candidate) is None

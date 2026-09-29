from scripts.blog_pipeline import _normalize_article_payload


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


def test_visuals_are_filled_and_screenshot_count_is_capped():
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

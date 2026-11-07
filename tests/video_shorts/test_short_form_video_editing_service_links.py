from __future__ import annotations

import pytest

from scripts.add_short_form_video_editing_service_links import (
    PATCHES,
    SERVICE_PATH,
    ArticlePatch,
    update_article_content,
)


@pytest.mark.parametrize("patch", PATCHES)
def test_article_link_patch_is_idempotent(patch: ArticlePatch) -> None:
    updated, changed = update_article_content(f"Before\n\n{patch.old}\n\nAfter", patch)

    assert changed is True
    assert SERVICE_PATH in updated
    assert patch.old not in updated

    unchanged, changed_again = update_article_content(updated, patch)
    assert unchanged == updated
    assert changed_again is False


def test_article_link_patch_fails_if_expected_copy_changed() -> None:
    with pytest.raises(ValueError, match="Expected insertion text"):
        update_article_content("Unrelated article copy", PATCHES[0])

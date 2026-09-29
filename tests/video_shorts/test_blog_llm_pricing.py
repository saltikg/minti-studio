from decimal import Decimal

from app.video_shorts.services.blog_llm import compute_image_cost


def test_gpt_image_2_uses_standard_output_rate():
    assert compute_image_cost("gpt-image-2", 0, 0, 1_000_000) == Decimal("30.00000")


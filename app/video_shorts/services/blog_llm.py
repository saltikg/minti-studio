from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.video_shorts.services.blog_pipeline import BLOG_MONTHLY_BUDGET_USD, current_month_spend
from app.video_shorts.services.db import get_db

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None


BLOG_MODEL_JUDGE = os.getenv("BLOG_MODEL_JUDGE", "gpt-5.4-nano")
BLOG_MODEL_WRITER = os.getenv("BLOG_MODEL_WRITER", "gpt-6-astra")
BLOG_MODEL_REVIEWER = os.getenv("BLOG_MODEL_REVIEWER", "gpt-5.4-mini")
BLOG_MODEL_DESIGNER = os.getenv("BLOG_MODEL_DESIGNER", "gpt-5.4-mini")
BLOG_IMAGE_COVER = os.getenv("BLOG_IMAGE_COVER", "gpt-image-2")
BLOG_IMAGE_COVER_QUALITY = os.getenv("BLOG_IMAGE_COVER_QUALITY", "medium")
BLOG_IMAGE_INLINE = os.getenv("BLOG_IMAGE_INLINE", "gpt-image-1-mini")
BLOG_IMAGE_INLINE_QUALITY = os.getenv("BLOG_IMAGE_INLINE_QUALITY", "medium")

PRICES: dict[str, dict[str, Decimal]] = {
    "gpt-5-nano": {"input": Decimal("0.05"), "cached_input": Decimal("0.005"), "output": Decimal("0.40")},
    "gpt-5.4-nano": {"input": Decimal("0.20"), "cached_input": Decimal("0.02"), "output": Decimal("1.25")},
    "gpt-5.4-mini": {"input": Decimal("0.75"), "cached_input": Decimal("0.075"), "output": Decimal("4.50")},
    "gpt-6-astra": {
        "input": Decimal("10.00"),
        "cached_input": Decimal("1.00"),
        "cache_write": Decimal("12.50"),
        "output": Decimal("50.00"),
        "batch_input": Decimal("5.00"),
        "batch_cached_input": Decimal("0.50"),
        "batch_cache_write": Decimal("6.25"),
        "batch_output": Decimal("25.00"),
    },
    "gpt-6-luna": {
        "input": Decimal("0.10"),
        "cached_input": Decimal("0.01"),
        "cache_write": Decimal("0.125"),
        "output": Decimal("0.50"),
        "batch_input": Decimal("0.05"),
        "batch_cached_input": Decimal("0.005"),
        "batch_cache_write": Decimal("0.0625"),
        "batch_output": Decimal("0.25"),
    },
    "gpt-image-2": {
        "text_input": Decimal("2.50"),
        "text_cached_input": Decimal("0.625"),
        "image_input": Decimal("4.00"),
        "image_cached_input": Decimal("1.00"),
        "image_output": Decimal("15.00"),
        "batch_text_input": Decimal("1.25"),
        "batch_text_cached_input": Decimal("0.3125"),
        "batch_image_input": Decimal("2.00"),
        "batch_image_cached_input": Decimal("0.50"),
        "batch_image_output": Decimal("7.50"),
    },
    "gpt-image-1-mini": {"text_input": Decimal("2.00"), "text_cached_input": Decimal("0.20"), "image_input": Decimal("2.50"), "image_cached_input": Decimal("0.25"), "image_output": Decimal("8.00"), "image_medium_1024": Decimal("0.011")},
}


@dataclass
class LLMResult:
    content: str
    provider: str
    model: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    cost_usd: Decimal


def provider_for_model(model: str) -> str:
    explicit = os.getenv("BLOG_PROVIDER") or ""
    if explicit.strip().lower() in {"anthropic", "gemini", "openai"}:
        return explicit.strip().lower()
    if model.startswith("claude"):
        return "anthropic"
    if model.startswith("gemini"):
        return "gemini"
    return "openai"


def _usage_value(usage: Any, *names: str) -> int:
    for name in names:
        value = getattr(usage, name, None)
        if value is not None:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _cached_tokens(usage: Any) -> int:
    details = getattr(usage, "prompt_tokens_details", None) or getattr(usage, "input_tokens_details", None)
    if not details:
        return 0
    return _usage_value(details, "cached_tokens", "cached_input_tokens")


def compute_text_cost(model: str, input_tokens: int, cached_input_tokens: int, output_tokens: int) -> Decimal:
    price = PRICES.get(model, PRICES["gpt-5-nano"])
    uncached = max(0, int(input_tokens or 0) - int(cached_input_tokens or 0))
    total = (
        Decimal(uncached) * price["input"]
        + Decimal(cached_input_tokens or 0) * price.get("cached_input", price["input"])
        + Decimal(output_tokens or 0) * price["output"]
    ) / Decimal(1_000_000)
    return total.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)


def log_usage(stage: str, result: LLMResult, *, topic_id: int | None = None, images: int = 0) -> None:
    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO blog_llm_usage (
                stage, topic_id, provider, model, input_tokens, cached_input_tokens,
                output_tokens, images, cost_usd
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                stage,
                topic_id,
                result.provider,
                result.model,
                result.input_tokens,
                result.cached_input_tokens,
                result.output_tokens,
                images,
                result.cost_usd,
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _check_budget() -> None:
    spent = current_month_spend()
    if spent >= BLOG_MONTHLY_BUDGET_USD:
        raise RuntimeError(f"BLOG_MONTHLY_BUDGET_USD reached: spent ${spent} of ${BLOG_MONTHLY_BUDGET_USD}")


def _reasoning_effort_for(stage: str, model: str) -> str | None:
    env_name = f"BLOG_REASONING_{stage.upper()}"
    configured = (os.getenv(env_name) or "").strip().lower()
    if configured:
        return configured
    if model.startswith("gpt-6"):
        return "medium"
    return None


def call_json(stage: str, *, model: str, system_prompt: str, user_prompt: str) -> LLMResult:
    _check_budget()
    provider = provider_for_model(model)
    if provider != "openai":
        raise RuntimeError(f"Provider {provider} is configured but only openai path is enabled in Phase 1")
    if OpenAI is None:
        raise RuntimeError("openai package is not installed")
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not configured")
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    last_error: Exception | None = None
    for attempt in range(2):
        request_kwargs: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt if attempt == 0 else user_prompt + "\n\nReturn valid JSON only."},
            ],
            "response_format": {"type": "json_object"},
        }
        reasoning_effort = _reasoning_effort_for(stage, model)
        if reasoning_effort:
            request_kwargs["reasoning_effort"] = reasoning_effort
        response = client.chat.completions.create(**request_kwargs)
        content = response.choices[0].message.content or ""
        try:
            json.loads(content)
        except Exception as exc:
            last_error = exc
            continue
        usage = getattr(response, "usage", None)
        input_tokens = _usage_value(usage, "prompt_tokens", "input_tokens")
        output_tokens = _usage_value(usage, "completion_tokens", "output_tokens")
        cached_tokens = _cached_tokens(usage)
        return LLMResult(
            content=content,
            provider="openai",
            model=model,
            input_tokens=input_tokens,
            cached_input_tokens=cached_tokens,
            output_tokens=output_tokens,
            cost_usd=compute_text_cost(model, input_tokens, cached_tokens, output_tokens),
        )
    raise RuntimeError(f"Invalid JSON from LLM after retry: {last_error}")

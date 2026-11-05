from __future__ import annotations

from contextlib import contextmanager, nullcontext
from typing import Any, Dict, Iterator, Optional

from flask import g


_MISSING = object()


@contextmanager
def clean_job_context(
    app,
    *,
    request_path: Optional[str] = None,
    request_method: str = "GET",
    request_data: Optional[Dict[str, Any]] = None,
    request_headers: Optional[Dict[str, str]] = None,
    g_values: Optional[Dict[str, Any]] = None,
) -> Iterator[None]:
    """Run one worker operation with a fresh ``g`` and optional request context."""
    with app.app_context():
        request_context = (
            app.test_request_context(
                request_path,
                method=request_method,
                data=request_data,
                headers=request_headers,
            )
            if request_path
            else nullcontext()
        )
        with request_context:
            previous = {
                key: getattr(g, key, _MISSING)
                for key in (g_values or {})
            }
            try:
                for key, value in (g_values or {}).items():
                    setattr(g, key, value)
                yield
            finally:
                for key, value in previous.items():
                    if value is _MISSING:
                        g.pop(key, None)
                    else:
                        setattr(g, key, value)

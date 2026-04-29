"""Live integration tests for `app.tools` — hit real Vertex AI.

Marked `@pytest.mark.live` so they're skipped by default. Run with
`pytest -m live` against a project with valid Vertex auth (the same
ADC the rest of adklaw uses).
"""

from __future__ import annotations

import pytest

from app.tools import web_search


@pytest.mark.live
@pytest.mark.asyncio
async def test_web_search_live_returns_real_answer() -> None:
    result = await web_search("What is the capital of Taiwan?")
    assert result["status"] == "success"
    answer = result["answer"]
    # Either English or Traditional Chinese form is acceptable —
    # the model picks based on grounding sources.
    assert any(
        token in answer for token in ("Taipei", "臺北", "台北")
    ), f"Unexpected answer: {answer!r}"
    assert any(
        s["url"].startswith("http") for s in result["sources"]
    ), f"No http(s) source URLs in {result['sources']}"

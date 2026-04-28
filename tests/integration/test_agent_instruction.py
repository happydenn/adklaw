"""Contract tests for `app.agent.BASE_INSTRUCTION` and `build_app`.

These are intentionally string-level: a refactor that drops or renames
load-bearing pieces (web_search policy, edit_file invariants) fails
immediately and loudly, regardless of the surrounding prose. They also
verify that the channel-extension seam (`extra_tools`,
`extra_instruction`) wires through to the agent the way callers expect.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from google.adk.agents.readonly_context import ReadonlyContext

from app.agent import BASE_INSTRUCTION, build_app

# ---------------------------------------------------------------------------
# BASE_INSTRUCTION contract
# ---------------------------------------------------------------------------


def test_base_instruction_documents_web_search() -> None:
    assert "web_search" in BASE_INSTRUCTION
    assert "ALWAYS" in BASE_INSTRUCTION


def test_base_instruction_documents_edit_file() -> None:
    assert "edit_file" in BASE_INSTRUCTION
    assert "read the file first" in BASE_INSTRUCTION
    assert "undo_last_edit" in BASE_INSTRUCTION


def test_base_instruction_does_not_document_envelopes() -> None:
    """Envelope semantics moved out of BASE_INSTRUCTION and into each
    channel's `extra_instruction` (e.g. `DISCORD_CHANNEL_INSTRUCTION`)
    so CLI runs that never see envelopes don't carry the explanation."""
    for marker in ("[origin]", "[reply_to]", "[context]"):
        assert marker not in BASE_INSTRUCTION, (
            f"{marker} should not appear in BASE_INSTRUCTION; "
            "it belongs in the channel's extra_instruction."
        )


# ---------------------------------------------------------------------------
# build_app — channel extension seam
# ---------------------------------------------------------------------------


def _read_instruction(app: Any, workspace_dir: Path) -> str:
    """Resolve the agent's dynamic instruction provider with a stub
    context, returning the rendered system instruction string."""
    provider = app.root_agent.instruction
    # ReadonlyContext requires an `InvocationContext`; we don't actually
    # use it because the provider only reads the workspace via env. So
    # MagicMock-style stub is fine for these tests.
    from unittest.mock import MagicMock

    return provider(MagicMock(spec=ReadonlyContext))


def test_build_app_appends_extra_instruction(workspace_dir: Path) -> None:
    app = build_app(extra_instruction="EXTRA-MARKER-FOO")
    rendered = _read_instruction(app, workspace_dir)
    assert "EXTRA-MARKER-FOO" in rendered
    assert BASE_INSTRUCTION.split("\n", 1)[0] in rendered  # base still present


def test_build_app_no_extra_instruction_keeps_base_only(
    workspace_dir: Path,
) -> None:
    app = build_app()
    rendered = _read_instruction(app, workspace_dir)
    assert "EXTRA-MARKER-FOO" not in rendered


def test_build_app_appends_extra_tools(workspace_dir: Path) -> None:
    def _fake_tool(x: str) -> dict:
        """A fake tool added via extras."""
        return {"status": "success", "echo": x}

    app = build_app(extra_tools=[_fake_tool])
    tools = app.root_agent.tools
    # Tools may be wrapped in FunctionTool by ADK; check by reference
    # to the underlying callable.
    assert any(
        getattr(t, "func", None) is _fake_tool or t is _fake_tool
        for t in tools
    ), f"Expected _fake_tool in agent tools, got {tools!r}"


def test_build_app_default_app_has_no_extra(workspace_dir: Path) -> None:
    """The module-level `app` (built via `build_app()` with no extras)
    matches what CLI / playground / Agent Runtime see."""
    from app.agent import app as default_app

    rendered = _read_instruction(default_app, workspace_dir)
    # Base sections present.
    assert "web_search" in rendered
    assert "edit_file" in rendered
    # No channel-specific extras.
    assert "Channel context" not in rendered


# ---------------------------------------------------------------------------
# DISCORD_CHANNEL_INSTRUCTION — regression guard
# ---------------------------------------------------------------------------


def test_discord_channel_instruction_documents_envelopes() -> None:
    """The format lives in `app/channels/base.py`; the explanation
    lives in `app/channels/discord.py`. A future block added to one
    file without updating the other should fail this test."""
    from app.channels.discord import DISCORD_CHANNEL_INSTRUCTION

    for marker in ("[origin]", "[reply_to]", "[context]"):
        assert marker in DISCORD_CHANNEL_INSTRUCTION, (
            f"{marker} should appear in DISCORD_CHANNEL_INSTRUCTION."
        )

"""Tests for the `Origin` / `ContextMessage` dataclasses and envelope
formatting helpers (`_format_origin`, `_format_context`, `_id_label`)."""

from __future__ import annotations

import dataclasses

import pytest

from app.channels.base import (
    ContextMessage,
    Origin,
    _format_context,
    _format_origin,
    _format_reply_to,
    _id_label,
)


def test_origin_is_frozen() -> None:
    o = Origin(transport="discord", sender_id="1", location_id="2")
    with pytest.raises(dataclasses.FrozenInstanceError):
        o.transport = "slack"  # type: ignore[misc]


def test_id_label_with_display() -> None:
    assert _id_label("papi", "1234") == "papi (id=1234)"


def test_id_label_without_display() -> None:
    assert _id_label(None, "1234") == "id=1234"


def test_format_origin_full() -> None:
    o = Origin(
        transport="discord",
        sender_id="111",
        location_id="222",
        sender_display="papi",
        location_display="DM",
    )
    assert _format_origin(o) == (
        "[origin]\n"
        "transport: discord\n"
        "sender: papi (id=111)\n"
        "location: DM (id=222)\n"
        "[/origin]\n\n"
    )


def test_format_origin_no_displays() -> None:
    o = Origin(transport="sms", sender_id="+15551", location_id="+15552")
    assert _format_origin(o) == (
        "[origin]\n"
        "transport: sms\n"
        "sender: id=+15551\n"
        "location: id=+15552\n"
        "[/origin]\n\n"
    )


def test_context_message_is_frozen() -> None:
    m = ContextMessage(sender_id="1", text="hi")
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.text = "bye"  # type: ignore[misc]


def test_format_context_empty_returns_empty_string() -> None:
    assert _format_context([]) == ""


def test_format_context_with_displays() -> None:
    msgs = [
        ContextMessage(sender_id="222", sender_display="alice", text="hi"),
        ContextMessage(sender_id="333", sender_display="bob", text="yo"),
    ]
    out = _format_context(msgs)
    assert out == (
        "[context] (recent messages, oldest first)\n"
        "alice (id=222): hi\n"
        "bob (id=333): yo\n"
        "[/context]\n\n"
    )


def test_format_context_without_displays() -> None:
    msgs = [ContextMessage(sender_id="222", text="hi")]
    out = _format_context(msgs)
    assert "id=222: hi" in out
    assert "(id=222)" not in out


def test_format_context_does_not_truncate_long_messages() -> None:
    """Discord caps user messages at 2000 chars; we don't add a second
    cap on top. Mirrors OpenClaw's count-only-cap policy."""
    long_text = "x" * 1800
    msgs = [ContextMessage(sender_id="1", text=long_text)]
    out = _format_context(msgs)
    assert long_text in out


def test_format_reply_to_with_display() -> None:
    m = ContextMessage(sender_id="555", sender_display="alice", text="PR opened")
    out = _format_reply_to(m)
    assert out == (
        "[reply_to] (the user is replying to this specific message)\n"
        "alice (id=555): PR opened\n"
        "[/reply_to]\n\n"
    )


def test_format_reply_to_without_display() -> None:
    m = ContextMessage(sender_id="555", text="PR opened")
    out = _format_reply_to(m)
    assert "id=555: PR opened" in out
    assert "[reply_to]" in out
    assert "[/reply_to]" in out

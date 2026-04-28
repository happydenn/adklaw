# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Tests for the `Origin` dataclass and origin formatting helpers."""

from __future__ import annotations

import dataclasses

import pytest

from app.channels.base import Origin, _format_origin, _id_label


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

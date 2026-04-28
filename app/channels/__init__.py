# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Channels — adapters that route messages from external transports to the
agent. The channel base lives in `base.py`; concrete channels (Discord,
Slack, Telegram, etc.) sit alongside it as their own modules."""

from .base import ChannelBase

__all__ = ["ChannelBase"]

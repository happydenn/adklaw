"""Channels — adapters that route messages from external transports to the
agent. The channel base lives in `base.py`; concrete channels (Discord,
Slack, Telegram, etc.) sit alongside it as their own modules.

Importing this package eagerly loads the project's `.env` so channel
processes (`python -m app.channels.<name>`) get the same auto-discovery
as `agents-cli run` and `agents-cli playground`. The load happens
before any submodule imports `app.agent`, so Vertex AI auth picks up
`GOOGLE_CLOUD_PROJECT` from `.env` without the user having to source
it manually.
"""

from ._env import load_channel_env

load_channel_env()

from .base import ChannelBase, Origin  # noqa: E402  (must follow env load)

__all__ = ["ChannelBase", "Origin"]

# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Discord channel.

Wires the agent up to a Discord bot account. In guild channels the bot
only responds when @-mentioned (so it doesn't interject on every
message); in DMs it always responds. Each Discord channel/thread/DM
gets its own ADK session, so conversations are isolated per location.

Run with:

    uv sync --extra discord
    DISCORD_BOT_TOKEN=... uv run python -m app.channels.discord
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from google.adk.apps import App
from google.adk.sessions import BaseSessionService

from .base import ChannelBase

if TYPE_CHECKING:
    import discord

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000


class DiscordChannel(ChannelBase):
    """Discord adapter for adklaw."""

    def __init__(self, app: App, session_service: BaseSessionService, token: str):
        super().__init__(app, session_service)
        # Imported lazily so the rest of the project doesn't require
        # discord.py to be installed.
        try:
            import discord
        except ModuleNotFoundError as e:
            raise SystemExit(
                "discord.py is not installed. Run `uv sync` (the `channels` "
                "dependency group is installed by default) and try again."
            ) from e

        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)
        self._token = token

        @self._client.event
        async def on_ready() -> None:
            logger.info("Discord bot logged in as %s", self._client.user)

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            await self._on_message(message)

    async def _on_message(self, message: discord.Message) -> None:
        # Ignore our own messages and other bots.
        if message.author.bot:
            return

        # Activation policy: respond in DMs always; in guilds, only when
        # the bot is mentioned. Avoids spamming busy channels.
        is_dm = message.guild is None
        is_mentioned = self._client.user in message.mentions
        if not is_dm and not is_mentioned:
            return

        # Strip the bot mention out of the prompt so the agent doesn't
        # see "@adklaw what skills do you have?" — it sees just the
        # actual question.
        prompt = message.clean_content
        if is_mentioned and self._client.user is not None:
            mention_name = f"@{self._client.user.display_name}"
            if prompt.startswith(mention_name):
                prompt = prompt[len(mention_name) :].lstrip()

        if not prompt:
            return

        try:
            async with message.channel.typing():
                response = await self.handle_message(
                    user_id=str(message.author.id),
                    session_id=str(message.channel.id),
                    message=prompt,
                )
        except Exception:
            logger.exception("Agent run failed for Discord message %s", message.id)
            await message.reply(
                "Sorry — something went wrong handling that message. "
                "Check the bot logs."
            )
            return

        if not response:
            await message.reply("(no response)")
            return

        for chunk in _split_for_discord(response):
            await message.reply(chunk)

    def run(self) -> None:
        """Block on the Discord client. Returns when the bot disconnects."""
        self._client.run(self._token)


def _split_for_discord(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split a long response into Discord-sized chunks at line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        # Try to break at the last newline before the limit; fall back
        # to a hard split if no newline is reachable.
        split_at = remaining.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def main() -> None:
    """Entrypoint for `python -m app.channels.discord`."""
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_BOT_TOKEN is not set. Add it to .env or your shell environment."
        )

    # Imported here (not at module top) so importing the module doesn't
    # require Vertex AI credentials and so the agent's heavy imports
    # only happen when actually launching the bot.
    from google.adk.sessions.sqlite_session_service import SqliteSessionService

    from app.agent import app
    from app.state import get_state_dir

    db_path = get_state_dir() / "sessions.db"
    session_service = SqliteSessionService(db_path=str(db_path))
    DiscordChannel(app, session_service, token).run()


if __name__ == "__main__":
    main()

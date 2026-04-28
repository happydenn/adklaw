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

import functools
import logging
import os
from typing import TYPE_CHECKING

from google.adk.apps import App
from google.adk.sessions import BaseSessionService

from .base import ChannelBase, Origin

if TYPE_CHECKING:
    import discord

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_LIMIT = 2000


@functools.cache
def _allowed_user_ids() -> frozenset[str]:
    """Return the configured allowlist as a frozenset of user IDs.

    Empty (env var unset or blank) means "no allowlist — allow all."
    Cached for the process lifetime; restart the bot to pick up env
    changes.
    """
    raw = os.environ.get("DISCORD_ALLOWED_USER_IDS", "").strip()
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


@functools.cache
def _allowlist_scope() -> str:
    """Return how broadly the allowlist applies: "dm" (default) or "all".

    - "dm": only DMs are gated. Guild mentions always bypass the
      allowlist (anyone who can @-mention the bot in a server you've
      invited it to is implicitly trusted enough).
    - "all": both DMs and guild mentions are gated.
    """
    raw = os.environ.get("DISCORD_ALLOWLIST_SCOPE", "dm").strip().lower()
    if raw not in ("dm", "all"):
        logger.warning(
            "Unknown DISCORD_ALLOWLIST_SCOPE=%r; defaulting to 'dm'.", raw
        )
        return "dm"
    return raw


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
        # Non-allowlisted DM senders we've already replied to once in
        # this process. Subsequent DMs from them are silently ignored.
        # Resets on restart by design — keeps the feature stateless on
        # disk; the cost is one repeat notice after a bot restart.
        self._notified_disallowed: set[str] = set()

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

        is_dm = message.guild is None
        sender_id = str(message.author.id)

        # Allowlist gate. Empty allowlist means "allow all" (default).
        # `DISCORD_ALLOWLIST_SCOPE` decides whether the allowlist applies
        # to DMs only ("dm", default) or every surface ("all"). Under "dm",
        # guild mentions are never gated by the allowlist — they're already
        # restricted to people who share a server with the bot.
        # First-time non-allowed DMs get one polite reply telling the
        # operator how to add the user; everything else is silent.
        allowed = _allowed_user_ids()
        scope = _allowlist_scope()
        gated = (scope == "all") or is_dm
        if allowed and gated and sender_id not in allowed:
            if is_dm and sender_id not in self._notified_disallowed:
                self._notified_disallowed.add(sender_id)
                await message.reply(
                    f"You are not on the allowlist. Add `{sender_id}` "
                    f"to `DISCORD_ALLOWED_USER_IDS` and try again."
                )
                logger.info("Notified non-allowlisted DM sender %s", sender_id)
            else:
                logger.info(
                    "Ignoring %s from non-allowlisted user %s",
                    "DM" if is_dm else f"mention in #{message.channel.name}",
                    sender_id,
                )
            return

        # Activation policy: respond in DMs always; in guilds, only when
        # the bot is mentioned. Avoids spamming busy channels.
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

        # Build the Origin envelope so the agent knows who/where it's
        # talking to. IDs are stable; display names are mutable but
        # readable for the LLM.
        if is_dm:
            location_display = "DM"
        else:
            guild_name = message.guild.name if message.guild else "?"
            location_display = (
                f"guild '{guild_name}'"
                + (f" (id={message.guild.id})" if message.guild else "")
                + f" / channel #{message.channel.name}"
            )
        origin = Origin(
            transport="discord",
            sender_id=sender_id,
            sender_display=message.author.display_name,
            location_id=str(message.channel.id),
            location_display=location_display,
        )

        try:
            async with message.channel.typing():
                response = await self.handle_message(
                    user_id=sender_id,
                    session_id=str(message.channel.id),
                    message=prompt,
                    origin=origin,
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

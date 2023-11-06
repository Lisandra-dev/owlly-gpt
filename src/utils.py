import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

import discord
from base import Message, Persona
from constants import (
    ALLOWED_SERVER_IDS,
    INACTIVATE_THREAD_PREFIX,
    KNOWLEDGE_CUTOFF,
    MAX_CHARS_PER_REPLY_MSG,
    SYSTEM_MESSAGE,
)
from discord import Message as DiscordMessage

logger = logging.getLogger(__name__)


def discord_message_to_message(
    message: DiscordMessage, bot_name: str
) -> Optional[Message]:
    user_name = "assistant" if message.author.name == bot_name else "user"
    if (
        message.reference
        and message.type == discord.MessageType.thread_starter_message
        and message.reference.cached_message
        and len(message.reference.cached_message.embeds) > 0
        and len(message.reference.cached_message.embeds[0].fields) > 0
    ):
        field = message.reference.cached_message.embeds[0].fields[0]
        logger.info(f"field.name - {field.name}")
        return Message(user="user", text=field.value)
    elif message.content:
        user_name = "assistant" if message.author.name == bot_name else "user"
        return Message(user=user_name, text=message.content)
    return None


def remove_last_bot_message(message: list[Message]) -> list[Message]:
    # remove the lasts messages send by the bot
    while message[-1].user == "assistant":
        message.pop()
    return message


def split_into_shorter_messages(
    text,  # noqa
    limit=MAX_CHARS_PER_REPLY_MSG,  # noqa
    code_block="```",  # noqa
) -> Any | List[Any]:  # noqa
    def split_at_boundary(s, boundary) -> Any | List[Any]:  # noqa
        parts = s.split(boundary)
        result = []
        for i, part in enumerate(parts):
            if i % 2 == 1:
                result.extend(split_code_block(part))
            else:
                result += split_substring(part)
        return result

    def split_substring(s) -> Any | List[Any]:  # noqa
        if len(s) <= limit:
            return [s]
        for boundary in ("\n", " "):
            if boundary in s:
                break
        else:
            return [s[:limit]] + split_substring(s[limit:])

        pieces = s.split(boundary)
        result = []
        current_part = pieces[0]
        for piece in pieces[1:]:
            if len(current_part) + len(boundary) + len(piece) > limit:
                result.append(current_part)
                current_part = piece
            else:
                current_part += boundary + piece
        result.append(current_part)
        return result

    def split_code_block(s) -> Any | List[Any]:  # noqa
        if len(code_block + s + code_block) <= limit:
            return [code_block + s + code_block]
        else:
            lines = s.split("\n")
            result = [code_block]
            for line in lines:
                if len(result[-1] + "\n" + line) > limit:
                    result[-1] += code_block
                    result.append(code_block + line)
                else:
                    result[-1] += "\n" + line
            result[-1] += code_block
            return result

    if code_block in text:
        return split_at_boundary(text, code_block)
    else:
        return split_substring(text)


def is_last_message_stale(
    interaction_message: DiscordMessage, last_message: DiscordMessage, bot_id: str
) -> bool:
    return (
        last_message
        and last_message.id != interaction_message.id
        and last_message.author
        and last_message.author.id != bot_id
    )


async def close_thread(thread: discord.Thread) -> None:
    await thread.edit(name=INACTIVATE_THREAD_PREFIX)
    await thread.send(
        embed=discord.Embed(
            description="**Thread closed** - Context limit reached, closing...",
            color=discord.Color.blue(),
        )
    )
    await thread.edit(archived=True, locked=True)


def should_block(guild: Optional[discord.Guild]) -> bool:
    if guild is None:
        # dm's not supported
        logger.info("DM not supported")
        return True

    if guild.id and guild.id not in ALLOWED_SERVER_IDS:
        # not allowed in this server
        logger.info(f"Guild {guild} not allowed")
        return True
    return False


def get_persona(persona: str | None) -> Persona:
    """Get the persona from the persona.json file"""
    all_personas = json.load(Path("persona.json").open(encoding="utf-8"))
    if persona:
        get_persona = all_personas.get(persona, None)
        if get_persona:
            # convert to Persona object
            return Persona(
                name=persona if persona else "default",
                icon=get_persona.get("icon", ""),
                system=get_persona.get("system", ""),
            )
    current_date = datetime.now().strftime("%Y-%m-%d")
    return Persona(
        name="GPT-4",
        icon="🤖",
        system=SYSTEM_MESSAGE.format(
            knowledge_cutoff=KNOWLEDGE_CUTOFF, current_date=current_date
        ),
    )

import asyncio
import datetime
import logging
from typing import Optional

import discord
import tiktoken
from base import Message
from completion import generate_completion_response, process_response
from constants import (
    ACTIVATE_THREAD_PREFX,
    BOT_INVITE_URL,
    DISCORD_BOT_TOKEN,
    MAX_INPUTS_TOKENS,
    SECONDS_DELAY_RECEIVING_MSG,
)
from discord import Message as DiscordMessage
from discord.ext import commands
from utils import (
    close_thread,
    count_token_message,
    generate_choice_persona,
    generate_initial_system,
    get_persona,
    is_last_message_stale,
    logger,
    remove_last_bot_message,
    should_block,
)

logging.basicConfig(
    format="[%(asctime)s] [%(filename)s:%(lineno)d] %(message)s", level=logging.INFO
)

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)
bot = commands.Bot(command_prefix="!", intents=intents)
model = tiktoken.encoding_for_model(
    "gpt-4"
)  # need to be upgraded to the new gpt-4 turbo but can do the job for now
personas_choice = generate_choice_persona()


class Client(discord.Client):
    def __init__(self, *, intents: discord.Intents) -> None:  # noqa
        super().__init__(intents=intents)
        self.tree = discord.app_commands.CommandTree(self)

    async def setup_hook(self) -> None:  # noqa
        logger.info("Setting up slash commands")
        await self.tree.sync()


@client.event
async def on_ready() -> None:
    logger.info(f"We have logged in as {client.user}. Invite URL: {BOT_INVITE_URL}")
    await tree.sync()


# /chat message:
@tree.command(name="chat", description="Create a new thread for conversation")
@discord.app_commands.describe(
    persona="The persona to use with the model, changing this response style"
)
@discord.app_commands.choices(persona=personas_choice)
@discord.app_commands.checks.has_permissions(send_messages=True)
@discord.app_commands.checks.has_permissions(view_channel=True)
@discord.app_commands.checks.bot_has_permissions(send_messages=True)
@discord.app_commands.checks.bot_has_permissions(view_channel=True)
@discord.app_commands.checks.bot_has_permissions(manage_threads=True)
async def chat_command(
    int: discord.Interaction,
    message: str,
    persona: Optional[discord.app_commands.Choice[str]] = None,
) -> None:
    try:
        # only support creating thread in text channel
        if not isinstance(int.channel, discord.TextChannel) or should_block(int.guild):
            return

        user = int.user
        try:
            persona_system = get_persona(persona.value if persona else None)

            embed = discord.Embed(
                title=f"{persona_system.icon} {persona_system.title}",
                description=f"<@{user.id}> started a new chat",
                color=discord.Color.from_str(persona_system.color),
            )
            embed.add_field(name="Message :", value=f"> {message}")

            await int.response.send_message(embed=embed)
            response = await int.original_response()
        except Exception as e:
            logger.exception(e)
            await int.response.send_message(
                f"Failed to start chat {str(e)}", ephemeral=True
            )
            return

        today_date = datetime.datetime.now().strftime("%d-%m-%Y-%H:%M")
        # create the thread
        thread = await response.create_thread(
            name=f"{ACTIVATE_THREAD_PREFX} - {persona_system.icon} [{today_date}] - {user.display_name[:10]}",
            reason="gpt-bot",
            auto_archive_duration=60,
        )
        async with thread.typing():
            # prepare the initial system message
            system_message = (
                persona_system.system.replace("\n-", "")
                .replace("\n", "")
                .replace("__", "")
            )  # remove newlines
            logger.info(f"Thread created - {user}: {message}")
            # fetch completion
            messages = [
                Message(user="system", text=system_message),
                Message(user="user", text=message),
            ]
            response_data = await generate_completion_response(
                messages=messages,
            )
            # send the result
            await process_response(thread=thread, response_data=response_data)
    except Exception as e:
        logger.exception(e)
        await int.response.send_message(
            f"Failed to start chat {str(e)}", ephemeral=True
        )


# calls for each message
@client.event
async def on_message(message: DiscordMessage) -> None:  # noqa
    try:
        # block servers not in allow list
        if (
            should_block(guild=message.guild)
            or not client.user
            or message.author == client.user
        ):
            return
        # ignore messages not in a thread
        channel = message.channel
        if not isinstance(channel, discord.Thread):
            return

        # ignore threads not created by the bot
        thread = channel

        # ignore threads that are archived locked or title is not what we want
        if (
            not thread
            or not thread.last_message
            or thread.owner_id != client.user.id
            or thread.archived
            or thread.locked
            or not thread.name.startswith(ACTIVATE_THREAD_PREFX)
        ):
            # ignore this thread
            return
        channel_messages = await generate_initial_system(client, thread)
        nb_tokens = count_token_message(channel_messages, model)

        if nb_tokens > MAX_INPUTS_TOKENS:
            await close_thread(thread)
            return

        # wait a bit in case user has more messages
        if SECONDS_DELAY_RECEIVING_MSG > 0:
            await asyncio.sleep(SECONDS_DELAY_RECEIVING_MSG)
            if is_last_message_stale(
                interaction_message=message,
                last_message=thread.last_message,
                bot_id=str(client.user.id),
            ):
                # there is another message, so ignore this one
                return

        logger.info(
            f"Thread message to process - {message.author}: {message.content[:50]} - {thread.name} {thread.jump_url}"
        )

        # prepare the initial system message
        if message.content.lower().strip() == "$$stop":
            await close_thread(thread)
            return
        # generate the response
        async with thread.typing():
            response_data = await generate_completion_response(
                messages=channel_messages
            )

        # if the message is == "/rerun" then rerun with the same message before the /rerun and the last reply of the bot
        if message.content.lower() == "$$rerun":
            await message.delete()
            # remove the last message from the channel_messages
            async with thread.typing():
                response_data = await generate_completion_response(
                    messages=remove_last_bot_message(channel_messages[:-1])
                )

        if is_last_message_stale(
            interaction_message=message,
            last_message=thread.last_message,
            bot_id=str(client.user.id),
        ):
            # there is another message and its not from us, so ignore this response
            return
        async with thread.typing():
            await process_response(thread=thread, response_data=response_data)
    except Exception as e:
        logger.exception(e)


@tree.command(name="help", description="Print each persona and their description")
@discord.app_commands.checks.has_permissions(send_messages=True)
@discord.app_commands.checks.has_permissions(view_channel=True)
@discord.app_commands.checks.bot_has_permissions(send_messages=True)
@discord.app_commands.checks.bot_has_permissions(view_channel=True)
@discord.app_commands.describe(
    persona="The persona to use with the model, changing this response style"
)
@discord.app_commands.choices(persona=personas_choice)
async def help_command(
    int: discord.Interaction, persona: Optional[discord.app_commands.Choice[str]] = None
) -> None:
    persona_system = get_persona(persona.value if persona else None)
    embed = discord.Embed(
        title=f"{persona_system.icon} {persona_system.title}",
        description=persona_system.system,
        color=discord.Colour.from_str(persona_system.color),
    )
    await int.response.send_message(embed=embed)


@tree.command(name="persona", description="Change the persona")
@discord.app_commands.describe(persona="The persona to use with the model")
@discord.app_commands.choices(persona=personas_choice)
async def change_persona(int: discord.Interaction, persona: Optional[discord.app_commands.Choice[str]] = None) -> None:
    thread = int.channel


client.run(DISCORD_BOT_TOKEN)

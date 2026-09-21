import asyncio
import logging
import os
import subprocess
import sys

import discord
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TOKEN = os.getenv("DISCORD_TOKEN")
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0"))
GOODBYE_CHANNEL_ID = int(os.getenv("GOODBYE_CHANNEL_ID", "0"))
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))

if not TOKEN:
    raise RuntimeError("Brak DISCORD_TOKEN w pliku .env")
if not AUTHORIZED_USER_ID:
    raise RuntimeError("Brak AUTHORIZED_USER_ID w pliku .env")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

client = discord.Client(intents=intents)
BOT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))


def reload_bot() -> None:
    os.execv(sys.executable, [sys.executable, *sys.argv])


def update_bot() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", BOT_DIRECTORY, "pull", "--ff-only"],
        capture_output=True,
        text=True,
        check=False,
    )


def get_message_channel(guild: discord.Guild, channel_id: int) -> discord.TextChannel | None:
    if channel_id:
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel

    if isinstance(guild.system_channel, discord.TextChannel):
        return guild.system_channel

    return None


@client.event
async def on_ready() -> None:
    logging.info("Zalogowano jako %s", client.user)


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.content.startswith("!"):
        return

    if message.author.id != AUTHORIZED_USER_ID:
        await message.channel.send(
            f"{message.author.mention}, nie masz uprawnień do używania tego bota."
        )
        return

    if message.content.strip() == "!ping":
        await message.channel.send("Pong!")
    elif message.content.strip() == "!reload":
        await message.channel.send("Przeładowuję bota...")
        await client.close()
        reload_bot()
    elif message.content.strip() == "!update":
        result = await asyncio.to_thread(update_bot)
        output = (result.stdout + result.stderr).strip()

        if result.returncode != 0:
            logging.error("Aktualizacja bota nie powiodła się: %s", output)
            await message.channel.send(
                "Aktualizacja nie powiodła się. Sprawdź logi bota."
            )
            return

        logging.info("Zaktualizowano bota z GitHub: %s", output or "brak zmian")
        await message.channel.send("Bot został zaktualizowany. Przeładowuję...")
        await client.close()
        reload_bot()


@client.event
async def on_member_join(member: discord.Member) -> None:
    channel = get_message_channel(member.guild, WELCOME_CHANNEL_ID)
    if channel is None:
        logging.warning("Nie znaleziono kanału powitalnego na serwerze %s", member.guild.name)
        return

    await channel.send(f"Witaj! {member.mention} miło cię widzieć na {member.guild.name}.")


@client.event
async def on_member_remove(member: discord.Member) -> None:
    channel = get_message_channel(member.guild, GOODBYE_CHANNEL_ID)
    if channel is None:
        logging.warning("Nie znaleziono kanału pożegnalnego na serwerze %s", member.guild.name)
        return

    await channel.send(f"Żegnaj {member.mention} nikt cię tu nie trzyma")


async def run_bot() -> None:
    while True:
        try:
            await client.start(TOKEN, reconnect=True)
        except (OSError, asyncio.TimeoutError) as error:
            logging.warning("Utracono połączenie z internetem: %s", error)
            logging.info("Ponawiam uruchomienie bota za 10 sekund...")
            await asyncio.sleep(10)
        else:
            logging.info("Połączenie bota zostało zamknięte. Ponawiam za 10 sekund...")
            await asyncio.sleep(10)


asyncio.run(run_bot())

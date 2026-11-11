import asyncio
import logging
import os
import subprocess
import sys
from datetime import timedelta

import aiohttp
import discord
import mariadb
from dotenv import load_dotenv

import channel_backup
import tickets
from database import record_moderation_warning

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

TOKEN = os.getenv("DISCORD_TOKEN")
WELCOME_CHANNEL_ID = int(os.getenv("WELCOME_CHANNEL_ID", "0"))
GOODBYE_CHANNEL_ID = int(os.getenv("GOODBYE_CHANNEL_ID", "0"))
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))
TEST_CHANNEL_ID = 1551275904280432683
LOG_CHANNEL_ID = 1555304186231521341
MESSAGE_LOG_CHANNEL_ID = 1560761928031272960
WELCOME_TEMPLATE_USER_ID = 557628352828014614
PROBOT_USER_ID = 282859044593598464
PROBOT_WELCOME_CHANNEL_ID = 846065271675420737
PROBOT_GOODBYE_CHANNEL_ID = 899304602292346891
PROBOT_ANNOUNCEMENT_TIMEOUT = 15

if not TOKEN:
    raise RuntimeError("Brak DISCORD_TOKEN w pliku .env")
if not AUTHORIZED_USER_ID:
    raise RuntimeError("Brak AUTHORIZED_USER_ID w pliku .env")

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

client: discord.Client
BOT_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
recent_voice_audit_entries: list[discord.AuditLogEntry] = []
used_voice_move_audit_entry_ids: dict[int, int] = {}
MODERATION_PERMISSIONS = {
    "!ban": "ban_members",
    "!kick": "kick_members",
    "!timeout": "moderate_members",
    "!untimeout": "moderate_members",
    "!warn": "moderate_members",
    "!clear": "manage_messages",
}
MODERATION_COMMAND_ALIASES = {
    "!to": "!timeout",
    "!t/o": "!timeout",
}


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


def can_use_moderation_command(
    author: discord.abc.User,
    command: str,
) -> bool:
    if author.id == AUTHORIZED_USER_ID:
        return True
    return (
        isinstance(author, discord.Member)
        and getattr(author.guild_permissions, MODERATION_PERMISSIONS[command])
    )


async def handle_moderation_command(message: discord.Message) -> bool:
    parts = message.content.strip().split()
    command = parts[0].casefold()
    command = MODERATION_COMMAND_ALIASES.get(command, command)
    if command not in MODERATION_PERMISSIONS:
        return False

    if message.guild is None or not isinstance(message.author, discord.Member):
        await message.channel.send("Tej komendy można używać tylko na serwerze.")
        return True

    if not can_use_moderation_command(message.author, command):
        await message.channel.send(
            f"{message.author.mention}, nie masz uprawnienia do używania {command}."
        )
        return True

    if command == "!clear":
        if len(parts) != 2 or not parts[1].isdigit():
            await message.channel.send("Użycie: `!clear <liczba 1-100>`")
            return True
        count = int(parts[1])
        if not 1 <= count <= 100:
            await message.channel.send("Możesz usunąć od 1 do 100 wiadomości.")
            return True
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            await message.channel.send("Tej komendy można używać tylko na kanale tekstowym.")
            return True

        try:
            deleted = await message.channel.purge(limit=count + 1)
            deleted_count = sum(deleted_message.id != message.id for deleted_message in deleted)
            await message.channel.send(f"Usunięto {deleted_count} wiadomości.")
        except discord.Forbidden:
            await message.channel.send("Bot nie ma uprawnień do usuwania wiadomości na tym kanale.")
        except discord.HTTPException:
            logging.exception("Nie udało się usunąć wiadomości na kanale %s.", message.channel.id)
            await message.channel.send("Nie udało się usunąć wiadomości.")
        return True

    if len(parts) < 2:
        usage = {
            "!ban": "`!ban @użytkownik [powód]`",
            "!kick": "`!kick @użytkownik [powód]`",
            "!timeout": "`!timeout` / `!to` / `!t/o @użytkownik <minuty> [powód]`",
            "!untimeout": "`!untimeout @użytkownik [powód]`",
            "!warn": "`!warn @użytkownik [powód]`",
        }[command]
        await message.channel.send(f"Użycie: {usage}")
        return True

    target_token = parts[1]
    if target_token.startswith("<@") and target_token.endswith(">"):
        target_id_text = target_token[2:-1].lstrip("!")
    else:
        target_id_text = target_token
    if not target_id_text.isdigit():
        await message.channel.send("Podaj użytkownika przez wzmiankę lub jego ID.")
        return True

    try:
        target = message.guild.get_member(int(target_id_text))
        if target is None:
            target = await message.guild.fetch_member(int(target_id_text))
    except discord.NotFound:
        await message.channel.send("Nie znaleziono tego użytkownika na serwerze.")
        return True
    except discord.HTTPException:
        logging.exception("Nie udało się pobrać członka serwera %s.", target_id_text)
        await message.channel.send("Nie udało się pobrać danych tego użytkownika.")
        return True

    if target.id == message.author.id:
        await message.channel.send("Nie możesz użyć tej komendy na sobie.")
        return True
    if client.user is not None and target.id == client.user.id:
        await message.channel.send("Nie możesz użyć tej komendy na bocie.")
        return True
    if target.id == message.guild.owner_id:
        await message.channel.send("Nie można moderować właściciela serwera.")
        return True
    if (
        message.author.id not in (message.guild.owner_id, AUTHORIZED_USER_ID)
        and target.top_role >= message.author.top_role
    ):
        await message.channel.send(
            "Nie możesz moderować osoby z równą lub wyższą rolą."
        )
        return True

    reason_start = 2
    if command == "!timeout":
        if len(parts) < 3 or not parts[2].isdigit():
            await message.channel.send(
                "Użycie: `!timeout` / `!to` / `!t/o @użytkownik <minuty> [powód]`"
            )
            return True
        timeout_minutes = int(parts[2])
        if not 1 <= timeout_minutes <= 28 * 24 * 60:
            await message.channel.send("Timeout musi trwać od 1 minuty do 28 dni.")
            return True
        reason_start = 3

    reason = " ".join(parts[reason_start:]).strip() or "Nie podano powodu."
    if len(reason) > 500:
        await message.channel.send("Powód może mieć maksymalnie 500 znaków.")
        return True

    try:
        if command == "!ban":
            await target.ban(reason=reason)
            confirmation = f"Zbanowano {target.mention}. Powód: {reason}"
        elif command == "!kick":
            await target.kick(reason=reason)
            confirmation = f"Wyrzucono {target.mention}. Powód: {reason}"
        elif command == "!timeout":
            await target.timeout(timedelta(minutes=timeout_minutes), reason=reason)
            confirmation = (
                f"Nałożono timeout na {target.mention} na {timeout_minutes} min. "
                f"Powód: {reason}"
            )
        elif command == "!untimeout":
            await target.timeout(None, reason=reason)
            confirmation = f"Zdjęto timeout z {target.mention}. Powód: {reason}"
        else:
            await asyncio.to_thread(
                record_moderation_warning,
                message.guild.id,
                target.id,
                message.author.id,
                reason,
            )
            confirmation = f"Ostrzeżono {target.mention}. Powód: {reason}"
        await message.channel.send(confirmation)
    except discord.Forbidden:
        await message.channel.send(
            "Bot nie ma uprawnień do wykonania tej akcji albo jego rola jest za nisko."
        )
    except discord.HTTPException:
        logging.exception("Nie udało się wykonać komendy %s na użytkowniku %s.", command, target.id)
        await message.channel.send("Nie udało się wykonać tej akcji.")
    except mariadb.Error:
        await message.channel.send("Nie udało się zapisać ostrzeżenia w bazie danych.")
    return True


async def on_ready() -> None:
    logging.info("Zalogowano jako %s", client.user)
    try:
        await asyncio.to_thread(tickets.initialize_database_state)
    except (mariadb.Error, OSError, RuntimeError, TypeError, ValueError):
        logging.exception("Nie można przygotować MariaDB; bot zostanie uruchomiony ponownie.")
        await client.close()
        return
    await client.change_presence(activity=discord.Game(name="by 505na"))
    channel_backup.start_scheduled_backup(client)


def message_mentions_member(message: discord.Message, member_id: int) -> bool:
    mention_tokens = (f"<@{member_id}>", f"<@!{member_id}>")
    message_text = [message.content]
    for embed in message.embeds:
        message_text.extend(
            (
                embed.title or "",
                embed.description or "",
            )
        )
        if embed.author is not None:
            message_text.append(embed.author.name or "")
        if embed.footer is not None:
            message_text.append(embed.footer.text or "")
        for field in embed.fields:
            message_text.extend((field.name, field.value))

    return any(token in text for token in mention_tokens for text in message_text)


async def wait_for_probot_announcement(
    channel_id: int,
    guild_id: int,
    member_id: int,
) -> bool:
    try:
        await client.wait_for(
            "message",
            timeout=PROBOT_ANNOUNCEMENT_TIMEOUT,
            check=lambda message: (
                message.author.id == PROBOT_USER_ID
                and message.guild is not None
                and message.guild.id == guild_id
                and (
                    message.channel.id == channel_id
                    or message_mentions_member(message, member_id)
                )
            ),
        )
    except asyncio.TimeoutError:
        return False

    return True


async def on_message(message: discord.Message) -> None:
    is_welcome_message = message.content.strip().casefold().endswith("welcome")
    mentions_template_user = any(
        user.id == WELCOME_TEMPLATE_USER_ID for user in message.mentions
    )
    if (
        is_welcome_message
        and (
            message.author.id == WELCOME_TEMPLATE_USER_ID
            or mentions_template_user
        )
    ):
        await message.channel.send(
            """Skopiuj i wypełnij wzór:

᯾ Ile masz lat?
᯾ Ocen swoje pvp 1-10
᯾ Twoja ostatnia gildia i dlaczego w niej nie jesteś?
᯾ Dlaczego akurat my?
᯾ Przyszedłeś do nas na konkretny serwer czy na wszystkie?
᯾ Umiesz robić water skille tak zwane wiadra?
᯾ Potrafisz wychodzić z fosy slimeblockiem/zwykłym blokiem np coblem?
᯾ Ile czasu poświęciłbyś dziennie na gildię?
᯾ Skąd wiesz o rekrutacji?
᯾ Posiadasz premkę?
᯾ Jesteś toxic?
᯾ Potrafisz uszanować młodsze/starsze osoby?
᯾ Jaki masz nick w mc?
᯾  opisz swoją osobowość
᯾  czy potrafisz się zachować?
᯾  czy potrafisz przegrywać? (wyjebiesz sie na edycji i grasz dalej a nie ryk bo ranking i itemy zrobiły papa)"""
        )
        return

    if message.author.bot:
        return

    if not message.content.startswith("!"):
        return

    if message.content.strip().casefold() == "!cmd":
        commands = ["`!cmd` — wyświetla listę dostępnych komend."]
        if isinstance(message.author, discord.Member):
            moderation_help = {
                "!ban": "`!ban @użytkownik [powód]` — blokuje użytkownika.",
                "!kick": "`!kick @użytkownik [powód]` — wyrzuca użytkownika.",
                "!timeout": "`!timeout` / `!to` / `!t/o @użytkownik <minuty> [powód]` — nakłada timeout.",
                "!untimeout": "`!untimeout @użytkownik [powód]` — zdejmuje timeout.",
                "!warn": "`!warn @użytkownik [powód]` — zapisuje ostrzeżenie.",
                "!clear": "`!clear <liczba 1-100>` — usuwa wiadomości.",
            }
            commands.extend(
                description
                for command, description in moderation_help.items()
                if can_use_moderation_command(message.author, command)
            )
        if isinstance(message.author, discord.Member) and any(
            role.id in tickets.RECRUITER_ROLE_IDS for role in message.author.roles
        ):
            commands.append("`!rekru` — wysyła panel rekrutacyjny.")
        if message.author.id in (
            channel_backup.EXTRA_AUTHORIZED_USER_IDS | {AUTHORIZED_USER_ID}
        ):
            commands.extend(
                (
                    "`!backup [ID_kanału]` — wykonuje backup kanałów.",
                    "`!restore` — przywraca brakujące kanały i wiadomości.",
                )
            )
        if message.author.id == AUTHORIZED_USER_ID:
            commands.extend(
                (
                    "`!test` — wysyła test działania bota.",
                    "`!reload` — uruchamia bota ponownie.",
                    "`!stop` — zatrzymuje bota.",
                    "`!update` — aktualizuje bota i uruchamia go ponownie.",
                )
            )

        embed = discord.Embed(
            title="Dostępne komendy",
            description="\n".join(commands),
            color=discord.Color.blurple(),
        )
        await message.channel.send(embed=embed)
        return

    if await channel_backup.handle_command(
        client,
        message,
        AUTHORIZED_USER_ID,
    ):
        return

    if await handle_moderation_command(message):
        return

    if message.content.strip() == "!rekru":
        if not isinstance(message.author, discord.Member) or not any(
            role.id in tickets.RECRUITER_ROLE_IDS for role in message.author.roles
        ):
            await message.channel.send(
                f"{message.author.mention}, nie masz uprawnień do używania tej komendy."
            )
            return

        try:
            await tickets.send_recruitment_panel(message.channel)
        except discord.HTTPException:
            logging.exception(
                "Nie udało się wysłać panelu rekrutacyjnego na kanale %s.",
                message.channel.id,
            )
            await message.channel.send(
                "Nie udało się wysłać panelu rekrutacyjnego. Sprawdź uprawnienia bota."
            )
        return

    if message.author.id != AUTHORIZED_USER_ID:
        await message.channel.send(
            f"{message.author.mention}, nie masz uprawnień do używania tego bota."
        )
        return

    if message.content.strip() == "!test":
        test_channel = client.get_channel(TEST_CHANNEL_ID)
        if not isinstance(test_channel, discord.TextChannel):
            logging.warning("Nie znaleziono kanału testowego %s", TEST_CHANNEL_ID)
            return

        latency_ms = round(client.latency * 1000)
        await test_channel.send(
            f"Test bota zakończony pomyślnie. Bot działa poprawnie. Opóźnienie: {latency_ms} ms."
        )

        embed = discord.Embed(
            title="Member Joined",
            description="@wtajemniczona wtajemniczona15",
            color=discord.Color.from_rgb(15, 16, 19),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name="Member Joined", icon_url="https://cdn.discordapp.com/emojis/1374002125509664829.png")
        embed.add_field(name="Account Age", value="4 years, 0 months, 14 days", inline=False)
        embed.add_field(name="ID", value="100687914602183219", inline=False)
        embed.set_footer(text="18.08.2026 21:32")
        await test_channel.send(embed=embed)
    elif message.content.strip() == "!reload":
        await message.channel.send("Przeładowuję bota...")
        await client.close()
        reload_bot()
    elif message.content.strip() == "!stop":
        await message.channel.send("Zatrzymuję bota...")
        await client.close()
        os._exit(0)
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


async def on_member_join(member: discord.Member) -> None:
    welcome_channel = get_message_channel(member.guild, WELCOME_CHANNEL_ID)
    probot_announcement = asyncio.create_task(
        wait_for_probot_announcement(
            PROBOT_WELCOME_CHANNEL_ID,
            member.guild.id,
            member.id,
        )
    )
    await asyncio.sleep(0)

    log_channel = get_message_channel(member.guild, LOG_CHANNEL_ID)
    if log_channel is not None:
        joined_at = member.joined_at or discord.utils.utcnow()
        created_at = member.created_at
        age_days = max(int((discord.utils.utcnow() - created_at).total_seconds() // 86400), 0)
        years, remaining_days = divmod(age_days, 365)
        months, days = divmod(remaining_days, 30)

        if years:
            age_parts = [
                f"{years} year{'s' if years != 1 else ''}",
                f"{months} month{'s' if months != 1 else ''}",
                f"{days} day{'s' if days != 1 else ''}",
            ]
        elif months:
            age_parts = [
                f"{months} month{'s' if months != 1 else ''}",
                f"{days} day{'s' if days != 1 else ''}",
            ]
        else:
            age_parts = [f"{days} day{'s' if days != 1 else ''}"]

        age_text = ", ".join(age_parts)
        embed = discord.Embed(
            description=(
                f"@{member.name} {member.display_name}\n\n"
                f"**Account Age**\n"
                f"{age_text}\n\n"
                f"**ID:** {member.id}"
            ),
            color=discord.Color.from_rgb(15, 16, 19),
            timestamp=joined_at,
        )
        embed.color = discord.Color.from_rgb(76, 175, 80)
        embed.set_author(name="Member Joined", icon_url="https://cdn.discordapp.com/emojis/1374002125509664829.png")
        embed.set_footer(text=f"Dołączenie: {joined_at.strftime('%d.%m.%Y %H:%M:%S')}")
        await log_channel.send(embed=embed)

    if welcome_channel is None:
        logging.warning("Nie znaleziono kanału powitalnego na serwerze %s", member.guild.name)
        return

    if probot_announcement is not None and await probot_announcement:
        logging.info(
            "Pominięto powitanie dla użytkownika %s; ProBot opublikował wiadomość.",
            member.id,
        )
        return

    await welcome_channel.send(
        f"Witaj! {member.mention} ({member.name}) miło cię widzieć na {member.guild.name}."
    )


async def on_member_remove(member: discord.Member) -> None:
    channel = get_message_channel(member.guild, GOODBYE_CHANNEL_ID)
    probot_announcement = asyncio.create_task(
        wait_for_probot_announcement(
            PROBOT_GOODBYE_CHANNEL_ID,
            member.guild.id,
            member.id,
        )
    )
    await asyncio.sleep(0)

    log_channel = get_message_channel(member.guild, LOG_CHANNEL_ID)
    if log_channel is not None:
        left_at = discord.utils.utcnow()
        embed = discord.Embed(
            description=(
                f"@{member.name} {member.display_name}\n\n"
                f"**ID:** {member.id}"
            ),
            color=discord.Color.from_rgb(15, 16, 19),
            timestamp=left_at,
        )
        embed.color = discord.Color.from_rgb(255, 82, 82)
        embed.set_author(name="Member Left", icon_url="https://cdn.discordapp.com/emojis/1374002125509664829.png")
        embed.set_footer(text=f"Wyjście: {left_at.strftime('%d.%m.%Y %H:%M:%S')}")
        await log_channel.send(embed=embed)

    try:
        cutoff = discord.utils.utcnow() - timedelta(minutes=5)
        async for entry in member.guild.audit_logs(limit=20, action=discord.AuditLogAction.kick):
            if entry.target and entry.target.id == member.id and entry.created_at >= cutoff:
                await send_message_log_embed(
                    member.guild,
                    "Wyrzucenie użytkownika",
                    "Użytkownik został wyrzucony z serwera.",
                    discord.Color.dark_orange(),
                    author=member,
                    fields={
                        "Moderator": entry.user.mention if entry.user else "Nieznany",
                        "Powód": entry.reason or "Brak",
                    },
                )
                break
    except Exception:
        logging.debug("Nie można sprawdzić audit logów wyrzucenia użytkownika %s.", member.id, exc_info=True)

    if channel is None:
        logging.warning("Nie znaleziono kanału pożegnalnego na serwerze %s", member.guild.name)
        return

    if probot_announcement is not None and await probot_announcement:
        logging.info(
            "Pominięto pożegnanie dla użytkownika %s; ProBot opublikował wiadomość.",
            member.id,
        )
        return

    await channel.send(f"Żegnaj {member.mention} ({member.name}) nikt cię tu nie trzyma")


async def send_message_log_embed(
    guild: discord.Guild,
    title: str,
    description: str,
    color: discord.Color,
    *,
    author: discord.abc.User | None = None,
    channel: discord.abc.GuildChannel | discord.Thread | None = None,
    fields: dict[str, str] | None = None,
) -> None:
    log_channel = get_message_channel(guild, MESSAGE_LOG_CHANNEL_ID)
    if log_channel is None:
        return

    embed = discord.Embed(title=title, description=description, color=color, timestamp=discord.utils.utcnow())
    if author is not None:
        embed.add_field(name="Autor", value=f"{author} (`{author.id}`)", inline=False)
    if channel is not None:
        embed.add_field(name="Kanał", value=f"{channel.mention} (`{channel.id}`)", inline=True)
    embed.add_field(name="Serwer", value=f"{guild.name} (`{guild.id}`)", inline=False)
    if fields is not None:
        for name, value in fields.items():
            embed.add_field(name=name, value=value[:1024] or "Brak", inline=False)

    await log_channel.send(embed=embed)


async def send_deleted_message_log_embed(
    guild: discord.Guild,
    channel: discord.abc.GuildChannel | discord.Thread,
    message_id: int,
    *,
    author: discord.abc.User | None = None,
    attachments: list[discord.Attachment] | None = None,
    content: str | None = None,
) -> None:
    log_channel = get_message_channel(guild, MESSAGE_LOG_CHANNEL_ID)
    if log_channel is None:
        return

    embed = discord.Embed(
        title="Usunięto wiadomość",
        description=(
            f"**Treść wiadomości:**\n{content[:4070]}"
            if content
            else (
                "**Treść wiadomości:**\n*(wiadomość nie zawierała tekstu)*"
                if content == ""
                else "**Treść wiadomości:**\n*(niedostępna — wiadomości nie było w cache)*"
            )
        ),
        color=discord.Color.from_rgb(255, 82, 82),
        timestamp=discord.utils.utcnow(),
    )
    if author is None:
        author_value = "Nieznany (wiadomość nie była w cache)"
    else:
        author_value = f"{author} (`{author.id}`)"
    embed.add_field(name="Autor", value=author_value, inline=False)
    embed.add_field(name="Kanał", value=f"{channel.mention} (`{channel.id}`)", inline=True)
    embed.add_field(name="ID wiadomości", value=str(message_id), inline=True)
    embed.add_field(name="Serwer", value=f"{guild.name} (`{guild.id}`)", inline=False)

    if attachments is None:
        attachments_value = "Brak danych (wiadomość nie była w cache)"
    else:
        attachments_value = "\n".join(attachment.filename for attachment in attachments) or "Brak"
        attachments_value = attachments_value[:1024]
    embed.add_field(name="Załączniki", value=attachments_value, inline=False)
    await log_channel.send(embed=embed)


async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    if before.author is None or before.author.bot:
        return
    if before.content == after.content:
        return

    guild = after.guild
    embed = discord.Embed(
        title="Edytowano wiadomość",
        color=discord.Color.from_rgb(255, 193, 7),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="Autor",
        value=f"{after.author} (`{after.author.id}`)",
        inline=False,
    )
    embed.add_field(name="Kanał", value=after.channel.mention, inline=True)
    embed.add_field(name="ID wiadomości", value=str(after.id), inline=True)
    embed.add_field(name="Przed edycją", value=before.content[:1024] or "(brak treści)", inline=False)
    embed.add_field(name="Po edycji", value=after.content[:1024] or "(brak treści)", inline=False)
    if guild is not None:
        embed.add_field(name="Serwer", value=f"{guild.name} (`{guild.id}`)", inline=False)
        log_channel = get_message_channel(guild, MESSAGE_LOG_CHANNEL_ID)
        if log_channel is not None:
            await log_channel.send(embed=embed)


async def on_message_delete(message: discord.Message) -> None:
    if message.author is None or message.author.bot:
        return

    await send_deleted_message_log_embed(
        message.guild,
        message.channel,
        message.id,
        author=message.author,
        attachments=message.attachments,
        content=message.content,
    )


async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent) -> None:
    if payload.guild_id is None:
        return
    await channel_backup.restore_deleted_backup(
        client,
        payload.guild_id,
        payload.channel_id,
        (payload.message_id,),
    )

    cached_message = payload.cached_message
    if cached_message is not None:
        if cached_message.author is not None and not cached_message.author.bot:
            return

        await send_deleted_message_log_embed(
            cached_message.guild,
            cached_message.channel,
            cached_message.id,
            author=cached_message.author,
            attachments=cached_message.attachments,
            content=cached_message.content,
        )
        return

    guild = client.get_guild(payload.guild_id)
    if guild is None:
        return

    channel = guild.get_channel(payload.channel_id)
    if channel is None:
        return

    await send_deleted_message_log_embed(
        guild,
        channel,
        payload.message_id,
    )


async def on_raw_bulk_message_delete(payload: discord.RawBulkMessageDeleteEvent) -> None:
    if payload.guild_id is None:
        return

    await channel_backup.restore_deleted_backup(
        client,
        payload.guild_id,
        payload.channel_id,
        payload.message_ids,
    )

    guild = client.get_guild(payload.guild_id)
    if guild is None:
        return

    channel = guild.get_channel(payload.channel_id)
    if channel is None:
        return

    message_ids = ", ".join(str(message_id) for message_id in payload.message_ids[:20])
    if len(payload.message_ids) > 20:
        message_ids += ", ..."

    await send_message_log_embed(
        guild,
        "Masowe usunięcie wiadomości",
        f"Usunięto **{len(payload.message_ids)}** wiadomości.",
        discord.Color.dark_red(),
        channel=channel,
        fields={"ID wiadomości": message_ids or "Brak"},
    )


async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    if before.roles == after.roles:
        return

    added_roles = [role.name for role in after.roles if role not in before.roles]
    removed_roles = [role.name for role in before.roles if role not in after.roles]

    if not added_roles and not removed_roles:
        return

    fields = {}
    if added_roles:
        fields["Dodane role"] = ", ".join(added_roles)
    if removed_roles:
        fields["Usunięte role"] = ", ".join(removed_roles)

    await send_message_log_embed(
        after.guild,
        "Zmiana ról użytkownika",
        "Zaktualizowano role użytkownika.",
        discord.Color.blurple(),
        author=after,
        fields=fields,
    )


async def find_voice_audit_log_entry(
    member: discord.Member,
    action: discord.AuditLogAction,
    *,
    expected_channel: discord.abc.GuildChannel | None = None,
    expected_changes: dict[str, tuple[bool, bool]] | None = None,
    allow_missing_target: bool = False,
) -> discord.AuditLogEntry | None:
    async def matches(entry: discord.AuditLogEntry) -> bool:
        max_age = 5 if allow_missing_target else 30
        if entry.created_at < discord.utils.utcnow() - timedelta(seconds=max_age):
            return False
        if entry.target is None:
            if not allow_missing_target:
                return False
        elif entry.target.id != member.id:
            return False

        if expected_channel is not None:
            audit_channel = getattr(entry.extra, "channel", None)
            if audit_channel is None or audit_channel.id != expected_channel.id:
                return False

        if expected_changes is not None:
            before_changes = entry.changes.before
            after_changes = entry.changes.after
            if not any(
                hasattr(before_changes, name)
                and hasattr(after_changes, name)
                and getattr(before_changes, name) == old_value
                and getattr(after_changes, name) == new_value
                for name, (old_value, new_value) in expected_changes.items()
            ):
                return False

        return True

    if (
        action == discord.AuditLogAction.member_move
        and allow_missing_target
        and expected_channel is not None
    ):
        try:
            for attempt in range(8):
                candidates = {
                    entry.id: entry
                    for entry in recent_voice_audit_entries
                    if used_voice_move_audit_entry_ids.get(entry.id, 0)
                    < max(1, getattr(entry.extra, "count", 1))
                    and entry.action == action
                    and entry.guild.id == member.guild.id
                    and await matches(entry)
                }

                if attempt % 3 == 0:
                    async for entry in member.guild.audit_logs(limit=20, action=action):
                        if entry.created_at < discord.utils.utcnow() - timedelta(seconds=5):
                            break
                        if (
                            used_voice_move_audit_entry_ids.get(entry.id, 0)
                            < max(1, getattr(entry.extra, "count", 1))
                            and await matches(entry)
                        ):
                            candidates[entry.id] = entry

                if len(candidates) == 1 and attempt >= 1:
                    entry = next(iter(candidates.values()))
                    used_voice_move_audit_entry_ids[entry.id] = (
                        used_voice_move_audit_entry_ids.get(entry.id, 0) + 1
                    )
                    if len(used_voice_move_audit_entry_ids) > 500:
                        oldest_entry_id = min(used_voice_move_audit_entry_ids)
                        del used_voice_move_audit_entry_ids[oldest_entry_id]
                    if entry in recent_voice_audit_entries:
                        recent_voice_audit_entries.remove(entry)
                    return entry

                if attempt < 7:
                    await asyncio.sleep(0.75)
        except discord.Forbidden:
            logging.warning(
                "Brak uprawnień do odczytu dziennika audytu zmian głosowych użytkownika %s.",
                member.id,
            )
        except discord.HTTPException:
            logging.exception(
                "Nie udało się odczytać dziennika audytu zmian głosowych użytkownika %s.",
                member.id,
            )
        return None

    for entry in reversed(recent_voice_audit_entries):
        if (
            entry.action == action
            and entry.guild.id == member.guild.id
            and await matches(entry)
        ):
            recent_voice_audit_entries.remove(entry)
            return entry

    try:
        for attempt in range(8 if allow_missing_target else 1):
            for entry in reversed(recent_voice_audit_entries):
                if (
                    entry.action == action
                    and entry.guild.id == member.guild.id
                    and await matches(entry)
                ):
                    recent_voice_audit_entries.remove(entry)
                    return entry

            if attempt % 3 == 0:
                cutoff = discord.utils.utcnow() - timedelta(seconds=30)
                async for entry in member.guild.audit_logs(limit=20, action=action):
                    if entry.created_at < cutoff:
                        break
                    if await matches(entry):
                        return entry

            if allow_missing_target and attempt < 7:
                await asyncio.sleep(0.75)
    except discord.Forbidden:
        logging.warning(
            "Brak uprawnień do odczytu dziennika audytu zmian głosowych użytkownika %s.",
            member.id,
        )
    except discord.HTTPException:
        logging.exception(
            "Nie udało się odczytać dziennika audytu zmian głosowych użytkownika %s.",
            member.id,
        )

    return None


async def on_audit_log_entry_create(entry: discord.AuditLogEntry) -> None:
    if entry.action not in (
        discord.AuditLogAction.member_move,
        discord.AuditLogAction.member_disconnect,
        discord.AuditLogAction.member_update,
    ):
        return

    recent_voice_audit_entries.append(entry)
    del recent_voice_audit_entries[:-50]


async def send_voice_channel_log(
    member: discord.Member,
    actor: discord.abc.User | None,
    description: str,
    *,
    color: discord.Color,
) -> None:
    log_channel = get_message_channel(member.guild, MESSAGE_LOG_CHANNEL_ID)
    if log_channel is None:
        logging.warning(
            "Nie znaleziono kanału logów dla zmiany kanału głosowego użytkownika %s.",
            member.id,
        )
        return

    embed = discord.Embed(
        description=description,
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    if actor is None:
        embed.set_author(name="Unknown")
    else:
        embed.set_author(
            name=actor.display_name,
            icon_url=actor.display_avatar.url,
        )
    embed.set_footer(text=f"ID: {member.id}")
    await log_channel.send(embed=embed)


async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
    if (
        before.channel == after.channel
        and before.mute == after.mute
        and before.deaf == after.deaf
        and before.self_mute == after.self_mute
        and before.self_deaf == after.self_deaf
        and before.suppress == after.suppress
    ):
        return

    changes: dict[str, str] = {
        "Użytkownik": f"{member.mention} (`{member.id}`)",
    }
    title = "Zmiana stanu głosowego"
    description = "Zmieniono stan użytkownika na kanale głosowym."
    color = discord.Color.blue()

    if before.channel != after.channel:
        if before.channel is not None and after.channel is None:
            audit_entry = await find_voice_audit_log_entry(
                member,
                discord.AuditLogAction.member_disconnect,
            )
            if audit_entry is not None:
                actor = audit_entry.user
                description = (
                    f"{actor.mention} disconnected {member.mention} from voice channel "
                    f"{before.channel.mention}"
                    if actor is not None
                    else f"{member.mention} left voice channel {before.channel.mention}"
                )
            else:
                actor = member
                description = f"{member.mention} left voice channel {before.channel.mention}"
            await send_voice_channel_log(
                member,
                actor,
                description,
                color=discord.Color.from_rgb(149, 165, 166),
            )
            return
        elif after.channel is not None and before.channel is not None:
            audit_entry = await find_voice_audit_log_entry(
                member,
                discord.AuditLogAction.member_move,
                expected_channel=after.channel,
                allow_missing_target=True,
            )
            actor = (
                audit_entry.user
                if audit_entry is not None
                and audit_entry.user is not None
                and audit_entry.user.id != member.id
                else None
            )
            await send_voice_channel_log(
                member,
                actor,
                (
                    f"{actor.mention if actor is not None else 'Unknown'} moved "
                    f"{member.mention} from {before.channel.mention} to "
                    f"{after.channel.mention}"
                ),
                color=discord.Color.from_rgb(46, 204, 113),
            )
            return
        elif after.channel is not None:
            await send_voice_channel_log(
                member,
                member,
                f"{member.mention} joined voice channel {after.channel.mention}",
                color=discord.Color.from_rgb(46, 204, 113),
            )
            return

    if before.mute != after.mute:
        changes["Wyciszenie serwerowe"] = "Włączone" if after.mute else "Wyłączone"
    if before.deaf != after.deaf:
        changes["Głuchota serwerowa"] = "Włączona" if after.deaf else "Wyłączona"
    if before.self_mute != after.self_mute:
        changes["Wyciszenie własne"] = "Włączone" if after.self_mute else "Wyłączone"
    if before.self_deaf != after.self_deaf:
        changes["Głuchota własna"] = "Włączona" if after.self_deaf else "Wyłączona"
    if before.suppress != after.suppress:
        changes["Wyciszenie przez kanał"] = "Włączone" if after.suppress else "Wyłączone"
        changes["Wykonał zmianę wyciszenia przez kanał"] = (
            "Nieznany (brak wpisu w dzienniku audytu)"
        )

    expected_moderated_changes: dict[str, tuple[bool, bool]] = {}
    if before.mute != after.mute:
        expected_moderated_changes["mute"] = (before.mute, after.mute)
    if before.deaf != after.deaf:
        expected_moderated_changes["deaf"] = (before.deaf, after.deaf)
    if expected_moderated_changes:
        audit_entry = await find_voice_audit_log_entry(
            member,
            discord.AuditLogAction.member_update,
            expected_changes=expected_moderated_changes,
        )
        if audit_entry is not None and audit_entry.user is not None:
            changes["Wykonał zmianę wyciszenia/głuchoty"] = (
                f"{audit_entry.user.mention} (`{audit_entry.user.id}`)"
            )
            if audit_entry.reason:
                changes["Powód zmiany wyciszenia/głuchoty"] = audit_entry.reason
        else:
            changes["Wykonał zmianę wyciszenia/głuchoty"] = (
                "Nieznany (brak wpisu w dzienniku audytu)"
            )

    if before.self_mute != after.self_mute or before.self_deaf != after.self_deaf:
        changes["Wykonał zmianę własnego stanu"] = f"{member.mention} (`{member.id}`)"

    await send_message_log_embed(
        member.guild,
        title,
        description,
        color,
        fields=changes,
    )


async def on_member_ban(guild: discord.Guild, user: discord.User) -> None:
    await send_message_log_embed(
        guild,
        "Zablokowanie użytkownika",
        "Użytkownik został zablokowany na serwerze.",
        discord.Color.dark_red(),
        author=user,
    )


async def on_member_unban(guild: discord.Guild, user: discord.User) -> None:
    await send_message_log_embed(
        guild,
        "Odblokowanie użytkownika",
        "Blokada użytkownika została zdjęta.",
        discord.Color.green(),
        author=user,
    )


def create_client() -> discord.Client:
    new_client = discord.Client(intents=intents)
    tickets.register_ticket_views(new_client)
    new_client.event(on_ready)
    new_client.event(on_message)
    new_client.event(on_member_join)
    new_client.event(on_member_remove)
    new_client.event(on_message_edit)
    new_client.event(on_message_delete)
    new_client.event(on_raw_message_delete)
    new_client.event(on_raw_bulk_message_delete)
    new_client.event(on_member_update)
    new_client.event(on_voice_state_update)
    new_client.event(on_audit_log_entry_create)
    new_client.event(on_member_ban)
    new_client.event(on_member_unban)
    return new_client


async def discord_available() -> bool:
    """Sprawdza dostęp do API Discorda z krótkim timeoutem."""
    timeout = aiohttp.ClientTimeout(total=5)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://discord.com/api/v10/gateway") as response:
                return response.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
        logging.warning("Discord jest niedostępny: %s", error)
        return False


async def run_bot() -> None:
    global client

    retry_delay = 10

    while True:
        # Nie pozwalamy discord.py wisieć ~60-70 sekund na DNS,
        # gdy telefon nie ma Internetu.
        if not await discord_available():
            logging.warning(
                "Brak dostępu do Discorda. Ponawiam sprawdzenie za %s sekund...",
                retry_delay,
            )
            await asyncio.sleep(retry_delay)
            continue

        client = create_client()

        try:
            logging.info("Internet działa. Łączenie z Discordem...")
            await client.start(TOKEN, reconnect=True)

            logging.info(
                "Połączenie bota zostało zamknięte. Ponawiam za %s sekund...",
                retry_delay,
            )

        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as error:
            logging.warning(
                "Błąd połączenia z Discordem: %s. Ponawiam za %s sekund...",
                error,
                retry_delay,
            )

        except discord.LoginFailure:
            logging.critical(
                "Logowanie do Discorda nie powiodło się. Sprawdź, czy DISCORD_TOKEN "
                "w pliku .env jest aktualnym tokenem bota."
            )
            raise

        except asyncio.CancelledError:
            logging.info("Zatrzymywanie bota...")
            raise

        except Exception:
            logging.exception(
                "Nieoczekiwany błąd. Ponawiam za %s sekund...",
                retry_delay,
            )

        finally:
            channel_backup.stop_scheduled_backup()
            try:
                await client.close()
            except Exception:
                logging.debug("Błąd podczas zamykania klienta.", exc_info=True)

        await asyncio.sleep(retry_delay)


asyncio.run(run_bot())

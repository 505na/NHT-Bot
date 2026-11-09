import asyncio
import logging
import os
import subprocess
from datetime import timedelta

import aiohttp
import discord
from dotenv import load_dotenv

import channel_backup
import tickets

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
SYSTEMD_SERVICE_NAME = "nht-bot.service"

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


def update_bot() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", BOT_DIRECTORY, "pull", "--ff-only"],
        capture_output=True,
        text=True,
        check=False,
    )


def manage_systemd_service(action: str) -> subprocess.CompletedProcess[str]:
    if action not in {"start", "stop", "restart", "status"}:
        raise ValueError(f"Nieobsługiwana akcja systemctl: {action}")

    arguments = (
        ["status", SYSTEMD_SERVICE_NAME, "--no-pager"]
        if action == "status"
        else ["--no-block", action, SYSTEMD_SERVICE_NAME]
    )
    return subprocess.run(
        ["sudo", "-n", "/usr/bin/systemctl", *arguments],
        capture_output=True,
        text=True,
        timeout=15,
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


async def on_ready() -> None:
    logging.info("Zalogowano jako %s", client.user)
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
        await message.channel.send(
            "**Dostępne komendy:**\n"
            "`!rekru` — wysyła panel rekrutacyjny (wymaga roli rekrutera).\n"
            "`!backup [ID_kanału]` — wykonuje backup (wymaga uprawnień).\n"
            "`!restore` — przywraca brakujące kanały (wymaga uprawnień).\n"
            "`!test` — wysyła test na kanał testowy (tylko administrator bota).\n"
            "`!update` — aktualizuje bota i restartuje usługę (tylko administrator bota).\n"
            "`!systemctl status|start|stop|restart` — zarządza usługą "
            "(tylko administrator bota; wymaga konfiguracji systemd)."
        )
        return

    if await channel_backup.handle_command(
        client,
        message,
        AUTHORIZED_USER_ID,
    ):
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

    command_parts = message.content.strip().casefold().split()
    if command_parts and command_parts[0] == "!systemctl":
        if len(command_parts) != 2 or command_parts[1] not in {
            "start",
            "stop",
            "restart",
            "status",
        }:
            await message.channel.send(
                "Użycie: `!systemctl status|start|stop|restart`"
            )
            return

        action = command_parts[1]
        try:
            result = await asyncio.to_thread(manage_systemd_service, action)
        except (OSError, subprocess.TimeoutExpired):
            logging.exception("Nie udało się wykonać systemctl %s.", action)
            await message.channel.send(
                "Nie udało się wykonać systemctl. Sprawdź, czy bot działa jako "
                "usługa systemd oraz czy skonfigurowano uprawnienia sudo."
            )
            return

        output = (result.stdout + result.stderr).strip()
        if action == "status" and output:
            safe_output = output.replace("```", "'''")[:1500]
            await message.channel.send(
                f"```text\n{safe_output}\n```",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        elif result.returncode != 0:
            logging.error("systemctl %s nie powiódł się: %s", action, output)
            safe_output = output.replace("```", "'''")[:1200]
            lower_output = output.casefold()
            authorization_hint = ""
            if (
                "interactive authentication" in lower_output
                or "a password is required" in lower_output
                or "not allowed to execute" in lower_output
            ):
                authorization_hint = (
                    "\nBrak bezhasłowego uprawnienia sudo dla użytkownika usługi. "
                    "Sprawdź `User=` w pliku usługi i regułę sudoers w "
                    "`SYSTEMD.md`."
                )
            await message.channel.send(
                f"Polecenie `systemctl {action}` nie powiodło się."
                + authorization_hint
                + (f"\n```text\n{safe_output}\n```" if safe_output else ""),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await message.channel.send(
                f"Polecenie `systemctl {action}` zostało przyjęte dla "
                f"`{SYSTEMD_SERVICE_NAME}`."
            )
    elif message.content.strip() == "!test":
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
        try:
            restart_result = await asyncio.to_thread(
                manage_systemd_service,
                "restart",
            )
        except (OSError, subprocess.TimeoutExpired):
            logging.exception("Aktualizacja pobrana, ale restart usługi się nie udał.")
            await message.channel.send(
                "Aktualizacja została pobrana, ale restart usługi się nie powiódł. "
                "Uruchom `!systemctl restart`."
            )
            return

        if restart_result.returncode != 0:
            restart_output = (restart_result.stdout + restart_result.stderr).strip()
            logging.error(
                "Aktualizacja pobrana, ale restart usługi się nie udał: %s",
                restart_output,
            )
            await message.channel.send(
                "Aktualizacja została pobrana, ale restart usługi się nie powiódł. "
                "Uruchom `!systemctl restart`."
            )
            return

        await message.channel.send(
            "Bot został zaktualizowany. Restart usługi został zlecony."
        )


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

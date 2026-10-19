import asyncio
import html
import io
import json
import logging
import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import discord

RECRUITER_ROLE_IDS = (
    1477729438098788684,
    859133596925034513,
    864573950338924545,
    846065271580000341,
    846065271580000342,
    864505176291016734,
)
TICKET_STAFF_ROLE_IDS = (
    1477729438098788684,
    859133596925034513,
    864573950338924545,
    846065271580000341,
    846065271580000342,
)
ACCEPTED_ROLE_IDS = (
    898671794939125830,
    846065271570825241,
    898667104000155658,
)

QUESTIONS = (
    "Ile masz lat?",
    "Oceń swoje pvp 1-10.",
    "Twoja ostatnia gildia i dlaczego w niej nie jesteś?",
    "Dlaczego akurat my?",
    "Przyszedłeś do nas na konkretny serwer czy na wszystkie?",
    "Umiesz robić water skille tak zwane wiadra?",
    "Potrafisz wychodzić z fosy slimeblockiem/zwykłym blokiem np coblem?",
    "Ile czasu poświęciłbyś dziennie na gildię?",
    "Skąd wiesz o rekrutacji?",
    "Posiadasz premkę?",
    "Jesteś toxic?",
    "Potrafisz uszanować młodsze/starsze osoby?",
    "Jaki masz nick w mc?",
    "Opisz swoją osobowość.",
    "Czy potrafisz się zachować?",
    "Czy potrafisz przegrywać? (wyjebiesz sie na edycji i grasz dalej a nie ryk bo ranking i itemy zrobiły papa)",
)

OPEN_TICKET_CUSTOM_ID = "recruitment:open_ticket"
CLOSE_TICKET_CUSTOM_ID = "recruitment:close_ticket"
START_QUESTIONNAIRE_CUSTOM_ID = "recruitment:start_questionnaire"
TICKET_OWNER_TOPIC_PREFIX = "ticket-owner:"
MINECRAFT_NICKNAME_QUESTION = "Jaki masz nick w mc?"
TICKET_TRANSCRIPT_CHANNEL_ID = 1561373677671686285
STATE_FILE = Path(__file__).with_name("ticket_state.json")
TICKET_COUNTER_FILE = Path(__file__).with_name("ticket_counter.txt")
TICKET_STATUS_FILE = Path(__file__).with_name("ticket_status.json")


@dataclass
class Questionnaire:
    channel_id: int
    member_id: int
    answers: list[str] = field(default_factory=list)


def load_questionnaires() -> dict[int, Questionnaire]:
    if not STATE_FILE.exists():
        return {}

    try:
        raw_state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw_state, dict):
            raise ValueError("Zapisany stan ticketów musi być obiektem JSON.")
        questionnaires = {}
        for channel_id, data in raw_state.items():
            if not isinstance(data, dict):
                raise ValueError(f"Nieprawidłowy zapis ticketu {channel_id}.")
            answers = data["answers"]
            if (
                not isinstance(answers, list)
                or len(answers) > len(QUESTIONS)
                or not all(isinstance(answer, str) for answer in answers)
            ):
                raise ValueError(f"Nieprawidłowe odpowiedzi ticketu {channel_id}.")
            questionnaire = Questionnaire(
                channel_id=int(channel_id),
                member_id=int(data["member_id"]),
                answers=answers,
            )
            questionnaires[questionnaire.channel_id] = questionnaire
        return questionnaires
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        logging.exception("Nie udało się wczytać zapisanych formularzy ticketów.")
        raise


ACTIVE_QUESTIONNAIRES = load_questionnaires()


def load_ticket_status() -> tuple[set[int], set[int]]:
    if not TICKET_STATUS_FILE.exists():
        return set(), set()

    try:
        state = json.loads(TICKET_STATUS_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("Zapisany stan ticketów musi być obiektem JSON.")
        closed_ids = state.get("closed", [])
        transcript_ids = state.get("transcript_sent", [])
        if not all(
            isinstance(ids, list)
            and all(isinstance(ticket_id, int) for ticket_id in ids)
            for ids in (closed_ids, transcript_ids)
        ):
            raise ValueError("Nieprawidłowy zapis statusów ticketów.")
        return set(closed_ids), set(transcript_ids)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        logging.exception("Nie udało się wczytać statusów ticketów.")
        raise


CLOSED_TICKETS, TRANSCRIPTED_TICKETS = load_ticket_status()


def save_questionnaires() -> None:
    state = {
        str(channel_id): {
            "member_id": questionnaire.member_id,
            "answers": questionnaire.answers,
        }
        for channel_id, questionnaire in ACTIVE_QUESTIONNAIRES.items()
    }
    temporary_file = STATE_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary_file, STATE_FILE)


def save_ticket_status() -> None:
    state = {
        "closed": sorted(CLOSED_TICKETS),
        "transcript_sent": sorted(TRANSCRIPTED_TICKETS),
    }
    temporary_file = TICKET_STATUS_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary_file, TICKET_STATUS_FILE)


def get_ticket_owner_id(channel: discord.TextChannel) -> int | None:
    owner_id = (channel.topic or "").removeprefix(TICKET_OWNER_TOPIC_PREFIX)
    return int(owner_id) if owner_id.isdecimal() else None


async def reject_ticket_owner_interaction(
    interaction: discord.Interaction,
    channel: discord.TextChannel,
) -> bool:
    if interaction.user.id != get_ticket_owner_id(channel):
        return False

    await interaction.response.send_message(
        "Autor ticketu nie może używać tych przycisków.",
        ephemeral=True,
    )
    return True


def reserve_ticket_number(guild: discord.Guild) -> int:
    last_number = 0
    if TICKET_COUNTER_FILE.exists():
        try:
            last_number = int(TICKET_COUNTER_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logging.exception("Nie udało się odczytać licznika ticketów.")
            raise

    for channel in guild.text_channels:
        match = re.match(r"^ticket-(\d+)-", channel.name)
        if match:
            last_number = max(last_number, int(match.group(1)))

    ticket_number = last_number + 1
    temporary_file = TICKET_COUNTER_FILE.with_suffix(".tmp")
    try:
        temporary_file.write_text(str(ticket_number), encoding="utf-8")
        os.replace(temporary_file, TICKET_COUNTER_FILE)
    except OSError:
        logging.exception("Nie udało się zapisać licznika ticketów.")
        raise

    return ticket_number


def get_ticket_name_slug(member: discord.Member) -> str:
    username = member.name.translate(str.maketrans({"ł": "l", "Ł": "L"}))
    normalized_name = unicodedata.normalize("NFKD", username)
    ascii_name = normalized_name.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9-]", "-", ascii_name.casefold())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:80].rstrip("-") or str(member.id)


class OpenTicketView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Otwórz ticket rekrutacyjny",
        style=discord.ButtonStyle.primary,
        custom_id=OPEN_TICKET_CUSTOM_ID,
    )
    async def open_ticket(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Ten przycisk działa tylko na serwerze.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        member = interaction.user
        owner_topic = f"{TICKET_OWNER_TOPIC_PREFIX}{member.id}"
        existing_ticket = discord.utils.get(
            guild.text_channels,
            topic=owner_topic,
        )
        if existing_ticket is not None:
            if existing_ticket.id in CLOSED_TICKETS:
                await interaction.followup.send(
                    f"Twój ticket jest zamknięty: {existing_ticket.mention}. "
                    "Skontaktuj się z obsługą, aby go ponownie otworzyć.",
                    ephemeral=True,
                )
                return

            questionnaire = ACTIVE_QUESTIONNAIRES.get(existing_ticket.id)
            if questionnaire is None:
                questionnaire = Questionnaire(
                    channel_id=existing_ticket.id,
                    member_id=member.id,
                )
                ACTIVE_QUESTIONNAIRES[existing_ticket.id] = questionnaire
                try:
                    save_questionnaires()
                except OSError:
                    ACTIVE_QUESTIONNAIRES.pop(existing_ticket.id, None)
                    logging.exception(
                        "Nie udało się zapisać wznowionego formularza %s.",
                        existing_ticket.id,
                    )
                    await interaction.followup.send(
                        "Nie udało się wznowić formularza. Spróbuj ponownie później.",
                        ephemeral=True,
                    )
                    return

            if len(questionnaire.answers) == len(QUESTIONS):
                try:
                    await send_answer_embeds(
                        existing_ticket,
                        member,
                        questionnaire.answers,
                    )
                    ACTIVE_QUESTIONNAIRES.pop(existing_ticket.id, None)
                    save_questionnaires()
                except discord.HTTPException:
                    logging.exception(
                        "Nie udało się wznowić podsumowania ticketu %s.",
                        existing_ticket.id,
                    )
                    await interaction.followup.send(
                        "Nie udało się wysłać podsumowania. Spróbuj ponownie.",
                        ephemeral=True,
                    )
                    return
            else:
                await interaction.followup.send(
                    f"Wznawiam formularz w tickecie {existing_ticket.mention}. "
                    "Kliknij tam przycisk „Odpowiedz na pytania”, aby kontynuować prywatnie.",
                    ephemeral=True,
                )
            return

        bot_member = guild.me
        if bot_member is None:
            bot_user = interaction.client.user
            bot_member = guild.get_member(bot_user.id) if bot_user is not None else None
        if bot_member is None:
            logging.error("Nie można znaleźć członka bota na serwerze %s.", guild.id)
            await interaction.followup.send(
                "Nie udało się utworzyć ticketu. Spróbuj ponownie później.",
                ephemeral=True,
            )
            return

        overwrites: dict[
            discord.Role | discord.Member, discord.PermissionOverwrite
        ] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            ),
            bot_member: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
            ),
        }
        for role_id in TICKET_STAFF_ROLE_IDS:
            role = guild.get_role(role_id)
            if role is None:
                logging.warning(
                    "Nie znaleziono roli obsługi ticketów %s na serwerze %s.",
                    role_id,
                    guild.id,
                )
                continue
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )

        try:
            ticket_number = reserve_ticket_number(guild)
        except (OSError, ValueError):
            await interaction.followup.send(
                "Nie udało się przygotować numeru ticketu. Spróbuj ponownie później.",
                ephemeral=True,
            )
            return

        channel_name = f"ticket-{ticket_number:02d}-{get_ticket_name_slug(member)}"
        ticket_channel: discord.TextChannel | None = None
        try:
            ticket_channel = await guild.create_text_channel(
                name=channel_name,
                topic=owner_topic,
                overwrites=overwrites,
                position=0,
                reason=f"Ticket rekrutacyjny użytkownika {member} ({member.id})",
            )
            await ticket_channel.send(
                f"Ticket użytkownika {member.mention}",
                view=CloseTicketView(),
            )
        except discord.HTTPException:
            logging.exception(
                "Nie udało się utworzyć lub przygotować ticketu dla użytkownika %s.",
                member.id,
            )
            if ticket_channel is not None:
                try:
                    await ticket_channel.delete(
                        reason="Nie udało się przygotować ticketu rekrutacyjnego"
                    )
                except discord.HTTPException:
                    logging.exception(
                        "Nie udało się usunąć nieprzygotowanego ticketu %s.",
                        ticket_channel.id,
                    )
            await interaction.followup.send(
                "Nie udało się utworzyć ticketu ani wysłać wiadomości powitalnej. "
                "Sprawdź uprawnienia bota.",
                ephemeral=True,
            )
            return

        ACTIVE_QUESTIONNAIRES[ticket_channel.id] = Questionnaire(
            channel_id=ticket_channel.id,
            member_id=member.id,
        )
        try:
            save_questionnaires()
        except OSError:
            ACTIVE_QUESTIONNAIRES.pop(ticket_channel.id, None)
            logging.exception(
                "Nie udało się zapisać stanu nowego ticketu %s.",
                ticket_channel.id,
            )
            try:
                await ticket_channel.delete(
                    reason="Nie udało się zapisać stanu formularza rekrutacyjnego"
                )
            except discord.HTTPException:
                logging.exception(
                    "Nie udało się usunąć ticketu %s po błędzie zapisu.",
                    ticket_channel.id,
                )
            await interaction.followup.send(
                "Nie udało się przygotować formularza. Spróbuj ponownie później.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Utworzono ticket: {ticket_channel.mention}. "
            "Kliknij w nim przycisk „Odpowiedz na pytania”, aby rozpocząć prywatny formularz.",
            ephemeral=True,
        )


class CloseTicketView(discord.ui.View):
    def __init__(self, *, include_questionnaire: bool = True) -> None:
        super().__init__(timeout=None)
        if not include_questionnaire:
            self.remove_item(
                next(
                    item
                    for item in self.children
                    if getattr(item, "custom_id", None)
                    == START_QUESTIONNAIRE_CUSTOM_ID
                )
            )

    @discord.ui.button(
        label="Odpowiedz na pytania",
        style=discord.ButtonStyle.primary,
        custom_id=START_QUESTIONNAIRE_CUSTOM_ID,
    )
    async def start_questionnaire(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or not (
            channel.topic or ""
        ).startswith(TICKET_OWNER_TOPIC_PREFIX):
            await interaction.response.send_message(
                "Nie można otworzyć formularza w tym kanale.",
                ephemeral=True,
            )
            return

        if interaction.user.id != get_ticket_owner_id(channel):
            await interaction.response.send_message(
                "Tylko autor ticketu może odpowiadać na pytania.",
                ephemeral=True,
            )
            return

        questionnaire = ACTIVE_QUESTIONNAIRES.get(channel.id)
        if questionnaire is None:
            await interaction.response.send_message(
                "Formularz został już zakończony albo nie jest dostępny.",
                ephemeral=True,
            )
            return
        if channel.id in CLOSED_TICKETS:
            await interaction.response.send_message(
                "Ticket jest zamknięty; nie możesz dalej korzystać z formularza.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await send_private_question(interaction, channel.id)

    @discord.ui.button(
        label="Zamknij ticket",
        style=discord.ButtonStyle.danger,
        custom_id=CLOSE_TICKET_CUSTOM_ID,
    )
    async def close_ticket(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or not (
            channel.topic or ""
        ).startswith(TICKET_OWNER_TOPIC_PREFIX):
            await interaction.response.send_message(
                "Nie można zamknąć tego kanału jako ticketu.",
                ephemeral=True,
            )
            return

        if await reject_ticket_owner_interaction(interaction, channel):
            return

        await interaction.response.send_message(
            "ZAMKNIJ ALE NIE ZAPOMNIJ ZROBIĆ TRANSKRYPCJI",
            view=ConfirmCloseTicketView(channel.id),
        )


class ConfirmCloseTicketView(discord.ui.View):
    def __init__(self, channel_id: int) -> None:
        super().__init__(timeout=180)
        self.channel_id = channel_id

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger)
    async def confirm_close(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        channel = guild.get_channel(self.channel_id) if guild is not None else None
        if not isinstance(channel, discord.TextChannel) or not (
            channel.topic or ""
        ).startswith(TICKET_OWNER_TOPIC_PREFIX):
            await interaction.response.send_message(
                "Ten ticket nie jest już dostępny.",
                ephemeral=True,
            )
            return

        if await reject_ticket_owner_interaction(interaction, channel):
            return

        try:
            owner_id_text = (channel.topic or "").removeprefix(
                TICKET_OWNER_TOPIC_PREFIX
            )
            if owner_id_text.isdecimal():
                owner = channel.guild.get_member(int(owner_id_text))
                if owner is not None:
                    await channel.set_permissions(
                        owner,
                        view_channel=True,
                        send_messages=False,
                        read_message_history=True,
                    )
            CLOSED_TICKETS.add(channel.id)
            save_ticket_status()
        except (discord.HTTPException, OSError):
            CLOSED_TICKETS.discard(channel.id)
            owner_id_text = (channel.topic or "").removeprefix(
                TICKET_OWNER_TOPIC_PREFIX
            )
            if owner_id_text.isdecimal():
                owner = channel.guild.get_member(int(owner_id_text))
                if owner is not None:
                    try:
                        await channel.set_permissions(
                            owner,
                            view_channel=True,
                            send_messages=True,
                            read_message_history=True,
                            attach_files=True,
                            embed_links=True,
                        )
                    except discord.HTTPException:
                        logging.exception(
                            "Nie udało się przywrócić uprawnień autora ticketu %s.",
                            channel.id,
                        )
            logging.exception(
                "Nie udało się zamknąć ticketu %s.",
                channel.id,
            )
            await interaction.response.send_message(
                "Nie udało się zamknąć ticketu. Sprawdź uprawnienia bota i spróbuj ponownie.",
                ephemeral=True,
            )
            return

        closed_embed = discord.Embed(
            description=f"Ticket Closed by {interaction.user.mention}",
            color=discord.Color.yellow(),
        )
        try:
            await interaction.response.edit_message(
                content="NAJPIERW TRANSCRIPT A POTEM DELETE",
                embed=closed_embed,
                view=ClosedTicketView(),
            )
        except discord.HTTPException:
            logging.exception(
                "Ticket %s został zamknięty, ale nie udało się wyświetlić przycisków.",
                channel.id,
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_close(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        channel = interaction.channel
        if (
            isinstance(channel, discord.TextChannel)
            and await reject_ticket_owner_interaction(interaction, channel)
        ):
            return
        await interaction.response.edit_message(
            content="Zamykanie ticketu anulowane.",
            view=None,
        )


class ClosedTicketView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Przyjęty",
        style=discord.ButtonStyle.success,
        custom_id="recruitment:closed:accepted",
    )
    async def accept_applicant(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or (
            channel.id not in CLOSED_TICKETS
        ):
            await interaction.response.send_message(
                "Ten ticket nie jest zamknięty.",
                ephemeral=True,
            )
            return
        if await reject_ticket_owner_interaction(interaction, channel):
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "Nie udało się ustalić użytkownika serwera.",
                ephemeral=True,
            )
            return
        if not (
            member.guild_permissions.administrator
            or any(role.id in TICKET_STAFF_ROLE_IDS for role in member.roles)
        ):
            await interaction.response.send_message(
                "Nie masz uprawnień do zatwierdzania kandydatów.",
                ephemeral=True,
            )
            return

        owner_id = get_ticket_owner_id(channel)
        owner = channel.guild.get_member(owner_id) if owner_id is not None else None
        if owner is None and owner_id is not None:
            try:
                owner = await channel.guild.fetch_member(owner_id)
            except discord.NotFound:
                await interaction.response.send_message(
                    "Nie znaleziono autora ticketu na serwerze.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException:
                logging.exception(
                    "Nie udało się pobrać autora ticketu %s.",
                    channel.id,
                )
                await interaction.response.send_message(
                    "Nie udało się pobrać autora ticketu. Spróbuj ponownie.",
                    ephemeral=True,
                )
                return
        if owner is None:
            await interaction.response.send_message(
                "Nie udało się ustalić autora ticketu.",
                ephemeral=True,
            )
            return

        roles = [channel.guild.get_role(role_id) for role_id in ACCEPTED_ROLE_IDS]
        missing_roles = [
            str(role_id)
            for role_id, role in zip(ACCEPTED_ROLE_IDS, roles)
            if role is None
        ]
        if missing_roles:
            logging.error(
                "Brak ról przyjęcia %s na serwerze %s.",
                ", ".join(missing_roles),
                channel.guild.id,
            )
            await interaction.response.send_message(
                "Nie udało się znaleźć wszystkich wymaganych rang. "
                "Powiadom administratora.",
                ephemeral=True,
            )
            return

        try:
            await owner.add_roles(
                *(role for role in roles if role is not None),
                reason=f"Kandydat przyjęty przez {member} ({member.id})",
            )
        except discord.Forbidden:
            logging.exception(
                "Bot nie może nadać ról przyjęcia użytkownikowi %s.",
                owner.id,
            )
            await interaction.response.send_message(
                "Bot nie ma uprawnień do nadania rang. Sprawdź uprawnienia "
                "i hierarchię ról bota.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            logging.exception(
                "Nie udało się nadać ról przyjęcia użytkownikowi %s.",
                owner.id,
            )
            await interaction.response.send_message(
                "Nie udało się nadać rang. Spróbuj ponownie później.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"{owner.mention} został przyjęty. Nadano wskazane role.",
            allowed_mentions=discord.AllowedMentions(users=True),
        )

    @discord.ui.button(
        label="Transcript",
        style=discord.ButtonStyle.secondary,
        custom_id="recruitment:closed:transcript",
        emoji="📄",
    )
    async def transcript(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or (
            channel.id not in CLOSED_TICKETS
        ):
            await interaction.response.send_message(
                "Ten ticket nie jest zamknięty.",
                ephemeral=True,
            )
            return

        if await reject_ticket_owner_interaction(interaction, channel):
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await send_ticket_transcript(
                interaction.client,
                channel,
                interaction.user,
            )
            TRANSCRIPTED_TICKETS.add(channel.id)
            save_ticket_status()
        except (discord.HTTPException, OSError, ValueError):
            TRANSCRIPTED_TICKETS.discard(channel.id)
            logging.exception(
                "Nie udało się wysłać transkryptu ticketu %s.",
                channel.id,
            )
            await interaction.followup.send(
                "Nie udało się wysłać transkryptu. Ticket nadal jest zamknięty; "
                "sprawdź uprawnienia bota i spróbuj ponownie.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            "Transkrypt wysłano. Możesz teraz usunąć ticket.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Open",
        style=discord.ButtonStyle.secondary,
        custom_id="recruitment:closed:open",
        emoji="🔓",
    )
    async def reopen(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Nie znaleziono kanału ticketu.",
                ephemeral=True,
            )
            return

        if await reject_ticket_owner_interaction(interaction, channel):
            return

        owner_id_text = (channel.topic or "").removeprefix(
            TICKET_OWNER_TOPIC_PREFIX
        )
        owner = (
            channel.guild.get_member(int(owner_id_text))
            if owner_id_text.isdecimal()
            else None
        )
        if owner is None:
            await interaction.response.send_message(
                "Nie udało się znaleźć autora ticketu.",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        was_closed = channel.id in CLOSED_TICKETS
        had_transcript = channel.id in TRANSCRIPTED_TICKETS
        try:
            CLOSED_TICKETS.discard(channel.id)
            TRANSCRIPTED_TICKETS.discard(channel.id)
            save_ticket_status()
            await channel.set_permissions(
                owner,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )
            await interaction.message.edit(
                content=f"Ticket ponownie otwarty przez {interaction.user.mention}.",
                embed=None,
                view=CloseTicketView(),
            )
        except (discord.HTTPException, OSError):
            if was_closed:
                CLOSED_TICKETS.add(channel.id)
            if had_transcript:
                TRANSCRIPTED_TICKETS.add(channel.id)
            try:
                save_ticket_status()
            except OSError:
                logging.exception(
                    "Nie udało się przywrócić stanu zamkniętego ticketu %s.",
                    channel.id,
                )
            logging.exception("Nie udało się ponownie otworzyć ticketu %s.", channel.id)
            await interaction.followup.send(
                "Nie udało się ponownie otworzyć ticketu. Sprawdź uprawnienia bota.",
                ephemeral=True,
            )

    @discord.ui.button(
        label="Delete",
        style=discord.ButtonStyle.danger,
        custom_id="recruitment:closed:delete",
        emoji="⛔",
    )
    async def delete(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or (
            channel.id not in CLOSED_TICKETS
        ):
            await interaction.response.send_message(
                "Ten ticket nie jest zamknięty.",
                ephemeral=True,
            )
            return
        if await reject_ticket_owner_interaction(interaction, channel):
            return
        if channel.id not in TRANSCRIPTED_TICKETS:
            await interaction.response.send_message(
                "Najpierw kliknij Transcript, aby zapisać historię ticketu.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await channel.delete(reason=f"Ticket usunięty przez {interaction.user}")
        except discord.HTTPException:
            logging.exception("Nie udało się usunąć ticketu %s.", channel.id)
            await interaction.followup.send(
                "Nie udało się usunąć ticketu. Sprawdź uprawnienia bota.",
                ephemeral=True,
            )
            return

        ACTIVE_QUESTIONNAIRES.pop(channel.id, None)
        CLOSED_TICKETS.discard(channel.id)
        TRANSCRIPTED_TICKETS.discard(channel.id)
        try:
            save_questionnaires()
            save_ticket_status()
        except OSError:
            logging.exception(
                "Nie udało się wyczyścić stanu usuniętego ticketu %s.",
                channel.id,
            )

async def send_ticket_transcript(
    client: discord.Client,
    channel: discord.TextChannel,
    closed_by: discord.abc.User,
) -> None:
    transcript_channel = client.get_channel(TICKET_TRANSCRIPT_CHANNEL_ID)
    if transcript_channel is None:
        transcript_channel = await client.fetch_channel(
            TICKET_TRANSCRIPT_CHANNEL_ID
        )
    if not isinstance(transcript_channel, discord.TextChannel):
        raise ValueError(
            f"Kanał transkryptów {TICKET_TRANSCRIPT_CHANNEL_ID} "
            "nie jest kanałem tekstowym."
        )

    transcript_lines = [
        f"Transkrypt ticketu #{channel.name}",
        f"ID kanału: {channel.id}",
        f"Serwer: {channel.guild.name} ({channel.guild.id})",
        f"Utworzono: {channel.created_at.isoformat()}",
        "",
    ]
    html_messages = []
    author_counts: Counter[tuple[int, str]] = Counter()
    message_count = 0
    attachment_count = 0
    async for message in channel.history(limit=None, oldest_first=True):
        message_count += 1
        timestamp = message.created_at.isoformat()
        author = f"{message.author} ({message.author.id})"
        author_counts[(message.author.id, str(message.author))] += 1
        transcript_lines.append(f"[{timestamp}] {author}")
        transcript_lines.append(message.content or "(brak treści tekstowej)")

        html_parts = [
            f"<article><header><time>{html.escape(timestamp)}</time> "
            f"<strong>{html.escape(author)}</strong></header>"
        ]
        if message.content:
            escaped_content = html.escape(message.content).replace("\n", "<br>")
            html_parts.append(
                f"<p>{escaped_content}</p>"
            )
        else:
            html_parts.append("<p><em>(brak treści tekstowej)</em></p>")

        for attachment in message.attachments:
            attachment_count += 1
            transcript_lines.append(
                f"Załącznik: {attachment.filename} — {attachment.url}"
            )
            attachment_name = html.escape(attachment.filename)
            attachment_url = html.escape(attachment.url, quote=True)
            html_parts.append(
                f'<p>Załącznik: <a href="{attachment_url}">{attachment_name}</a></p>'
            )
        for embed_index, embed in enumerate(message.embeds, start=1):
            embed_data = json.dumps(embed.to_dict(), ensure_ascii=False, indent=2)
            transcript_lines.append(
                f"Embed {embed_index}: {embed_data}"
            )
            html_parts.append(
                f"<details><summary>Embed {embed_index}</summary>"
                f"<pre>{html.escape(embed_data)}</pre></details>"
            )
        transcript_lines.append("")
        html_parts.append("</article>")
        html_messages.append("\n".join(html_parts))

    transcript = "\n".join(transcript_lines).encode("utf-8")
    filename_base = f"transcript-{channel.id}"
    html_transcript = f"""<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Transkrypt ticketu #{html.escape(channel.name)}</title>
<style>
body {{ background: #313338; color: #dbdee1; font: 15px Arial, sans-serif; margin: 0 auto; max-width: 1000px; padding: 24px; }}
h1 {{ border-bottom: 1px solid #4e5058; padding-bottom: 12px; }}
.metadata, article {{ background: #2b2d31; border-radius: 8px; margin: 12px 0; padding: 14px; }}
article header {{ color: #b5bac1; margin-bottom: 8px; }}
article p {{ line-height: 1.5; overflow-wrap: anywhere; white-space: normal; }}
a {{ color: #00a8fc; }}
pre {{ overflow-x: auto; white-space: pre-wrap; overflow-wrap: anywhere; }}
</style>
</head>
<body>
<h1>Transkrypt ticketu #{html.escape(channel.name)}</h1>
<section class="metadata">
<p><strong>ID kanału:</strong> {channel.id}</p>
<p><strong>Serwer:</strong> {html.escape(channel.guild.name)} ({channel.guild.id})</p>
<p><strong>Utworzono:</strong> {html.escape(channel.created_at.isoformat())}</p>
</section>
{''.join(html_messages)}
</body>
</html>"""
    owner_id_text = (channel.topic or "").removeprefix(TICKET_OWNER_TOPIC_PREFIX)
    owner_id = int(owner_id_text) if owner_id_text.isdecimal() else None
    owner = channel.guild.get_member(owner_id) if owner_id is not None else None
    owner_name = str(owner) if owner is not None else (
        f"Użytkownik {owner_id}" if owner_id is not None else "Nieznany"
    )

    transcript_message = await transcript_channel.send(
        files=[
            discord.File(
                io.BytesIO(transcript),
                filename=f"{filename_base}.txt",
            ),
            discord.File(
                io.BytesIO(html_transcript.encode("utf-8")),
                filename=f"{filename_base}.html",
            ),
        ],
    )

    html_attachment = next(
        (
            attachment
            for attachment in transcript_message.attachments
            if attachment.filename.endswith(".html")
        ),
        None,
    )
    detail_embed = discord.Embed(
        description=f"**{owner_name}**",
        color=discord.Color.green(),
    )
    detail_embed.add_field(
        name="Ticket Owner",
        value=f"<@{owner_id}>" if owner_id is not None else owner_name,
        inline=True,
    )
    detail_embed.add_field(name="Ticket Name", value=channel.name, inline=True)
    detail_embed.add_field(name="Panel Name", value="REKRUTACJA GILDIE", inline=True)
    users_in_transcript = "\n".join(
        f"{count} - <@{user_id}> - {username}"
        for (user_id, username), count in author_counts.most_common(10)
    ) or "Brak wiadomości"
    detail_embed.add_field(
        name="Users in transcript",
        value=users_in_transcript[:1024],
        inline=False,
    )
    direct_link_view = None
    if html_attachment is not None:
        direct_link_view = discord.ui.View()
        direct_link_view.add_item(
            discord.ui.Button(
                label="Direct Link",
                style=discord.ButtonStyle.link,
                url=html_attachment.url,
                emoji="📎",
            )
        )
    try:
        await transcript_channel.send(embed=detail_embed, view=direct_link_view)
    except discord.HTTPException:
        logging.exception(
            "Transkrypt został wysłany, ale nie udało się wysłać szczegółów ticketu %s.",
            channel.id,
        )

    log_embed = discord.Embed(
        description=(
            f"**Logged Info**\n"
            f"Ticket: {channel.name}\n"
            f"Action: Deleted\n\n"
            f"**Panel**\nREKRUTACJA GILDIE\n\n"
            f"Zamknął: {closed_by} ({closed_by.id})"
        ),
        color=discord.Color.red(),
    )
    try:
        await transcript_channel.send(embed=log_embed)
    except discord.HTTPException:
        logging.exception(
            "Transkrypt został wysłany, ale nie udało się wysłać logu zamknięcia ticketu %s.",
            channel.id,
        )


async def send_private_question(
    interaction: discord.Interaction,
    channel_id: int,
) -> None:
    questionnaire = ACTIVE_QUESTIONNAIRES[channel_id]
    question_index = len(questionnaire.answers)
    if question_index >= len(QUESTIONS):
        await interaction.followup.send(
            "Formularz jest już w trakcie kończenia. Użyj ponownie panelu, jeśli podsumowanie nie pojawi się.",
            ephemeral=True,
        )
        return
    question = QUESTIONS[question_index]
    embed = discord.Embed(
        title=f"Pytanie {question_index + 1}/{len(QUESTIONS)}",
        description=question,
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Ta wiadomość jest widoczna tylko dla Ciebie.")
    view = AnswerQuestionView(
        channel_id,
        questionnaire.member_id,
        question_index,
    )
    interaction.client.add_view(view)
    channel = interaction.client.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        try:
            channel = await interaction.client.fetch_channel(channel_id)
        except discord.HTTPException:
            logging.exception(
                "Nie udało się pobrać kanału ticketu %s, aby wysłać pytanie.",
                channel_id,
            )
            await interaction.followup.send(
                "Nie udało się wysłać pytania w tickecie. Spróbuj ponownie później.",
                ephemeral=True,
            )
            return

    if not isinstance(channel, discord.TextChannel):
        logging.error(
            "Kanał %s nie jest kanałem tekstowym; nie można wysłać pytania ticketu.",
            channel_id,
        )
        await interaction.followup.send(
            "Nie udało się wysłać pytania w tickecie. Spróbuj ponownie później.",
            ephemeral=True,
        )
        return

    if interaction.response.is_done():
        await interaction.followup.send(
            embed=embed,
            view=view,
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            embed=embed,
            view=view,
            ephemeral=True,
        )


async def ticket_channel_exists(
    client: discord.Client,
    questionnaire: Questionnaire,
    guild_id: int,
) -> bool:
    try:
        channel = await client.fetch_channel(questionnaire.channel_id)
    except discord.NotFound:
        ACTIVE_QUESTIONNAIRES.pop(questionnaire.channel_id, None)
        try:
            save_questionnaires()
        except OSError:
            logging.exception(
                "Nie udało się usunąć stanu usuniętego ticketu %s.",
                questionnaire.channel_id,
            )
        return False
    except discord.HTTPException:
        logging.exception(
            "Nie udało się sprawdzić kanału ticketu %s.",
            questionnaire.channel_id,
        )
        raise

    if (
        isinstance(channel, discord.TextChannel)
        and channel.guild.id == guild_id
        and channel.topic
        == f"{TICKET_OWNER_TOPIC_PREFIX}{questionnaire.member_id}"
    ):
        return True

    ACTIVE_QUESTIONNAIRES.pop(questionnaire.channel_id, None)
    try:
        save_questionnaires()
    except OSError:
        logging.exception(
            "Nie udało się usunąć nieprawidłowego stanu ticketu %s.",
            questionnaire.channel_id,
        )
    return False


async def on_guild_channel_delete(channel: discord.abc.GuildChannel) -> None:
    changed = ACTIVE_QUESTIONNAIRES.pop(channel.id, None) is not None
    if channel.id in CLOSED_TICKETS:
        CLOSED_TICKETS.discard(channel.id)
        changed = True
    if channel.id in TRANSCRIPTED_TICKETS:
        TRANSCRIPTED_TICKETS.discard(channel.id)
        changed = True
    if changed:
        try:
            save_questionnaires()
            save_ticket_status()
        except OSError:
            logging.exception(
                "Nie udało się usunąć stanu usuniętego ticketu %s.",
                channel.id,
            )


class AnswerQuestionView(discord.ui.View):
    def __init__(
        self,
        channel_id: int,
        member_id: int,
        question_index: int,
    ) -> None:
        super().__init__(timeout=None)
        self.channel_id = channel_id
        self.member_id = member_id
        self.question_index = question_index
        button = discord.ui.Button(
            label="Odpowiedz",
            style=discord.ButtonStyle.primary,
            custom_id=(
                f"recruitment:answer:{channel_id}:{member_id}:{question_index}"
            ),
        )
        button.callback = self.answer_question
        self.add_item(button)

    async def answer_question(self, interaction: discord.Interaction) -> None:
        questionnaire = ACTIVE_QUESTIONNAIRES.get(self.channel_id)
        if questionnaire is None or interaction.user.id != self.member_id:
            await interaction.response.send_message(
                "To pytanie nie jest już dostępne.",
                ephemeral=True,
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "Ticket nie jest już dostępny.",
                ephemeral=True,
            )
            return
        try:
            channel_exists = await ticket_channel_exists(
                interaction.client,
                questionnaire,
                interaction.guild.id,
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                "Nie udało się sprawdzić ticketu. Spróbuj ponownie później.",
                ephemeral=True,
            )
            return
        if not channel_exists:
            await interaction.response.send_message(
                "Ten ticket został usunięty — nie można już odpowiadać na jego pytania.",
                ephemeral=True,
            )
            return

        if len(questionnaire.answers) != self.question_index:
            await interaction.response.send_message(
                "To pytanie jest już nieaktualne. Otwórz ponownie panel rekrutacyjny, aby wznowić formularz.",
                ephemeral=True,
            )
            return
        if self.channel_id in CLOSED_TICKETS:
            await interaction.response.send_message(
                "Ticket jest zamknięty; nie możesz dalej korzystać z formularza.",
                ephemeral=True,
            )
            return

        question = QUESTIONS[len(questionnaire.answers)]
        await interaction.response.send_modal(
            AnswerQuestionModal(
                self.channel_id,
                self.member_id,
                self.question_index,
                question,
            )
        )


class AnswerQuestionModal(discord.ui.Modal):
    def __init__(
        self,
        channel_id: int,
        member_id: int,
        question_index: int,
        question: str,
    ) -> None:
        super().__init__(title=question[:45])
        self.channel_id = channel_id
        self.member_id = member_id
        self.question_index = question_index
        self.answer = discord.ui.TextInput(
            label="Twoja odpowiedź",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=4000,
        )
        self.add_item(self.answer)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        questionnaire = ACTIVE_QUESTIONNAIRES.get(self.channel_id)
        if questionnaire is None or interaction.user.id != self.member_id:
            await interaction.response.send_message(
                "Nie udało się zapisać odpowiedzi. Otwórz ponownie ticket.",
                ephemeral=True,
            )
            return

        if interaction.guild is None:
            await interaction.response.send_message(
                "Ticket nie jest już dostępny.",
                ephemeral=True,
            )
            return
        try:
            channel_exists = await ticket_channel_exists(
                interaction.client,
                questionnaire,
                interaction.guild.id,
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                "Nie udało się sprawdzić ticketu. Spróbuj ponownie później.",
                ephemeral=True,
            )
            return
        if not channel_exists:
            await interaction.response.send_message(
                "Ten ticket został usunięty — odpowiedź nie została zapisana.",
                ephemeral=True,
            )
            return

        if len(questionnaire.answers) != self.question_index:
            await interaction.response.send_message(
                "To pytanie jest już nieaktualne. Otwórz ponownie panel rekrutacyjny, aby wznowić formularz.",
                ephemeral=True,
            )
            return
        if self.channel_id in CLOSED_TICKETS:
            await interaction.response.send_message(
                "Ticket jest zamknięty; odpowiedź nie została zapisana.",
                ephemeral=True,
            )
            return

        answer = self.answer.value.strip()
        if not answer:
            await interaction.response.send_message(
                "Odpowiedź nie może być pusta.",
                ephemeral=True,
            )
            return

        question = QUESTIONS[len(questionnaire.answers)]
        questionnaire.answers.append(answer)
        try:
            save_questionnaires()
        except OSError:
            questionnaire.answers.pop()
            logging.exception(
                "Nie udało się zapisać odpowiedzi użytkownika %s.",
                self.member_id,
            )
            await interaction.response.send_message(
                "Nie udało się zapisać odpowiedzi. Spróbuj ponownie.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        if question == MINECRAFT_NICKNAME_QUESTION:
            if isinstance(interaction.user, discord.Member):
                await append_minecraft_nickname(interaction.user, answer)

        if len(questionnaire.answers) < len(QUESTIONS):
            await send_private_question(interaction, self.channel_id)
            return

        channel = interaction.guild.get_channel(self.channel_id) if interaction.guild else None
        if not isinstance(channel, discord.TextChannel):
            await interaction.followup.send(
                "Nie znaleziono kanału ticketu. Odpowiedzi zostały zapisane; "
                "skontaktuj się z obsługą serwera.",
                ephemeral=True,
            )
            return

        try:
            await send_answer_embeds(
                channel,
                interaction.user,
                questionnaire.answers,
            )
        except discord.HTTPException:
            logging.exception(
                "Nie udało się opublikować odpowiedzi z ticketu %s.",
                self.channel_id,
            )
            await interaction.followup.send(
                "Odpowiedzi zostały zebrane, ale nie udało się wysłać podsumowania. "
                "Powiadom obsługę serwera.",
                ephemeral=True,
            )
            return

        ACTIVE_QUESTIONNAIRES.pop(self.channel_id, None)
        try:
            save_questionnaires()
        except OSError:
            logging.exception(
                "Nie udało się usunąć zakończonego formularza %s ze stanu zapisanego na dysku.",
                self.channel_id,
            )
        await interaction.followup.send(
            "Formularz został zakończony, a odpowiedzi wysłane w tickecie.",
            ephemeral=True,
        )


async def append_minecraft_nickname(
    member: discord.Member,
    answer: str,
) -> None:
    minecraft_nickname = answer.splitlines()[0].strip()
    if not minecraft_nickname:
        logging.warning(
            "Nie zmieniono pseudonimu użytkownika %s: odpowiedź na nick MC jest pusta.",
            member.id,
        )
        return

    minecraft_nickname = minecraft_nickname[:28]
    suffix = f" ({minecraft_nickname})"
    current_nickname = member.nick or member.display_name
    if current_nickname.casefold().endswith(suffix.casefold()):
        return

    new_nickname = f"{current_nickname[:32 - len(suffix)]}{suffix}"
    try:
        await member.edit(
            nick=new_nickname,
            reason="Dodanie nicku Minecraft z formularza rekrutacyjnego",
        )
    except discord.Forbidden:
        logging.exception(
            "Brak uprawnień do zmiany pseudonimu użytkownika %s.",
            member.id,
        )
    except discord.HTTPException:
        logging.exception(
            "Nie udało się zmienić pseudonimu użytkownika %s.",
            member.id,
        )


async def send_answer_embeds(
    channel: discord.TextChannel,
    member: discord.abc.User,
    answers: list[str],
) -> None:
    fields: list[tuple[str, str]] = []
    for question, answer in zip(QUESTIONS, answers):
        answer_parts = [answer[index : index + 1024] for index in range(0, len(answer), 1024)]
        for part_index, part in enumerate(answer_parts):
            field_name = question[:256] if part_index == 0 else "Dalsza część odpowiedzi"
            fields.append((field_name, part))

    description = f"Autor: {member} (`{member.id}`)"
    pages: list[discord.Embed] = []
    page = discord.Embed(
        title="Odpowiedzi — ticket rekrutacyjny",
        description=description,
        color=discord.Color.green(),
    )
    page_length = len(page.title or "") + len(description)
    for field_name, field_value in fields:
        field_length = len(field_name) + len(field_value)
        if len(page.fields) >= 25 or page_length + field_length > 5500:
            pages.append(page)
            page = discord.Embed(
                title="Odpowiedzi — ticket rekrutacyjny",
                description=description,
                color=discord.Color.green(),
            )
            page_length = len(page.title or "") + len(description)
        page.add_field(name=field_name, value=field_value, inline=False)
        page_length += field_length
    pages.append(page)

    for page_number, embed in enumerate(pages, start=1):
        embed.set_footer(text=f"Strona {page_number}/{len(pages)}")
        await channel.send(
            embed=embed,
            view=(
                CloseTicketView(include_questionnaire=False)
                if page_number == 1
                else None
            ),
        )


def register_ticket_views(client: discord.Client) -> None:
    client.add_view(OpenTicketView())
    client.add_view(CloseTicketView())
    client.add_view(ClosedTicketView())
    client.event(on_guild_channel_delete)
    for questionnaire in ACTIVE_QUESTIONNAIRES.values():
        question_index = len(questionnaire.answers)
        if question_index < len(QUESTIONS):
            client.add_view(
                AnswerQuestionView(
                    questionnaire.channel_id,
                    questionnaire.member_id,
                    question_index,
                )
            )


async def send_recruitment_panel(channel: discord.abc.Messageable) -> None:
    await channel.send(
        embed=discord.Embed(
            title="Rekrutacja Gildie",
            description=(
                "Kliknij przycisk poniżej, aby rozpocząć ticket rekrutacyjny "
                "i odpowiedzieć na pytania."
            ),
            color=discord.Color.blurple(),
        ),
        view=OpenTicketView(),
    )

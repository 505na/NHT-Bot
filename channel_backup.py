import asyncio
import hashlib
import json
import logging
import os
import tempfile
from collections.abc import Iterable
from typing import TypedDict

import discord
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

DEFAULT_SOURCE_CHANNEL_IDS = (
    898125295830319115,
    996731934501781524,
    996731767350370336,
    846065271675420735,
    1487105434946113546,
    848219468937166899,
    1249445432137089044,
)
BACKUP_INTERVAL_SECONDS = 24 * 60 * 60
EXCLUDED_ROLE_IDS = frozenset((898666673626837013, 863364845411762176))
BACKUP_LOCK = asyncio.Lock()
STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    os.getenv("BACKUP_STATE_PATH", "channel_backup_state.jsonl"),
)


class MessageMapping(TypedDict):
    source_channel_id: int
    source_message_id: int
    target_channel_id: int
    target_message_id: int
    content_hash: str


class BackupState(TypedDict):
    channels: dict[str, int]
    messages: dict[str, MessageMapping]
    source_channel_aliases: dict[str, int]
    roles: dict[str, int]


def _read_id_list(name: str, default: Iterable[int] = ()) -> tuple[int, ...]:
    value = os.getenv(name)
    if not value:
        return tuple(default)
    try:
        return tuple(
            dict.fromkeys(
                int(item.strip()) for item in value.split(",") if item.strip()
            )
        )
    except ValueError as error:
        raise RuntimeError(
            f"{name} musi zawierać ID rozdzielone przecinkami."
        ) from error


SOURCE_GUILD_ID = int(os.getenv("BACKUP_SOURCE_GUILD_ID", "846065271570825237"))
TARGET_GUILD_ID = int(os.getenv("BACKUP_TARGET_GUILD_ID", "1563948543763288176"))
SOURCE_CHANNEL_IDS = _read_id_list(
    "BACKUP_SOURCE_CHANNEL_IDS",
    DEFAULT_SOURCE_CHANNEL_IDS,
)
EXTRA_AUTHORIZED_USER_IDS = frozenset(
    _read_id_list(
        "BACKUP_AUTHORIZED_USER_IDS",
        (
            469156309061861400,
            713774102900375603,
            1040904460764983366,
            662687439306883074,
            1078962482279890974,
        ),
    )
)


def _message_key(source_channel_id: int, source_message_id: int) -> str:
    return f"{source_channel_id}:{source_message_id}"


def _load_state() -> BackupState:
    state: BackupState = {
        "channels": {},
        "messages": {},
        "source_channel_aliases": {},
        "roles": {},
    }
    if not os.path.exists(STATE_PATH):
        return state

    with open(STATE_PATH, encoding="utf-8") as state_file:
        for line_number, line in enumerate(state_file, start=1):
            try:
                event = json.loads(line)
                event_type = event["type"]
                if event_type == "channel":
                    state["channels"][str(event["source_channel_id"])] = int(
                        event["target_channel_id"]
                    )
                elif event_type == "source_channel_alias":
                    state["source_channel_aliases"][
                        str(event["configured_channel_id"])
                    ] = int(event["restored_channel_id"])
                elif event_type == "role":
                    state["roles"][str(event["source_role_id"])] = int(
                        event["target_role_id"]
                    )
                elif event_type == "message":
                    mapping = MessageMapping(
                        source_channel_id=int(event["source_channel_id"]),
                        source_message_id=int(event["source_message_id"]),
                        target_channel_id=int(event["target_channel_id"]),
                        target_message_id=int(event["target_message_id"]),
                        content_hash=str(event["content_hash"]),
                    )
                    state["messages"][
                        _message_key(
                            mapping["source_channel_id"],
                            mapping["source_message_id"],
                        )
                    ] = mapping
                elif event_type == "delete_message":
                    state["messages"].pop(
                        _message_key(
                            int(event["source_channel_id"]),
                            int(event["source_message_id"]),
                        ),
                        None,
                    )
                else:
                    raise ValueError(f"Nieznany typ wpisu: {event_type}")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"Nieprawidłowy wpis {line_number} w pliku stanu backupu "
                    f"{STATE_PATH}."
                ) from error
    return state


def _append_state_event(event: dict[str, int | str]) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "a", encoding="utf-8") as state_file:
        state_file.write(json.dumps(event, ensure_ascii=False) + "\n")
        state_file.flush()
        os.fsync(state_file.fileno())


def _save_channel_mapping(
    state: BackupState,
    source_channel_id: int,
    target_channel_id: int,
) -> None:
    _append_state_event(
        {
            "type": "channel",
            "source_channel_id": source_channel_id,
            "target_channel_id": target_channel_id,
        }
    )
    state["channels"][str(source_channel_id)] = target_channel_id


def _save_source_channel_alias(
    state: BackupState,
    configured_channel_id: int,
    restored_channel_id: int,
) -> None:
    _append_state_event(
        {
            "type": "source_channel_alias",
            "configured_channel_id": configured_channel_id,
            "restored_channel_id": restored_channel_id,
        }
    )
    state["source_channel_aliases"][str(configured_channel_id)] = restored_channel_id


def _save_role_mapping(
    state: BackupState,
    source_role_id: int,
    target_role_id: int,
) -> None:
    _append_state_event(
        {
            "type": "role",
            "source_role_id": source_role_id,
            "target_role_id": target_role_id,
        }
    )
    state["roles"][str(source_role_id)] = target_role_id


def _save_message_mapping(state: BackupState, mapping: MessageMapping) -> None:
    _append_state_event({"type": "message", **mapping})
    state["messages"][
        _message_key(
            mapping["source_channel_id"],
            mapping["source_message_id"],
        )
    ] = mapping


def _delete_message_mapping(state: BackupState, mapping: MessageMapping) -> None:
    _append_state_event(
        {
            "type": "delete_message",
            "source_channel_id": mapping["source_channel_id"],
            "source_message_id": mapping["source_message_id"],
        }
    )
    state["messages"].pop(
        _message_key(
            mapping["source_channel_id"],
            mapping["source_message_id"],
        ),
        None,
    )


def _compact_state(state: BackupState) -> None:
    directory = os.path.dirname(STATE_PATH)
    os.makedirs(directory, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=".channel-backup-",
            suffix=".tmp",
            delete=False,
        ) as state_file:
            temporary_path = state_file.name
            for source_id, target_id in state["channels"].items():
                state_file.write(
                    json.dumps(
                        {
                            "type": "channel",
                            "source_channel_id": int(source_id),
                            "target_channel_id": target_id,
                        }
                    )
                    + "\n"
                )
            for configured_id, restored_id in state[
                "source_channel_aliases"
            ].items():
                state_file.write(
                    json.dumps(
                        {
                            "type": "source_channel_alias",
                            "configured_channel_id": int(configured_id),
                            "restored_channel_id": restored_id,
                        }
                    )
                    + "\n"
                )
            for source_id, target_id in state["roles"].items():
                state_file.write(
                    json.dumps(
                        {
                            "type": "role",
                            "source_role_id": int(source_id),
                            "target_role_id": target_id,
                        }
                    )
                    + "\n"
                )
            for mapping in state["messages"].values():
                state_file.write(
                    json.dumps({"type": "message", **mapping}) + "\n"
                )
            state_file.flush()
            os.fsync(state_file.fileno())
        os.replace(temporary_path, STATE_PATH)
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.remove(temporary_path)


async def _sync_server_roles(
    source_guild: discord.Guild,
    target_guild: discord.Guild,
    state: BackupState,
) -> None:
    bot_member = target_guild.me
    if bot_member is None:
        raise ValueError("Nie można pobrać członka bota z serwera backupu.")
    if not bot_member.guild_permissions.manage_roles:
        raise ValueError(
            "Bot nie ma uprawnienia „Zarządzanie rolami” na serwerze backupu."
        )

    state["roles"][str(source_guild.default_role.id)] = target_guild.default_role.id
    if target_guild.default_role.permissions != source_guild.default_role.permissions:
        await target_guild.default_role.edit(
            permissions=source_guild.default_role.permissions,
            reason="Synchronizacja uprawnień @everyone z serwera bazowego",
        )
    target_roles: dict[int, discord.Role] = {
        source_guild.default_role.id: target_guild.default_role
    }
    claimed_role_ids = {target_guild.default_role.id}

    for source_role in reversed(source_guild.roles):
        if (
            source_role.is_default()
            or source_role.managed
            or source_role.id in EXCLUDED_ROLE_IDS
        ):
            continue

        target_role_id = state["roles"].get(str(source_role.id))
        target_role = (
            target_guild.get_role(target_role_id)
            if target_role_id is not None
            else None
        )
        if target_role is None or target_role.managed:
            target_role = next(
                (
                    role
                    for role in target_guild.roles
                    if not role.is_default()
                    and not role.managed
                    and role.id not in claimed_role_ids
                    and role.name.casefold() == source_role.name.casefold()
                ),
                None,
            )

        role_options = {
            "name": source_role.name,
            "permissions": source_role.permissions,
            "colour": source_role.colour,
            "hoist": source_role.hoist,
            "mentionable": source_role.mentionable,
        }
        if target_role is None:
            target_role = await target_guild.create_role(
                **role_options,
                reason="Synchronizacja ról z serwera bazowego",
            )
        else:
            if not target_role.is_assignable():
                raise ValueError(
                    f"Nie można zsynchronizować roli „{target_role.name}”: "
                    "rola bota na serwerze backupu musi być wyżej w hierarchii."
                )
            if (
                target_role.name != source_role.name
                or target_role.permissions != source_role.permissions
                or target_role.colour != source_role.colour
                or target_role.hoist != source_role.hoist
                or target_role.mentionable != source_role.mentionable
            ):
                await target_role.edit(
                    **role_options,
                    reason="Synchronizacja ról z serwera bazowego",
                )

        target_roles[source_role.id] = target_role
        claimed_role_ids.add(target_role.id)
        _save_role_mapping(state, source_role.id, target_role.id)

    ordered_roles = [
        target_roles[source_role.id]
        for source_role in source_guild.roles
        if not source_role.is_default()
        and not source_role.managed
        and source_role.id in target_roles
    ]
    available_positions = max(bot_member.top_role.position - 1, 0)
    if len(ordered_roles) > available_positions:
        logging.warning(
            "Pominięto synchronizację kolejności ról: bot może zarządzać "
            "%s pozycji poniżej swojej najwyższej roli, ale potrzeba ich %s. "
            "Przenieś rolę bota wyżej na serwerze backupu.",
            available_positions,
            len(ordered_roles),
        )
        return

    positions = {
        role: position
        for position, role in enumerate(ordered_roles, start=1)
        if role.position != position
    }
    if positions:
        try:
            await target_guild.edit_role_positions(
                positions,
                reason="Synchronizacja hierarchii ról z serwera bazowego",
            )
        except discord.HTTPException as error:
            if error.code != 50013:
                raise
            logging.warning(
                "Discord odmówił synchronizacji kolejności ról na serwerze "
                "backupu. Pozostałe elementy backupu będą kontynuowane; "
                "sprawdź hierarchię ról bota."
            )


def _channel_overwrites(
    source_channel: discord.abc.GuildChannel,
    target_guild: discord.Guild,
    state: BackupState,
) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
    overwrites: dict[discord.Role | discord.Member, discord.PermissionOverwrite] = {}
    unmapped_members: list[int] = []
    for target, overwrite in source_channel.overwrites.items():
        if isinstance(target, discord.Role):
            target_id = state["roles"].get(str(target.id))
            target_role = (
                target_guild.get_role(target_id)
                if target_id is not None
                else None
            )
            if target_role is None and target.id in EXCLUDED_ROLE_IDS:
                target_role = next(
                    (
                        role
                        for role in target_guild.roles
                        if not role.is_default()
                        and not role.managed
                        and role.name.casefold() == target.name.casefold()
                    ),
                    None,
                )
            if target_role is not None:
                overwrites[target_role] = overwrite
        elif isinstance(target, discord.Member):
            target_member = target_guild.get_member(target.id)
            if target_member is not None:
                overwrites[target_member] = overwrite
            else:
                unmapped_members.append(target.id)

    if unmapped_members:
        logging.warning(
            "Nie skopiowano nadpisań uprawnień użytkowników dla kanału %s: "
            "użytkownicy nie należą do serwera backupu.",
            source_channel.id,
        )
    return overwrites


def _same_channel_type(
    source_channel: discord.abc.GuildChannel,
    target_channel: discord.abc.GuildChannel,
) -> bool:
    if isinstance(source_channel, discord.CategoryChannel):
        return isinstance(target_channel, discord.CategoryChannel)
    if isinstance(source_channel, discord.ForumChannel):
        return isinstance(target_channel, discord.ForumChannel)
    if isinstance(source_channel, discord.StageChannel):
        return isinstance(
            target_channel,
            (discord.StageChannel, discord.VoiceChannel),
        )
    if isinstance(source_channel, discord.VoiceChannel):
        return isinstance(target_channel, discord.VoiceChannel)
    if isinstance(source_channel, discord.TextChannel):
        return isinstance(target_channel, discord.TextChannel)
    return False


async def _sync_server_channels(
    source_guild: discord.Guild,
    target_guild: discord.Guild,
    state: BackupState,
) -> tuple[int, int]:
    bot_member = target_guild.me
    if bot_member is None:
        raise ValueError("Nie można pobrać członka bota z serwera backupu.")
    if not bot_member.guild_permissions.manage_channels:
        raise ValueError(
            "Bot nie ma uprawnienia „Zarządzanie kanałami” na serwerze backupu."
        )

    created_count = 0
    skipped_count = 0
    categories: dict[int, discord.CategoryChannel] = {}

    for source_category in sorted(source_guild.categories, key=lambda item: item.position):
        overwrites = _channel_overwrites(source_category, target_guild, state)
        target_id = state["channels"].get(str(source_category.id))
        target_category = (
            target_guild.get_channel(target_id) if target_id is not None else None
        )
        if not isinstance(target_category, discord.CategoryChannel):
            target_category = next(
                (
                    category
                    for category in target_guild.categories
                    if category.name.casefold() == source_category.name.casefold()
                ),
                None,
            )
        if target_category is None:
            target_category = await target_guild.create_category(
                source_category.name,
                overwrites=overwrites,
                position=source_category.position,
                reason="Synchronizacja kategorii z serwera bazowego",
            )
            created_count += 1
        else:
            await target_category.edit(
                name=source_category.name,
                overwrites=overwrites,
                position=source_category.position,
                reason="Synchronizacja kategorii z serwera bazowego",
            )
        categories[source_category.id] = target_category
        _save_channel_mapping(state, source_category.id, target_category.id)

    for source_channel in sorted(source_guild.channels, key=lambda item: item.position):
        if isinstance(source_channel, discord.CategoryChannel):
            continue

        category = (
            categories.get(source_channel.category_id)
            if source_channel.category_id is not None
            else None
        )
        overwrites = _channel_overwrites(source_channel, target_guild, state)
        target_id = state["channels"].get(str(source_channel.id))
        target_channel = (
            target_guild.get_channel(target_id) if target_id is not None else None
        )
        if target_channel is None or not _same_channel_type(
            source_channel,
            target_channel,
        ):
            target_channel = next(
                (
                    candidate
                    for candidate in target_guild.channels
                    if _same_channel_type(source_channel, candidate)
                    and candidate.name.casefold() == source_channel.name.casefold()
                    and candidate.category_id
                    == (category.id if category is not None else None)
                ),
                None,
            )

        common_options: dict[str, object] = {
            "name": source_channel.name,
            "category": category,
            "overwrites": overwrites,
            "position": source_channel.position,
            "reason": "Synchronizacja kanałów z serwera bazowego",
        }
        if isinstance(source_channel, discord.TextChannel):
            channel_options: dict[str, object] = {
                **common_options,
                "news": source_channel.is_news(),
                "topic": source_channel.topic,
                "slowmode_delay": source_channel.slowmode_delay,
                "nsfw": source_channel.nsfw,
                "default_auto_archive_duration": (
                    source_channel.default_auto_archive_duration
                ),
                "default_thread_slowmode_delay": (
                    source_channel.default_thread_slowmode_delay
                ),
            }
            create_channel = target_guild.create_text_channel
        elif isinstance(source_channel, discord.ForumChannel):
            channel_options = {
                **common_options,
                "topic": source_channel.topic,
                "slowmode_delay": source_channel.slowmode_delay,
                "nsfw": source_channel.nsfw,
                "media": source_channel.is_media(),
                "default_auto_archive_duration": (
                    source_channel.default_auto_archive_duration
                ),
                "default_thread_slowmode_delay": (
                    source_channel.default_thread_slowmode_delay
                ),
                "default_sort_order": source_channel.default_sort_order,
                "default_layout": source_channel.default_layout,
                "available_tags": [
                    discord.ForumTag(
                        name=tag.name,
                        moderated=tag.moderated,
                    )
                    for tag in source_channel.available_tags
                ],
            }
            create_channel = target_guild.create_forum
        elif isinstance(source_channel, discord.StageChannel):
            channel_options = {
                **common_options,
                "bitrate": source_channel.bitrate,
                "user_limit": source_channel.user_limit,
                "rtc_region": source_channel.rtc_region,
                "video_quality_mode": source_channel.video_quality_mode,
                "nsfw": source_channel.nsfw,
            }
            create_channel = target_guild.create_stage_channel
        elif isinstance(source_channel, discord.VoiceChannel):
            channel_options = {
                **common_options,
                "bitrate": source_channel.bitrate,
                "user_limit": source_channel.user_limit,
                "rtc_region": source_channel.rtc_region,
                "video_quality_mode": source_channel.video_quality_mode,
                "nsfw": source_channel.nsfw,
            }
            create_channel = target_guild.create_voice_channel
        else:
            logging.warning(
                "Pominięto nieobsługiwany typ kanału %s (%s).",
                source_channel.id,
                type(source_channel).__name__,
            )
            skipped_count += 1
            continue

        if target_channel is None:
            try:
                target_channel = await create_channel(**channel_options)
            except discord.HTTPException as error:
                if (
                    isinstance(source_channel, discord.StageChannel)
                    and error.code == 50024
                ):
                    logging.warning(
                        "Serwer backupu nie obsługuje kanału Stage „%s”; "
                        "tworzę zamiast niego kanał głosowy.",
                        source_channel.name,
                    )
                    target_channel = await target_guild.create_voice_channel(
                        **channel_options
                    )
                elif (
                    isinstance(source_channel, discord.TextChannel)
                    and source_channel.is_news()
                    and error.code == 50035
                ):
                    logging.warning(
                        "Nie można utworzyć kanału ogłoszeniowego „%s” na "
                        "serwerze backupu; tworzę zwykły kanał tekstowy.",
                        source_channel.name,
                    )
                    channel_options["news"] = False
                    target_channel = await target_guild.create_text_channel(
                        **channel_options
                    )
                else:
                    raise
            created_count += 1
        else:
            edit_options = {
                key: value
                for key, value in channel_options.items()
                if key not in {"reason", "news", "media"}
            }
            await target_channel.edit(**edit_options)
        _save_channel_mapping(state, source_channel.id, target_channel.id)

    return created_count, skipped_count


async def _sync_server_structure(
    source_guild: discord.Guild,
    target_guild: discord.Guild,
    state: BackupState,
) -> tuple[int, int]:
    if source_guild.id == target_guild.id:
        raise ValueError("Serwer bazowy i serwer backupu muszą być różne.")
    await _sync_server_roles(source_guild, target_guild, state)
    return await _sync_server_channels(source_guild, target_guild, state)


async def _get_target_channel(
    source_channel: discord.TextChannel,
    target_guild: discord.Guild,
    state: BackupState,
) -> discord.TextChannel:
    target_channel_id = state["channels"].get(str(source_channel.id))
    if target_channel_id is not None:
        mapped = target_guild.get_channel(target_channel_id)
        if isinstance(mapped, discord.TextChannel):
            return mapped

    category: discord.CategoryChannel | None = None
    if source_channel.category is not None:
        category = next(
            (
                target_category
                for target_category in target_guild.categories
                if target_category.name.casefold()
                == source_channel.category.name.casefold()
            ),
            None,
        )
        if category is None:
            category = await target_guild.create_category(
                source_channel.category.name,
                reason="Przygotowanie kanału do backupu",
            )

    target_channel = next(
        (
            candidate
            for candidate in target_guild.text_channels
            if candidate.name.casefold() == source_channel.name.casefold()
            and candidate.category_id
            == (category.id if category is not None else None)
        ),
        None,
    )
    if target_channel is None:
        target_channel = await target_guild.create_text_channel(
            source_channel.name,
            category=category,
            overwrites={
                target_guild.default_role: discord.PermissionOverwrite(
                    view_channel=source_channel.permissions_for(
                        source_channel.guild.default_role
                    ).view_channel,
                    read_message_history=True,
                    send_messages=False,
                )
            },
            reason="Utworzenie kanału kopii zapasowej",
        )

    _save_channel_mapping(state, source_channel.id, target_channel.id)
    return target_channel


def _message_hash(message: discord.Message) -> str:
    value = {
        "content": message.content,
        "embeds": [embed.to_dict() for embed in message.embeds],
        "attachments": [
            (
                attachment.id,
                attachment.filename,
                attachment.size,
                attachment.content_type,
            )
            for attachment in message.attachments
        ],
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _message_content(message: discord.Message) -> str:
    author_name = discord.utils.escape_markdown(message.author.display_name)
    timestamp = int(message.created_at.timestamp())
    header = f"**{author_name}** · <t:{timestamp}:f>\n"
    body = message.content[: max(0, 2000 - len(header))]
    return header + body


async def _copy_message(
    source_message: discord.Message,
    target_channel: discord.TextChannel,
    state: BackupState,
    target_message_ids: set[int],
) -> tuple[bool, bool]:
    content_hash = _message_hash(source_message)
    mapping_key = _message_key(
        source_message.channel.id,
        source_message.id,
    )
    mapping = state["messages"].get(mapping_key)
    target_message: discord.Message | None = None
    if mapping is not None:
        target_exists = mapping["target_message_id"] in target_message_ids
        if mapping["content_hash"] == content_hash and target_exists:
            return False, False
        if not target_exists:
            logging.info(
                "Nie znaleziono kopii wiadomości %s; zostanie odtworzona.",
                source_message.id,
            )
        else:
            try:
                target_message = await target_channel.fetch_message(
                    mapping["target_message_id"]
                )
            except discord.NotFound:
                logging.info(
                    "Nie znaleziono kopii wiadomości %s; zostanie odtworzona.",
                    source_message.id,
                )

    is_new_copy = target_message is None

    if len(source_message.attachments) > 10:
        logging.error(
            "Wiadomość %s ma ponad 10 załączników i nie może zostać "
            "skopiowana w jednej wiadomości Discorda.",
            source_message.id,
        )
        return False, False

    files = []
    try:
        for attachment in source_message.attachments:
            files.append(await attachment.to_file(use_cached=True))
        embeds = [
            discord.Embed.from_dict(embed.to_dict())
            for embed in source_message.embeds
        ]
        allowed_mentions = discord.AllowedMentions.none()
        content = _message_content(source_message)
        if target_message is None:
            target_message = await target_channel.send(
                content=content,
                embeds=embeds,
                files=files,
                allowed_mentions=allowed_mentions,
            )
        else:
            target_message = await target_message.edit(
                content=content,
                embeds=embeds,
                attachments=files,
                allowed_mentions=allowed_mentions,
            )
    finally:
        for file in files:
            file.close()

    _save_message_mapping(
        state,
        {
            "source_channel_id": source_message.channel.id,
            "source_message_id": source_message.id,
            "target_channel_id": target_channel.id,
            "target_message_id": target_message.id,
            "content_hash": content_hash,
        },
    )
    target_message_ids.add(target_message.id)
    return is_new_copy, not is_new_copy


async def backup_channels(
    client: discord.Client,
    source_channel_ids: tuple[int, ...] | None = None,
) -> tuple[int, int, int]:
    async with BACKUP_LOCK:
        source_guild = client.get_guild(SOURCE_GUILD_ID)
        target_guild = client.get_guild(TARGET_GUILD_ID)
        if source_guild is None or target_guild is None:
            raise ValueError(
                "Bot musi być na serwerze źródłowym "
                f"{SOURCE_GUILD_ID} i docelowym {TARGET_GUILD_ID}."
            )

        state = _load_state()
        selected_ids = (
            SOURCE_CHANNEL_IDS if source_channel_ids is None else source_channel_ids
        )
        structure_created, structure_skipped = await _sync_server_structure(
            source_guild,
            target_guild,
            state,
        )
        copied_count = 0
        updated_count = 0
        skipped_count = 0
        for configured_channel_id in selected_ids:
            source_channel_id = state["source_channel_aliases"].get(
                str(configured_channel_id),
                configured_channel_id,
            )
            source_channel = source_guild.get_channel(source_channel_id)
            if not isinstance(source_channel, discord.TextChannel):
                raise ValueError(
                    f"Kanał {configured_channel_id} nie istnieje na serwerze "
                    "źródłowym albo nie jest kanałem tekstowym."
                )

            target_channel = await _get_target_channel(
                source_channel,
                target_guild,
                state,
            )
            target_message_ids = {
                target_message.id
                async for target_message in target_channel.history(limit=None)
            }
            async for source_message in source_channel.history(
                limit=None,
                oldest_first=True,
            ):
                copied, updated = await _copy_message(
                    source_message,
                    target_channel,
                    state,
                    target_message_ids,
                )
                copied_count += int(copied)
                updated_count += int(updated)
                if len(source_message.attachments) > 10:
                    skipped_count += 1

        _compact_state(state)

        logging.info(
            "Backup serwera zakończony: %s utworzonych elementów struktury, "
            "%s pominiętych typów kanałów, %s nowych, %s zaktualizowanych, "
            "%s pominiętych wiadomości.",
            structure_created,
            structure_skipped,
            copied_count,
            updated_count,
            skipped_count,
        )
        return copied_count, updated_count, skipped_count


async def restore_missing_channels(
    client: discord.Client,
) -> tuple[int, int, int]:
    async with BACKUP_LOCK:
        source_guild = client.get_guild(SOURCE_GUILD_ID)
        backup_guild = client.get_guild(TARGET_GUILD_ID)
        if source_guild is None or backup_guild is None:
            raise ValueError(
                "Bot musi być na serwerze bazowym "
                f"{SOURCE_GUILD_ID} i serwerze backupu {TARGET_GUILD_ID}."
            )

        state = _load_state()
        restored_channels = 0
        restored_messages = 0
        skipped_channels = 0
        restored_categories: dict[int, discord.CategoryChannel] = {}
        restored_channel_pairs: dict[int, tuple[discord.TextChannel, discord.TextChannel]] = {}
        mapped_backup_ids_in_use = {
            backup_channel_id
            for source_channel_id, backup_channel_id in state["channels"].items()
            if source_guild.get_channel(
                state["source_channel_aliases"].get(
                    source_channel_id,
                    int(source_channel_id),
                )
            )
            is not None
        }

        def find_source_category(
            backup_category: discord.CategoryChannel | None,
        ) -> discord.CategoryChannel | None:
            if backup_category is None:
                return None
            mapped_source_id = next(
                (
                    int(source_id)
                    for source_id, target_id in state["channels"].items()
                    if target_id == backup_category.id
                ),
                None,
            )
            if mapped_source_id is not None:
                source_id = state["source_channel_aliases"].get(
                    str(mapped_source_id),
                    mapped_source_id,
                )
                category = source_guild.get_channel(source_id)
                if isinstance(category, discord.CategoryChannel):
                    return category
            return next(
                (
                    category
                    for category in source_guild.categories
                    if category.name.casefold() == backup_category.name.casefold()
                ),
                None,
            )

        def restore_overwrites(
            backup_channel: discord.abc.GuildChannel,
        ) -> dict[discord.Role | discord.Member, discord.PermissionOverwrite]:
            overwrites: dict[
                discord.Role | discord.Member, discord.PermissionOverwrite
            ] = {}
            for target, overwrite in backup_channel.overwrites.items():
                if isinstance(target, discord.Role):
                    source_role_id = next(
                        (
                            int(role_id)
                            for role_id, mapped_id in state["roles"].items()
                            if mapped_id == target.id
                        ),
                        None,
                    )
                    source_role = (
                        source_guild.get_role(source_role_id)
                        if source_role_id is not None
                        else None
                    )
                    if source_role is None:
                        source_role = next(
                            (
                                role
                                for role in source_guild.roles
                                if role.name.casefold() == target.name.casefold()
                            ),
                            None,
                        )
                    if source_role is not None:
                        overwrites[source_role] = overwrite
                elif isinstance(target, discord.Member):
                    source_member = source_guild.get_member(target.id)
                    if source_member is not None:
                        overwrites[source_member] = overwrite
            return overwrites

        for source_channel_id, backup_channel_id in tuple(state["channels"].items()):
            backup_channel = backup_guild.get_channel(backup_channel_id)
            if not isinstance(backup_channel, discord.CategoryChannel):
                resolved_source_id = state["source_channel_aliases"].get(
                    source_channel_id,
                    int(source_channel_id),
                )
                if source_guild.get_channel(resolved_source_id) is None:
                    logging.warning(
                        "Nie można odtworzyć kategorii %s: brak jej w backupie.",
                        source_channel_id,
                    )
                    skipped_channels += 1
                continue
            resolved_source_id = state["source_channel_aliases"].get(
                source_channel_id,
                int(source_channel_id),
            )
            source_channel = source_guild.get_channel(resolved_source_id)
            if isinstance(source_channel, discord.CategoryChannel):
                restored_categories[backup_channel.id] = source_channel
                continue
            if backup_channel.id in mapped_backup_ids_in_use:
                logging.warning(
                    "Pominięto kategorię %s: jej kopia jest już powiązana "
                    "z innym istniejącym kanałem.",
                    source_channel_id,
                )
                skipped_channels += 1
                continue

            restored_category = await source_guild.create_category(
                backup_channel.name,
                overwrites=restore_overwrites(backup_channel),
                position=backup_channel.position,
                reason="Odtworzenie kategorii z backupu",
            )
            restored_channels += 1
            restored_categories[backup_channel.id] = restored_category
            mapped_backup_ids_in_use.add(backup_channel.id)
            _save_source_channel_alias(
                state,
                int(source_channel_id),
                restored_category.id,
            )
            _save_channel_mapping(
                state,
                restored_category.id,
                backup_channel.id,
            )

        for source_channel_id, backup_channel_id in tuple(state["channels"].items()):
            backup_channel = backup_guild.get_channel(backup_channel_id)
            if (
                backup_channel is None
                or isinstance(backup_channel, discord.CategoryChannel)
            ):
                if backup_channel is None and source_guild.get_channel(
                    state["source_channel_aliases"].get(
                        source_channel_id,
                        int(source_channel_id),
                    )
                ) is None:
                    logging.warning(
                        "Nie można odtworzyć kanału %s: brak jego kopii.",
                        source_channel_id,
                    )
                    skipped_channels += 1
                continue
            resolved_source_id = state["source_channel_aliases"].get(
                source_channel_id,
                int(source_channel_id),
            )
            source_channel = source_guild.get_channel(resolved_source_id)
            if source_channel is not None:
                if (
                    int(source_channel_id) in SOURCE_CHANNEL_IDS
                    and isinstance(source_channel, discord.TextChannel)
                    and isinstance(backup_channel, discord.TextChannel)
                    and resolved_source_id != int(source_channel_id)
                ):
                    restored_channel_pairs[int(source_channel_id)] = (
                        source_channel,
                        backup_channel,
                    )
                continue
            if backup_channel.id in mapped_backup_ids_in_use:
                logging.warning(
                    "Pominięto kanał %s: jego kopia jest już powiązana "
                    "z innym istniejącym kanałem.",
                    source_channel_id,
                )
                skipped_channels += 1
                continue

            category: discord.CategoryChannel | None = None
            if backup_channel.category is not None:
                category = restored_categories.get(backup_channel.category.id)
                if category is None:
                    category = find_source_category(backup_channel.category)

            common_options: dict[str, object] = {
                "name": backup_channel.name,
                "category": category,
                "overwrites": restore_overwrites(backup_channel),
                "position": backup_channel.position,
                "reason": "Odtworzenie kanału z backupu",
            }
            if isinstance(backup_channel, discord.ForumChannel):
                source_channel = await source_guild.create_forum(
                    **common_options,
                    topic=backup_channel.topic,
                    slowmode_delay=backup_channel.slowmode_delay,
                    nsfw=backup_channel.nsfw,
                    media=backup_channel.is_media(),
                    default_auto_archive_duration=(
                        backup_channel.default_auto_archive_duration
                    ),
                    default_thread_slowmode_delay=(
                        backup_channel.default_thread_slowmode_delay
                    ),
                    default_sort_order=backup_channel.default_sort_order,
                    default_layout=backup_channel.default_layout,
                    available_tags=[
                        discord.ForumTag(
                            name=tag.name,
                            moderated=tag.moderated,
                        )
                        for tag in backup_channel.available_tags
                    ],
                )
            elif isinstance(backup_channel, discord.StageChannel):
                source_channel = await source_guild.create_stage_channel(
                    **common_options,
                    bitrate=backup_channel.bitrate,
                    user_limit=backup_channel.user_limit,
                    rtc_region=backup_channel.rtc_region,
                    video_quality_mode=backup_channel.video_quality_mode,
                    nsfw=backup_channel.nsfw,
                )
            elif isinstance(backup_channel, discord.VoiceChannel):
                source_channel = await source_guild.create_voice_channel(
                    **common_options,
                    bitrate=backup_channel.bitrate,
                    user_limit=backup_channel.user_limit,
                    rtc_region=backup_channel.rtc_region,
                    video_quality_mode=backup_channel.video_quality_mode,
                    nsfw=backup_channel.nsfw,
                )
            elif isinstance(backup_channel, discord.TextChannel):
                source_channel = await source_guild.create_text_channel(
                    **common_options,
                    news=backup_channel.is_news(),
                    topic=backup_channel.topic,
                    slowmode_delay=backup_channel.slowmode_delay,
                    nsfw=backup_channel.nsfw,
                    default_auto_archive_duration=(
                        backup_channel.default_auto_archive_duration
                    ),
                    default_thread_slowmode_delay=(
                        backup_channel.default_thread_slowmode_delay
                    ),
                )
            else:
                logging.warning(
                    "Pominięto kanał backupu %s: nieobsługiwany typ %s.",
                    backup_channel.id,
                    type(backup_channel).__name__,
                )
                skipped_channels += 1
                continue

            restored_channels += 1
            _save_source_channel_alias(
                state,
                int(source_channel_id),
                source_channel.id,
            )
            _save_channel_mapping(state, source_channel.id, backup_channel.id)
            mapped_backup_ids_in_use.add(backup_channel.id)
            if (
                int(source_channel_id) in SOURCE_CHANNEL_IDS
                and isinstance(source_channel, discord.TextChannel)
                and isinstance(backup_channel, discord.TextChannel)
            ):
                restored_channel_pairs[int(source_channel_id)] = (
                    source_channel,
                    backup_channel,
                )

        for source_channel, backup_channel in restored_channel_pairs.values():

            existing_mappings = {
                mapping["target_message_id"]: mapping
                for mapping in state["messages"].values()
                if mapping["target_channel_id"] == backup_channel.id
                and mapping["source_channel_id"] == source_channel.id
            }
            async for backup_message in backup_channel.history(
                limit=None,
                oldest_first=True,
            ):
                mapping = existing_mappings.get(backup_message.id)
                if mapping is not None:
                    try:
                        await source_channel.fetch_message(
                            mapping["source_message_id"]
                        )
                        continue
                    except discord.NotFound:
                        pass

                if len(backup_message.attachments) > 10:
                    logging.error(
                        "Wiadomość backupu %s ma ponad 10 załączników i "
                        "nie może zostać odtworzona.",
                        backup_message.id,
                    )
                    continue

                files = []
                try:
                    for attachment in backup_message.attachments:
                        files.append(await attachment.to_file(use_cached=True))
                    restored_message = await source_channel.send(
                        content=backup_message.content,
                        embeds=[
                            discord.Embed.from_dict(embed.to_dict())
                            for embed in backup_message.embeds
                        ],
                        files=files,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                finally:
                    for file in files:
                        file.close()

                _save_message_mapping(
                    state,
                    {
                        "source_channel_id": source_channel.id,
                        "source_message_id": restored_message.id,
                        "target_channel_id": backup_channel.id,
                        "target_message_id": backup_message.id,
                        "content_hash": _message_hash(restored_message),
                    },
                )
                restored_messages += 1

        _compact_state(state)
        return restored_channels, restored_messages, skipped_channels


async def restore_deleted_backup(
    client: discord.Client,
    guild_id: int | None,
    channel_id: int,
    message_ids: Iterable[int],
) -> None:
    selected_ids = tuple(message_ids)
    if guild_id != TARGET_GUILD_ID or not selected_ids:
        return

    async with BACKUP_LOCK:
        try:
            state = _load_state()
            deleted_ids = set(selected_ids)
            mappings = [
                mapping
                for mapping in state["messages"].values()
                if mapping["target_channel_id"] == channel_id
                and mapping["target_message_id"] in deleted_ids
            ]
            if not mappings:
                return

            for mapping in mappings:
                _delete_message_mapping(state, mapping)

            source_guild = client.get_guild(SOURCE_GUILD_ID)
            target_guild = client.get_guild(TARGET_GUILD_ID)
            if source_guild is None or target_guild is None:
                raise ValueError("Nie znaleziono serwera źródłowego lub docelowego.")

            for mapping in mappings:
                source_channel_id = mapping["source_channel_id"]
                source_message_id = mapping["source_message_id"]
                source_channel = source_guild.get_channel(source_channel_id)
                if not isinstance(source_channel, discord.TextChannel):
                    logging.error(
                        "Nie można odtworzyć backupu wiadomości %s: "
                        "brak kanału źródłowego %s.",
                        source_message_id,
                        source_channel_id,
                    )
                    continue
                try:
                    source_message = await source_channel.fetch_message(
                        source_message_id
                    )
                    target_channel = await _get_target_channel(
                        source_channel,
                        target_guild,
                        state,
                    )
                    await _copy_message(
                        source_message,
                        target_channel,
                        state,
                        set(),
                    )
                except (
                    discord.HTTPException,
                    OSError,
                    RuntimeError,
                    ValueError,
                ):
                    logging.exception(
                        "Nie udało się odtworzyć backupu wiadomości %s "
                        "z kanału %s.",
                        source_message_id,
                        source_channel_id,
                    )
            _compact_state(state)
        except (RuntimeError, OSError, ValueError):
            logging.exception(
                "Nie udało się obsłużyć usuniętych kopii z kanału %s.",
                channel_id,
            )


@tasks.loop(seconds=BACKUP_INTERVAL_SECONDS)
async def _scheduled_backup(client: discord.Client) -> None:
    try:
        await backup_channels(client)
    except discord.Forbidden:
        logging.exception(
            "Automatyczny backup nie ma wymaganych uprawnień. "
            "Sprawdź „Zarządzanie rolami”, „Zarządzanie kanałami” oraz "
            "hierarchię ról bota na serwerze backupu."
        )
    except (
        discord.HTTPException,
        OSError,
        RuntimeError,
        ValueError,
    ):
        logging.exception("Automatyczny backup kanałów nie powiódł się.")


def start_scheduled_backup(client: discord.Client) -> None:
    if not _scheduled_backup.is_running():
        _scheduled_backup.start(client)


def stop_scheduled_backup() -> None:
    if _scheduled_backup.is_running():
        _scheduled_backup.cancel()


async def handle_command(
    client: discord.Client,
    message: discord.Message,
    default_authorized_user_id: int,
) -> bool:
    parts = message.content.strip().split()
    if not parts:
        return False
    command, *arguments = parts
    if command.casefold() == "!restore":
        allowed_user_ids = EXTRA_AUTHORIZED_USER_IDS | {default_authorized_user_id}
        if message.author.id not in allowed_user_ids:
            await message.channel.send(
                f"{message.author.mention}, nie masz uprawnień do przywracania kanałów."
            )
            return True
        if arguments:
            await message.channel.send("Użycie: `!restore`.")
            return True
        if message.guild is None or message.guild.id != SOURCE_GUILD_ID:
            await message.channel.send(
                "Tej komendy można użyć tylko na serwerze bazowym."
            )
            return True

        await message.channel.send(
            "Porównuję kanały serwera bazowego z backupem i przywracam brakujące..."
        )
        try:
            channels, messages, skipped = await restore_missing_channels(client)
        except (
            discord.HTTPException,
            OSError,
            RuntimeError,
            ValueError,
        ) as error:
            logging.exception("Przywracanie kanałów z backupu nie powiodło się.")
            await message.channel.send(
                "Przywracanie nie powiodło się. Sprawdź log bota i uprawnienia "
                "bota na obu serwerach.\n"
                f"Powód: `{error}`"
            )
            return True

        await message.channel.send(
            "Przywracanie zakończone. "
            f"Nowe kanały: {channels}, odtworzone wiadomości: {messages}, "
            f"kanały bez dostępnej kopii: {skipped}. "
            "Ustawienia uprawnień kanałów mogą wymagać ręcznego sprawdzenia."
        )
        return True

    if command.casefold() != "!backup":
        return False

    allowed_user_ids = EXTRA_AUTHORIZED_USER_IDS | {default_authorized_user_id}
    if message.author.id not in allowed_user_ids:
        await message.channel.send(
            f"{message.author.mention}, nie masz uprawnień do backupu."
        )
        return True

    source_channel_ids: tuple[int, ...] | None = None
    if arguments:
        if len(arguments) != 1 or not arguments[0].isdigit():
            await message.channel.send("Użycie: `!backup` lub `!backup <ID_kanału>`.")
            return True
        selected_id = int(arguments[0])
        if selected_id not in SOURCE_CHANNEL_IDS:
            await message.channel.send(
                "Ten kanał nie znajduje się na liście kanałów objętych backupem."
            )
            return True
        source_channel_ids = (selected_id,)

    await message.channel.send("Rozpoczynam backup wskazanych kanałów...")
    try:
        copied, updated, skipped = await backup_channels(
            client,
            source_channel_ids,
        )
    except (
        discord.HTTPException,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        logging.exception("Ręczny backup kanałów nie powiódł się.")
        await message.channel.send(
            "Backup nie powiódł się. Sprawdź log bota i uprawnienia "
            "bota na obu serwerach.\n"
            f"Powód: `{error}`"
        )
        return True

    await message.channel.send(
        "Backup zakończony. "
        f"Nowe: {copied}, zaktualizowane: {updated}, pominięte: {skipped}."
    )
    return True

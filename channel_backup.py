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
    state: BackupState = {"channels": {}, "messages": {}}
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

        selected_ids = (
            SOURCE_CHANNEL_IDS if source_channel_ids is None else source_channel_ids
        )
        state = _load_state()
        copied_count = 0
        updated_count = 0
        skipped_count = 0
        for source_channel_id in selected_ids:
            source_channel = source_guild.get_channel(source_channel_id)
            if not isinstance(source_channel, discord.TextChannel):
                raise ValueError(
                    f"Kanał {source_channel_id} nie istnieje na serwerze "
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
            "Backup kanałów zakończony: %s nowych, %s zaktualizowanych, "
            "%s pominiętych wiadomości.",
            copied_count,
            updated_count,
            skipped_count,
        )
        return copied_count, updated_count, skipped_count


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
    command, *arguments = message.content.strip().split()
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

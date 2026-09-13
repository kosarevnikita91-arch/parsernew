from pathlib import Path
import asyncio
import sqlite3
from telethon import TelegramClient, events
import logging
from datetime import datetime, timezone
from collections import Counter
import re
import logging

from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv
import os
from telethon import TelegramClient
from telethon.errors import FloodWaitError



from telethon.tl.functions.channels import (
    GetParticipantRequest,
    GetFullChannelRequest,
)

from telethon.tl.functions.messages import (
    GetDialogFiltersRequest,
)

from telethon.tl.types import (
    ChannelParticipantAdmin,
    ChannelParticipantCreator,
    DialogFilter,
    UserStatusOnline,
    UserStatusRecently,
    UserStatusLastWeek,
    UserStatusLastMonth,
)


import os
import asyncio
import logging

from telethon import TelegramClient, events


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    force=True
)

print("Файл bott.py запущен", flush=True)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SESSION_PATH = Path("/app/parser_session")


print("Переменные окружения загружены", flush=True)
print(f"API_ID: {API_ID}", flush=True)
print(f"API_HASH задан: {bool(API_HASH)}", flush=True)
print(f"BOT_TOKEN задан: {bool(BOT_TOKEN)}", flush=True)


telegram_client = TelegramClient(
    str(SESSION_PATH),
    API_ID,
    API_HASH,
)


async def main():
    print("Подключение к Telegram...", flush=True)

    await telegram_client.start()

    me = await telegram_client.get_me()
    print(f"Бот авторизован: @{me.username or me.id}", flush=True)

    print("Бот успешно запущен", flush=True)

    await asyncio.gather(     dp.start_polling(bot),     telegram_client.run_until_disconnected(), )





# =========================================================
# Настройки
# =========================================================
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


@dp.message(Command("start"))
async def start_handler(message: Message):
    print(
        f"AIOGRAM получил /start от {message.from_user.id}",
        flush=True,
    )

    if not is_authorized(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    await message.answer("Команда обработана через aiogram")



RECIPIENT_IDS = [
    1005183943,
    6977495013,
    2117149396,
    8190307950,
    7066297390,
    8177297993,
]

AUTHORIZED_USERS = set(RECIPIENT_IDS)

BATCH_SIZE = 50
TARGET_USERS = 300
REQUIRED_USERS = TARGET_USERS

DAYS_TO_PARSE = 2
PARSING_HOURS = 6
STATUS_INTERVAL_SECONDS = 60 * 60


# =========================================================
# Глобальное состояние
# =========================================================

parsing_task = None
parsing_started_at = None
parsing_deadline = None

distribution_lock = asyncio.Lock()
parsing_lock = asyncio.Lock()


# =========================================================
# Клиенты
# =========================================================


print("BOT_TOKEN существует:", bool(os.getenv("BOT_TOKEN")))


BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден")

bot = Bot(token=BOT_TOKEN)

print("Текущая папка:", Path.cwd())
print("Путь к сессии:", SESSION_PATH.with_suffix(".session"))
print(
    "Файл существует:",
    SESSION_PATH.with_suffix(".session").exists()
)


dp = Dispatcher()


# =========================================================
# База данных
# =========================================================

db = sqlite3.connect(
    "users.db",
    check_same_thread=False,
)

db.row_factory = sqlite3.Row


def init_db():
    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT,
            last_name TEXT,

            deleted INTEGER DEFAULT 0,
            premium INTEGER DEFAULT 0,
            last_seen TEXT DEFAULT 'hidden',
            is_admin INTEGER DEFAULT 0,

            possible_bot INTEGER DEFAULT 0,
            bot_reasons TEXT,

            source_chat_id INTEGER,
            source_chat_title TEXT,
            source_message_id INTEGER,
            source_message_link TEXT,

            comment_count INTEGER DEFAULT 0,
            assigned INTEGER DEFAULT 0,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS processed_posts (
            chat_id INTEGER NOT NULL,
            post_id INTEGER NOT NULL,
            processed_at TEXT DEFAULT CURRENT_TIMESTAMP,

            PRIMARY KEY (chat_id, post_id)
        )
    """)

    db.commit()


# =========================================================
# Общие функции
# =========================================================

def is_authorized(user_id: int) -> bool:
    return user_id in AUTHORIZED_USERS


def get_unassigned_count() -> int:
    row = db.execute("""
        SELECT COUNT(*) AS total
        FROM users
        WHERE assigned = 0
    """).fetchone()

    return row["total"]


async def notify_authorized_users(text: str):
    for user_id in RECIPIENT_IDS:
        try:
            await bot.send_message(
                chat_id=user_id,
                text=text,
            )
            await asyncio.sleep(0.3)

        except Exception as error:
            print(
                f"Ошибка отправки уведомления "
                f"{user_id}: {error}"
            )


def get_remaining_time_text() -> str:
    if parsing_deadline is None:
        return "Автоматический парсинг не запущен."

    remaining = parsing_deadline - datetime.now(
        timezone.utc
    )

    if remaining.total_seconds() <= 0:
        return "Время парсинга истекло."

    total_seconds = int(
        remaining.total_seconds()
    )

    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    return (
        f"Осталось: {hours} ч. "
        f"{minutes} мин. {seconds} сек."
    )


def get_last_seen_category(user):
    status = getattr(user, "status", None)

    if status is None:
        return "hidden"

    if isinstance(status, UserStatusOnline):
        return "online"

    if isinstance(status, UserStatusRecently):
        return "recently"

    if isinstance(status, UserStatusLastWeek):
        return "last_week"

    if isinstance(status, UserStatusLastMonth):
        return "last_month"

    return "offline"


def detect_possible_bot(user, comment_texts: list[str]):
    reasons = []

    username = (
        getattr(user, "username", None) or ""
    ).lower()

    first_name = (
        getattr(user, "first_name", None) or ""
    ).strip()

    if not first_name:
        reasons.append("нет имени")

    bot_words = (
        "bot",
        "robot",
        "auto",
        "support",
        "service",
        "admin",
    )

    if username and any(
        word in username
        for word in bot_words
    ):
        reasons.append(
            "подозрительный username"
        )

    normalized_texts = [
        re.sub(
            r"\s+",
            " ",
            text.lower(),
        ).strip()
        for text in comment_texts
        if text.strip()
    ]

    if len(normalized_texts) >= 3:
        repeated = Counter(
            normalized_texts
        ).most_common(1)[0][1]

        if repeated >= 2:
            reasons.append(
                "повторяющиеся комментарии"
            )

    if getattr(user, "deleted", False):
        reasons.append(
            "удалённый аккаунт"
        )

    return bool(reasons), "; ".join(reasons)


async def check_is_admin(chat, user_id: int) -> bool:
    try:
        result = await telegram_client(
            GetParticipantRequest(
                channel=chat,
                participant=user_id,
            )
        )

        return isinstance(
            result.participant,
            (
                ChannelParticipantAdmin,
                ChannelParticipantCreator,
            ),
        )

    except Exception:
        return False


def get_public_chat_name(entity):
    return (
        getattr(entity, "title", None)
        or getattr(entity, "username", None)
        or str(getattr(entity, "id", "unknown"))
    )


async def make_message_link(entity, message_id: int):
    try:
        return await telegram_client.get_message_link(
            entity,
            message_id,
        )

    except Exception:
        return None


def make_user_link(row):
    username = row["username"]
    message_link = row["source_message_link"]
    telegram_id = row["telegram_id"]

    if username:
        return f"https://t.me/{username}"

    if message_link:
        return message_link

    return f"tg://user?id={telegram_id}"


def make_result_line(row):
    link = make_user_link(row)
    labels = []

    if row["deleted"]:
        labels.append("удалённый")

    if row["premium"]:
        labels.append("Premium")

    if row["is_admin"]:
        labels.append("администратор")

    if row["possible_bot"]:
        labels.append(
            "возможный бот: "
            + (
                row["bot_reasons"]
                or "подозрительные признаки"
            )
        )

    suffix = ""

    if labels:
        suffix = " — " + ", ".join(labels)

    return f"{link}{suffix}"


# =========================================================
# Поиск источника
# =========================================================

async def resolve_source(source_text: str):
    source_text = source_text.strip()

    if not source_text:
        raise ValueError(
            "Источник не указан."
        )

    if source_text.lstrip("-").isdigit():
        return await telegram_client.get_entity(
            int(source_text)
        )

    original_source = source_text

    for prefix in (
        "https://t.me/",
        "http://t.me/",
        "https://telegram.me/",
        "http://telegram.me/",
        "telegram.me/",
    ):
        source_text = source_text.replace(
            prefix,
            "",
        )

    if source_text.startswith("+"):
        return await telegram_client.get_entity(
            "https://t.me/" + source_text
        )

    username = source_text.split("/")[0].strip()
    username = username.lstrip("@")

    try:
        return await telegram_client.get_entity(
            username
        )

    except Exception:
        pass

    async for dialog in telegram_client.iter_dialogs():
        dialog_name = (
            dialog.name or ""
        ).strip().lower()

        if dialog_name == original_source.lower():
            return dialog.entity

    matches = []

    async for dialog in telegram_client.iter_dialogs():
        dialog_name = (
            dialog.name or ""
        ).strip().lower()

        if original_source.lower() in dialog_name:
            matches.append(dialog)

    if len(matches) == 1:
        return matches[0].entity

    if len(matches) > 1:
        names = "\n".join(
            f"- {item.name} | ID: {item.id}"
            for item in matches[:10]
        )

        raise ValueError(
            "Найдено несколько похожих источников:\n\n"
            + names
        )

    raise ValueError(
        "Группа или канал не найдены."
    )


# =========================================================
# Папка Stay
# =========================================================

async def get_stay_folder_id():
    result = await telegram_client(
        GetDialogFiltersRequest()
    )

    for folder in result.filters:
        if not isinstance(folder, DialogFilter):
            continue

        title = str(folder.title)

        if title.lower() == "stay":
            return folder.id

    return None


async def get_channels_from_stay():
    stay_folder_id = await get_stay_folder_id()

    if stay_folder_id is None:
        raise ValueError(
            "Папка Stay не найдена."
        )

    channels = []

    async for dialog in telegram_client.iter_dialogs():
        if dialog.folder_id != stay_folder_id:
            continue

        entity = dialog.entity

        if getattr(entity, "broadcast", False):
            channels.append(entity)

    return channels


# =========================================================
# Проверка источников
# =========================================================

async def has_recent_posts(entity, days=2):
    since = (
        datetime.now(timezone.utc)
        - timedelta(days=days)
    )

    async for post in telegram_client.iter_messages(
        entity,
        limit=1,
    ):
        if not post.date:
            return False

        post_date = post.date

        if post_date.tzinfo is None:
            post_date = post_date.replace(
                tzinfo=timezone.utc
            )

        return post_date >= since

    return False


async def get_discussion_chat(channel):
    try:
        full = await telegram_client(
            GetFullChannelRequest(channel)
        )

        linked_chat_id = (
            full.full_chat.linked_chat_id
        )

        if not linked_chat_id:
            return None

        return await telegram_client.get_entity(
            linked_chat_id
        )

    except Exception:
        return None


async def comments_are_available(channel):
    discussion_chat = await get_discussion_chat(
        channel
    )

    if discussion_chat is None:
        return False

    try:
        await telegram_client.get_permissions(
            discussion_chat,
            "me",
        )
        return True

    except Exception:
        return False


# =========================================================
# Сохранение пользователей
# =========================================================

def save_user(
    user,
    entity,
    message,
    message_link,
    is_admin,
    possible_bot,
    bot_reasons,
):
    username = getattr(
        user,
        "username",
        None,
    )

    existing = db.execute("""
        SELECT id
        FROM users
        WHERE telegram_id = ?
    """, (user.id,)).fetchone()

    chat_title = get_public_chat_name(entity)

    if existing:
        db.execute("""
            UPDATE users
            SET
                username = COALESCE(?, username),
                first_name = ?,
                last_name = ?,
                deleted = ?,
                premium = ?,
                last_seen = ?,
                is_admin = ?,
                possible_bot = ?,
                bot_reasons = ?,
                source_chat_id = ?,
                source_chat_title = ?,
                source_message_id = COALESCE(
                    ?,
                    source_message_id
                ),
                source_message_link = COALESCE(
                    ?,
                    source_message_link
                ),
                comment_count = comment_count + 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE telegram_id = ?
        """, (
            username,
            getattr(user, "first_name", "") or "",
            getattr(user, "last_name", "") or "",
            int(bool(getattr(user, "deleted", False))),
            int(bool(getattr(user, "premium", False))),
            get_last_seen_category(user),
            int(is_admin),
            int(possible_bot),
            bot_reasons,
            getattr(entity, "id", None),
            chat_title,
            getattr(message, "id", None),
            message_link,
            user.id,
        ))

    else:
        db.execute("""
            INSERT INTO users (
                telegram_id,
                username,
                first_name,
                last_name,
                deleted,
                premium,
                last_seen,
                is_admin,
                possible_bot,
                bot_reasons,
                source_chat_id,
                source_chat_title,
                source_message_id,
                source_message_link,
                comment_count
            )
 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            user.id,
            username,
            getattr(user, "first_name", "") or "",
            getattr(user, "last_name", "") or "",
            int(bool(getattr(user, "deleted", False))),
            int(bool(getattr(user, "premium", False))),
            get_last_seen_category(user),
            int(is_admin),
            int(possible_bot),
            bot_reasons,
            getattr(entity, "id", None),
            chat_title,
            getattr(message, "id", None),
            message_link,
        ))

    db.commit()


# =========================================================
# Парсинг комментариев
# =========================================================

async def parse_comments_last_two_days(entity):
    processed_messages = 0
    collected_users = 0
    admin_cache = {}

    since = (
        datetime.now(timezone.utc)
        - timedelta(days=DAYS_TO_PARSE)
    )

    async for post in telegram_client.iter_messages(
        entity,
        limit=None,
    ):
        if not post.date:
            continue

        post_date = post.date

        if post_date.tzinfo is None:
            post_date = post_date.replace(
                tzinfo=timezone.utc
            )

        if post_date < since:
            break

        already_processed = db.execute("""
            SELECT 1
            FROM processed_posts
            WHERE chat_id = ? AND post_id = ?
        """, (
            entity.id,
            post.id,
        )).fetchone()

        if already_processed:
            continue

        try:
            if post.replies and post.replies.comments:
                async for comment in telegram_client.iter_messages(
                    entity,
                    reply_to=post.id,
                ):
                    user = await comment.get_sender()

                    if not user:
                        continue

                    message_link = await make_message_link(
                        entity,
                        comment.id,
                    )

                    old_row = db.execute("""
                        SELECT comment_count
                        FROM users
                        WHERE telegram_id = ?
                    """, (user.id,)).fetchone()

                    old_count = (
                        old_row["comment_count"]
                        if old_row
                        else 0
                    )

                    comment_text = comment.message or ""

                    possible_bot, bot_reasons = (
                        detect_possible_bot(
                            user,
                            [comment_text],
                        )
                    )

                    if user.id not in admin_cache:
                        admin_cache[user.id] = (
                            await check_is_admin(
                                entity,
                                user.id,
                            )
                        )

                    save_user(
                        user=user,
                        entity=entity,
                        message=comment,
                        message_link=message_link,
                        is_admin=admin_cache[user.id],
                        possible_bot=possible_bot,
                        bot_reasons=bot_reasons,
                    )

                    processed_messages += 1

                    if old_count == 0:
                        collected_users += 1

                    await asyncio.sleep(0.3)

            db.execute("""
                INSERT OR IGNORE INTO processed_posts (
                    chat_id,
                    post_id
                )
                VALUES (?, ?)
            """, (
                entity.id,
                post.id,
            ))

            db.commit()

        except FloodWaitError as error:
            print(
                f"Telegram требует паузу "
                f"{error.seconds} секунд."
            )
            await asyncio.sleep(error.seconds)

        except Exception as error:
            print(
                f"Ошибка обработки поста "
                f"{post.id}: {error}"
            )

        await asyncio.sleep(1)

    return processed_messages, collected_users


# =========================================================
# Распределение
# =========================================================

def get_unassigned_users():
    return db.execute("""
        SELECT
            telegram_id,
            username,
            first_name,
            last_name,
            deleted,
            premium,
            last_seen,
            is_admin,
            possible_bot,
            bot_reasons,
            source_message_link
        FROM users
        WHERE assigned = 0
        ORDER BY id
        LIMIT ?
    """, (TARGET_USERS,)).fetchall()


async def distribute_users():
    async with distribution_lock:
        rows = get_unassigned_users()

        if len(rows) < TARGET_USERS:
            return False

        for index, recipient_id in enumerate(
            RECIPIENT_IDS
        ):
            start = index * BATCH_SIZE
            end = start + BATCH_SIZE
            batch = rows[start:end]

            text = "\n\n".join(
                make_result_line(row)
                for row in batch
            )

            try:
                await bot.send_message(
                    chat_id=recipient_id,
                    text=text,
                    disable_web_page_preview=True,
                )
                await asyncio.sleep(1)

            except Exception as error:
                print(
                    f"Не удалось отправить пачку "
                    f"{recipient_id}: {error}"
                )
                return False

        db.executemany("""
            UPDATE users
            SET assigned = 1,
                updated_at = CURRENT_TIMESTAMP
            WHERE telegram_id = ?
        """, [
            (row["telegram_id"],)
            for row in rows
        ])

        db.commit()

        print(
            f"Распределено пользователей: "
            f"{len(rows)}"
        )

        return True


# =========================================================
# Обработка одного источника
# =========================================================

async def parse_one_source(entity):
    title = get_public_chat_name(entity)

    try:
        if not await has_recent_posts(
            entity,
            days=DAYS_TO_PARSE,
        ):
            print(
                f"{title}: нет постов за последние "
                f"{DAYS_TO_PARSE} дня."
            )
            return 0, 0

        if not await comments_are_available(entity):
            print(
                f"{title}: комментарии недоступны."
            )
            return 0, 0

        print(
            f"Начинаю обработку: {title}"
        )

        result = await parse_comments_last_two_days(
            entity
        )

        print(
            f"{title}: комментариев {result[0]}, "
            f"новых пользователей {result[1]}"
        )

        return result

    except FloodWaitError as error:
        print(
            f"FloodWait для {title}: "
            f"{error.seconds} секунд."
        )
        await asyncio.sleep(error.seconds)
        return 0, 0

    except Exception as error:
        print(
            f"Ошибка источника {title}: {error}"
        )
        return 0, 0


# =========================================================
# Автоматический режим
# =========================================================

async def auto_parsing_worker():
    global parsing_started_at
    global parsing_deadline

    parsing_started_at = datetime.now(timezone.utc)

    parsing_deadline = (
        parsing_started_at
        + timedelta(hours=PARSING_HOURS)
    )

    await notify_authorized_users(
        "Автоматический парсинг запущен "
        "на 6 часов."
    )

    try:
        while datetime.now(timezone.utc) < parsing_deadline:
            current_count = get_unassigned_count()

            if current_count >= TARGET_USERS:
                sent = await distribute_users()

                if sent:
                    await notify_authorized_users(
                        "Набрано 300 пользователей. "
                        "Они распределены между "
                        "шестью операторами."
                    )

            current_count = get_unassigned_count()

            if current_count < TARGET_USERS:
                shortage = TARGET_USERS - current_count

                await notify_authorized_users(
                    f"Нехватка пользователей: "
                    f"{shortage}.\n"
                    f"Сейчас доступно: "
                    f"{current_count}/{TARGET_USERS}.\n"
                    "Беру каналы из папки Stay."
                )

                try:
                    channels = await get_channels_from_stay()

                except Exception as error:
                    print(
                        f"Ошибка получения папки Stay: "
                        f"{error}"
                    )
                    channels = []

                if not channels:
                    await notify_authorized_users(
                        "В папке Stay не найдено "
                        "доступных каналов."
                    )

                for channel in channels:
                    if datetime.now(timezone.utc) >= parsing_deadline:
                        break

                    if get_unassigned_count() >= TARGET_USERS:
                        break

                    await parse_one_source(channel)

            current_count = get_unassigned_count()

            if current_count >= TARGET_USERS:
                sent = await distribute_users()

                if sent:
                    await notify_authorized_users(
                        "Очередные 300 пользователей "
                        "распределены."
                    )
            else:
                shortage = TARGET_USERS - current_count

                await notify_authorized_users(
                    f"Промежуточный результат: "
                    f"{current_count}/{TARGET_USERS}.\n"
                    f"Не хватает: {shortage}."
                )

            remaining_seconds = (
                parsing_deadline
                - datetime.now(timezone.utc)
            ).total_seconds()

            if remaining_seconds <= 0:
                break

            await asyncio.sleep(
                min(
                    STATUS_INTERVAL_SECONDS,
                    remaining_seconds,
                )
            )

    finally:
        parsing_started_at = None
        parsing_deadline = None

        await notify_authorized_users(
            "Автоматический парсинг завершён."
        )


async def start_auto_parsing():
    global parsing_task

    if parsing_task and not parsing_task.done():
        return False

    parsing_task = asyncio.create_task(
        auto_parsing_worker()
    )

    return True


# =========================================================
# Команды бота
# =========================================================
@dp.message(Command("start"))
async def start_handler(message: Message):
    if not is_authorized(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return

    await message.answer(
        "Бот готов!\n"
        "/auto — запустить парсинг на 6 часов\n"
        "/status — показать статус и время\n"
        "/stay — обработать каналы из Stay\n"
        "/parse — показать общий список участников\n"
        "/try — показать 50 пользователей\n"
        "/count — показать количество"
    )






@dp.message(Command("count"))
async def count_command(message: Message):
    print(
        f"ПОЛУЧЕНА КОМАНДА COUNT: {message.from_user.id}",
        flush=True,
    )

    if not is_authorized(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    count = get_unassigned_count()

    await message.answer(
        f"Новых пользователей: {count}/{TARGET_USERS}"
    )




@dp.message(Command("stay"))
async def stay_command(message: Message):
    if not is_authorized(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    if parsing_task and not parsing_task.done():
        await message.answer(
            "Уже работает автоматический парсинг."
        )
        return

    await message.answer(
        "Проверяю каналы из папки Stay."
    )

    try:
        channels = await get_channels_from_stay()

        total_processed = 0
        total_new_users = 0

        for channel in channels:
            processed, new_users = (
                await parse_one_source(channel)
            )

            total_processed += processed
            total_new_users += new_users

        await message.answer(
            "Обработка завершена.\n"
            f"Комментариев: {total_processed}\n"
            f"Новых пользователей: {total_new_users}"
        )

        await distribute_users()

    except Exception as error:
        await message.answer(
            f"Ошибка:\n{error}"
        )


@dp.message(Command("pars"))
async def pars_command(message: Message):
    if not is_authorized(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    if parsing_task and not parsing_task.done():
        await message.answer(
            "Уже работает автоматический парсинг."
        )
        return

    parts = message.text.split(maxsplit=1)

    if len(parts) < 2:
        await message.answer(
            "Укажи источник:\n\n"
            "/pars https://t.me/example\n"
            "/pars @example\n"
            "/pars Название группы\n"
            "/pars -1001234567890"
        )
        return

    await message.answer(
        "Ищу группу или канал..."
    )

    try:
        entity = await resolve_source(parts[1].strip())

        title = get_public_chat_name(entity)

        await message.answer(
            f"Источник найден: {title}\n"
            "Обрабатываю комментарии."
        )

        processed, new_users = (
            await parse_one_source(entity)
        )

        await message.answer(
            "Обработка завершена.\n"
            f"Комментариев: {processed}\n"
            f"Новых пользователей: {new_users}"
        )

        sent = await distribute_users()

        if sent:
            await message.answer(
                "300 пользователей распределены "
                "по шести пачкам."
            )

    except Exception as error:
        await message.answer(
            f"Ошибка:\n{error}"
        )


@dp.message(Command("try"))
async def try_command(message: Message):
    if not is_authorized(message.from_user.id):
        await message.answer("Нет доступа.")
        return

    rows = db.execute("""
                SELECT
                    telegram_id,
                    username,
                    deleted,
                    premium,
                    is_admin,
                    possible_bot,
                    bot_reasons,
                    source_message_link
                FROM users
                WHERE assigned = 0
                ORDER BY id
                LIMIT 50
            """).fetchall()

    if not rows:
        await message.answer(
            "Новых пользователей нет."
        )
        return

    text = "\n\n".join(
        make_result_line(row)
        for row in rows
    )

    await message.answer(
        text,
        disable_web_page_preview=True,
    )




   # =========================================================
# Запуск
# =========================================================
async def main():
    print("Подключение к Telegram...", flush=True)

    await telegram_client.start(bot_token=BOT_TOKEN)

    me = await telegram_client.get_me()
    print(f"Бот авторизован: @{me.username or me.id}", flush=True)

    await asyncio.gather(
        dp.start_polling(bot),
        telegram_client.run_until_disconnected(),
    )
